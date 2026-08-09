# -*- coding: utf-8 -*-
"""批量为 作品.txt 里的作品生成决赛四件套。

每个作品的四份报告归档到 data/output/<队伍编号>/：
    summary.pdf
    description.html
    development.html
    comparison.html

特性：
- 事务式发布：临时区内四份报告全部成功后，才一次性发布到正式目录。
- 逐个作品串行（显存/磁盘友好），运行日志和摘要数据在进程结束时删除。
- API key 额度不足时自动切换到备用 key（改写 config.toml + .env + 重跑 setup_opencode）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from finals.cleanup import cleanup_report_directory, purge_report_directory, remove_directory
from src.ingest.cloner import clone_repo

ROOT = Path(__file__).resolve().parent
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
        subprocess.run([PY, "setup_opencode.py"], cwd=ROOT, check=False,
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


MIN_FREE_GB = 3.0                         # 剩余空间低于此值即中止，避免撑爆磁盘


def fork_to_repo_name(url: str) -> str:
    return url.rstrip("/").split("/")[-1].removesuffix(".git")


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


def do_comparison(team_id: str, url: str, work_dir: Path, logfile: Path) -> tuple[bool, str]:
    repo_name = fork_to_repo_name(url)
    cmd = [PY, "-m", "src.pipeline", "--repo", url + ".git", "--baselines"]
    if os.environ.get("BATCH_ENABLE_AI_DETECT", "").strip().lower() in {"1", "true", "yes"}:
        cmd.append("--ai-detect")
    cmp_timeout = int(os.environ.get("BATCH_CMP_TIMEOUT", "3600"))  # 巨型仓库可调大
    ok, body = run_step("对比报告", cmd, logfile, timeout=cmp_timeout)
    # 归档 HTML：pipeline 落到 data/output/<repo_name>/<repo_name>_comparison.html
    src_html = OUT / repo_name / f"{repo_name}_comparison.html"
    dst = work_dir / "comparison.html"
    dst_digest = work_dir / "comparison.digest.json"
    if src_html.exists():
        shutil.copy2(src_html, dst)
        src_digest = OUT / repo_name / f"{repo_name}_comparison.digest.json"
        if src_digest.exists():
            shutil.copy2(src_digest, dst_digest)
        return ok and dst.exists() and dst_digest.exists(), body
    # 兜底：直接落在 output 根
    alt = OUT / f"{repo_name}_comparison.html"
    if alt.exists():
        shutil.copy2(alt, dst)
        alt_digest = OUT / f"{repo_name}_comparison.digest.json"
        if alt_digest.exists():
            shutil.copy2(alt_digest, dst_digest)
        return ok and dst.exists() and dst_digest.exists(), body
    return False, body


def do_description(team_id: str, url: str, work_dir: Path, logfile: Path) -> tuple[bool, str]:
    repo_name = fork_to_repo_name(url)
    cloned = REPOS / repo_name  # 对比报告已克隆
    dst = work_dir / "description.html"
    if cloned.exists():
        src_arg = ["--repo-path", str(cloned)]
    else:
        src_arg = ["--url", url + ".git"]
    cmd = [
        PY, "agent.py", *src_arg, "-o", str(dst),
        "--team-id", team_id, "--repository-url", url,
        "--keep-intermediates",
    ]
    ok, body = run_step("描述报告", cmd, logfile, timeout=2400)
    digest = dst.with_suffix(".digest.json")
    return ok and dst.exists() and digest.exists(), body


def do_development(team_id: str, url: str, work_dir: Path, logfile: Path) -> tuple[bool, str]:
    repo_name = fork_to_repo_name(url)
    cloned = REPOS / repo_name
    dst = work_dir / "development.html"
    cmd = [
        PY, "-m", "finals", "development",
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
        PY, "-m", "finals", "summary",
        "--description-digest", str(work_dir / "description.digest.json"),
        "--development-digest", str(work_dir / "development.digest.json"),
        "--comparison-digest", str(work_dir / "comparison.digest.json"),
        "--repo-id", team_id, "--output", str(dst),
    ]
    ok, body = run_step("一页摘要", cmd, logfile, timeout=180)
    return ok and dst.exists(), body


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    teams = json.loads(WORKS.read_text(encoding="utf-8"))
    st = load_state()
    active_key = st.get("key") if st.get("key") in {"primary", "fallback"} else "primary"
    st["key"] = active_key
    try:
        set_api_key(active_key)
        total = len(teams)
        log(f"==== 批处理启动，共 {total} 个作品 ====")

        for entry in teams:
            idx = entry["序号"]
            team_id = entry["队伍编号"]
            url = entry["Fork地址"]
            repo_name = fork_to_repo_name(url)
            final_dir = OUT / team_id
            final_dir.mkdir(parents=True, exist_ok=True)
            tstate = st["teams"].setdefault(team_id, {})
            deliverables = deliverable_paths(team_id, final_dir)

            if all(path.is_file() for path in deliverables):
                cleanup_final_dir(team_id, final_dir)
                cleanup_team(repo_name, final_dir=final_dir)
                log(f"[{idx}/{total}] {team_id} 已完成，跳过")
                for kind in ("comparison", "description", "development", "summary"):
                    tstate[kind] = "done"
                save_state(st)
                continue

            # 不完整的旧结果不可与本次结果混用；正式目录先回到空状态。
            purge_final_dir(team_id, final_dir)
            fg = free_gb()
            if fg < MIN_FREE_GB:
                log(f"⚠ 剩余磁盘 {fg:.1f}GB < {MIN_FREE_GB}GB，中止批处理（请先清理磁盘再重跑）")
                break

            log(f"[{idx}/{total}] {team_id}  {url}  (剩余 {fg:.1f}GB)")
            if not ensure_clone(url, repo_name):
                log(f"  克隆最终失败，未发布 {team_id}")
                for kind in ("comparison", "description", "development", "summary"):
                    tstate[kind] = "failed"
                save_state(st)
                cleanup_team(repo_name, final_dir=final_dir)
                continue

            readiness = {
                "comparison": False,
                "description": False,
                "development": False,
                "summary": False,
            }
            try:
                with tempfile.TemporaryDirectory(prefix=f"oskernel_reports_{team_id}_") as temporary:
                    work_dir = Path(temporary)

                    lf = LOGDIR / f"{team_id}_comparison.log"
                    ok, body = do_comparison(team_id, url, work_dir, lf)
                    if not ok and quota_exhausted(body) and st["key"] == "primary":
                        log("  检测到额度/鉴权问题，切换备用 key 后重试对比报告")
                        if switch_to_fallback(st):
                            ok, body = do_comparison(team_id, url, work_dir, lf)
                    readiness["comparison"] = ok
                    log(f"  对比报告 {'成功' if ok else '失败'}")

                    lf = LOGDIR / f"{team_id}_description.log"
                    ok, body = do_description(team_id, url, work_dir, lf)
                    if not ok and quota_exhausted(body) and st["key"] == "primary":
                        log("  检测到额度/鉴权问题，切换备用 key 后重试描述报告")
                        if switch_to_fallback(st):
                            ok, body = do_description(team_id, url, work_dir, lf)
                    readiness["description"] = ok
                    log(f"  描述报告 {'成功' if ok else '失败'}")

                    lf = LOGDIR / f"{team_id}_development.log"
                    ok, _body = do_development(team_id, url, work_dir, lf)
                    readiness["development"] = ok
                    log(f"  开发过程报告 {'成功' if ok else '失败'}")

                    digest_inputs = (
                        work_dir / "comparison.digest.json",
                        work_dir / "description.digest.json",
                        work_dir / "development.digest.json",
                    )
                    if all(path.is_file() for path in digest_inputs):
                        lf = LOGDIR / f"{team_id}_summary.log"
                        ok, _body = do_summary(team_id, work_dir, lf)
                    else:
                        ok = False
                        missing = "、".join(path.name for path in digest_inputs if not path.is_file())
                        log(f"  一页摘要未生成，缺少临时输入：{missing}")
                    readiness["summary"] = ok
                    log(f"  一页摘要 {'成功' if ok else '失败'}")

                    if all(readiness.values()):
                        publish_final_reports(team_id, work_dir, final_dir)
                    else:
                        purge_final_dir(team_id, final_dir)
            finally:
                cleanup_team(repo_name, final_dir=final_dir)

            for kind, ready in readiness.items():
                tstate[kind] = "done" if ready else "failed"
            save_state(st)
            log(f"[{idx}/{total}] {team_id} 处理完毕 "
                f"(summary={tstate.get('summary')}, desc={tstate.get('description')}, "
                f"dev={tstate.get('development')}, cmp={tstate.get('comparison')}, "
                f"剩余 {free_gb():.1f}GB)")

        done = sum(1 for t in st["teams"].values()
                   if all(t.get(kind) == "done" for kind in
                          ("summary", "description", "development", "comparison")))
        log(f"==== 批处理结束：{done}/{total} 完整完成 ====")
    except BatchConfigurationError as exc:
        raise SystemExit(f"[batch] 配置错误：{exc}") from None
    finally:
        # 无论成功、失败还是异常退出，运行日志、状态和克隆都不属于交付物。
        for entry in teams:
            final_dir = OUT / entry["队伍编号"]
            cleanup_team(fork_to_repo_name(entry["Fork地址"]), final_dir=final_dir)
            remove_empty_final_dir(entry["队伍编号"], final_dir)
        cleanup_batch_runtime()


if __name__ == "__main__":
    main()
