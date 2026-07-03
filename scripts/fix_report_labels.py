# -*- coding: utf-8 -*-
"""统一已生成对比报告里的档位标签，使报告不再叫法混乱。

把每个 data/output/<队伍编号>/<队伍编号>_comparison.html 里散落的旧叫法
（已确认借鉴 / needReview / 疑似借鉴·待人工判定 / 待复核 / 待人工判定 / 弱相似 …）
统一为两档 + 原创：
    confirmed → 高度疑似借鉴
    review    → 疑似借鉴（待复核）
    original  → 自研/原创

用规则见 src/report/label_normalize.py（与新报告写盘时用的是同一套，保证新旧一致）。
幂等：重复运行不会二次改写。默认改动前留一个 .bak 备份（--no-backup 关闭）。

用法：
    python scripts/fix_report_labels.py                       # 处理全部对比报告
    python scripts/fix_report_labels.py T2026100019911468 ... # 指定队伍
    python scripts/fix_report_labels.py --no-backup
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from report.label_normalize import normalize_labels, residual_legacy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "output"


def fix_one(path: Path, backup: bool = True) -> str:
    team = path.parent.name
    html = path.read_text(encoding="utf-8")
    new = normalize_labels(html)
    if new == html:
        resid = residual_legacy(new)
        return f"skip  {team}（已统一{'；残留:'+str(resid) if resid else ''}）"
    if backup:
        bak_dir = OUT / "_batch" / "label_bak"
        bak_dir.mkdir(parents=True, exist_ok=True)
        bak = bak_dir / f"{team}_comparison.html.bak"
        if not bak.exists():
            bak.write_text(html, encoding="utf-8")
    path.write_text(new, encoding="utf-8")
    resid = residual_legacy(new)
    warn = f"  ⚠残留:{resid}" if resid else ""
    return f"OK    {team}: 已统一档位标签{warn}"


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--no-backup"]
    backup = "--no-backup" not in sys.argv[1:]
    files = sorted(glob.glob(str(OUT / "T*" / "T*_comparison.html")))
    if args:
        files = [f for f in files if Path(f).parent.name in args]
    print(f"待处理对比报告：{len(files)} 份（backup={backup}）", flush=True)
    for f in files:
        print(fix_one(Path(f), backup=backup), flush=True)


if __name__ == "__main__":
    main()
