# -*- coding: utf-8 -*-
"""列出描述报告中仍含英文整句的作品目录名（每行一个），-v 详细模式。"""
import re
import sys
from pathlib import Path

ROOT = Path(r"d:\agent\project3136859-379280")
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # fix_workid_reports 同在 scripts/
import fix_workid_reports as F  # noqa: E402

SENT = re.compile(r"\b[A-Za-z][a-z]+(?:\s+(?:[A-Za-z][a-z']*|a|an|the|of|to|in|is|are|and|or|for|with)){7,}")
verbose = "-v" in sys.argv

hits = {}
for d in sorted((ROOT / "reports_by_work_id").iterdir()):
    if not d.is_dir():
        continue
    desc = d / "description.html"
    if not desc.exists():
        continue
    h = desc.read_text(encoding="utf-8")
    blocks = F.balanced_div_blocks(h, "node-content") + F.balanced_div_blocks(h, "verdict-content")
    n = 0
    for start, end, inner in blocks:
        for c in F.group_chunks(F.top_level_segments(inner)):
            t = re.sub(r"<code\b[^>]*>.*?</code>", " ", c, flags=re.S)
            t = re.sub(r"<[^>]+>", " ", t)
            t = re.sub(r"https?://\S+|[\w./\\-]+\.[A-Za-z0-9]+(?::\d+(?:-\d+)?)?", " ", t)
            t = re.sub(r"[\w-]+/[\w./-]+", " ", t)
            m = SENT.search(t)
            if m:
                n += 1
                if verbose:
                    print(f"# {d.name} len={len(c)} | {re.sub(chr(92)+'s+', ' ', m.group(0))[:100]}",
                          file=sys.stderr)
    if n:
        hits[d.name] = n

for name in hits:
    print(name)
print(f"# files={len(hits)} chunks={sum(hits.values())}", file=sys.stderr)
