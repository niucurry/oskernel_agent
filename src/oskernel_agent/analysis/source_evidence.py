"""Shared exact-quote checks for reviewer evidence and scoped assertions."""

from __future__ import annotations

import re
from pathlib import Path

from .read_coverage import check_requirements, source_functions

EVIDENCE_REF = re.compile(r"^(.+):(\d+)(?:-(\d+))?$")


def check_source_quote(repo: Path, audit: dict | None, item: object) -> tuple[dict | None, str | None]:
    match = EVIDENCE_REF.fullmatch(str(item.get("ref", ""))) if isinstance(item, dict) else None
    if not match or not isinstance(item.get("quote"), str):
        return None, "invalid_evidence"
    raw, low, high = match.groups()
    low, high = int(low), int(high or low)
    try:
        path = (repo / raw).resolve()
        relative = path.relative_to(repo.resolve()).as_posix()
        if not 1 <= low <= high or high - low >= 40 or path.stat().st_size > 10_000_000:
            raise ValueError("invalid_range")
        lines = path.read_text(encoding="utf-8").splitlines()
        if high > len(lines) or "\n".join(lines[low - 1:high]) != item["quote"]:
            raise ValueError("quote_mismatch")
        inventory = source_functions(repo, relative)
        # Explicit references also support config formats without a source suffix.
        base = {"path": relative, "sha256": inventory.get("sha256"), "parser_status": inventory["status"]}
        requirements = [dict(base, start_line=low, end_line=high, symbol="", scope="cited_lines")]
        requirements.extend(dict(base, start_line=f["start_line"], end_line=f["end_line"],
                                 symbol=f["symbol"], scope="function")
                            for f in inventory["functions"] if f["start_line"] <= high and low <= f["end_line"])
        if any(r["status"] != "returned" for r in check_requirements(repo, audit, requirements)):
            raise ValueError("evidence_not_fully_returned")
    except (OSError, ValueError, UnicodeError):
        return None, "unverified_evidence"
    return {"path": relative, "start_line": low, "end_line": high,
            "ref": f"{relative}:{low}-{high}", "quote": item["quote"], "requirements": requirements}, None
