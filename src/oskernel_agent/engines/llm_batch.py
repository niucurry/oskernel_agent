"""
LLM batch dispatcher: 并发 OpenCode 子进程 + JSON 校验 + 单次重试 + 文件缓存。

调用方（tree_builder）只关心三件事：
  - 用 OpenCode 跑一个 batch 任务，写 JSON 到指定路径
  - 失败 batch 重试一次（并触发 json_repair 兜底）
  - 多个 batch 之间并发跑（默认 4 路）

缓存策略：键由调用方算好（基于文件 mtime/size + prompt_version），命中则跳过 LLM。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

def _find_opencode() -> str:
    import shutil
    found = shutil.which("opencode")
    if found:
        return found
    for c in [
        Path.home() / ".local" / "bin" / "opencode",
        Path.home() / "AppData" / "Roaming" / "npm" / "opencode",
        Path.home() / "AppData" / "Roaming" / "npm" / "opencode.cmd",
    ]:
        if c.exists():
            return str(c)
    return "opencode"

_OPENCODE = _find_opencode()
_DEFAULT_CONCURRENCY = 4


def _opencode_env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = local_bin + ":" + env.get("PATH", "")
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
    (cache_dir / f"{key}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
    fallback: dict = field(default_factory=dict)  # 失败兜底返回
    # 写缓存前对 parsed 做增补（如把 agent 落盘的 HTML 正文读进 parsed），
    # 使正文随 JSON 一起进缓存，缓存命中时也能拿到完整正文。
    enrich: Callable[[dict], dict] | None = None


_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def _run_opencode_once(task: BatchTask, timeout: int) -> tuple[bool, str]:
    """跑一次 OpenCode 子进程。返回 (是否产生了 output_path 文件, stdout)。

    prompt 经临时文件用 `-f` 附件传入，不走 argv：大子系统的文件清单会把整条
    命令行撑过 Windows 上限，导致 CreateProcess 抛 WinError 206。
    """
    prompt_file = task.output_path.parent / f"{task.batch_id}.prompt.md"
    try:
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(task.user_request, encoding="utf-8")
    except OSError as e:
        _log(f"[llm_batch] {task.batch_id} 写 prompt 文件失败：{e}")
        return False, ""

    # 注意：positional message 必须在 -f 之前——-f 是 array 选项，若放在
    # message 之前会把后面的 message 也并吞成附件路径。
    cmd = [
        _OPENCODE, "run",
        "--agent", task.agent_name,
        "--dangerously-skip-permissions",
        "请完整阅读并执行附件中的全部指令。",
        "-f", str(prompt_file),
    ]
    try:
        proc = subprocess.run(
            cmd,
            env=_opencode_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _log(f"[llm_batch] {task.batch_id} 超时 {timeout}s")
        return False, ""
    finally:
        try:
            prompt_file.unlink()
        except OSError:
            pass
    if proc.returncode not in (0, 1):
        _log(f"[llm_batch] {task.batch_id} 退出码 {proc.returncode}")
    return task.output_path.exists(), proc.stdout or ""


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
        f"请修复成合法 JSON 并调用 write_report 写入 {repair_path}。"
    )
    repair_task = BatchTask(
        batch_id=f"{task.batch_id}-repair",
        agent_name="os-kernel-json-repair",
        user_request=repair_request,
        output_path=repair_path,
        cache_dir=task.cache_dir,
        cache_key="",
    )
    ok, _ = _run_opencode_once(repair_task, timeout)
    if not ok:
        return None
    return _parse_json_file(repair_path)


def run_batch_task(task: BatchTask, schema_hint: str = "",
                   timeout: int = 300) -> dict:
    """单 batch 完整执行：缓存查 → 跑 LLM → 解析 → 失败修复 → 兜底 fallback。"""
    cached = cache_read(task.cache_dir, task.cache_key)
    if cached is not None:
        _log(f"[llm_batch] {task.batch_id} 缓存命中")
        return cached

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

    # 增补正文后再入缓存：保证缓存命中时正文（含图表）不丢失
    if task.enrich is not None:
        try:
            parsed = task.enrich(parsed)
        except Exception as e:
            _log(f"[llm_batch] {task.batch_id} enrich 失败：{e}")

    # 只缓存成功结果：带 _error 的兜底**不写缓存**，否则一次瞬时失败（超时/限流/
    # 子进程异常）会被永久冻住，后续每次跑都命中空结果而不再重试。不缓存则下次自愈。
    if parsed.get("_error"):
        _log(f"[llm_batch] {task.batch_id} 失败结果不入缓存，下次将重试")
    else:
        cache_write(task.cache_dir, task.cache_key, parsed)
    return parsed


