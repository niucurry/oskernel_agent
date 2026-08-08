"""成功生成报告后，安全清理正式目录中的中间产物。"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Iterable


def remove_directory(path: str | Path) -> None:
    """删除一个已明确指定的目录，并处理 Windows 只读 Git 对象。"""
    target = Path(path)
    if not target.exists():
        return

    def remove_readonly(function, value, _exc):
        os.chmod(value, stat.S_IWRITE)
        function(value)

    shutil.rmtree(target, onerror=remove_readonly)


def cleanup_report_directory(
    report_dir: str | Path,
    deliverable_names: Iterable[str],
    *,
    output_root: str | Path,
) -> list[str]:
    """仅允许清理 ``output_root`` 的直接子目录，且四份交付物必须齐全。"""
    directory = Path(report_dir).resolve()
    root = Path(output_root).resolve()
    if directory.parent != root:
        raise ValueError(f"拒绝清理输出根目录之外的路径：{directory}")

    keep = {str(name) for name in deliverable_names}
    if len(keep) != 4 or any(Path(name).name != name for name in keep):
        raise ValueError("交付文件名必须是四个不含目录的文件名")
    missing = sorted(name for name in keep if not (directory / name).is_file())
    if missing:
        raise RuntimeError(f"四份报告尚未齐全，缺少：{'、'.join(missing)}")

    removed: list[str] = []
    for child in directory.iterdir():
        if child.is_file() and child.name in keep:
            continue
        removed.append(child.name)
        if child.is_dir():
            remove_directory(child)
        else:
            child.unlink()
    return removed
