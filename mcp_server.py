"""
MCP stdio server — 向 OpenCode 暴露 OS 内核分析工具集。

由 OpenCode 在启动时作为子进程启动。工具调用由 OpenCode 信号驱动，
本进程负责执行并返回结果。

"""

import argparse
import asyncio
import json
import sys
import threading
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

sys.path.insert(0, str(Path(__file__).parent))

import config as _config
from tools.mcp_tools import OSKernelMCPTools
from tools.reference_db import ReferenceOSDatabase
from report_html import write_html_sibling

# CLI 参数解析

_ap = argparse.ArgumentParser(add_help=False)
_ap.add_argument("--max-steps", type=int, default=200)
_args, _ = _ap.parse_known_args()

_MAX_STEPS = _args.max_steps

# 分析上下文

_contexts: dict[str, OSKernelMCPTools] = {}

# 会话级保护状态

_MAX_CONSECUTIVE_DUPS      = 5
_MAX_SUFFIX_HALLUCINATIONS = 3

_step_count:             int        = 0
_failed_symbols:         set[str]   = set()
_queried_cache:          set[tuple] = set()
_queried_results:        dict       = {}
_consecutive_dup_count:  int        = 0
_hallucination_count:    int        = 0
_stop_next:              bool       = False

# MCP 服务器

def _log(msg: str) -> None:
    print(f"[MCP] {msg}", file=sys.stderr, flush=True)


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
            from agent import select_engine
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
                "将完整的最终评审报告写入文件并返回完成信号。"
                "调用前必须先用 validate_refs 验证引用路径，确认无断链后再写入。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content":     {"type": "string", "description": "完整的 Markdown 报告文本"},
                    "output_path": {"type": "string", "description": "输出文件路径（可选）"},
                },
                "required": ["content"],
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

    # initialize_analysis 和 write_report 不参与步数/去重统计
    if name == "initialize_analysis":
        return await _handle_initialize(arguments)

    if name == "validate_refs":
        return _handle_validate_refs(arguments)

    if name == "write_report":
        return _handle_write_report(arguments)

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
        return [types.TextContent(type="text", text=(
            "[错误] 尚未初始化分析。请先调用 initialize_analysis(repo_path)。"
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
        _log(f"复用已初始化上下文，引擎{engine_state}")
        return [types.TextContent(type="text", text=existing._cached_init_response)]

    try:
        from prompts import build_layer_2, detect_crate_roles

        # 静态分析（ctags + tree-sitter 调用图）放到工作线程，避免阻塞 asyncio 事件循环
        def _static_analysis():
            from agent import _build_structure
            from parser.code_parser import build_profile
            from parser.os_tools import build_repo_map
            structure              = _build_structure(repo_path)
            profile                = build_profile(str(repo_path), structure)
            level1_map, level2_idx = build_repo_map(str(repo_path), structure, profile)
            profile["repo_name"]   = repo_path.name
            return structure, profile, level1_map, level2_idx

        structure, profile, level1_map, level2_idx = await asyncio.to_thread(_static_analysis)

        # 引擎懒加载：select_engine 中 rust-analyzer/clangd 启动可能耗时数十秒，
        # 放到后台线程，让本次 initialize_analysis 立即返回 Layer 2 给 LLM。
        predicted_info = _predict_engine_info(profile)
        engine         = _LazyEngine(str(repo_path), profile, level2_idx, predicted_info)
        ref_db         = ReferenceOSDatabase(
            _config.data.get("reference_db_dir", "data/reference_db")
        )
        ctx            = OSKernelMCPTools(
            str(repo_path), engine, level2_idx, profile, structure, ref_db
        )
        _contexts[""] = ctx

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

    # 汇总目录引用错误
    dir_lines: list[str] = []
    if dir_refs:
        dir_lines.append(
            f"[validate_refs] 以下 {len(dir_refs)} 处引用了目录而非具体文件，"
            f"必须改写为 文件路径:行号 形式：\n"
        )
        for dr in dir_refs:
            dir_lines.append(
                f"  目录引用（无效）：{dr}\n"
                f"    → 用 search_code 搜索该结论中提到的函数/符号名，"
                f"从返回的 文件:行号 中取得具体位置后填入报告；"
                f"若搜索无结果，将该结论改写为\"未找到具体实现\""
            )

    if not broken:
        if dir_lines:
            return [types.TextContent(type="text", text="\n".join(dir_lines))]
        return [types.TextContent(type="text",
            text=f"[validate_refs] 全部 {len(valid)} 个引用路径均可访问，可以调用 write_report。")]

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
    """把报告 Markdown 写到磁盘，并附带渲染一份 HTML。

    核验由独立的 os-kernel-verifier agent 在主 agent 完成后单独执行，
    本工具不再做任何证据校验。
    """
    content     = arguments.get("content", "")
    output_path = arguments.get("output_path", "").strip()

    if not output_path:
        return [types.TextContent(type="text", text="[完成] 报告生成完毕。")]

    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _log(f"报告已写入：{p}")
    msg = f"[完成] 报告已保存到 {p}。"

    repo_roots: list[Path] = []
    for ctx in _contexts.values():
        rp = getattr(ctx, "repo_path", None)
        if not rp:
            continue
        repo_root = Path(rp)
        repo_roots.append(repo_root)
        for src_rel in getattr(ctx, "structure", {}).get("source_roots_rel", []):
            repo_roots.append(repo_root / src_rel)
    try:
        html_path, broken = write_html_sibling(p, content, repo_roots=repo_roots)
        _log(f"HTML 报告已写入：{html_path}（repo_roots={len(repo_roots)}，断链={len(broken)}）")
        msg += f" HTML 版本：{html_path}。"
        if broken:
            broken_list = "\n".join(f"  - {bp}" for bp in sorted(broken))
            msg += (
                f"\n\n[警告] HTML 中以下 {len(broken)} 个文件引用无法解析为磁盘路径，"
                f"在报告里显示为红色断链，点击无法定位到正确源文件，"
                f"请用 search_code 找到正确路径后重新调用 write_report 修正：\n"
                f"{broken_list}"
            )
    except Exception as exc:
        _log(f"生成 HTML 报告失败：{exc}")
        msg += f" （HTML 生成失败：{exc}）"
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
