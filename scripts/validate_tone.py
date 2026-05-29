"""
语气分隔校验：扫 tree.json，确认
  - 下层 tree.*.summary 是中性事实，不含评判词
  - 顶层 verdict 含评判词且 dimensions 长度 == 5

退出码：0 = 通过，非 0 = 发现违规
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


# 评判词黑名单：下层 summary 中出现即违规
_JUDGMENT_BLACKLIST = [
    "亮点", "槽点", "优秀", "糟糕", "精巧", "粗糙", "建议改进", "应该重构",
    "代码质量差", "代码质量好", "设计良好", "设计不佳", "值得借鉴", "应予改进",
    "缺陷", "不足之处", "值得点赞", "令人遗憾",
]

# 评判词正面词表：verdict 中必须至少出现一个
_JUDGMENT_REQUIRED_ANY = _JUDGMENT_BLACKLIST + [
    "原创", "继承", "完整", "规整", "清晰", "薄弱", "缺乏",
]


def _check_summary_neutral(text: str, path: str) -> list[str]:
    issues: list[str] = []
    if not text:
        return issues
    for word in _JUDGMENT_BLACKLIST:
        if word in text:
            issues.append(f"  - {path}: summary 含评判词「{word}」")
    return issues


def _walk_tree(node: dict, path: str = "<root>") -> list[str]:
    issues: list[str] = []
    summary = node.get("summary", "")
    issues.extend(_check_summary_neutral(summary, path))
    for child in node.get("children", []) or []:
        cp = child.get("path") or child.get("name") or "?"
        issues.extend(_walk_tree(child, cp))
    return issues


def _check_verdict(verdict: dict) -> list[str]:
    issues: list[str] = []
    if not verdict:
        return ["  - verdict: 缺失"]

    dims = verdict.get("dimensions", [])
    if len(dims) != 5:
        issues.append(f"  - verdict: dimensions 数量 {len(dims)} ≠ 5")

    text = " ".join([
        verdict.get("one_line", ""),
        *(d.get("reason", "") for d in dims),
        *(h.get("quote", "") for h in verdict.get("highlights", [])),
        *(i.get("quote", "") for i in verdict.get("issues", [])),
    ])
    if not any(w in text for w in _JUDGMENT_REQUIRED_ANY):
        issues.append("  - verdict: 全文未检出任何评判词（要求至少 1 个）")
    if not verdict.get("highlights"):
        issues.append("  - verdict: highlights 为空")
    if not verdict.get("issues"):
        issues.append("  - verdict: issues 为空")
    return issues


def validate(tree_json: dict) -> tuple[bool, list[str]]:
    issues = []
    issues.extend(_walk_tree(tree_json.get("tree", {}) or {}))
    issues.extend(_check_verdict(tree_json.get("verdict", {}) or {}))
    return len(issues) == 0, issues


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python validate_tone.py <tree.json>", file=sys.stderr)
        sys.exit(2)
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    ok, issues = validate(data)
    if ok:
        print("[validate_tone] 通过")
        sys.exit(0)
    print("[validate_tone] 发现违规：", file=sys.stderr)
    for i in issues:
        print(i, file=sys.stderr)
    sys.exit(1)
