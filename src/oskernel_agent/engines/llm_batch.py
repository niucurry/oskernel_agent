"""
LLM batch dispatcher: 并发 OpenCode 子进程 + JSON 校验 + 单次重试 + 文件缓存。

调用方（tree_builder）只关心三件事：
  - 用 OpenCode 跑一个 batch 任务，写 JSON 到指定路径
  - 失败 batch 重试一次（并触发 json_repair 兜底）
  - 多个 batch 之间可并发调度；OpenCode CLI 默认隔离 data/state 目录以避开本地 SQLite 锁

缓存策略：键由调用方算好（基于文件 mtime/size + prompt_version），命中则跳过 LLM。
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import atexit
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from oskernel_agent.paths import SOURCE_ROOT

def _find_opencode() -> str:
    import shutil

    def _safe_exists(path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

    def _prefer_real_exe(path: Path) -> Path:
        # npm 全局 shim（opencode.cmd/.ps1/无扩展名）经 subprocess 调用时会走 cmd.exe，
        # 导致 prompt 里的 shell 元字符（如 severity "low|medium|high" 的 `|`）被 cmd
        # 当成管道符执行 → "'medium' is not recognized"，opencode 直接 255 失败。
        # 真实 opencode.exe 与 shim 同前缀，直接用它可绕过 cmd 解析。
        if path.suffix.lower() == ".exe":
            return path
        exe = path.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        return exe if _safe_exists(exe) else path

    for c in [
        Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe",
        Path.home() / ".local" / "bin" / "opencode",
        Path.home() / "AppData" / "Roaming" / "npm" / "opencode",
        Path.home() / "AppData" / "Roaming" / "npm" / "opencode.cmd",
    ]:
        if _safe_exists(c):
            return str(_prefer_real_exe(c))
    found = shutil.which("opencode")
    if found:
        return str(_prefer_real_exe(Path(found)))
    return "opencode"

_OPENCODE = _find_opencode()
_DEFAULT_CONCURRENCY = 4
_WINDOWS_SAFE_MESSAGE_LIMIT = 20_000
_OPENCODE_RUN_LOCK = threading.Lock()
_OPENCODE_ISOLATION_RUN_ID = f"{os.getpid()}-{uuid.uuid4().hex}"
_OPENCODE_DATA_ROOT: Path | None = None
_OPENCODE_DATA_ROOT_REGISTERED = False
_OPENCODE_DATA_ROOT_LOCK = threading.Lock()


def opencode_isolated_data_enabled() -> bool:
    raw = os.environ.get("AGENT_OPENCODE_ISOLATED_DATA", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return True


def opencode_serial_enabled() -> bool:
    raw = os.environ.get("AGENT_OPENCODE_SERIAL", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    if opencode_isolated_data_enabled():
        return False
    # OpenCode keeps mutable state in a single local SQLite DB. Parallel CLI
    # processes can fail with "database is locked", so serialize by default.
    return True


def _opencode_source_data_dir() -> Path:
    raw = os.environ.get("AGENT_OPENCODE_SOURCE_DATA_DIR", "").strip()
    if raw:
        return Path(raw)
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home) / "opencode"
    return Path.home() / ".local" / "share" / "opencode"


def _cleanup_opencode_data_root(path: Path) -> None:
    if os.environ.get("AGENT_OPENCODE_KEEP_DATA", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _opencode_isolated_root() -> Path:
    global _OPENCODE_DATA_ROOT, _OPENCODE_DATA_ROOT_REGISTERED
    with _OPENCODE_DATA_ROOT_LOCK:
        if _OPENCODE_DATA_ROOT is not None:
            return _OPENCODE_DATA_ROOT

        raw = os.environ.get("AGENT_OPENCODE_DATA_ROOT", "").strip()
        if raw:
            root = Path(raw)
        else:
            root = (
                Path(tempfile.gettempdir())
                / "oskernel_agent"
                / "opencode-data"
                / _OPENCODE_ISOLATION_RUN_ID
            )
        root.mkdir(parents=True, exist_ok=True)
        _OPENCODE_DATA_ROOT = root

        if not raw and not _OPENCODE_DATA_ROOT_REGISTERED:
            atexit.register(_cleanup_opencode_data_root, root)
            _OPENCODE_DATA_ROOT_REGISTERED = True
        return root


def _safe_path_part(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"{cleaned[:48] or 'batch'}-{digest}"


def _prepare_opencode_data_home(task: "BatchTask") -> Path:
    root = _opencode_isolated_root() / _safe_path_part(task.batch_id)
    data_home = root / "data"
    state_home = root / "state"
    opencode_data = data_home / "opencode"
    opencode_data.mkdir(parents=True, exist_ok=True)
    state_home.mkdir(parents=True, exist_ok=True)

    source_auth = _opencode_source_data_dir() / "auth.json"
    target_auth = opencode_data / "auth.json"
    if source_auth.exists() and not target_auth.exists():
        try:
            shutil.copy2(source_auth, target_auth)
        except OSError as e:
            _log(f"[llm_batch] 复制 OpenCode auth.json 失败：{e}")
    return root


def _opencode_lock_file() -> Path:
    raw = os.environ.get("AGENT_OPENCODE_LOCK_FILE", "").strip()
    if raw:
        return Path(raw)
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "oskernel_agent" / "opencode-run.lock"


@contextmanager
def _opencode_run_guard():
    if not opencode_serial_enabled():
        yield
        return

    with _OPENCODE_RUN_LOCK:
        lock_path = _opencode_lock_file()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_f:
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        lock_f.seek(0)
                        msvcrt.locking(lock_f.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.25)
                try:
                    yield
                finally:
                    lock_f.seek(0)
                    msvcrt.locking(lock_f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _opencode_env(task: "BatchTask") -> dict:
    env = os.environ.copy()
    src_root = str(SOURCE_ROOT)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        src_root
        if not existing_pythonpath
        else src_root + os.pathsep + existing_pythonpath
    )
    if task.repo_path is not None:
        env["OSKERNEL_AGENT_REPO_PATH"] = str(Path(task.repo_path).resolve())
    if opencode_isolated_data_enabled():
        isolated_root = _prepare_opencode_data_home(task)
        env["XDG_DATA_HOME"] = str(isolated_root / "data")
        env["XDG_STATE_HOME"] = str(isolated_root / "state")
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    # 为每个 worker 隔离 session
    env["OPENCODE_SESSION_ID"] = uuid.uuid4().hex
    return env


def get_concurrency() -> int:
    raw = os.environ.get("AGENT_LLM_CONCURRENCY", "").strip()
    if not raw:
        return _DEFAULT_CONCURRENCY
    try:
        n = int(raw)
        return max(1, min(n, 16))
    except ValueError:
        return _DEFAULT_CONCURRENCY


def cache_disabled() -> bool:
    return os.environ.get("AGENT_TREE_NO_CACHE", "").strip().lower() in ("1", "true", "yes")


# 缓存 I/O

def cache_key(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8", errors="replace"))
        h.update(b"\x1f")
    return h.hexdigest()


def cache_read(cache_dir: Path, key: str) -> dict | None:
    if cache_disabled():
        return None
    p = cache_dir / f"{key}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def cache_write(cache_dir: Path, key: str, data: dict) -> None:
    if cache_disabled():
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{key}.json"
    # 原子写：先写临时文件再 os.replace，避免并发覆盖/崩溃留下半个 JSON。
    tmp = cache_dir / f".{key}.{os.getpid()}.tmp"
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# OpenCode 调用

@dataclass
class BatchTask:
    """单个 LLM batch 任务的描述。"""
    batch_id: str
    agent_name: str
    user_request: str       # 完整的 user message（含 output_path 指令）
    output_path: Path       # LLM 必须 write_report 到这个路径
    cache_dir: Path
    cache_key: str
    # 描述报告要求每次完整重生成时设为 False：既不读取，也不写入文件缓存。
    cache_enabled: bool = True
    fallback: dict = field(default_factory=dict)  # 失败兜底返回
    repo_path: Path | None = None
    # 写缓存前对 parsed 做增补（如把 agent 落盘的 HTML 正文读进 parsed），
    # 使正文随 JSON 一起进缓存，缓存命中时也能拿到完整正文。
    enrich: Callable[[dict], dict] | None = None
    # 缓存准入校验。用于拒绝结构合法但不满足交付约束（如残留英文正文）的结果；
    # 同时会校验旧缓存，避免一次异常输出被长期复用。
    cache_validator: Callable[[dict], bool] | None = None
    # 大体积只读输入通过 OpenCode --file 附加，避免 Windows 命令行 32767 字符上限。
    input_files: tuple[Path, ...] = ()


_print_lock = threading.Lock()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def _tail_text(text: str, limit: int = 2000) -> str:
    text = _ANSI_RE.sub("", text or "").strip()
    if len(text) <= limit:
        return text
    return "...<truncated>...\n" + text[-limit:]


def _opencode_message(text: str) -> str:
    # OpenCode 1.17.x on Windows drops or truncates multiline positional messages.
    return re.sub(r"[\r\n]+", " ", text).strip()


def _opencode_command(task: BatchTask) -> list[str]:
    """构造不会超过 Windows CreateProcess 长度上限的 OpenCode 命令。"""
    message = _opencode_message(task.user_request)
    input_files = [Path(path).resolve() for path in task.input_files]
    if len(message) > _WINDOWS_SAFE_MESSAGE_LIMIT:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", task.batch_id)[:80] or "batch"
        prompt_path = task.output_path.parent / f".{safe_id}.prompt.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(task.user_request, encoding="utf-8")
        input_files.append(prompt_path.resolve())
        message = (
            "任务说明过长，已作为文本附件随消息提供。请完整读取附件中的任务说明和输入数据，"
            "严格按其中要求调用 write_report，不得省略任何部分。"
        )
        _log(
            f"[llm_batch] {task.batch_id} 超长任务改用附件："
            f"{len(task.user_request)} 字符"
        )

    cmd = [_OPENCODE, "run", "--agent", task.agent_name, message]
    for input_file in input_files:
        cmd.extend(["--file", str(input_file)])
    return cmd


def _iter_json_objects(text: str):
    decoder = json.JSONDecoder()
    clean = _ANSI_RE.sub("", text or "")
    for i, ch in enumerate(clean):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(clean[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _safe_tool_write(task: BatchTask, raw_path: str, content) -> bool:
    if content is None:
        return False
    raw_path = html.unescape(str(raw_path or "")).strip().strip('"')
    if not raw_path:
        return False

    target = Path(raw_path)
    if not target.is_absolute():
        target = task.output_path.parent / target
    try:
        resolved_target = target.resolve()
        allowed_root = task.output_path.parent.resolve()
        resolved_target.relative_to(allowed_root)
    except (OSError, ValueError):
        _log(f"[llm_batch] 忽略越界写入请求：{target}")
        return False

    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=2)
    content = html.unescape(content)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _log(f"[llm_batch] 从 stdout 兜底写入：{target}（{len(content)} 字符）")
    return True


def _tool_params_from_json(obj: dict) -> tuple[str, str] | None:
    candidates = []
    if isinstance(obj.get("tool"), dict):
        candidates.append(obj["tool"].get("parameters") or {})
    if isinstance(obj.get("parameters"), dict):
        candidates.append(obj["parameters"])
    candidates.append(obj)

    for params in candidates:
        if not isinstance(params, dict):
            continue
        content = params.get("content")
        path = (
            params.get("output_path")
            or params.get("path")
            or params.get("file_path")
        )
        if path and content is not None:
            return str(path), content
    return None


def _extract_tag_value(body: str, names: tuple[str, ...]) -> str | None:
    for name in names:
        m = re.search(
            rf"<{name}\b[^>]*>(.*?)</{name}>",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m:
            return m.group(1).strip()
    return None


def _materialize_stdout_writes(task: BatchTask, stdout: str) -> bool:
    """把模型输出中的伪工具调用/裸 JSON 兜底落盘。

    正常路径仍然依赖 MCP write_report。该函数只处理 OpenCode/模型偶发把
    tool call 当文本输出的情况，避免报告生成因为未落盘而退回 fallback。
    """
    if not stdout:
        return False

    wrote = False
    clean = _ANSI_RE.sub("", stdout or "")

    # 优先：整段 stdout 就是一个顶层 JSON 对象（报告对象）。只有这个"顶层对象"
    # 才允许写回 task.output_path；嵌套 dict 绝不写回报告路径，避免污染已落盘报告。
    try:
        top = json.loads(clean)
    except json.JSONDecodeError:
        top = None
    if isinstance(top, dict):
        params = _tool_params_from_json(top)
        if params is not None:
            path, content = params
            wrote = _safe_tool_write(task, path, content) or wrote
        elif any(k in top for k in (
            "modules", "dimensions", "score_total", "summary", "stages"
        )):
            wrote = _safe_tool_write(
                task,
                str(task.output_path),
                json.dumps(top, ensure_ascii=False, indent=2),
            ) or wrote

    # 扫描嵌套 JSON：只认带显式 output_path 的工具调用，不再把嵌套 magic-key 对象写盘。
    for obj in _iter_json_objects(stdout):
        if obj is top:
            continue
        params = _tool_params_from_json(obj)
        if params is not None:
            path, content = params
            wrote = _safe_tool_write(task, path, content) or wrote

    for m in re.finditer(
        r"<(?P<tag>write_report|write_to_file|WriteToFile)\b[^>]*>"
        r"(?P<body>.*?)</(?P=tag)>",
        stdout,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        body = m.group("body")
        content = _extract_tag_value(body, ("content",))
        path = _extract_tag_value(body, ("output_path", "path", "file_path"))
        if path and content is not None:
            wrote = _safe_tool_write(task, path, content) or wrote

    for m in re.finditer(
        r"<[^>]*invoke\b[^>]*name=[\"'](?P<tool>write_report|write_to_file)[\"'][^>]*>"
        r"(?P<body>.*?)</[^>]*invoke>",
        stdout,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        params: dict[str, str] = {}
        for p in re.finditer(
            r"<[^>]*parameter\b[^>]*name=[\"'](?P<name>[^\"']+)[\"'][^>]*>"
            r"(?P<value>.*?)</[^>]*parameter>",
            m.group("body"),
            flags=re.IGNORECASE | re.DOTALL,
        ):
            params[p.group("name")] = p.group("value").strip()
        content = params.get("content")
        path = params.get("output_path") or params.get("path") or params.get("file_path")
        if path and content is not None:
            wrote = _safe_tool_write(task, path, content) or wrote

    return wrote


def _run_opencode_once(task: BatchTask, timeout: int) -> tuple[bool, str]:
    """跑一次 OpenCode 子进程。返回 (是否产生了 output_path 文件, stdout)。"""
    cmd = _opencode_command(task)
    try:
        with _opencode_run_guard():
            proc = subprocess.run(
                cmd,
                env=_opencode_env(task),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                # opencode.exe run 即使带 positional message，仍会阻塞读 stdin；
                # 子进程 stdin 是未关闭的管道时会一直等到超时。给 DEVNULL 立即 EOF。
                stdin=subprocess.DEVNULL,
            )
    except subprocess.TimeoutExpired:
        _log(f"[llm_batch] {task.batch_id} 超时 {timeout}s")
        return False, ""
    stdout = proc.stdout or ""
    _materialize_stdout_writes(task, stdout)
    ok = task.output_path.exists()
    if proc.returncode not in (0, 1) or not ok:
        _log(
            f"[llm_batch] {task.batch_id} opencode returncode={proc.returncode} "
            f"output_exists={ok} output_path={task.output_path}"
        )
        stdout_tail = _tail_text(proc.stdout)
        stderr_tail = _tail_text(proc.stderr)
        if stdout_tail:
            _log(f"[llm_batch] {task.batch_id} stdout_tail:\n{stdout_tail}")
        if stderr_tail:
            _log(f"[llm_batch] {task.batch_id} stderr_tail:\n{stderr_tail}")
    return ok, stdout


def _parse_json_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log(f"[llm_batch] 解析失败 {path.name}: {e}")
        return None


def _try_repair_json(task: BatchTask, raw_text: str,
                     schema_hint: str, timeout: int) -> dict | None:
    """调用 json_repair agent 修复损坏文本。"""
    repair_path = task.output_path.with_suffix(".repair.json")
    repair_request = (
        f"raw:\n{raw_text}\n\n"
        f"expected_schema:\n{schema_hint}\n\n"
        f"output_path:\n{repair_path}\n\n"
        "请只调用 write_report：content 参数为修复后的合法 JSON 字符串，"
        "output_path 参数必须使用上面的绝对路径。不要读取仓库，不要输出解释。"
    )
    repair_task = BatchTask(
        batch_id=f"{task.batch_id}-repair",
        agent_name="os-kernel-json-repair",
        user_request=repair_request,
        output_path=repair_path,
        cache_dir=task.cache_dir,
        cache_key="",
        repo_path=task.repo_path,
    )
    ok, _ = _run_opencode_once(repair_task, timeout)
    if not ok:
        return None
    return _parse_json_file(repair_path)


def run_batch_task(task: BatchTask, schema_hint: str = "",
                   timeout: int = 300) -> dict:
    """单 batch 完整执行：缓存查 → 跑 LLM → 解析 → 失败修复 → 兜底 fallback。"""
    cached = cache_read(task.cache_dir, task.cache_key) if task.cache_enabled else None
    if cached is not None:
        if task.cache_validator is None or task.cache_validator(cached):
            _log(f"[llm_batch] {task.batch_id} 缓存命中")
            return cached
        _log(f"[llm_batch] {task.batch_id} 缓存未通过交付校验，重新生成")

    # 清理可能存在的旧文件
    if task.output_path.exists():
        try:
            task.output_path.unlink()
        except OSError:
            pass

    ok, stdout = _run_opencode_once(task, timeout)
    parsed = _parse_json_file(task.output_path) if ok else None

    if parsed is None:
        # 尝试 json 修复
        raw = ""
        if task.output_path.exists():
            try:
                raw = task.output_path.read_text(encoding="utf-8")
            except OSError:
                raw = ""
        if not raw:
            raw = stdout
        if raw and schema_hint:
            _log(f"[llm_batch] {task.batch_id} 触发 json_repair")
            parsed = _try_repair_json(task, raw, schema_hint, timeout)

    if parsed is None:
        # 最终重试 1 次
        _log(f"[llm_batch] {task.batch_id} 重试 1 次")
        ok2, _ = _run_opencode_once(task, timeout)
        parsed = _parse_json_file(task.output_path) if ok2 else None

    if parsed is None:
        _log(f"[llm_batch] {task.batch_id} 最终失败，使用 fallback")
        parsed = dict(task.fallback)
        parsed["_error"] = "llm_batch_failed"

    def enrich_result(value: dict) -> dict:
        if task.enrich is None:
            return value
        try:
            return task.enrich(value)
        except Exception as e:
            _log(f"[llm_batch] {task.batch_id} enrich 失败：{e}")
            return value

    # 增补正文后再做交付校验：保证缺正文、残留英文等语义不完整结果也会自动重试。
    parsed = enrich_result(parsed)
    delivery_valid = task.cache_validator is None or task.cache_validator(parsed)
    if not parsed.get("_error") and not delivery_valid:
        _log(f"[llm_batch] {task.batch_id} 未通过交付校验，重试 1 次")
        if task.output_path.exists():
            try:
                task.output_path.unlink()
            except OSError:
                pass
        ok2, _ = _run_opencode_once(task, timeout)
        retried = _parse_json_file(task.output_path) if ok2 else None
        if retried is not None:
            parsed = enrich_result(retried)

    # 只缓存成功结果：带 _error 的兜底**不写缓存**，否则一次瞬时失败（超时/限流/
    # 子进程异常）会被永久冻住，后续每次跑都命中空结果而不再重试。不缓存则下次自愈。
    cache_valid = task.cache_validator is None or task.cache_validator(parsed)
    if parsed.get("_error"):
        _log(f"[llm_batch] {task.batch_id} 失败结果不入缓存，下次将重试")
    elif not cache_valid:
        _log(f"[llm_batch] {task.batch_id} 未通过交付校验，不入缓存")
    elif not task.cache_enabled:
        _log(f"[llm_batch] {task.batch_id} 描述报告缓存已关闭")
    else:
        cache_write(task.cache_dir, task.cache_key, parsed)
    return parsed


