"""Known-parent local Git fixtures for production report regression tests."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


def git(repo: Path, *args: str, dates: tuple[str, str] | None = None,
        expected: tuple[int, ...] = (0,)) -> str:
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    if dates:
        env.update(GIT_AUTHOR_DATE=dates[0], GIT_COMMITTER_DATE=dates[1])
    result = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=repo,
                            capture_output=True, text=True, env=env, timeout=15)
    if result.returncode not in expected:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout


def initialize(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Synthetic history control")
    git(path, "config", "user.email", "fixture@invalid.example")
    return path


def commit(repo: Path, subject: str, day: int, *, author_day: int | None = None) -> str:
    git(repo, "add", "source.c")
    author = f"2026-10-{author_day or day:02d}T12:00:00+00:00"
    committer = f"2026-10-{day:02d}T12:00:00+00:00"
    git(repo, "commit", "-q", "-m", subject, dates=(author, committer))
    return git(repo, "rev-parse", "HEAD").strip()


def source_large() -> str:
    return "int values[] = {\n" + "".join(f"    {i},\n" for i in range(1200)) + "};\n"


def build_controls(root: Path) -> dict:
    root.mkdir(exist_ok=True)
    root_repo = initialize(root / "complete-root")
    (root_repo / "source.c").write_text(source_large())
    real_root = commit(root_repo, "Real initial source import", 1)

    origin = initialize(root / "complete-shallow-origin")
    (origin / "source.c").write_text(source_large())
    parent = commit(origin, "Known original parent", 1)
    (origin / "source.c").write_text(source_large().replace("    500,\n", "    1500,\n"))
    boundary = commit(origin, "One line changed at future shallow boundary", 2)
    (origin / "source.c").write_text(source_large().replace("    500,\n", "    1500,\n")
                                     .replace("    600,\n", "    1600,\n"))
    head = commit(origin, "One line changed after boundary", 3)
    git(root, "clone", "-q", "--depth", "2", origin.as_uri(), "depth-two")

    marked = initialize(root / "marked-real-root")
    (marked / "source.c").write_text(source_large())
    marked_sha = commit(marked, "Genuine parentless root with shallow marker", 1)
    (marked / ".git" / "shallow").write_text(marked_sha + "\n")

    merge_repo = initialize(root / "resolved-merge")
    (merge_repo / "source.c").write_text("int answer(void) { return 0; }\n")
    base = commit(merge_repo, "Base source", 1)
    git(merge_repo, "checkout", "-q", "-b", "left")
    (merge_repo / "source.c").write_text("int answer(void) { return 1; }\n")
    left = commit(merge_repo, "Left source", 2)
    git(merge_repo, "checkout", "-q", "-b", "right", base)
    (merge_repo / "source.c").write_text("int answer(void) { return 2; }\n")
    right = commit(merge_repo, "Right source", 3)
    git(merge_repo, "checkout", "-q", "left")
    git(merge_repo, "merge", "--no-commit", "right", expected=(1,))
    (merge_repo / "source.c").write_text("int answer(void) { return 3; }\n")
    merged = commit(merge_repo, "Resolve source conflict", 4)

    dated = initialize(root / "nonmonotone-author-dates")
    (dated / "source.c").write_text("int answer(void) { return 0; }\n")
    date_root = commit(dated, "Later declared author date", 7)
    (dated / "source.c").write_text("int answer(void) { return 1; }\n")
    date_head = commit(dated, "Earlier declared author date", 8, author_day=2)
    return {
        "complete_root": {"root": root_repo, "sha": real_root},
        "shallow_boundary": {"root": root / "depth-two", "origin": origin,
                             "parent": parent, "boundary": boundary, "head": head},
        "marked_real_root": {"root": marked, "sha": marked_sha},
        "resolved_merge": {"root": merge_repo, "base": base, "left": left,
                           "right": right, "merge": merged},
        "nonmonotone_dates": {"root": dated, "first": date_root, "second": date_head},
    }


def known_numstat(repo: Path, parent: str, child: str) -> int:
    rows = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--numstat", parent, child)
    return sum(int(a) + int(d) for line in rows.splitlines()
               for a, d, _ in [line.split("\t", 2)])


def neutral_ai(commits: list[dict], evidence: dict) -> dict:
    """Deterministic report-format input, never an independent outcome oracle."""
    return {
        "conclusion": "提交历史呈现源码修改，尚需评委结合其他证据核对。",
        "issues": [
            {"candidate_id": c["candidate_id"],
             "status": "report" if c.get("must_report") else "dismiss",
             "title": c["kind"], "analysis": "当前可见历史的证据范围需要核对。",
             "severity": "info", "confidence": 70,
             "commit_shas": c["commit_shas"][:1]}
            for c in evidence["candidates"]
        ],
        "stages": [{"name": "源码修改", "conclusion": "历史呈现源码修改。",
                    "reason": "本控制将全部可见提交划为一个连续区间。",
                    "confidence": 70, "start_sha": commits[0]["sha"],
                    "key_shas": [commits[-1]["sha"]]}],
    }
