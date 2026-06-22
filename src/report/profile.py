"""历史作品档案批量生成：每个历史仓库一份 Markdown 档案（架构/模块/特色）。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from loguru import logger

from src.normalize.store import DEFAULT_DB

from . import prompts as P
from .postcheck import add_allowed, scrub

DEFAULT_PROFILE_DIR = "data/db/profiles"
DEFAULT_REPOS_ROOT = "data/repos"
_REP_MODULES = ["sched", "mm", "fs", "trap", "driver", "arch"]


def safe_name(repo_id: str) -> str:
    return repo_id.replace("/", "_")


def module_distribution(conn: sqlite3.Connection, repo_id: str) -> dict[str, int]:
    cur = conn.execute(
        "SELECT module_tag, COUNT(*) FROM functions WHERE repo_id=? GROUP BY module_tag", (repo_id,)
    )
    return dict(cur.fetchall())


def representative_functions(conn: sqlite3.Connection, repo_id: str) -> list[dict]:
    """每个模块取行数最多的一个函数作为代表。"""
    reps = []
    for mod in _REP_MODULES:
        row = conn.execute(
            "SELECT file_path, start_line, end_line, func_name, module_tag, raw_code "
            "FROM functions WHERE repo_id=? AND module_tag=? "
            "ORDER BY (end_line - start_line) DESC LIMIT 1",
            (repo_id, mod),
        ).fetchone()
        if row:
            reps.append({
                "module_tag": row[4], "func_name": row[3],
                "ref": f"{row[0]}:{row[1]}-{row[2]}", "raw_code": row[5],
            })
    return reps


def _read_readme(repos_root: str | Path, repo_id: str) -> str:
    for name in ("README.md", "readme.md", "README"):
        p = Path(repos_root) / repo_id / name
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    return ""


async def _gen_profile_async(repo_id, dist, reps, readme, client) -> tuple[str, int]:
    allowed: dict = {}
    for r in reps:
        add_allowed(allowed, r["ref"])
    if client is None:
        body = "\n".join(f"- {r['module_tag']}：代表函数 {r['func_name']} ({r['ref']})" for r in reps)
        return f"# 作品档案 {repo_id}\n\n模块分布：{dist}\n\n{body}\n", 0
    text = await client.complete(
        [{"role": "system", "content": P.PROFILE_SYSTEM},
         {"role": "user", "content": P.profile_user(repo_id, dist, reps, readme)}], 0.2,
    )
    text, deleted = scrub(text, allowed)
    return f"# 作品档案 {repo_id}\n\n{text}\n", deleted


def generate_one(repo_id, db_path, repos_root, client) -> tuple[str, int]:
    conn = sqlite3.connect(db_path)
    dist = module_distribution(conn, repo_id)
    reps = representative_functions(conn, repo_id)
    conn.close()
    readme = _read_readme(repos_root, repo_id)
    return asyncio.run(_gen_profile_async(repo_id, dist, reps, readme, client))


def run_profiles(
    db_path: str | Path = DEFAULT_DB,
    *,
    repos_root: str | Path = DEFAULT_REPOS_ROOT,
    profile_dir: str | Path = DEFAULT_PROFILE_DIR,
    client=None,
    only: list[str] | None = None,
) -> dict:
    conn = sqlite3.connect(db_path)
    repo_ids = [r[0] for r in conn.execute("SELECT DISTINCT repo_id FROM functions").fetchall()]
    conn.close()
    if only:
        repo_ids = [r for r in repo_ids if r in only]

    out_dir = Path(profile_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    total_deleted = 0
    for repo_id in repo_ids:
        md, deleted = generate_one(repo_id, db_path, repos_root, client)
        (out_dir / f"{safe_name(repo_id)}.md").write_text(md, encoding="utf-8")
        total_deleted += deleted
        logger.info("档案 {} 写入（删除 {} 条）", repo_id, deleted)
    return {"profiles": len(repo_ids), "deleted": total_deleted, "dir": str(out_dir)}


def load_profile_summary(repo_id: str, *, profile_dir: str | Path = DEFAULT_PROFILE_DIR, limit: int = 300) -> str:
    """读取候选方档案摘要前 limit 字（供 review 卡片）。无则返回空串。"""
    p = Path(profile_dir) / f"{safe_name(repo_id)}.md"
    if not p.is_file():
        return ""
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    return text[:limit]
