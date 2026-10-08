"""Replay old model-written text through the renderer; no semantic gold or model calls."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re

from research.description_integrity.replay import ROOT, load_baseline


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.baseline:
        load_baseline()
    from oskernel_agent.reports.html_tree import _render_all_subsystems
    from oskernel_agent.finals.readability import remove_ai_filler

    rows = []
    sources = {}
    for path in sorted((ROOT / "data/behavior-experiments").rglob("summary.md")):
        text = path.read_text(encoding="utf-8")
        if "model_summary_unverified" not in text:
            continue
        # Only model summary bullets before the first level-two heading;
        # the rest are graph nodes, not generated summary statements.
        body = text.split("\n## ", 1)[0]
        relative = str(path.relative_to(ROOT))
        sources[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        for lineno, line in enumerate(body.splitlines(), 1):
            if not line.startswith("- "):
                continue
            raw = line[2:]
            # Old files sometimes put graph references on the same line.
            # Remove only that explicitly marked metadata, preserving the raw line too.
            statement = re.sub(r"（(?:kernel|src)/.*；语义待独立核验）$", "", raw)
            expected = remove_ai_filler(statement)
            if not expected:
                continue
            tree = {"facts": {}, "verdict": {}, "tree": {"children": [{
                "name": "内存管理", "type": "subsystem", "summary": statement,
                "children": [], "file_paths": [],
            }]}}
            rendered = _render_all_subsystems(tree, None)
            rows.append({"source": relative, "line": lineno, "raw": raw,
                         "statement": statement, "chars": len(expected),
                         "whole_text_present_anywhere": html.escape(expected) in rendered,
                         "has_overflow_details": 'data-description-overflow="true"' in rendered,
                         "rendered": rendered})

    summary = {"source_files": len(sources), "statements": len(rows),
               "unique_statements": len({row["statement"] for row in rows}),
               "whole_text_present_anywhere": sum(row["whole_text_present_anywhere"] for row in rows),
               "with_overflow_details": sum(row["has_overflow_details"] for row in rows)}
    result = {"kind": "archived_model_text_adapted_to_report_renderer_not_production_runs",
              "implementation": "baseline" if args.baseline else "working_tree",
              "limitations": ["No source truth judgment", "Old development material, not held out",
                              "One input statement per card; no realistic multi-statement competition",
                              "Exact text presence includes collapsed details; not reading utility"],
              "summary": summary, "source_sha256": sources, "rows": rows}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
