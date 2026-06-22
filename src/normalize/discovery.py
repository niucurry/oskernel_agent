"""仓库文件发现：遍历、语言识别、目录排除、第三方库识别。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .ts import lang_of

# 始终排除的目录名
EXCLUDE_DIRS = {"target", "build", ".git", "vendor", "third_party", "node_modules"}

# 判定为「第三方库目录」的许可证文件名（去扩展名后的 stem，大写比较）
_LICENSE_STEMS = {"LICENSE", "LICENCE", "COPYING"}


@dataclass(frozen=True)
class DiscoveredFile:
    path: Path          # 绝对/可读路径
    rel_path: str       # 相对仓库根（用于 file_path 与归类）
    lang: str           # rust / c / asm


def _has_license(dirpath: str, filenames: list[str]) -> bool:
    for fn in filenames:
        stem = Path(fn).stem.upper()
        if stem in _LICENSE_STEMS or Path(fn).name.upper() in _LICENSE_STEMS:
            return True
    return False


def discover_files(repo: str | Path) -> list[DiscoveredFile]:
    """遍历仓库，返回待处理的源码文件。

    排除：EXCLUDE_DIRS 中的目录；以及任何（非仓库根）含 LICENSE/COPYING 的子目录
    （视为第三方库，整棵子树跳过）。行数过大的文件由调用方在读取时跳过。
    """
    repo = Path(repo).resolve()
    out: list[DiscoveredFile] = []

    for dirpath, dirnames, filenames in os.walk(repo):
        # 第三方库子目录：含 LICENSE 且不是仓库根 → 整棵子树跳过
        if Path(dirpath) != repo and _has_license(dirpath, filenames):
            dirnames[:] = []
            continue
        # 排除指定目录（原地修改 dirnames 以阻止 os.walk 下降）
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)

        for fn in sorted(filenames):
            lang = lang_of(fn)
            if lang is None:
                continue
            p = Path(dirpath) / fn
            out.append(
                DiscoveredFile(
                    path=p,
                    rel_path=str(p.relative_to(repo)),
                    lang=lang,
                )
            )
    return out
