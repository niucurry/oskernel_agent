"""复用第三方库识别（报告层）。

被各队 vendored 进内核的公开库（lwext4 / smoltcp / fatfs / virtio-drivers …）属多队
合法共用，不应计入「值得关注的借鉴/抄袭」。本模块综合路径、包清单与真实导入关系识别
库本体及已登记的适配层，
供对比报告：① 从图/清单中剔除库复用；② 单列「复用库统计」小节。

识别口径见 config/libraries.yaml 注释。注意真实数据里 file_path 用反斜杠
（``crates\\lwext4_rust\\c\\lwext4\\src\\ext4.c``），匹配前统一归一为正斜杠 + 小写。
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
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
_DEFAULT_INTEGRATION_SEGMENTS: dict[str, list[str]] = {
    "lwext4": ["ext4_lw"],
}
_DEFAULT_IMPORT_NAMES: dict[str, list[str]] = {
    "lwext4": ["lwext4_rust"],
}
_DEFAULT_VENDOR_DIRS = ("vendor", "third_party", "3rdparty", "thirdparty", "extern", "external")
_DEFAULT_CONTEXT_REQUIRED_SEGMENTS = frozenset({"riscv", "fatfs"})
_DEPENDENCY_CONTAINERS = frozenset({"crates", "libs", "deps", "dependencies", "packages"})
_SOURCE_SUFFIXES = frozenset({
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".java", ".js", ".jsx",
    ".kt", ".kts", ".py", ".rs", ".swift", ".ts", ".tsx",
})
_IGNORED_DISCOVERY_DIRS = frozenset({
    ".git", ".hg", ".svn", "build", "dist", "node_modules", "target",
})


@dataclass(frozen=True)
class LibraryContext:
    """仓库级第三方组件边界。

    ``roots`` 同时包含由包清单确认的依赖源码根目录，以及经“包存在 + 源码真实导入”
    双重证据确认的适配层目录。路径均为相对仓库根目录的规范化前缀。
    """

    roots: tuple[tuple[str, str], ...] = ()
    integration_roots: tuple[tuple[str, str], ...] = ()

    def match(self, file_path: str | None) -> str | None:
        normalized = _normalize_path(file_path)
        if not normalized:
            return None
        for prefix, library in self.roots:
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return library
        return None


def _normalize_path(file_path: str | None) -> str:
    return "/".join(
        part for part in str(file_path or "").replace("\\", "/").lower().split("/")
        if part and part != "."
    )


def _normalize_identifier(value: str) -> str:
    return re.sub(r"[-.]", "_", value.strip().lower())


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


@lru_cache(maxsize=4)
def load_library_component_registry(
    path: str | None = None,
) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...]:
    """加载库组件元数据：核心路径段、适配目录段和源码导入名。

    ``integration_segments`` 不会仅凭目录名命中；它们必须由
    :func:`discover_library_context` 用仓库内包清单和真实导入语句共同激活。
    """
    p = Path(path or DEFAULT_LIBRARIES_PATH)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {} if p.exists() else {}
    configured = data.get("libraries") or []
    entries = configured or [
        {"name": name, "segments": segments}
        for name, segments in _DEFAULT_LIBRARIES.items()
    ]
    result = []
    for entry in entries:
        name = str(entry["name"])
        segments = tuple(str(x).lower() for x in (entry.get("segments") or [name]))
        integrations = tuple(str(x).lower() for x in (
            entry.get("integration_segments")
            or _DEFAULT_INTEGRATION_SEGMENTS.get(name, ())
        ))
        imports = tuple(_normalize_identifier(str(x)) for x in (
            entry.get("import_names")
            or _DEFAULT_IMPORT_NAMES.get(name)
            or segments
        ))
        result.append((name, segments, integrations, imports))
    return tuple(result)


def _is_ignored_relative(path: Path) -> bool:
    return any(part.lower() in _IGNORED_DISCOVERY_DIRS for part in path.parts)


def _manifest_package_name(manifest: Path) -> str:
    """读取常见包清单中的包名；损坏/未知清单安全地返回空串。"""
    try:
        if manifest.name == "Cargo.toml":
            value = (tomllib.loads(manifest.read_text(encoding="utf-8"))
                     .get("package", {}).get("name", ""))
        elif manifest.name == "pyproject.toml":
            parsed = tomllib.loads(manifest.read_text(encoding="utf-8"))
            value = (parsed.get("project", {}).get("name")
                     or parsed.get("tool", {}).get("poetry", {}).get("name", ""))
        else:
            value = json.loads(manifest.read_text(encoding="utf-8")).get("name", "")
    except (OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError):
        return ""
    return _normalize_identifier(str(value)) if value else ""


def _contains_library_import(directory: Path, import_names: tuple[str, ...]) -> bool:
    if not import_names:
        return False
    names = "|".join(re.escape(name) for name in import_names if name)
    if not names:
        return False
    # 只接受真实导入/包含语句；注释里偶然出现库名不能激活整个适配目录。
    import_re = re.compile(
        rf"(?m)^\s*(?:(?:pub\s+)?use|extern\s+crate|import|from)\s+(?:{names})(?=\b|::)"
        rf"|^\s*#\s*include\s*[<\"](?:{names})(?=[/>\"])",
        re.IGNORECASE,
    )
    inspected = 0
    for source in directory.rglob("*"):
        if not source.is_file() or source.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        try:
            relative = source.relative_to(directory)
        except ValueError:
            continue
        if _is_ignored_relative(relative):
            continue
        try:
            text = source.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        inspected += 1
        if import_re.search(text):
            return True
        # 防止异常大目录拖慢报告；适配层的依赖导入通常位于最前面的少量源文件中。
        if inspected >= 256:
            break
    return False


def discover_library_context(
    repo_root: str | Path | None,
    *,
    path: str | None = None,
) -> LibraryContext:
    """从仓库元数据发现改名依赖包及其适配层，形成可复用的路径上下文。

    适配层采用三项联合证据：注册表显式登记适配目录别名、仓库中存在对应包清单、
    适配目录源码存在真实导入语句。目录名或 ``ext4`` 等关键词本身均不足以分类。
    """
    if not repo_root:
        return LibraryContext()
    root = Path(repo_root).resolve()
    if not root.is_dir():
        return LibraryContext()

    components = load_library_component_registry(path)
    package_to_library: dict[str, str] = {}
    for library, segments, _integrations, imports in components:
        identifiers = {_normalize_identifier(library), *map(_normalize_identifier, segments), *imports}
        for identifier in identifiers:
            if identifier:
                package_to_library[identifier] = library

    integration_map = {
        segment: (library, imports)
        for library, _segments, integrations, imports in components
        for segment in integrations
    }
    manifests: list[Path] = []
    integration_dirs: list[tuple[Path, Path, tuple[str, tuple[str, ...]]]] = []
    for entry in root.rglob("*"):
        try:
            relative = entry.relative_to(root)
        except ValueError:
            continue
        if _is_ignored_relative(relative):
            continue
        if entry.is_file() and entry.name in {"Cargo.toml", "pyproject.toml", "package.json"}:
            manifests.append(entry)
        elif entry.is_dir() and entry.name.lower() in integration_map:
            integration_dirs.append((entry, relative, integration_map[entry.name.lower()]))

    roots: set[tuple[str, str]] = set()
    available: set[str] = set()
    for manifest in manifests:
        library = package_to_library.get(_manifest_package_name(manifest))
        if not library:
            continue
        relative_root = _normalize_path(manifest.parent.relative_to(root).as_posix())
        if relative_root:
            roots.add((relative_root, library))
        available.add(library)

    integration_roots: set[tuple[str, str]] = set()
    if available and integration_map:
        for directory, relative, (library, imports) in integration_dirs:
            if library not in available or not _contains_library_import(directory, imports):
                continue
            normalized = _normalize_path(relative.as_posix())
            integration_roots.add((normalized, library))
            roots.add((normalized, library))

    ordered_roots = tuple(sorted(roots, key=lambda item: (-len(item[0]), item)))
    ordered_integrations = tuple(sorted(integration_roots))
    return LibraryContext(roots=ordered_roots, integration_roots=ordered_integrations)


def _has_dependency_context(parts: list[str], index: int, vendor_dirs: frozenset[str]) -> bool:
    """歧义目录名是否处于可验证的依赖包布局，而不是 ``src/arch/riscv`` 等业务目录。"""
    if any(part in vendor_dirs or part in _DEPENDENCY_CONTAINERS for part in parts[:index]):
        return True
    # 仓库根目录本身就是 crate：riscv/src/...、fatfs/src/...。
    return index == 0 and index + 1 < len(parts) and parts[index + 1] == "src"


def match_library(file_path: str | None, *, path: str | None = None,
                  context: LibraryContext | None = None) -> str | None:
    """文件路径属于某复用库则返回库名，否则 None。

    规则：① 路径位于 vendor_dirs 之一目录下 → 取其下一段为库名（结构兜底）；
         ② 否则任一目录段 == 某库 segments 之一 → 该库（目录段全等，避免子串误伤）。
    """
    if not file_path:
        return None
    if context:
        contextual = context.match(file_path)
        if contextual:
            return contextual
    seg_items, vendor_dirs = load_library_registry(path)
    parts = _normalize_path(file_path).split("/")
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


def library_of_suspect(s: dict, *, path: str | None = None,
                       context: LibraryContext | None = None) -> str | None:
    """嫌疑对是否为库复用——以 query（新作品）侧为准：新作品该函数本就是 vendored 库代码，
    不是原创，命中什么都不算值得关注的借鉴。"""
    return match_library(
        (s.get("query_func") or {}).get("file_path"), path=path, context=context)


def tag_library_reuse(suspects: list[dict], *, path: str | None = None,
                      context: LibraryContext | None = None) -> int:
    """就地给库复用的嫌疑对打 ``reuse_library`` 标签，返回标注数。幂等。"""
    n = 0
    for s in suspects:
        name = library_of_suspect(s, path=path, context=context)
        if name:
            s["reuse_library"] = name
            n += 1
        elif "reuse_library" in s:  # 注册表变化后重算时清掉旧标
            del s["reuse_library"]
    return n


def reused_library_stats(suspects: list[dict], recall: dict | None = None,
                         *, path: str | None = None,
                         context: LibraryContext | None = None) -> list[dict]:
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
            name = match_library(q.get("file_path"), path=path, context=context)
            if name:
                func_keys.setdefault(name, set()).add((q.get("file_path", ""), q.get("start_line", 0)))

    for s in suspects:
        name = s.get("reuse_library") or library_of_suspect(
            s, path=path, context=context)
        if not name:
            continue
        pair_count[name] = pair_count.get(name, 0) + 1
        if not recall:  # recall 缺失时用 suspects 的 query 函数兜底
            q = s.get("query_func") or {}
            func_keys.setdefault(name, set()).add((q.get("file_path", ""), q.get("start_line", 0)))
        c = s.get("candidate_func") or {}
        if match_library(c.get("file_path"), path=path, context=context) == name:
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
