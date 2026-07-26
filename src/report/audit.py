"""交付报告与历史库完整性审计，供 ``python -m src.report audit`` 使用。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from src.buildlib.coverage import audit_config
from src.retrieval_contract import CONTRACT_VERSION

from .label_normalize import residual_legacy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORTS_ROOT = PROJECT_ROOT / "reports_by_work_id"
DEFAULT_AUDIT_OUTPUT = PROJECT_ROOT / "data/output/recall_completeness_audit.json"


def audit_reports(reports_root: str | Path) -> dict:
    root = Path(reports_root)
    # 同时覆盖批量归档名 ``comparison.html`` 与流水线正式产物 ``<repo>_comparison.html``；
    # reports_root 既可指向输出根，也可直接指向某个作品目录。
    files = sorted({
        *root.glob("*/comparison.html"),
        *root.glob("*/*_comparison.html"),
        *root.glob("comparison.html"),
        *root.glob("*_comparison.html"),
    })
    complete: list[str] = []
    stale: list[str] = []
    unmarked: list[str] = []
    legacy: dict[str, list[str]] = {}
    forbidden_claims: dict[str, int] = {}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(root).as_posix()
        if (f'data-retrieval-contract-version="{CONTRACT_VERSION}"' in text
                and 'data-retrieval-complete="true"' in text):
            complete.append(rel)
        elif 'data-retrieval-complete="false"' in text:
            stale.append(rel)
        else:
            unmarked.append(rel)
        terms = residual_legacy(text)
        if terms:
            legacy[rel] = terms
        # 允许“不等于原创认定”等边界说明，禁止把函数清单/比例直接定名为原创。
        n = len(re.findall(
            r"(?:自研/原创（函数）|原创代码清单|>原创代码<|/ 原创 \d|"
            r"原创度高|原创性良好|作品自研部分)",
            text,
        ))
        if n:
            forbidden_claims[rel] = n
    return {
        "comparison_reports": len(files),
        "complete_reports": len(complete),
        "stale_reports": len(stale),
        "unmarked_reports": unmarked,
        "legacy_label_reports": legacy,
        "forbidden_original_claims": forbidden_claims,
        "stale_report_paths": stale,
    }


def run_audit(
    *,
    db_path: str | Path,
    config_path: str | Path,
    reports_root: str | Path = DEFAULT_REPORTS_ROOT,
    output_path: str | Path = DEFAULT_AUDIT_OUTPUT,
) -> dict:
    coverage = audit_config(db_path, config_path)
    reports = audit_reports(reports_root)
    valid = (
        coverage.complete
        and reports["comparison_reports"] > 0
        and reports["complete_reports"] == reports["comparison_reports"]
        and not reports["unmarked_reports"]
        and not reports["legacy_label_reports"]
        and not reports["forbidden_original_claims"]
    )
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "valid": valid,
        "history_coverage": coverage.as_dict(),
        "reports": reports,
    }
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["_output_path"] = str(out)
    return payload
