"""基于 config/module_rules.yaml 的函数所属子系统归类。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from oskernel_agent.paths import PROJECT_ROOT

import yaml

from oskernel_agent.comparison.models import ModuleTag

DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "module_rules.yaml"


@dataclass(frozen=True)
class _Rule:
    tag: str
    path_keywords: tuple[str, ...]
    symbol_keywords: tuple[str, ...]
    code_keywords: tuple[str, ...]


def _identifier_terms(text: str) -> frozenset[str]:
    """一次提取完整标识符及 snake/camel 子词，供所有规则复用。"""
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(text or ""))
    identifiers = re.findall(r"[A-Za-z][A-Za-z0-9_]*", expanded.lower())
    terms = set(identifiers)
    for identifier in identifiers:
        terms.update(part for part in identifier.split("_") if part)
    return frozenset(terms)


def _keyword_hits(terms: frozenset[str], keywords: tuple[str, ...]) -> int:
    """按标识符边界匹配关键词，避免 ``ext`` 误命中 ``context`` 等子串。"""
    return sum(1 for keyword in keywords if keyword in terms)


@dataclass(frozen=True)
class ModuleClassifier:
    rules: tuple[_Rule, ...]
    default: str

    def classify(
        self,
        file_path: str | Path,
        lang: str,
        *,
        is_macro: bool = False,
        func_name: str = "",
        raw_code: str = "",
    ) -> ModuleTag:
        """按路径、函数名与代码标识符综合归类。

        函数名比路径更能表达 ``lib.rs`` 等聚合文件内单个函数的职责；代码特征只作为
        辅助证据。三者均按标识符边界匹配，不使用容易把 ``context`` 判成 ``ext`` 的
        任意子串规则。
        """
        if is_macro:
            return ModuleTag.MACRO
        if lang == "asm":
            return ModuleTag.ARCH
        path_terms = _identifier_terms(str(file_path).replace("\\", "/"))
        symbol_terms = _identifier_terms(func_name)
        code_terms = _identifier_terms(raw_code)
        ranked: list[tuple[int, int, str]] = []
        for order, rule in enumerate(self.rules):
            path_hits = _keyword_hits(path_terms, rule.path_keywords)
            symbol_hits = _keyword_hits(symbol_terms, rule.symbol_keywords)
            code_hits = _keyword_hits(code_terms, rule.code_keywords)
            if not (path_hits or symbol_hits or code_hits):
                continue
            # 函数名权重大于单一路径词；代码锚点每项低权重且总分封顶，单一 API 名
            # 不能压过明确路径。
            score = min(11, path_hits * 7) + min(13, symbol_hits * 10) + min(8, code_hits * 2)
            ranked.append((score, -order, rule.tag))
        if ranked:
            return ModuleTag(max(ranked)[2])
        return ModuleTag(self.default)


@lru_cache(maxsize=8)
def load_classifier(path: str | None = None) -> ModuleClassifier:
    p = Path(path or DEFAULT_RULES_PATH)
    if not p.exists():
        raise FileNotFoundError(f"归类规则文件不存在：{p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    rules = []
    for rule in data.get("rules", []):
        # 兼容旧配置：keywords 等价于 path_keywords。
        legacy = rule.get("keywords", [])
        rules.append(_Rule(
            tag=rule["tag"],
            path_keywords=tuple(
                str(k).lower() for k in rule.get("path_keywords", legacy)),
            symbol_keywords=tuple(
                str(k).lower() for k in rule.get("symbol_keywords", [])),
            code_keywords=tuple(
                str(k).lower() for k in rule.get("code_keywords", [])),
        ))
    return ModuleClassifier(rules=tuple(rules), default=data.get("default", "other"))
