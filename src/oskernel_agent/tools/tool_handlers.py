"""
工具的底层实现：
  - read_file                读取文件内容（带行号，自动截断）
  - search_code              正则文本搜索，返回 file:line:content

参考 OS 对比只允许走 ToolDispatcher 的代码指纹链路；这里不再保留函数名集合降级实现。
"""

import fnmatch
import os
import re
from pathlib import Path

# 公共常量

_SKIP_DIRS = frozenset({"vendor", "third_party", "target", ".venv", "__pycache__"})

_BINARY_EXTS = frozenset({
    ".bin", ".img", ".o", ".elf", ".a", ".so", ".wasm",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".pdf",
})

# T1: read_file

_HEAD_LINES = 80    # 无范围截断时保留头部行数
_TAIL_LINES = 40    # 无范围截断时保留尾部行数
_MAX_RANGE_LINES = 200  # 指定范围时的单次最大行数

def read_file(
    repo_path: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """读取文件，返回带行号文本。

    - 二进制后缀：直接拒绝
    - 超大文件（>200KB）且未指定范围：提示先用 find_symbol_definition 定位行号
    - 指定了范围：精确返回，超过 _MAX_RANGE_LINES 行时截断并提示
    - 未指定范围且文件较短：全部返回
    - 未指定范围且文件较长：HEAD + 省略说明 + TAIL
    """
    path_clean = re.sub(r":\d+(-\d+)?$", "", path.strip())
    full_path = Path(repo_path) / path_clean.lstrip("/")

    if not full_path.exists():
        return (
            f"[错误] 文件不存在：{path_clean}\n"
            f"请检查路径是否正确，可以先查看仓库结构地图确认。"
        )

    if full_path.is_dir():
        entries = sorted(full_path.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = [f"[提示] {path_clean} 是目录，不能直接读取。请指定目录下的某个文件："]
        for entry in entries[:60]:
            rel = str(entry.relative_to(Path(repo_path)))
            marker = "/" if entry.is_dir() else ""
            lines.append(f"  {rel}{marker}")
        if len(list(full_path.iterdir())) > 60:
            lines.append("  ...（仅显示前 60 项）")
        return "\n".join(lines)

    # 二进制文件保护
    if full_path.suffix.lower() in _BINARY_EXTS:
        size = full_path.stat().st_size
        return f"[跳过] {path} 是二进制文件（{size} 字节），无法以文本形式读取。"

    # 超大文件且未指定范围
    file_size = full_path.stat().st_size
    if file_size > 200_000 and start_line is None and end_line is None:
        return (
            f"[警告] {path} 文件过大（{file_size // 1024} KB），"
            f"请使用 start_line/end_line 参数指定范围读取。\n"
            f"建议：先调用 find_symbol_definition 定位目标函数的行号范围。"
        )

    try:
        all_lines = full_path.read_text(errors="replace").splitlines()
    except Exception as exc:
        return f"[错误] 读取失败：{exc}"

    total = len(all_lines)

    # 指定了范围
    if start_line is not None or end_line is not None:
        s = max(0, (start_line or 1) - 1)
        e = min(total, end_line if end_line else total)
        selected = all_lines[s:e]

        truncated = len(selected) > _MAX_RANGE_LINES
        if truncated:
            selected = selected[:_MAX_RANGE_LINES]

        body = "\n".join(f"{s + i + 1:5d} | {line}" for i, line in enumerate(selected))
        header = f"## {path}  行 {s + 1}-{s + len(selected)}（共 {total} 行）\n\n"
        footer = (
            f"\n... [已截断：共 {e - s} 行，仅显示前 {_MAX_RANGE_LINES} 行"
            f"（继续读取请用 start_line={s + _MAX_RANGE_LINES + 1}）]"
            if truncated
            else ""
        )
        return header + body + footer

    # 未指定范围：智能截断
    MAX_FULL = _HEAD_LINES + _TAIL_LINES   # 120 行以内直接全返回

    if total <= MAX_FULL:
        body = "\n".join(f"{i + 1:5d} | {line}" for i, line in enumerate(all_lines))
        return f"## {path}（共 {total} 行）\n\n" + body

    omitted = total - _HEAD_LINES - _TAIL_LINES
    head_part = "\n".join(
        f"{i + 1:5d} | {line}" for i, line in enumerate(all_lines[:_HEAD_LINES])
    )
    tail_part = "\n".join(
        f"{total - _TAIL_LINES + i + 1:5d} | {line}"
        for i, line in enumerate(all_lines[-_TAIL_LINES:])
    )
    return (
        f"## {path}（共 {total} 行，已截断）\n\n"
        + head_part
        + f"\n\n  ... 省略 {omitted} 行"
        + f"（使用 start_line/end_line 查看中间部分，如 start_line={_HEAD_LINES + 1}"
        + f", end_line={total - _TAIL_LINES}）...\n\n"
        + tail_part
    )


# search_code: 仓库内文本/正则搜索

_SEARCH_DEFAULT_EXTS = frozenset({
    ".c", ".h", ".cc", ".cpp", ".hpp",
    ".rs",
    ".S", ".s", ".asm",
    ".py", ".sh",
    ".md", ".txt", ".rst",
    ".toml", ".lds", ".ld",
})

_SEARCH_MAX_LINE_LEN = 240   # 单行内容超长时截断
_SEARCH_DEFAULT_MAX  = 50    # 默认返回上限


_REGEX_METACHARS = set(".+*?[]{}|^$()\\")


def _looks_like_regex(pattern: str) -> bool:
    """简单启发：含正则元字符即视为正则模式，否则按关键词走 FTS5。"""
    return any(ch in _REGEX_METACHARS for ch in pattern)


def search_code(
    repo_path: str,
    pattern: str,
    file_glob: str | None = None,
    case_sensitive: bool = False,
    max_results: int = _SEARCH_DEFAULT_MAX,
    symbol_db=None,
) -> str:
    """在仓库内搜索代码。

    - pattern         关键词或 Python 正则表达式
    - file_glob       文件名匹配模式（如 "*.rs"、"trap*"），不带目录时只匹配 basename
    - case_sensitive  默认大小写不敏感（仅对正则路径生效；FTS5 自带 unicode61 大小写归一）
    - max_results     命中数上限，默认 50
    - symbol_db       可选 SymbolDB 实例；存在且 pattern 为纯关键词时走 FTS5

    优先走 SQLite FTS5（毫秒级）；含正则元字符或 FTS5 不可用时降级到逐行正则扫描。
    """
    if not pattern:
        return "[错误] 搜索模式不能为空。"

    if max_results <= 0 or max_results > 500:
        max_results = _SEARCH_DEFAULT_MAX

    # FTS5 快路径：关键词搜索且 symbol_db 可用
    if symbol_db is not None and not _looks_like_regex(pattern):
        try:
            return _search_via_fts(symbol_db, pattern, file_glob, max_results)
        except Exception as exc:
            # FTS 失败不应阻塞工具；降级到正则扫描
            return _search_via_regex(
                repo_path, pattern, file_glob, case_sensitive, max_results,
                fts_error=str(exc),
            )

    return _search_via_regex(repo_path, pattern, file_glob, case_sensitive, max_results)


def _search_via_fts(
    symbol_db,
    pattern: str,
    file_glob: str | None,
    max_results: int,
) -> str:
    """FTS5 全文搜索路径。"""
    # FTS5 短语查询：用双引号包裹，避免特殊词被拆成 OR
    fts_query = f'"{pattern}"' if " " in pattern else pattern
    hits = symbol_db.fts_search(fts_query, file_glob=file_glob, limit=max_results)
    if not hits:
        glob_part = f"，glob={file_glob}" if file_glob else ""
        return f"[未找到] 模式 {pattern!r} 在 FTS 索引中无匹配{glob_part}。"

    lines = []
    for h in hits:
        content = h["content"].rstrip()
        if len(content) > _SEARCH_MAX_LINE_LEN:
            content = content[:_SEARCH_MAX_LINE_LEN] + " …"
        lines.append(f"{h['file']}:{h['line']} | {content}")

    truncated = len(hits) >= max_results
    header = (
        f"搜索 {pattern!r}（FTS5"
        f"{'，glob=' + file_glob if file_glob else ''}）"
        f"命中 {len(hits)} 条"
        f"{'（已达上限，结果被截断）' if truncated else ''}：\n"
    )
    return header + "\n".join(lines)


def _search_via_regex(
    repo_path: str,
    pattern: str,
    file_glob: str | None,
    case_sensitive: bool,
    max_results: int,
    fts_error: str | None = None,
) -> str:
    """正则线性扫描路径（FTS5 不可用或正则模式）。"""
    try:
        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return f"[错误] 正则表达式编译失败：{exc}"

    root = Path(repo_path)
    if not root.exists():
        return f"[错误] 仓库路径不存在：{repo_path}"

    hits: list[str] = []
    scanned_files = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        # 原地剪枝跳过目录
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]

        for name in filenames:
            full = Path(dirpath) / name

            # 扩展名筛选：未给 glob 时按白名单，给了 glob 时只看 glob
            if file_glob:
                if not fnmatch.fnmatch(name, file_glob):
                    continue
            else:
                if full.suffix not in _SEARCH_DEFAULT_EXTS:
                    continue

            if full.suffix.lower() in _BINARY_EXTS:
                continue

            try:
                # 大文件保护：>1MB 跳过（仓库内源码文件通常远小于此）
                if full.stat().st_size > 1_000_000:
                    continue
                text = full.read_text(errors="replace")
            except Exception:
                continue

            scanned_files += 1
            rel_path = str(full.relative_to(root))

            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    content = line.rstrip()
                    if len(content) > _SEARCH_MAX_LINE_LEN:
                        content = content[:_SEARCH_MAX_LINE_LEN] + " …"
                    hits.append(f"{rel_path}:{lineno} | {content}")
                    if len(hits) >= max_results:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    if not hits:
        return (
            f"[未找到] 模式 {pattern!r} 在仓库中无匹配"
            f"（已扫描 {scanned_files} 个文件"
            f"{'，glob=' + file_glob if file_glob else ''}）。"
        )

    fts_note = f"，FTS5 降级：{fts_error}" if fts_error else ""
    header = (
        f"搜索 {pattern!r}（正则扫描，大小写{'敏感' if case_sensitive else '不敏感'}"
        f"{'，glob=' + file_glob if file_glob else ''}{fts_note}）"
        f"命中 {len(hits)} 条"
        f"{'（已达上限，结果被截断）' if truncated else ''}：\n"
    )
    return header + "\n".join(hits)
