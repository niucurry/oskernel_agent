"""基于 config/module_rules.yaml 的函数所属子系统归类。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from src.models import ModuleTag

DEFAULT_RULES_PATH = "config/module_rules.yaml"


@dataclass(frozen=True)
class _Rule:
    tag: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class ModuleClassifier:
    rules: tuple[_Rule, ...]
    default: str

    def classify(self, file_path: str | Path, lang: str, *, is_macro: bool = False) -> ModuleTag:
        """按路径关键词归类。

        优先级：宏定义 > 汇编 > 路径关键词 > default。
        """
        if is_macro:
            return ModuleTag.MACRO
        if lang == "asm":
            return ModuleTag.ARCH
        low = str(file_path).replace("\\", "/").lower()
        for rule in self.rules:
            if any(kw in low for kw in rule.keywords):
                return ModuleTag(rule.tag)
        return ModuleTag(self.default)


@lru_cache(maxsize=8)
def load_classifier(path: str | None = None) -> ModuleClassifier:
    p = Path(path or DEFAULT_RULES_PATH)
    if not p.exists():
        raise FileNotFoundError(f"归类规则文件不存在：{p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    rules = tuple(
        _Rule(tag=r["tag"], keywords=tuple(k.lower() for k in r["keywords"]))
        for r in data.get("rules", [])
    )
    return ModuleClassifier(rules=rules, default=data.get("default", "other"))
