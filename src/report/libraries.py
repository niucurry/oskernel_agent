"""复用第三方库识别（报告层）。

被各队 vendored 进内核的公开库（lwext4 / smoltcp / fatfs / virtio-drivers …）属多队
合法共用，不应计入「值得关注的借鉴/抄袭」。本模块按**文件路径**把这类库代码识别出来，
供对比报告：① 从图/清单中剔除库复用；② 单列「复用库统计」小节。

识别口径见 config/libraries.yaml 注释。注意真实数据里 file_path 用反斜杠
（``crates\\lwext4_rust\\c\\lwext4\\src\\ext4.c``），匹配前统一归一为正斜杠 + 小写。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_LIBRARIES_PATH = "config/libraries.yaml"

# 内置兜底：config/libraries.yaml 缺失时仍能工作（与该文件保持同步）。
_DEFAULT_LIBRARIES: dict[str, list[str]] = {
    "lwext4": ["lwext4", "lwext4_rust"],
    "smoltcp": ["smoltcp"],
    "fatfs": ["fatfs", "rust-fatfs", "rust_fatfs"],
    "virtio-drivers": ["virtio-drivers", "virtio_drivers", "virtio-drivers-la", "virtio_drivers_la"],
    "buddy_system_allocator": ["buddy_system_allocator"],
    "slab_allocator": ["slab_allocator"],
    "riscv": ["riscv"],
}
_DEFAULT_VENDOR_DIRS = ("vendor", "third_party", "3rdparty", "thirdparty", "extern", "external")
_DEFAULT_CONTEXT_REQUIRED_SEGMENTS = frozenset({"riscv", "fatfs"})
_DEPENDENCY_CONTAINERS = frozenset({"crates", "libs", "deps", "dependencies", "packages"})


@lru_cache(maxsize=4)
def load_library_registry(
    path: str | None = None,
) -> tuple[tuple[tuple[str, str, bool], ...], frozenset[str]]:
    """加载库注册表，返回 (seg_to_name 项, vendor_dirs)。

    seg_to_name 以元组对形式返回（可哈希、可缓存）；调用方用 ``dict(...)`` 取用。
    """
    libraries = _DEFAULT_LIBRARIES
    vendor_dirs = set(_DEFAULT_VENDOR_DIRS)
    p = Path(path or DEFAULT_LIBRARIES_PATH)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        libs = data.get("libraries")
        if libs:
            libraries = {entry["name"]: (entry.get("segments") or [entry["name"]]) for entry in libs}
        if data.get("vendor_dirs"):
            vendor_dirs = set(data["vendor_dirs"])
    seg_to_entry: dict[str, tuple[str, bool]] = {}
    for name, segs in libraries.items():
        required = set(_DEFAULT_CONTEXT_REQUIRED_SEGMENTS)
        if p.exists():
            entry = next(
                (item for item in (data.get("libraries") or []) if item.get("name") == name),
                {},
            )
            required = {str(x).lower() for x in entry.get("context_required_segments", ())}
        for seg in segs:
            low = seg.lower()
            seg_to_entry[low] = (name, low in required)
    return (
        tuple((seg, name, required) for seg, (name, required) in seg_to_entry.items()),
        frozenset(d.lower() for d in vendor_dirs),
    )


def _has_dependency_context(parts: list[str], index: int, vendor_dirs: frozenset[str]) -> bool:
    """歧义目录名是否处于可验证的依赖包布局，而不是 ``src/arch/riscv`` 等业务目录。"""
    if any(part in vendor_dirs or part in _DEPENDENCY_CONTAINERS for part in parts[:index]):
        return True
    # 仓库根目录本身就是 crate：riscv/src/...、fatfs/src/...。
    return index == 0 and index + 1 < len(parts) and parts[index + 1] == "src"


def match_library(file_path: str | None, *, path: str | None = None) -> str | None:
    """文件路径属于某复用库则返回库名，否则 None。

    规则：① 路径位于 vendor_dirs 之一目录下 → 取其下一段为库名（结构兜底）；
         ② 否则任一目录段 == 某库 segments 之一 → 该库（目录段全等，避免子串误伤）。
    """
    if not file_path:
        return None
    seg_items, vendor_dirs = load_library_registry(path)
    parts = file_path.replace("\\", "/").lower().split("/")
    for i, seg in enumerate(parts):
        if seg in vendor_dirs and i + 1 < len(parts):
            return parts[i + 1]
    seg_to_entry = {seg: (name, required) for seg, name, required in seg_items}
    for index, seg in enumerate(parts):
        entry = seg_to_entry.get(seg)
        if entry:
            name, context_required = entry
            if context_required and not _has_dependency_context(parts, index, vendor_dirs):
                continue
            return name
    return None


def library_of_suspect(s: dict, *, path: str | None = None) -> str | None:
    """嫌疑对是否为库复用——以 query（新作品）侧为准：新作品该函数本就是 vendored 库代码，
    不是原创，命中什么都不算值得关注的借鉴。"""
    return match_library((s.get("query_func") or {}).get("file_path"), path=path)


def tag_library_reuse(suspects: list[dict], *, path: str | None = None) -> int:
    """就地给库复用的嫌疑对打 ``reuse_library`` 标签，返回标注数。幂等。"""
    n = 0
    for s in suspects:
        name = library_of_suspect(s, path=path)
        if name:
            s["reuse_library"] = name
            n += 1
        elif "reuse_library" in s:  # 注册表变化后重算时清掉旧标
            del s["reuse_library"]
    return n


def reused_library_stats(suspects: list[dict], recall: dict | None = None,
                         *, path: str | None = None) -> list[dict]:
    """按库聚合复用统计，按新作品中函数数降序。

    每项：{name, func_count（新作品中属于该库的函数数）, pair_count（触发的嫌疑对数）,
           repo_count（candidate 侧也含同库的不同历史仓库数，即「还有多少队也 vendored 了它」）}。
    func_count 优先数 recall（覆盖未触发嫌疑的库函数，更真实），无 recall 时退化为去重的
    query 函数数。
    """
    func_keys: dict[str, set[tuple[str, int]]] = {}
    pair_count: dict[str, int] = {}
    repos: dict[str, set[str]] = {}

    # 函数规模：优先 recall（全量 query 函数）
    if recall:
        for item in recall.get("results", []):
            q = item.get("query") or {}
            name = match_library(q.get("file_path"), path=path)
            if name:
                func_keys.setdefault(name, set()).add((q.get("file_path", ""), q.get("start_line", 0)))

    for s in suspects:
        name = s.get("reuse_library") or library_of_suspect(s, path=path)
        if not name:
            continue
        pair_count[name] = pair_count.get(name, 0) + 1
        if not recall:  # recall 缺失时用 suspects 的 query 函数兜底
            q = s.get("query_func") or {}
            func_keys.setdefault(name, set()).add((q.get("file_path", ""), q.get("start_line", 0)))
        c = s.get("candidate_func") or {}
        if match_library(c.get("file_path"), path=path) == name:
            repos.setdefault(name, set()).add(c.get("repo_id", "?"))

    names = set(func_keys) | set(pair_count)
    out = [
        {
            "name": name,
            "func_count": len(func_keys.get(name, ())),
            "pair_count": pair_count.get(name, 0),
            "repo_count": len(repos.get(name, ())),
        }
        for name in names
    ]
    out.sort(key=lambda x: (-x["func_count"], -x["pair_count"], x["name"]))
    return out
