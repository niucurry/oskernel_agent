"""仓库文件发现：遍历、语言识别、目录排除、第三方库识别。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .ts import lang_of

# 始终排除的目录名（构建产物、明确的 vendored 依赖目录）
EXCLUDE_DIRS = {
    "target", "build", ".git", "vendor", "third_party", "third-party",
    "node_modules", ".cargo", "deps", "dependencies",
}


@dataclass(frozen=True)
class DiscoveredFile:
    path: Path          # 绝对/可读路径
    rel_path: str       # 相对仓库根（用于 file_path 与归类）
    lang: str           # rust / c / asm


def discover_files(repo: str | Path) -> list[DiscoveredFile]:
    """遍历仓库，返回待处理的源码文件。

    排除：EXCLUDE_DIRS 中的目录名所对应的子树。

    注意：早期版本曾把「含 LICENSE/COPYING 的子目录」整棵当第三方库跳过，但 Rust 工程惯例是
    每个 crate（包括参赛队自己写的）都带 LICENSE，该规则会误杀整个作品源码（实测某作品 480 个
    rust 文件只剩 9 个函数）。故移除该启发式，仅按目录名排除；少量随仓库签入的 vendored crate
    会被纳入，但 SimHash 的 IDF 降权、>5 仓库通用串过滤、基线通道与 LLM common_pattern 判定
    已专门用于消化「广泛共享代码」，宁可多收也不漏检。
    """
    repo = Path(repo).resolve()
    out: list[DiscoveredFile] = []

    for dirpath, dirnames, filenames in os.walk(repo):
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
