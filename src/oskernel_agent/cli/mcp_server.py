"""
MCP stdio server — 向 OpenCode 暴露 OS 内核分析工具集。

由 OpenCode 在启动时作为子进程启动。工具调用由 OpenCode 信号驱动，
本进程负责执行并返回结果。

"""

import argparse
import asyncio
import json
import os
import re
import sys
import threading
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from .. import config as _config
from ..tools.mcp_tools import OSKernelMCPTools
from ..tools.reference_db import ReferenceOSDatabase

# CLI 参数解析

_ap = argparse.ArgumentParser(add_help=False)
_ap.add_argument("--max-steps", type=int, default=20)
_args, _ = _ap.parse_known_args()

_MAX_STEPS = _args.max_steps

# 分析上下文

_contexts: dict[str, OSKernelMCPTools] = {}

# 会话级保护状态

_MAX_CONSECUTIVE_DUPS      = 5
_MAX_SUFFIX_HALLUCINATIONS = 3

_step_count:             int        = 0
_init_repeat_count:      int        = 0
_failed_symbols:         set[str]   = set()
_queried_cache:          set[tuple] = set()
_queried_results:        dict       = {}
_consecutive_dup_count:  int        = 0
_hallucination_count:    int        = 0
_stop_next:              bool       = False

# MCP 服务器

def _log(msg: str) -> None:
    print(f"[MCP] {msg}", file=sys.stderr, flush=True)


_QUICK_TOOL_NAMES = {
    "read_file",
    "search_code",
    "list_implemented_syscalls",
    "analyze_subtree",
    "get_index_status",
}


def _normalize_tool_arguments(name: str, arguments: dict) -> dict:
    args = dict(arguments or {})
    if name == "read_file" and "path" not in args:
        alias = args.get("file_path") or args.get("filepath")
        if alias:
            args["path"] = alias
    if name == "analyze_subtree" and "subtree_path" not in args:
        args["subtree_path"] = args.get("path", "")
    return args


def _quick_source_files(repo_path: Path, subtree_path: str = "") -> list[Path]:
    source_exts = {".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".S", ".s", ".asm"}
    skip_dirs = {"target", ".git", ".venv", "__pycache__", "node_modules"}
    base = repo_path / subtree_path.strip().lstrip("/\\")
    if base.is_file():
        return [base] if base.suffix in source_exts else []
    if not base.exists():
        return []
    files: list[Path] = []
    for p in base.rglob("*"):
        if not p.is_file() or p.suffix not in source_exts:
            continue
        if any(part in skip_dirs for part in p.relative_to(repo_path).parts):
            continue
        files.append(p)
    return files


_QUICK_SYMBOL_RE = re.compile(
    r"\b(?:pub\s+)?(?:unsafe\s+)?(?:extern\s+\"C\"\s+)?fn\s+([A-Za-z_][\w]*)"
    r"|^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_][\w]*)",
    re.MULTILINE,
)


def _quick_analyze_subtree(repo_path: Path, subtree_path: str = "") -> str:
    files = _quick_source_files(repo_path, subtree_path)
    if not files:
        return f"[快速模式] 子树 {subtree_path or '<root>'} 下未找到源文件。"

    lines = [
        f"## 子树分析（快速模式）：{subtree_path or '<root>'}",
        f"文件数：{len(files)}",
        "",
        "### 文件列表",
    ]
    symbol_total = 0
    shown_symbols: list[str] = []
    for p in files[:80]:
        rel = p.relative_to(repo_path).as_posix()
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        syms = []
        for m in _QUICK_SYMBOL_RE.finditer(text):
            name = m.group(1) or m.group(2)
            if not name:
                continue
            line = text[:m.start()].count("\n") + 1
            syms.append(f"`{name}` ({rel}:{line})")
        symbol_total += len(syms)
        lines.append(f"- {rel}（{len(syms)} 个快速符号）")
        shown_symbols.extend(syms[:8])

    if len(files) > 80:
        lines.append(f"  ... 还有 {len(files) - 80} 个文件")
    lines.extend(["", "### 符号样例", f"快速符号总数：{symbol_total}"])
    lines.extend(f"- {s}" for s in shown_symbols[:120])
    lines.append("\n[提示] 当前为快速模式，跳过调用图；可继续用 read_file 查看关键文件。")
    return "\n".join(lines)


def _quick_list_syscalls(repo_path: Path) -> str:
    from ..tools.tool_dispatcher import _STANDARD_SYSCALLS, _SYSCALL_CATEGORIES

    func_re = re.compile(r"\bfn\s+(?:sys_|syscall_)([A-Za-z0-9_]+)")
    const_re = re.compile(r"\b(?:SYSCALL_|SYS_|NR_)([A-Z0-9_]+)\b")
    found: dict[str, tuple[str, int, str]] = {}
    for p in _quick_source_files(repo_path):
        rel = p.relative_to(repo_path).as_posix()
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            for m in func_re.finditer(line):
                found.setdefault(m.group(1).lower(), (rel, lineno, "函数定义"))
            for m in const_re.finditer(line):
                found.setdefault(m.group(1).lower(), (rel, lineno, "分发表常量"))

    standard_found = sorted(k for k in found if k in _STANDARD_SYSCALLS)
    lines = [
        "## Syscall 实现扫描（快速模式）",
        f"标准 syscall 覆盖：{len(standard_found)}/{len(_STANDARD_SYSCALLS)}",
        "",
        "### 按类别",
    ]
    for cat, names in _SYSCALL_CATEGORIES.items():
        hit = sorted(n for n in names if n in found)
        if hit:
            lines.append(f"- {cat}: {len(hit)}/{len(names)} — {', '.join(hit)}")
    lines.extend(["", "### 证据样例"])
    for name in standard_found[:80]:
        rel, line, kind = found[name]
        lines.append(f"- {name}: {rel}:{line}（{kind}）")
    return "\n".join(lines)


def _execute_quick_tool(name: str, arguments: dict, repo_path_str: str) -> str | None:
    if name not in _QUICK_TOOL_NAMES:
        return None

    args = _normalize_tool_arguments(name, arguments)
    repo_path = Path(repo_path_str).resolve()
    if not repo_path.exists():
        return f"[错误] 仓库路径不存在：{repo_path}"

    if name == "read_file":
        from ..tools.tool_handlers import read_file
        return read_file(
            str(repo_path),
            args.get("path", ""),
            args.get("start_line"),
            args.get("end_line"),
        )
    if name == "search_code":
        from ..tools.tool_handlers import search_code
        return search_code(
            str(repo_path),
            args.get("pattern", ""),
            args.get("file_glob"),
            bool(args.get("case_sensitive", False)),
            int(args.get("max_results", 50) or 50),
        )
    if name == "list_implemented_syscalls":
        return _quick_list_syscalls(repo_path)
    if name == "analyze_subtree":
        return _quick_analyze_subtree(repo_path, args.get("subtree_path", "") or "")
    if name == "get_index_status":
        return "[快速模式] 静态索引尚未初始化；read_file/search_code/analyze_subtree 可直接使用。"
    return None


# 引擎懒加载代理：rust-analyzer/clangd 启动可能耗时数十秒，会让 initialize_analysis
# 超出 MCP 客户端的请求超时。这里把引擎初始化放到后台线程，让 initialize_analysis
# 立即返回。需要引擎的工具调用（如 find_symbol_definition）首次访问时阻塞等待，
# 不需要引擎的工具（read_file/search_code/list_implemented_syscalls）则不受影响。

# 单次工具调用最多等待引擎多少秒。设置低于 OpenCode 默认 MCP 请求超时（约 60s），
# 超时后返回友好提示而不是让 OpenCode 在 -32001 上失败。
_LAZY_ENGINE_TIMEOUT = 50.0


class _LazyEngine:
    """后台线程中初始化的引擎代理。

    - get_engine_info(): 引擎就绪前返回预测信息，就绪后透传到真实引擎
    - is_ready(): 非阻塞探测
    - 其他属性访问（go_to_definition、find_references 等）：阻塞至就绪后透传
    - 初始化失败的异常会在首次属性访问时抛出
    """

    def __init__(self, repo_path: str, profile: dict, level2_index,
                 predicted_info: dict):
        self._args = (repo_path, profile, level2_index)
        self._predicted_info = predicted_info
        self._engine = None
        self._error: Exception | None = None
        self._done = threading.Event()
        threading.Thread(target=self._init, daemon=True).start()

    def _init(self) -> None:
        try:
            from .agent import select_engine
            self._engine = select_engine(*self._args)
            info = self._engine.get_engine_info()
            _log(f"[lazy-engine] 就绪：{info.get('engine')}（精度 {info.get('precision')}）")
        except Exception as exc:
            self._error = exc
            _log(f"[lazy-engine] 初始化失败：{exc}")
        finally:
            self._done.set()

    def is_ready(self) -> bool:
        return self._done.is_set() and self._engine is not None

    def get_engine_info(self) -> dict:
        if self._engine is not None:
            return self._engine.get_engine_info()
        return self._predicted_info

    def _wait(self):
        if not self._done.wait(timeout=_LAZY_ENGINE_TIMEOUT):
            raise TimeoutError(
                f"语义引擎仍在初始化（已等待 {_LAZY_ENGINE_TIMEOUT:.0f} 秒，"
                "大型 Rust 工作区 rust-analyzer 索引通常需要 60–120 秒）。"
                "建议先用 read_file/search_code/list_implemented_syscalls 继续"
                "阶段一文档扫读与阶段二事实收集，几次工具调用后再回到此符号查询。"
            )
        if self._error is not None:
            raise RuntimeError(f"语义引擎初始化失败：{self._error}")
        return self._engine

    def __getattr__(self, name: str):
        # 仅在实例/类上找不到该属性时触发；返回真实引擎的属性
        return getattr(self._wait(), name)


def _predict_engine_info(profile: dict) -> dict:
    """根据 profile 预测最终选用的引擎，用于 initialize_analysis 立即返回的 Layer 2。

    引擎类型由 select_engine 中的降级链决定：rust → A，c → B，其他 → C。
    预测可能与最终结果不一致（如 rust-analyzer 启动失败会降级到 C），但不影响
    LLM 进入工作流。最终 engine_info 会在引擎就绪后通过 get_engine_info() 自动更新。
    """
    primary_lang = profile.get("primary_lang", "")
    has_cargo    = bool(profile.get("has_cargo"))

    if primary_lang == "rust" or has_cargo:
        return {
            "engine": "rust-analyzer（后台启动中）",
            "precision": "high",
            "capabilities": [
                "跨文件符号定义查找（含 trait/泛型解析）",
                "精确引用追踪",
                "调用链展开",
            ],
            "limitations": [
                "首次符号查询可能阻塞数十秒等待引擎就绪",
                "可优先使用 read_file/search_code/list_implemented_syscalls 等",
                "不依赖语义引擎的工具开展阶段一文档扫读与阶段二事实收集",
            ],
        }
    if primary_lang == "c":
        return {
            "engine": "clangd 或 tree-sitter（后台选择中）",
            "precision": "medium-to-high",
            "capabilities": ["符号定义查找", "引用追踪"],
            "limitations": [
                "若 clangd 启动失败将自动降级到 tree-sitter（精度下降）",
                "首次符号查询前可先做文档扫读与 syscall 统计",
            ],
        }
    return {
        "engine": "tree-sitter",
        "precision": "medium",
        "capabilities": ["AST 级符号匹配", "基于名称的引用扫描"],
        "limitations": ["基于名称匹配，同名函数可能误判"],
    }


app = Server("os-kernel-tools")

_TOOL_DEFS_CACHE: list[types.Tool] | None = None

def _make_tool_defs() -> list[types.Tool]:
    return [
        types.Tool(
            name="initialize_analysis",
            description=(
                "初始化仓库分析（必须第一步调用）。"
                "执行静态分析，构建符号索引和调用图，返回仓库代码地图。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "仓库的绝对路径"},
                },
                "required": ["repo_path"],
            },
        ),
        types.Tool(
            name="read_file",
            description=(
                "读取仓库中指定文件的源代码，返回带行号的文本。"
                "支持 start_line/end_line 指定行号范围，避免一次性加载大文件；"
                "对二进制文件、超大文件会自动拒绝或提示先用符号工具定位。"
                "适用场景：查看 README 与设计文档、在符号查询失败时手动确认文件内容、"
                "或在调用链返回的位置周围扩展阅读上下文。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path":       {"type": "string", "description": "相对仓库根目录的文件路径，如 os/src/task/mod.rs"},
                    "start_line": {"type": "integer", "description": "起始行号，从 1 开始（可选）"},
                    "end_line":   {"type": "integer", "description": "结束行号（可选）"},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="search_code",
            description=(
                "在仓库内做正则文本搜索，返回 file:line:内容 列表（默认上限 50 条）。"
                "适用场景：找 TODO/unimplemented/panic 等标记、定位错误信息常量、"
                "搜索宏名或字符串字面量、在不知道精确符号名时按关键字探索。"
                "默认大小写不敏感，自动跳过二进制文件和 vendor/target 等目录。"
                "注意：本工具只做文本匹配，不是语义查询；需要符号定义请用 find_symbol_definition。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern":        {"type": "string", "description": "Python 正则表达式"},
                    "file_glob":      {"type": "string", "description": "文件名 glob，如 \"*.rs\"、\"trap*\"（可选）"},
                    "case_sensitive": {"type": "boolean", "description": "是否大小写敏感，默认 false", "default": False},
                    "max_results":    {"type": "integer", "description": "命中数上限，默认 50，最大 500", "default": 50},
                },
                "required": ["pattern"],
            },
        ),
        types.Tool(
            name="find_symbol_definition",
            description=(
                "查找函数、结构体或类型的跨文件定义位置，返回完整源码。"
                "注意：未通过此工具查询的符号，不得在报告中描述其实现细节。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol_name":  {"type": "string", "description": "符号名称"},
                    "context_file": {"type": "string", "description": "辅助消歧的当前文件路径（可选）"},
                },
                "required": ["symbol_name"],
            },
        ),
        types.Tool(
            name="find_symbol_references",
            description="查找所有调用或引用了某个符号的位置，按子系统分组。",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol_name": {"type": "string"},
                },
                "required": ["symbol_name"],
            },
        ),
        types.Tool(
            name="list_implemented_syscalls",
            description=(
                "扫描仓库，列出已实现的 syscall 并与标准 Linux 集合比对，计算覆盖率。"
                "在分析阶段一调用一次，是报告第 2 章的数据来源。"
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        types.Tool(
            name="get_subsystem_call_chain",
            description="从入口函数展开调用树（默认深度 3 层，最大 5 层）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "entry_function": {"type": "string", "description": "入口函数名"},
                    "max_depth":      {"type": "integer", "default": 3, "description": "展开深度（默认 3）"},
                },
                "required": ["entry_function"],
            },
        ),
        types.Tool(
            name="find_entry_symbol",
            description=(
                "轻量符号存在性检查：返回 file:line 和 kind，不读取源码、不展开调用树。"
                "适合在 expand_callees 之前先验证符号是否存在，避免对幻觉名称做昂贵的调用树展开。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "符号名（通常是函数名）"},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="expand_callees",
            description=(
                "展开某符号的调用树（最大 5 层）。"
                "假定符号已存在；如不确定请先调用 find_entry_symbol。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name":      {"type": "string", "description": "入口函数名"},
                    "max_depth": {"type": "integer", "default": 3, "description": "展开深度（默认 3，最大 5）"},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="get_index_status",
            description=(
                "返回当前 SQLite 索引的健康度：符号总数、FTS 行数、磁盘占用、上次索引时间、语义引擎状态。"
                "用于诊断 search_code 是否走 FTS5 快路径、初始化是否复用了缓存。"
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        types.Tool(
            name="analyze_subtree",
            description=(
                "枚举某个子树（subtree_path 及其所有子目录）下的全部符号"
                "（函数 / 结构体 / typedef / 宏 / trait 等），并基于全局调用图"
                "构建子树内的调用关系，同时标注跨子树的外部依赖。"
                "适合 DIR agent 在分析某层级时一次性获得『立体代码地图』。"
                "跨子树需要看外部符号定义或文件内容时，仍调用 "
                "find_symbol_definition / read_file（全局有效）。"
                "依赖：已经调用过 initialize_analysis。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subtree_path": {
                        "type": "string",
                        "description": "相对仓库根的子树路径（如 'kernel' 或 'os/src'）；"
                                       "传空字符串表示整个仓库。",
                    },
                },
                "required": ["subtree_path"],
            },
        ),
        types.Tool(
            name="compare_with_reference_os",
            description="将当前仓库与参考 OS（rCore/xv6/uCore）进行函数级相似度比对。",
            inputSchema={
                "type": "object",
                "properties": {
                    "reference_name": {
                        "type": "string",
                        "enum": ["rcore-tutorial-v3", "rcore-tutorial-v2", "xv6-riscv", "ucore"],
                    },
                },
                "required": ["reference_name"],
            },
        ),
        types.Tool(
            name="validate_refs",
            description=(
                "在调用 write_report 之前，验证报告草稿中所有 路径:行号 引用的文件是否真实存在于仓库磁盘。"
                "对每个断链路径给出同文件名的候选实际路径，供你修正后再写报告。"
                "必须在 write_report 之前调用，若有断链则先修正再写报告。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "完整报告草稿的 Markdown 文本"},
                },
                "required": ["content"],
            },
        ),
        types.Tool(
            name="write_report",
            description=(
                "将内容原样写入指定文件并返回完成信号（不做任何格式转换）。"
                "必须实际调用本工具；不要把 <write_report>、JSON 或 XML 工具标签写成普通文本。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content":     {"type": "string", "description": "要写入的内容（HTML 片段或结构化 JSON），原样落盘"},
                    "output_path": {"type": "string", "description": "输出文件的精确路径，原样写入（不会改后缀）"},
                },
                "required": ["content", "output_path"],
            },
        ),
        types.Tool(
            name="write_to_file",
            description=(
                "write_report 的兼容别名。将 content 原样写入 path/output_path。"
                "必须实际调用本工具；不要把工具调用写成普通文本。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content":     {"type": "string", "description": "要写入的内容，原样落盘"},
                    "path":        {"type": "string", "description": "输出文件路径"},
                    "output_path": {"type": "string", "description": "输出文件路径"},
                },
                "required": ["content"],
            },
        ),
        types.Tool(
            name="load_skill",
            description=(
                "按需取回某项技能的完整指引（系统提示词「可按需加载的技能」目录里列出的 name）。"
                "仅在命中该技能的触发条件时调用，例如 reference_os 非空时取 reference-os-comparison。"
                "不消耗工具调用预算。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名（kebab-case），如 reference-os-comparison"},
                },
                "required": ["name"],
            },
        ),
    ]


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    global _TOOL_DEFS_CACHE
    if _TOOL_DEFS_CACHE is None:
        _TOOL_DEFS_CACHE = _make_tool_defs()
    return _TOOL_DEFS_CACHE


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    global _step_count, _consecutive_dup_count, _hallucination_count, _stop_next

    arguments = _normalize_tool_arguments(name, arguments or {})

    # initialize_analysis 和 write_report 不参与步数/去重统计
    if name == "initialize_analysis":
        return await _handle_initialize(arguments)

    if name == "validate_refs":
        return _handle_validate_refs(arguments)

    if name in ("write_report", "write_to_file"):
        return _handle_write_report(arguments)

    if name == "load_skill":
        from ..prompts import skills
        body = skills.get_skill_body(arguments.get("name", "").strip())
        return [types.TextContent(type="text", text=body)]

    _step_count += 1

    # 预算保护：步数上限
    if _step_count > _MAX_STEPS:
        return [types.TextContent(type="text", text=(
            f"[保护] 已达最大工具调用步数（{_MAX_STEPS} 步）。"
            "请直接根据已收集的信息输出最终报告，不要再调用任何工具。"
        ))]

    # 停止信号传播
    if _stop_next:
        _stop_next = False
        _hallucination_count   = 0
        _consecutive_dup_count = 0
        return [types.TextContent(type="text", text=(
            "请直接根据已收集的信息输出最终报告，不要再调用任何工具。"
        ))]

    # 提取主要查询符号（用于日志和去重）
    sym = (
        arguments.get("symbol_name")
        or arguments.get("entry_function")
        or arguments.get("struct_name")
        or arguments.get("reference_name")
    )

    # 重复调用检测
    cache_key = (name, json.dumps(arguments, sort_keys=True))
    if cache_key in _queried_cache:
        _consecutive_dup_count += 1
        hint = f"已调用过 {name}({sym!r})，结果同前，请勿重复调用。"
        _log(f"重复调用（连续第 {_consecutive_dup_count} 次）：{name}({sym!r})")
        if _consecutive_dup_count >= _MAX_CONSECUTIVE_DUPS:
            _stop_next = True
            hint += "\n\n[保护] 检测到持续重复调用，请直接输出最终报告。"
        return [types.TextContent(type="text", text=hint)]

    _consecutive_dup_count = 0

    # 幻觉扩展检测：curr 是某个已失败符号的后缀变体
    base = _suffix_base(sym)
    if base is not None:
        _hallucination_count += 1
        _failed_symbols.add(sym)
        _log(f"幻觉扩展（{_hallucination_count}/{_MAX_SUFFIX_HALLUCINATIONS}）：{sym!r} 衍生自 {base!r}")
        msg = f"符号 {sym!r} 不存在（是对不存在符号 {base!r} 的猜测变体）。请勿继续猜测。"
        if _hallucination_count >= _MAX_SUFFIX_HALLUCINATIONS:
            _stop_next = True
            msg += "\n\n[保护] 多次幻觉扩展，请直接输出最终报告。"
        return [types.TextContent(type="text", text=msg)]

    ctx = _get_context()
    if ctx is None:
        auto_repo = os.environ.get("OSKERNEL_AGENT_REPO_PATH", "").strip()
        if auto_repo:
            quick = _execute_quick_tool(name, arguments, auto_repo)
            if quick is not None:
                _queried_cache.add(cache_key)
                _queried_results[cache_key] = quick
                _log(f"快速工具 {name} 返回 {len(quick)} 字符")
                return [types.TextContent(type="text", text=quick)]
            init_result = await _handle_initialize({"repo_path": auto_repo})
            ctx = _get_context()
            if ctx is None:
                return init_result
        else:
            return [types.TextContent(type="text", text=(
                "[错误] 尚未初始化分析，且 OSKERNEL_AGENT_REPO_PATH 未设置。"
            ))]

    # 执行工具：放到工作线程，避免懒加载引擎阻塞 asyncio 事件循环
    try:
        result = await asyncio.to_thread(ctx.execute, name, arguments)
    except Exception as exc:
        return [types.TextContent(type="text", text=f"[工具执行错误] {name}: {exc}")]

    if _is_not_found(result):
        if sym:
            _failed_symbols.add(sym)
        result_str = f"符号 {sym!r} 在代码库中不存在。"
    else:
        result_str = str(result)

    _queried_cache.add(cache_key)
    _queried_results[cache_key] = result_str
    _log(f"步骤 {_step_count} {name}({sym!r}) 返回 {len(result_str)} 字符")
    return [types.TextContent(type="text", text=result_str)]


# 工具处理器

async def _handle_initialize(arguments: dict) -> list[types.TextContent]:
    global _init_repeat_count

    repo_path_str = arguments.get("repo_path", "").strip()

    if not repo_path_str:
        return [types.TextContent(type="text", text="[错误] repo_path 参数不能为空。")]

    repo_path = Path(repo_path_str).resolve()
    if not repo_path.exists():
        return [types.TextContent(
            type="text", text=f"[错误] 仓库路径不存在：{repo_path}"
        )]

    _log(f"初始化分析：{repo_path}")

    # 重试时复用：已初始化且指向同一仓库 → 重新发送缓存的 Layer 2，
    # 不重复执行静态分析、也不重复启动后台引擎。
    existing = _contexts.get("")
    if (existing is not None
            and getattr(existing, "repo_path", None) == str(repo_path)
            and getattr(existing, "_cached_init_response", None)):
        engine = existing.engine
        engine_state = "就绪" if getattr(engine, "is_ready", lambda: True)() else "后台初始化中"
        _init_repeat_count += 1
        _log(f"重复 initialize_analysis 第 {_init_repeat_count} 次，引擎{engine_state}")
        return [types.TextContent(type="text", text=(
            f"[已初始化] 仓库上下文已存在，引擎{engine_state}。"
            "不要再调用 initialize_analysis。"
            "请直接继续调用 analyze_subtree/read_file/find_symbol_definition 收集事实，"
            "或在信息足够时调用 write_report 写入 outputs 中指定的文件。"
        ))]

    try:
        from ..prompts.builder import build_layer_2, detect_crate_roles

        # 静态分析（ctags + tree-sitter 调用图）放到工作线程，避免阻塞 asyncio 事件循环
        def _static_analysis():
            from .agent import _build_structure
            from ..parsers.code_parser import build_profile
            from ..parsers.os_tools import build_repo_map
            structure              = _build_structure(repo_path)
            profile                = build_profile(str(repo_path), structure)
            cache_dir              = _config.data.get("cache_dir", "data/cache")
            level1_map, level2_idx = build_repo_map(
                str(repo_path), structure, profile, cache_dir=cache_dir
            )
            profile["repo_name"]   = repo_path.name
            return structure, profile, level1_map, level2_idx

        structure, profile, level1_map, level2_idx = await asyncio.to_thread(_static_analysis)

        # 引擎懒加载：select_engine 中 rust-analyzer/clangd 启动可能耗时数十秒，
        # 放到后台线程，让本次 initialize_analysis 立即返回 Layer 2 给 LLM。
        predicted_info = _predict_engine_info(profile)
        engine         = _LazyEngine(str(repo_path), profile, level2_idx, predicted_info)
        ref_db         = ReferenceOSDatabase(
            _config.data.get("reference_db_dir", "reference_db")
        )
        ctx            = OSKernelMCPTools(
            str(repo_path), engine, level2_idx, profile, structure, ref_db
        )
        _contexts[""] = ctx
        _init_repeat_count = 0

        crate_roles = detect_crate_roles(str(repo_path), profile)
        layer2      = build_layer_2(structure, profile, level1_map, predicted_info, crate_roles)

        result = (
            f"[初始化完成] 静态结构已就绪。"
            f"语义引擎正在后台启动（{predicted_info['engine']}）。\n"
            f"提示：可立即用 read_file/search_code/list_implemented_syscalls 开展"
            f"阶段一文档扫读与阶段二事实收集；首次符号查询将阻塞至引擎就绪。\n\n"
            f"{layer2}"
        )
        ctx._cached_init_response = result  # 供后续 initialize_analysis 重试时直接复用
        _log(f"初始化成功，Layer2 长度 {len(layer2)} 字符，引擎后台启动中")
        return [types.TextContent(type="text", text=result)]

    except Exception as exc:
        _log(f"初始化失败：{exc}")
        return [types.TextContent(type="text", text=f"[初始化失败] {exc}")]


def _handle_validate_refs(arguments: dict) -> list[types.TextContent]:
    """扫描报告草稿中的所有 路径.ext:行号 引用，逐一核查磁盘可达性。

    对每个找不到的路径，在仓库里搜索同文件名的实际位置作为修正建议。
    """
    import re as _re

    content = arguments.get("content", "")
    if not content:
        return [types.TextContent(type="text", text="[错误] content 不能为空。")]

    # 收集所有上下文中已初始化的仓库根目录
    repo_roots: list[Path] = []
    for ctx in _contexts.values():
        rp = getattr(ctx, "repo_path", None)
        if rp:
            repo_roots.append(Path(rp))

    if not repo_roots:
        return [types.TextContent(type="text",
            text="[错误] 尚未初始化仓库，请先调用 initialize_analysis。")]

    # 提取所有 path/to/file.ext[:line[-line]] 引用，去重
    _EXTS = r"c|h|cc|cpp|cxx|hpp|rs|S|s|ld|lds|toml|md|py|sh|mk|cfg|go|json|yaml|yml|txt"
    ref_re = _re.compile(
        rf"([A-Za-z0-9_./\-]+\.(?:{_EXTS}))(?::\d+(?:-\d+)?)?",
        _re.MULTILINE,
    )
    seen: set[str] = set()
    paths: list[str] = []
    for m in ref_re.finditer(content):
        fp = m.group(1)
        if fp not in seen:
            seen.add(fp)
            paths.append(fp)

    # 同时检测目录引用（路径以 / 结尾，或匹配常见目录模式但无文件扩展名）
    _dir_re = _re.compile(
        r"(?<![`\w])([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+)/"
        r"(?![A-Za-z0-9_.\-])",
        _re.MULTILINE,
    )
    dir_refs: list[str] = []
    for m in _dir_re.finditer(content):
        dr = m.group(1) + "/"
        if dr not in seen:
            seen.add(dr)
            dir_refs.append(dr)

    if not paths and not dir_refs:
        return [types.TextContent(type="text",
            text="[validate_refs] 报告中未检测到任何文件引用，可直接调用 write_report。")]

    valid: list[str] = []
    broken: list[str] = []

    for fp in paths:
        found = False
        p = Path(fp)
        for root in repo_roots:
            candidate = root / fp if not p.is_absolute() else p
            if candidate.exists():
                found = True
                break
            # 逐级剥前缀回退
            parts = p.parts
            for strip in range(1, len(parts)):
                cand = root / Path(*parts[strip:])
                if cand.exists():
                    found = True
                    break
            if found:
                break
        if found:
            valid.append(fp)
        else:
            broken.append(fp)

    # 目录引用：区分「模块定位」（目录存在，合法）和「幻觉路径」（目录不存在，报错）
    dir_lines: list[str] = []
    phantom_dirs: list[str] = []
    for dr in dir_refs:
        dir_path = dr.rstrip("/")
        exists = any((root / dir_path).is_dir() for root in repo_roots)
        if not exists:
            phantom_dirs.append(dr)

    if phantom_dirs:
        dir_lines.append(
            f"[validate_refs] 以下 {len(phantom_dirs)} 处目录引用在仓库中不存在（幻觉路径），"
            f"请修正为真实路径或改写为 文件路径:行号 形式：\n"
        )
        for dr in phantom_dirs:
            dir_lines.append(
                f"  目录不存在：{dr}\n"
                f"    → 用 search_code 搜索该结论中提到的函数/符号名，"
                f"从返回的 文件:行号 中取得具体位置后填入报告；"
                f"若搜索无结果，将该结论改写为\"未找到具体实现\""
            )

    if not broken:
        if dir_lines:
            return [types.TextContent(type="text", text="\n".join(dir_lines))]
        valid_dirs = len(dir_refs) - len(phantom_dirs)
        extra = f"（另有 {valid_dirs} 处目录定位引用已跳过检查）" if valid_dirs else ""
        return [types.TextContent(type="text",
            text=f"[validate_refs] 全部 {len(valid)} 个文件引用路径均可访问{extra}，可以调用 write_report。")]

    # 对断链路径搜索同文件名的候选位置
    lines = [
        f"[validate_refs] 共 {len(paths)} 个引用，{len(valid)} 个有效，"
        f"{len(broken)} 个无法在磁盘找到，请修正后再调用 write_report：\n"
    ]
    for fp in broken:
        fname = Path(fp).name
        candidates: list[str] = []
        for root in repo_roots:
            for hit in root.rglob(fname):
                try:
                    rel = str(hit.relative_to(root))
                    candidates.append(rel)
                except ValueError:
                    pass
                if len(candidates) >= 5:
                    break
            if len(candidates) >= 5:
                break
        if candidates:
            suggestion = "候选实际路径：" + "  |  ".join(candidates[:3])
        else:
            suggestion = "仓库中未找到同名文件，可能是幻觉路径，请用 search_code 搜索相关函数名定位"
        lines.append(f"  断链：{fp}\n    {suggestion}")

    return [types.TextContent(type="text", text="\n".join(dir_lines + lines))]


def _handle_write_report(arguments: dict) -> list[types.TextContent]:
    """把 agent 产出的内容原样写到 output_path。

    agent 现在直接产出 HTML 片段 / 结构化 JSON，写到调用方（树管道）指定的精确
    路径（.json / .content.md / .module-NNN.md）。最终报告由树管道的 html_tree
    统一渲染，本工具不再做任何 markdown→HTML 转换。引用路径的合法性由 validate_refs
    在写入前负责校验。
    """
    content = arguments.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=2)
    output_path = (
        arguments.get("output_path")
        or arguments.get("path")
        or arguments.get("file_path")
        or ""
    ).strip()

    if not output_path:
        return [types.TextContent(type="text", text="[完成] 报告生成完毕。")]

    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(content, encoding="utf-8")
        _log(f"报告已写入：{p}（{len(content)} 字符）")
        msg = f"[完成] 报告已保存到 {p}。"
    except Exception as exc:
        _log(f"写入报告失败：{exc}")
        msg = f"[完成] 报告生成完毕，但写入失败：{exc}"
    return [types.TextContent(type="text", text=msg)]


# 辅助函数

def _get_context() -> OSKernelMCPTools | None:
    return _contexts.get("")


def _is_not_found(result) -> bool:
    if result is None:
        return True
    if isinstance(result, str):
        return not result or result.startswith("[未找到]")
    if isinstance(result, dict):
        if not result:
            return True
        vals = list(result.values())
        return bool(vals) and isinstance(vals[0], dict) and vals[0].get("__not_found__")
    return isinstance(result, list) and not result


def _suffix_base(curr: str | None) -> str | None:
    if not curr:
        return None
    for base in _failed_symbols:
        if len(curr) > len(base) and curr.startswith(base + "_"):
            return base
    return None


# 入口

async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
