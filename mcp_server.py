"""
MCP stdio server — 向 OpenCode 暴露 OS 内核分析工具集。

由 OpenCode 在启动时作为子进程启动。工具调用由 OpenCode 信号驱动，
本进程负责执行并返回结果。

"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

sys.path.insert(0, str(Path(__file__).parent))

import config as _config
from tools.mcp_tools import OSKernelMCPTools
from tools.reference_db import ReferenceOSDatabase

# CLI 参数解析

_ap = argparse.ArgumentParser(add_help=False)
_ap.add_argument("--max-steps", type=int, default=200)
_args, _ = _ap.parse_known_args()

_MAX_STEPS = _args.max_steps

# 多上下文存储（key="" 单仓库，key="a"/"b" 比较模式）

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


app = Server("os-kernel-tools")

_TOOL_DEFS_CACHE: list[types.Tool] | None = None

_REPO_PARAM = {
    "repo": {
        "type": "string",
        "description": "比较模式下指定仓库标签（'a' 或 'b'），单仓库模式留空",
        "default": "",
    }
}


def _make_tool_defs() -> list[types.Tool]:
    return [
        types.Tool(
            name="initialize_analysis",
            description=(
                "初始化仓库分析（必须第一步调用）。"
                "执行静态分析，构建符号索引和调用图，返回仓库代码地图。"
                "比较模式下调用两次，分别传 label='a' 和 label='b'。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "仓库的绝对路径"},
                    "label": {
                        "type": "string",
                        "description": "比较模式标签（'a'/'b'），单仓库留空",
                        "default": "",
                    },
                },
                "required": ["repo_path"],
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
                    **_REPO_PARAM,
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
                    **_REPO_PARAM,
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
                "properties": {**_REPO_PARAM},
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
                    **_REPO_PARAM,
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
                    **_REPO_PARAM,
                },
                "required": ["reference_name"],
            },
        ),
        types.Tool(
            name="write_report",
            description=(
                "将完整的最终评审报告写入文件并返回完成信号。"
                "所有分析完成后调用一次。如果用户指定了输出路径，传入 output_path。"
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

    # 重复调用检测（排除 repo 路由参数）
    cache_args = {k: v for k, v in arguments.items() if k != "repo"}
    cache_key  = (name, json.dumps(cache_args, sort_keys=True))
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

    # 选择上下文
    label = arguments.get("repo", "")
    ctx   = _get_context(label)
    if ctx is None:
        return [types.TextContent(type="text", text=(
            "[错误] 尚未初始化分析。请先调用 initialize_analysis(repo_path)。"
        ))]

    # 执行工具
    exec_args = {k: v for k, v in arguments.items() if k != "repo"}
    try:
        result = ctx.execute(name, exec_args)
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
    label         = arguments.get("label", "")

    if not repo_path_str:
        return [types.TextContent(type="text", text="[错误] repo_path 参数不能为空。")]

    repo_path = Path(repo_path_str).resolve()
    if not repo_path.exists():
        return [types.TextContent(
            type="text", text=f"[错误] 仓库路径不存在：{repo_path}"
        )]

    _log(f"初始化分析（label={label!r}）：{repo_path}")

    try:
        from agent import _build_structure, select_engine
        from parser.code_parser import build_profile
        from parser.os_tools import build_repo_map
        from prompts import build_layer_2, detect_crate_roles

        structure              = _build_structure(repo_path)
        profile                = build_profile(str(repo_path), structure)
        level1_map, level2_idx = build_repo_map(str(repo_path), structure, profile)
        profile["repo_name"]   = repo_path.name
        engine                 = select_engine(str(repo_path), profile, level2_idx)
        ref_db                 = ReferenceOSDatabase(
            _config.data.get("reference_db_dir", "data/reference_db")
        )
        ctx                    = OSKernelMCPTools(
            str(repo_path), engine, level2_idx, profile, structure, ref_db
        )
        _contexts[label]       = ctx

        engine_info  = engine.get_engine_info()
        crate_roles  = detect_crate_roles(str(repo_path), profile)
        layer2       = build_layer_2(structure, profile, level1_map, engine_info, crate_roles)

        label_tag = f"（仓库 {label.upper()}）" if label else ""
        result    = (
            f"[初始化完成{label_tag}] "
            f"引擎：{engine_info['engine']}，精度：{engine_info['precision']}\n\n"
            f"{layer2}"
        )
        _log(f"初始化成功（label={label!r}），Layer2 长度 {len(layer2)} 字符")
        return [types.TextContent(type="text", text=result)]

    except Exception as exc:
        _log(f"初始化失败：{exc}")
        return [types.TextContent(type="text", text=f"[初始化失败] {exc}")]


def _handle_write_report(arguments: dict) -> list[types.TextContent]:
    content     = arguments.get("content", "")
    output_path = arguments.get("output_path", "").strip()

    if output_path:
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        _log(f"报告已写入：{p}")
        return [types.TextContent(type="text", text=f"[完成] 报告已保存到 {p}。")]
    return [types.TextContent(type="text", text="[完成] 报告生成完毕。")]


# 辅助函数

def _get_context(label: str) -> OSKernelMCPTools | None:
    if label in _contexts:
        return _contexts[label]
    if _contexts:
        return next(iter(_contexts.values()))
    return None


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
