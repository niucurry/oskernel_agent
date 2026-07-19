"""历史库覆盖率审计：配置中的每个作品都必须真实进入 functions.db。"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.ingest.config import RepoEntry, load_repos


def db_mapping_signature(db_path: str | Path) -> dict:
    """计算 func_id→代码映射签名，供所有派生索引做代际一致性校验。"""
    conn = sqlite3.connect(db_path)
    h = hashlib.sha256()
    count = 0
    for row in conn.execute(
        "SELECT id, repo_id, file_path, start_line, end_line, func_name, normalized_hash "
        "FROM functions ORDER BY id"
    ):
        h.update("\x1f".join(str(v) for v in row).encode("utf-8", "replace"))
        h.update(b"\n")
        count += 1
    conn.close()
    return {"function_count": count, "mapping_sha256": h.hexdigest()}


@dataclass(frozen=True)
class CoverageAudit:
    configured: int
    covered: int
    function_count: int
    missing_repo_ids: tuple[str, ...]
    counts: dict[str, int]

    @property
    def complete(self) -> bool:
        return not self.missing_repo_ids and self.covered == self.configured

    def as_dict(self) -> dict:
        return {
            "complete": self.complete,
            "configured": self.configured,
            "covered": self.covered,
            "function_count": self.function_count,
            "missing_repo_ids": list(self.missing_repo_ids),
            "counts": self.counts,
        }


def audit_entries(db_path: str | Path, entries: list[RepoEntry]) -> CoverageAudit:
    """核验每个配置仓库至少有一个函数；数据库/表缺失时全部判缺失。"""
    expected = list(dict.fromkeys(e.repo_id for e in entries))
    counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(db_path)
        counts = {str(repo): int(n) for repo, n in conn.execute(
            "SELECT repo_id, COUNT(*) FROM functions GROUP BY repo_id"
        )}
        conn.close()
    except (sqlite3.Error, OSError):
        counts = {}
    selected = {repo_id: counts.get(repo_id, 0) for repo_id in expected}
    missing = tuple(repo_id for repo_id, n in selected.items() if n <= 0)
    return CoverageAudit(
        configured=len(expected),
        covered=len(expected) - len(missing),
        function_count=sum(selected.values()),
        missing_repo_ids=missing,
        counts=selected,
    )


def audit_config(db_path: str | Path, config_path: str | Path) -> CoverageAudit:
    return audit_entries(db_path, load_repos(config_path))


def write_manifest(audit: CoverageAudit, path: str | Path) -> Path:
    """写入机器可读覆盖清单，供部署和回归留痕。"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = audit.as_dict()
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
