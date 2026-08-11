"""Core orchestrator — executes report kinds in dependency order with checkpoint resume."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class ReportKind(Enum):
    COMPARISON = "comparison"
    DESCRIPTION = "description"
    DEVELOPMENT = "development"
    SUMMARY = "summary"


@dataclass
class KindResult:
    kind: str
    status: str              # "ok" | "failed" | "skipped"
    html_path: str | None    # relative to output_dir
    digest_path: str | None  # relative to output_dir
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class JobResult:
    repo_id: str
    kinds: dict[str, KindResult]
    started_at: str
    finished_at: str


_FINAL_FILES = frozenset({
    "summary.pdf", "description.html", "development.html", "comparison.html",
})

_STATE_SCHEMA_VERSION = "report-jobs-v1"

_SUMMARY_UPSTREAM = {"comparison", "description", "development"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_path(output_dir: Path) -> Path:
    return output_dir / ".report_jobs_state.json"


def _load_state(output_dir: Path) -> dict:
    sp = _state_path(output_dir)
    if not sp.exists():
        return {}
    try:
        data = json.loads(sp.read_text(encoding="utf-8"))
        if data.get("schema_version") == _STATE_SCHEMA_VERSION:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_state(output_dir: Path, state: dict) -> None:
    state["schema_version"] = _STATE_SCHEMA_VERSION
    state["updated_at"] = _now()
    sp = _state_path(output_dir)
    sp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _init_state(repo_id: str, repo_url: str, kinds: list[str]) -> dict:
    return {
        "repo_id": repo_id,
        "repo_url": repo_url,
        "kinds_requested": kinds,
        "started_at": _now(),
        "kinds": {},
    }


def _find_files_by_suffix(directory: Path, suffix: str, skip_dirs: set | None = None) -> list[Path]:
    if skip_dirs is None:
        skip_dirs = {"_repos", "node_modules", ".git", ".semantic_cache"}
    results: list[Path] = []
    try:
        for entry in directory.iterdir():
            if entry.is_dir():
                if entry.name not in skip_dirs and not entry.name.startswith("."):
                    results.extend(_find_files_by_suffix(entry, suffix, skip_dirs))
            elif entry.name.lower().endswith(suffix):
                results.append(entry)
    except OSError:
        pass
    return results


def _find_single_by_suffix(directory: Path, suffix: str) -> Path | None:
    candidates = _find_files_by_suffix(directory, suffix)
    if not candidates:
        # fallback: top-level
        for f in directory.iterdir():
            if f.is_file() and f.name.lower().endswith(suffix):
                candidates.append(f)
        if not candidates:
            return None
    with_stat = [(f, f.stat().st_mtime) for f in candidates]
    with_stat.sort(key=lambda x: -x[1])
    return with_stat[0][0]


def _find_clone_path(output_dir: Path) -> Path | None:
    repos_dir = output_dir / "_repos"
    if not repos_dir.is_dir():
        return None
    for entry in repos_dir.iterdir():
        if entry.is_dir() and (entry / ".git").exists():
            return entry
    return None


def _safe_dirname(url: str, fallback: str) -> str:
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        name = parsed.path.rstrip("/").split("/")[-1] or fallback
    except Exception:
        name = url.replace("\\", "/").rstrip("/").split("/")[-1] or fallback
    name = name.replace(".git", "")
    invalid = r'<>:"/\|?*' + "".join(chr(c) for c in range(0, 32))
    for ch in invalid:
        name = name.replace(ch, "_")
    return name or fallback


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "git@", "ssh://"))


# ---- per-kind runners ----

def _run_comparison(repo: str, output_dir: Path, baselines: bool = True) -> KindResult:
    result = KindResult(kind="comparison", status="failed", html_path=None, digest_path=None)
    result.started_at = _now()
    try:
        from oskernel_agent.comparison.pipeline.__main__ import main as cmp_main

        argv = [
            "--repo", repo,
            "--output-dir", str(output_dir),
        ]
        if baselines:
            argv.append("--baselines")
        rc = cmp_main(argv)

        if rc != 0:
            print(f"[report_jobs] comparison pipeline exited with code {rc}", file=sys.stderr)
            result.error = f"comparison pipeline exited {rc}"
            result.finished_at = _now()
            return result

        # find and promote outputs
        html_src = _find_single_by_suffix(output_dir, "_comparison.html")
        if html_src is None:
            result.error = "comparison pipeline completed but no HTML found"
            result.finished_at = _now()
            return result

        dest_html = output_dir / "comparison.html"
        if html_src.resolve() != dest_html.resolve():
            shutil.copy2(html_src, dest_html)
        result.html_path = "comparison.html"

        digest_src = _find_single_by_suffix(output_dir, "_comparison.digest.json")
        if digest_src is None:
            result.error = "comparison pipeline completed but no digest found"
            result.finished_at = _now()
            return result

        dest_digest = output_dir / "comparison.digest.json"
        if digest_src.resolve() != dest_digest.resolve():
            shutil.copy2(digest_src, dest_digest)
        result.digest_path = "comparison.digest.json"

        result.status = "ok"
    except Exception as exc:
        result.error = f"comparison failed: {exc}"
    result.finished_at = _now()
    return result


def _ensure_clone(repo: str, output_dir: Path) -> Path | None:
    existing = _find_clone_path(output_dir)
    if existing is not None:
        return existing

    if not _is_url(repo):
        local = Path(repo)
        if local.is_dir():
            return local
        return None

    try:
        from oskernel_agent.comparison.ingest.cloner import clone_repo

        repos_dir = output_dir / "_repos"
        repos_dir.mkdir(parents=True, exist_ok=True)
        dest = repos_dir / _safe_dirname(repo, "repo")
        if dest.exists():
            dest = repos_dir / f"{_safe_dirname(repo, 'repo')}_{int(time.time())}"
        clone_repo(repo, dest, depth=200)
        return dest
    except Exception as exc:
        print(f"[report_jobs] clone failed for {repo}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None


def _run_description(clone_path: Path, output_dir: Path) -> KindResult:
    result = KindResult(kind="description", status="failed", html_path=None, digest_path=None)
    result.started_at = _now()
    try:
        from oskernel_agent.cli.agent import _run_tree_mode

        html_path = output_dir / "description.html"
        repo_name = clone_path.name
        out = _run_tree_mode(clone_path, repo_name, str(html_path), cli_depth=3)
        if out is None:
            result.error = "description report generation returned None"
            result.finished_at = _now()
            return result

        result.html_path = "description.html"

        digest_path = output_dir / "description.digest.json"
        if digest_path.exists():
            result.digest_path = "description.digest.json"
        else:
            result.error = "description report produced HTML but no digest"
            result.finished_at = _now()
            return result

        result.status = "ok"
    except Exception as exc:
        print(f"[report_jobs] description failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        result.error = f"description failed: {exc}"
    result.finished_at = _now()
    return result


def _run_development(clone_path: Path, output_dir: Path, repo_id: str,
                     min_commits: int | None) -> KindResult:
    result = KindResult(kind="development", status="failed", html_path=None, digest_path=None)
    result.started_at = _now()
    try:
        from oskernel_agent.finals.development import generate_development_report

        html_path = output_dir / "development.html"
        dev_result = generate_development_report(
            str(clone_path), str(html_path),
            repo_id=repo_id, min_commits=min_commits,
        )
        result.html_path = "development.html"

        digest_raw = dev_result.get("digest_path")
        if digest_raw and Path(digest_raw).exists():
            result.digest_path = "development.digest.json"
        else:
            result.error = "development report produced HTML but no digest"
            result.finished_at = _now()
            return result

        result.status = "ok"
    except Exception as exc:
        print(f"[report_jobs] development failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        result.error = f"development failed: {exc}"
    result.finished_at = _now()
    return result


def _run_summary(output_dir: Path, repo_id: str) -> KindResult:
    result = KindResult(kind="summary", status="failed", html_path=None, digest_path=None)
    result.started_at = _now()
    try:
        digests = []
        for kind in ("description", "development", "comparison"):
            dp = output_dir / f"{kind}.digest.json"
            if not dp.exists():
                result.error = f"missing upstream digest: {kind}.digest.json"
                result.finished_at = _now()
                return result
            digests.append(str(dp))

        from oskernel_agent.finals.summary_pdf import generate_summary_pdf

        pdf_path = output_dir / "summary.pdf"
        generate_summary_pdf(digests, str(pdf_path), repo_id=repo_id)
        result.html_path = "summary.pdf"
        result.status = "ok"
    except Exception as exc:
        print(f"[report_jobs] summary failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        result.error = f"summary failed: {exc}"
    result.finished_at = _now()
    return result


def _cleanup_non_final(output_dir: Path) -> None:
    for entry in list(output_dir.iterdir()):
        if entry.is_file() and entry.name in _FINAL_FILES:
            continue
        if entry.name == ".report_jobs_state.json":
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            pass


# ---- main entry ----

def run(
    repo: str,
    repo_id: str,
    output_dir: str | Path,
    kinds: list[str],
    *,
    baselines: bool = True,
    min_commits: int | None = None,
) -> JobResult:
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    state = _load_state(out)
    # only reuse state if the request matches
    if (state.get("repo_id") != repo_id
            or state.get("repo_url") != repo
            or state.get("kinds_requested") != kinds):
        state = _init_state(repo_id, repo, kinds)
        _save_state(out, state)

    job_started = state.get("started_at", _now())
    kind_results: dict[str, KindResult] = {}

    requested_set = set(kinds)
    # if summary requested, ensure all upstream kinds are also requested
    if "summary" in requested_set:
        requested_set.update(_SUMMARY_UPSTREAM)

    # execution order
    order = ["comparison", "description", "development", "summary"]
    enabled = [k for k in order if k in requested_set]

    for kind in enabled:
        prior = state.get("kinds", {}).get(kind, {})
        if prior.get("status") == "ok":
            kind_results[kind] = KindResult(
                kind=kind, status="skipped",
                html_path=prior.get("html_path"), digest_path=prior.get("digest_path"),
                started_at=prior.get("started_at"), finished_at=prior.get("finished_at"),
            )
            continue

        kr: KindResult
        with contextlib.redirect_stdout(io.StringIO()):
            if kind == "comparison":
                kr = _run_comparison(repo, out, baselines=baselines)
            elif kind == "description":
                clone_path = _ensure_clone(repo, out)
                if clone_path is None:
                    kr = KindResult(kind="description", status="failed",
                                    html_path=None, digest_path=None,
                                    error="no local clone available; comparison step may be needed")
                else:
                    kr = _run_description(clone_path, out)
            elif kind == "development":
                clone_path = _ensure_clone(repo, out)
                if clone_path is None:
                    kr = KindResult(kind="development", status="failed",
                                    html_path=None, digest_path=None,
                                    error="no local clone available; comparison step may be needed")
                else:
                    kr = _run_development(clone_path, out, repo_id, min_commits)
            elif kind == "summary":
                kr = _run_summary(out, repo_id)

        kind_results[kind] = kr
        state.setdefault("kinds", {})[kind] = {
            "status": kr.status,
            "html_path": kr.html_path,
            "digest_path": kr.digest_path,
            "error": kr.error,
            "started_at": kr.started_at,
            "finished_at": kr.finished_at,
        }
        _save_state(out, state)

        if kr.status != "ok":
            # don't proceed to downstream kinds on failure
            break

    # cleanup non-final artifacts if summary succeeded or wasn't requested
    summary_result = kind_results.get("summary")
    if (summary_result and summary_result.status == "ok") or "summary" not in requested_set:
        _cleanup_non_final(out)

    return JobResult(
        repo_id=repo_id,
        kinds=kind_results,
        started_at=job_started,
        finished_at=_now(),
    )
