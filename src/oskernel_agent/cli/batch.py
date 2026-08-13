# -*- coding: utf-8 -*-
"""决赛四报告的单步执行库（不再提供整批编排）。

每个步骤（对比 / 描述 / 开发过程 / 一页摘要）独立执行、独立发布，供一对一驱动
（如 run_incremental_1931.py）调用：任一报告成功即发布，失败只影响它自身，其余
步骤照常进行。不再有批处理的事务式“四份齐全才发布”。

- do_comparison / do_description / do_development / do_summary 复用完整交付门禁。
- API key 额度切换、网络/门禁抖动重试、克隆、清理与发布等辅助函数供各驱动复用。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from oskernel_agent.finals.cleanup import cleanup_report_directory, purge_report_directory, remove_directory
from oskernel_agent.comparison.ingest.cloner import clone_repo
from oskernel_agent.paths import PROJECT_ROOT
from oskernel_agent.repository_identity import repository_storage_key

ROOT = PROJECT_ROOT
# 跨平台定位 venv 解释器：优先当前解释器（激活 venv 后即为 venv python），
# 否则按平台找 Scripts/python.exe（Windows）或 bin/python（Linux）。
def _venv_python() -> str:
    cand = [ROOT / ".venv" / "bin" / "python",
            ROOT / ".venv" / "Scripts" / "python.exe"]
    for c in cand:
        if c.exists():
            return str(c)
    return sys.executable
PY = _venv_python()
WORKS = ROOT / "作品.txt"
OUT = ROOT / "data" / "output"
REPOS = OUT / "_repos"
LOGDIR = OUT / "_batch"
PROGRESS = LOGDIR / "progress.log"
STATE = LOGDIR / "state.json"

_BATCH_PRIMARY_KEY_ENV = "BATCH_PRIMARY_LLM_API_KEY"
_BATCH_FALLBACK_KEY_ENV = "BATCH_FALLBACK_LLM_API_KEY"

# 额度耗尽 / 鉴权失败的日志特征
QUOTA_TOKENS = [
    "insufficient_quota", "Insufficient Balance", "insufficient balance",
    "Arrearage", "arrearage", "欠费", "余额不足", "额度", "exceeded your current quota",
    "AllocationQuota", "Access denied", "invalid_api_key", "InvalidApiKey",
    "Throttling.User", "402", "401 ", "authentication_error",
]

def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    LOGDIR.mkdir(parents=True, exist_ok=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"key": "primary", "teams": {}}


def save_state(st: dict) -> None:
    LOGDIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


class BatchConfigurationError(RuntimeError):
    """批处理凭据缺失或无效；错误信息不得包含凭据值。"""


def _batch_api_key(which: str) -> str:
    """从环境读取指定槽位的密钥，不允许源码或默认值兜底。"""
    env_name = {
        "primary": _BATCH_PRIMARY_KEY_ENV,
        "fallback": _BATCH_FALLBACK_KEY_ENV,
    }.get(which)
    if env_name is None:
        raise BatchConfigurationError(f"未知 API key 槽位：{which!r}")

    key = os.environ.get(env_name, "").strip()
    if not key:
        raise BatchConfigurationError(
            f"未配置环境变量 {env_name}；请写入本地 .env 或进程环境，勿提交真实密钥"
        )
    if which == "fallback":
        primary = os.environ.get(_BATCH_PRIMARY_KEY_ENV, "").strip()
        if primary and primary == key:
            raise BatchConfigurationError(
                f"{_BATCH_FALLBACK_KEY_ENV} 必须与 {_BATCH_PRIMARY_KEY_ENV} 不同"
            )
    return key


def set_api_key(which: str) -> None:
    """把 config.toml [api].key 和 .env LLM_API_KEY 改成 primary/fallback，并重跑 setup_opencode。"""
    key = _batch_api_key(which)
    cfg = ROOT / "config.toml"
    if not cfg.is_file():
        raise BatchConfigurationError("缺少本地 config.toml，请先由 config.toml.example 创建")
    text = cfg.read_text(encoding="utf-8")
    text, replacements = re.subn(
        r'(?m)^(key\s*=\s*)".*?"',
        lambda match: f'{match.group(1)}"{key}"',
        text,
        count=1,
    )
    if replacements != 1:
        raise BatchConfigurationError("config.toml 中未找到 [api] key 配置项")
    cfg.write_text(text, encoding="utf-8")

    env = ROOT / ".env"
    etext = env.read_text(encoding="utf-8") if env.is_file() else ""
    if re.search(r"(?m)^LLM_API_KEY=", etext):
        etext = re.sub(
            r"(?m)^LLM_API_KEY=.*$", lambda _: f"LLM_API_KEY={key}", etext
        )
    else:
        etext = f"LLM_API_KEY={key}\n" + etext
    env.write_text(etext, encoding="utf-8")
    os.environ["LLM_API_KEY"] = key

    # 让 OpenCode（描述报告 DIR/VERDICT agent）用上新 key
    try:
        subprocess.run([PY, "-m", "oskernel_agent.cli.setup_opencode"], cwd=ROOT, check=False,
                       capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        log(f"  setup_opencode 失败（忽略）：{e}")
    log(f"  已切换 API key → {which}")


def switch_to_fallback(st: dict) -> bool:
    """安全切换备用凭据；配置错误只记录变量名，不改变持久状态。"""
    try:
        set_api_key("fallback")
    except BatchConfigurationError as exc:
        log(f"  无法切换备用 API key：{exc}")
        return False
    st["key"] = "fallback"
    save_state(st)
    return True


def child_env() -> dict:
    env = dict(os.environ)
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def run_step(name: str, cmd: list[str], logfile: Path, timeout: int) -> tuple[bool, str]:
    log(f"  {name}: {' '.join(cmd)}")
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with logfile.open("w", encoding="utf-8", errors="replace") as lf:
        lf.write(f"# {name}\n# {' '.join(cmd)}\n# start {datetime.now()}\n\n")
        lf.flush()
        try:
            p = subprocess.run(cmd, cwd=ROOT, env=child_env(), stdout=lf,
                               stderr=subprocess.STDOUT, text=True, timeout=timeout)
            ok = p.returncode == 0
        except subprocess.TimeoutExpired:
            lf.write(f"\n# TIMEOUT after {timeout}s\n")
            ok = False
    body = logfile.read_text(encoding="utf-8", errors="replace")
    return ok, body


def quota_exhausted(body: str) -> bool:
    return any(tok in body for tok in QUOTA_TOKENS)


# 网络/供应商瞬时故障的日志特征（非代码缺陷，重试即可恢复）
NETWORK_TOKENS = [
    "APIConnectionError", "ConnectionError", "Connection reset",
    "Connection aborted", "RemoteDisconnected", "BrokenPipe",
    "Read timed out", "timed out", "Timeout", "超时",
    "No route to host", "name resolution failed", "resolve",
    "TLS", "SSL", "handshake", "schannel", "网络", "连接",
]


def network_flaked(body: str) -> bool:
    return any(tok in body for tok in NETWORK_TOKENS)


# 报告期 LLM 交付门禁的偶发失败特征：模型漏段、格式抖动等，非代码缺陷；
# 重跑时确定性阶段几分钟即可完成，复核/语义缓存命中后模型调用极少。
REPORT_FLAKE_TOKENS = [
    "IncompleteReportError", "模型复核未完整完成",
    "语义级分析模型未返回", "语义级分析功能簇内容不完整",
    "语义级分析模型调用失败", "语义级分析未通过中文交付校验",
    "创新实现语义归纳模型调用失败", "创新实现语义归纳模型未返回合法 JSON",
]


def report_flaked(body: str) -> bool:
    return any(tok in body for tok in REPORT_FLAKE_TOKENS)


# 描述报告的 LLM 门禁偶发失败特征（步骤约 16 分钟，只兜底重试 1 次）
DESCRIPTION_FLAKE_TOKENS = [
    "AI 未能完成系统级高风险硬编码线索的定向复核",
    "未通过交付校验，不入缓存",
]


def description_flaked(body: str) -> bool:
    return any(tok in body for tok in DESCRIPTION_FLAKE_TOKENS)


def retry_step(name: str, body: str, step_thunk, retries: int = 3) -> tuple[bool, str]:
    """LLM 步骤偶发网络抖动时按指数退避整体重跑；报告重跑可增量续跑缓存。"""
    ok = False  # 本函数只在步骤失败后被调用；必须从失败态开始，否则不重试且误报成功
    attempt = 0
    while not ok and attempt < retries:
        attempt += 1
        log(f"  {name} 网络中断，等待后重试（第 {attempt}/{retries} 次）")
        time.sleep(20 * attempt)
        ok, body = step_thunk()
    return ok, body


MIN_FREE_GB = 3.0                         # 剩余空间低于此值即中止，避免撑爆磁盘


def fork_to_repo_name(url: str) -> str:
    """兼容旧调用名；返回 URL 派生的稳定唯一存储键。"""
    return repository_storage_key(url)


def free_gb() -> float:
    return shutil.disk_usage(ROOT).free / (1024 ** 3)


def deliverable_paths(team_id: str, final_dir: Path) -> tuple[Path, ...]:
    """决赛最终交付物；结构化数据和模型工作文件均不是报告。"""
    return (
        final_dir / "summary.pdf",
        final_dir / "description.html",
        final_dir / "development.html",
        final_dir / "comparison.html",
    )


def _report_digests_match_team(team_id: str, final_dir: Path, *, extra_ids: set[str] | None = None) -> bool:
    """确认三份结构化摘要属于当前队伍；失败时保持可重跑。

    comparison pipeline 内部用 clone 目录名作为 repo_id（即 storage key），
    与 team_id 不同；extra_ids 用于接受这些别名。
    """
    valid = {team_id}
    if extra_ids:
        valid.update(extra_ids)
    for kind in ("comparison", "description", "development"):
        path = final_dir / f"{kind}.digest.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if str(payload.get("repo_id") or "") not in valid:
            return False
    return True


def cleanup_final_dir(team_id: str, final_dir: Path) -> None:
    """四份报告齐全后删除该队伍目录中的所有中间产物。"""
    resolved = final_dir.resolve()
    if resolved.name != team_id:
        raise ValueError(f"拒绝清理非队伍输出目录：{resolved}")
    cleanup_report_directory(
        resolved,
        (path.name for path in deliverable_paths(team_id, resolved)),
        output_root=OUT,
    )


def purge_final_dir(team_id: str, final_dir: Path) -> None:
    """清空未完成或发布失败的队伍目录，不让半成品和中间产物留在交付区。"""
    resolved = final_dir.resolve()
    if resolved.name != team_id:
        raise ValueError(f"拒绝清理非队伍输出目录：{resolved}")
    purge_report_directory(resolved, output_root=OUT)


def publish_final_reports(team_id: str, staging_dir: Path, final_dir: Path) -> None:
    """仅当临时区四份报告齐全时发布，并保证正式目录恰好只有四个文件。"""
    staged = deliverable_paths(team_id, staging_dir)
    missing = [path.name for path in staged if not path.is_file()]
    if missing:
        raise RuntimeError(f"拒绝发布不完整报告，缺少：{'、'.join(missing)}")

    final_dir.mkdir(parents=True, exist_ok=True)
    purge_final_dir(team_id, final_dir)
    try:
        for source, destination in zip(staged, deliverable_paths(team_id, final_dir)):
            shutil.copy2(source, destination)
        cleanup_final_dir(team_id, final_dir)
    except Exception:
        purge_final_dir(team_id, final_dir)
        raise


def cleanup_batch_runtime() -> None:
    """删除日志、断点状态和克隆目录；它们都不是四份正式报告。"""
    remove_directory(LOGDIR)
    remove_directory(REPOS)
    (OUT / "recall_completeness_audit.json").unlink(missing_ok=True)


def remove_empty_final_dir(team_id: str, final_dir: Path) -> None:
    """失败后不留下空队伍目录，同时拒绝操作正式输出根之外的路径。"""
    resolved = final_dir.resolve()
    if resolved.name != team_id or resolved.parent != OUT.resolve():
        raise ValueError(f"拒绝清理非队伍输出目录：{resolved}")
    if resolved.is_dir() and not any(resolved.iterdir()):
        resolved.rmdir()


def cleanup_team(repo_name: str, *, final_dir: Path | None = None) -> None:
    """删掉该队伍的克隆 + 遗留中间产物，控制峰值磁盘占用。"""
    remove_directory(REPOS / repo_name)
    for p in OUT.glob(f"{repo_name}_*"):        # 遗留的 *_recall/_suspects*.json 等
        if p.is_file():
            p.unlink(missing_ok=True)
    pipeline_dir = OUT / repo_name
    # 仓库名可能与队伍编号相同，此时 pipeline_dir 就是正式报告目录。
    # 正式目录已由 cleanup_final_dir 精确清理，绝不能再次整目录删除。
    if final_dir is None or pipeline_dir.resolve() != final_dir.resolve():
        remove_directory(pipeline_dir)


_SRC_EXTS = {".rs", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
             ".go", ".S", ".s", ".asm", ".zig", ".java"}


def _clone_has_source(dest: Path) -> bool:
    """工作区里是否至少有一个源码文件（首个命中即返回，快）。
    防止大仓库被低速中止截断成「只有 .git」的空壳，导致解析出 0 源文件、报告为空。"""
    try:
        for p in dest.rglob("*"):
            if ".git" in p.parts:
                continue
            if p.is_file() and p.suffix in _SRC_EXTS:
                return True
    except OSError:
        pass
    return False


def ensure_clone(url: str, repo_name: str, retries: int = 4) -> bool:
    """带重试地把仓库克隆到 data/output/_repos/<repo_name>，并校验拉全。
    gitlab.eduxiji.net 偶发 exit 128（网络抖动）+ 大仓库偶发截断，两者重试可救回。"""
    dest = REPOS / repo_name
    if (dest / ".git").exists() and _clone_has_source(dest):
        return True
    REPOS.mkdir(parents=True, exist_ok=True)
    for i in range(1, retries + 1):
        try:
            clone_repo(url + ".git", dest, depth=200)
        except Exception as exc:  # noqa: BLE001 - 网络、Git 与文件系统错误均可重试
            log(f"  克隆失败（第 {i}/{retries} 次）：{exc}")
            time.sleep(min(10 * i, 40))
            continue
        # git 成功还不够：校验工作区真有源码，否则视为截断，重试
        if _clone_has_source(dest):
            log(f"  克隆成功（第 {i} 次）→ {dest}")
            return True
        log(f"  克隆疑似不完整（工作区无源码，第 {i}/{retries} 次），重试")
        time.sleep(min(10 * i, 40))
    # 重试用尽：至少 .git 在（可能真是空仓库）→ 放行让流水线自行给出「无源文件」
    return (dest / ".git").exists()


def _comparison_resume_ready(repo_name: str) -> bool:
    """resume-from report 所需前序产物是否完整落盘。

    AI 检测会加载约 14GB 参考模型，内存不足时原生崩溃且无 traceback；此时若
    确定性阶段产物与存档 ai_detect 已齐备，用 --resume-from report 续跑报告
    阶段即可复用存档产物（按源码指纹校验），不再触碰模型加载。
    """
    for suffix in ("_filematch.json", "_recall.json", "_suspects.json",
                   "_suspects_v2.json", "_suspects_final.json"):
        path = OUT / f"{repo_name}{suffix}"
        if not path.is_file() or path.stat().st_size == 0:
            return False
    return (OUT / repo_name / f"{repo_name}_ai_detect.json").is_file()


def do_comparison(team_id: str, url: str, work_dir: Path, logfile: Path) -> tuple[bool, str]:
    repo_name = fork_to_repo_name(url)
    cmd = [PY, "-m", "oskernel_agent.comparison.pipeline", "--repo", url + ".git", "--baselines"]
    if os.environ.get("BATCH_ENABLE_AI_DETECT", "").strip().lower() in {"1", "true", "yes"}:
        cmd.append("--ai-detect")
    if _comparison_resume_ready(repo_name):
        cmd.extend(["--resume-from", "report"])
        log(f"  对比报告前序产物齐备，续跑报告阶段（跳过模型加载）")
    cmp_timeout = int(os.environ.get("BATCH_CMP_TIMEOUT", "3600"))  # 巨型仓库可调大
    source_pairs = (
        (
            OUT / repo_name / f"{repo_name}_comparison.html",
            OUT / repo_name / f"{repo_name}_comparison.digest.json",
        ),
        (
            OUT / f"{repo_name}_comparison.html",
            OUT / f"{repo_name}_comparison.digest.json",
        ),
    )

    def signature(path: Path) -> tuple[str, int] | None:
        if not path.is_file():
            return None
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns
        except OSError:
            return None

    before = [tuple(signature(path) for path in pair) for pair in source_pairs]
    ok, body = run_step("对比报告", cmd, logfile, timeout=cmp_timeout)
    if not ok:
        return False, body

    dst = work_dir / "comparison.html"
    dst_digest = work_dir / "comparison.digest.json"
    for index, pair in enumerate(source_pairs):
        current = tuple(signature(path) for path in pair)
        if current[0] is None or current[1] is None or current == before[index]:
            continue
        shutil.copy2(pair[0], dst)
        shutil.copy2(pair[1], dst_digest)
        normalize_comparison_identity(dst, dst_digest, team_id, repo_name)
        return True, body
    return False, body + "\n对比报告命令虽返回成功，但未产生本轮新的 HTML 与摘要文件。"


def normalize_comparison_identity(
    html_path: Path, digest_path: Path, team_id: str, storage_key: str,
) -> None:
    """将内部防碰撞存储键替换为评委可见的比赛队伍编号。"""
    payload = json.loads(digest_path.read_text(encoding="utf-8"))
    original_repo_id = str(payload.get("repo_id") or "")
    if original_repo_id not in {storage_key, team_id}:
        raise RuntimeError(
            f"comparison digest identity mismatch: {original_repo_id!r}"
        )
    payload["repo_id"] = team_id
    digest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    html = html_path.read_text(encoding="utf-8")
    html_path.write_text(html.replace(storage_key, team_id), encoding="utf-8")


def do_description(team_id: str, url: str, work_dir: Path, logfile: Path) -> tuple[bool, str]:
    repo_name = fork_to_repo_name(url)
    cloned = REPOS / repo_name  # 对比报告已克隆
    dst = work_dir / "description.html"
    if cloned.exists():
        src_arg = ["--repo-path", str(cloned)]
    else:
        src_arg = ["--url", url + ".git"]
    cmd = [
        PY, "-m", "oskernel_agent.cli.agent", *src_arg, "-o", str(dst),
        "--team-id", team_id, "--repository-url", url,
        "--keep-intermediates",
    ]
    ok, body = run_step("描述报告", cmd, logfile, timeout=7200)
    digest = dst.with_suffix(".digest.json")
    return ok and dst.exists() and digest.exists(), body


def do_development(team_id: str, url: str, work_dir: Path, logfile: Path) -> tuple[bool, str]:
    repo_name = fork_to_repo_name(url)
    cloned = REPOS / repo_name
    dst = work_dir / "development.html"
    cmd = [
        PY, "-m", "oskernel_agent.finals", "development",
        "--repo", str(cloned), "--repo-id", team_id, "--output", str(dst),
        "--keep-intermediates",
    ]
    minimum_commits = os.environ.get("FINALS_MIN_COMMITS", "").strip()
    if minimum_commits:
        cmd.extend(["--min-commits", minimum_commits])
    ok, body = run_step("开发过程报告", cmd, logfile, timeout=600)
    return ok and dst.exists() and dst.with_suffix(".digest.json").exists(), body


def do_summary(team_id: str, work_dir: Path, logfile: Path) -> tuple[bool, str]:
    dst = work_dir / "summary.pdf"
    cmd = [
        PY, "-m", "oskernel_agent.finals", "summary",
        "--description-digest", str(work_dir / "description.digest.json"),
        "--development-digest", str(work_dir / "development.digest.json"),
        "--comparison-digest", str(work_dir / "comparison.digest.json"),
        "--repo-id", team_id, "--output", str(dst),
    ]
    ok, body = run_step("一页摘要", cmd, logfile, timeout=600)
    return ok and dst.exists(), body


