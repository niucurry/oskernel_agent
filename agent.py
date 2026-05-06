import json
from pathlib import Path
from openai import OpenAI

import config
from engines.base import AnalysisEngine
from parser.code_parser import (
    build_profile,
    find_source_roots, classify_files_by_content,
    detect_naming_style, find_doc_files, detect_anomalies,
)
from parser.os_tools import build_repo_map
from tools.tool_registry import get_tool_definitions, mcp_to_openai_schema
from tools.mcp_tools import OSKernelMCPTools
from tools.reference_db import ReferenceOSDatabase
from prompts import assemble_system_prompt, detect_crate_roles

client = OpenAI(api_key=config.api["key"], base_url=config.api["base_url"])

# 工具 Schema：6 个 MCP 风格工具 + get_struct_fields（引擎原生，不在 MCP 列表中但保留兼容）

tools_schema = mcp_to_openai_schema(get_tool_definitions()) + [
    {
        "type": "function",
        "function": {
            "name": "get_struct_fields",
            "description": "获取操作系统核心数据结构的完整字段列表，用于核实内存布局或寄存器设计。",
            "parameters": {
                "type": "object",
                "properties": {
                    "struct_name": {"type": "string", "description": "结构体名称"}
                },
                "required": ["struct_name"],
            },
        },
    },
]


#引擎选择：路径 A → B → C 依次降级

def select_engine(repo_path: str, profile: dict, level2_index) -> AnalysisEngine:
    primary_lang = profile["primary_lang"]

    ecfg = config.engine

    if primary_lang == "rust" or profile.get("has_cargo"):
        from engines.path_a import RustAnalyzerEngine
        engine = RustAnalyzerEngine(repo_path, level2_index,
                                    timeout=ecfg["rust_analyzer_timeout"])
        if engine.initialize():
            print("[引擎选择] 路径 A：rust-analyzer")
            return engine

    if primary_lang == "c":
        from engines.path_b import ClangdEngine, try_generate_compile_commands
        cc_path = try_generate_compile_commands(repo_path)
        if cc_path:
            engine = ClangdEngine(repo_path, cc_path, level2_index,
                                  timeout=ecfg["clangd_timeout"])
            if engine.initialize():
                print("[引擎选择] 路径 B：clangd")
                return engine

    from engines.path_c import TreeSitterEngine
    lang = primary_lang if primary_lang in ("c", "rust") else "c"
    print(f"[引擎选择] 路径 C：tree-sitter（{lang}）")
    return TreeSitterEngine(repo_path, lang, skip_dirs=ecfg["skip_dirs"])


#核心执行引擎

_MAX_SUFFIX_HALLUCINATIONS = 3   # 触发幻觉扩展终止所需的次数
_MAX_STEPS = 200                 # 单次 agent_run 最大工具调用步数
_MAX_CONSECUTIVE_DUPS = 5        # 连续重复调用同一工具超过此次数时强制终止


def _is_not_found(result) -> bool:
    if result is None:
        return True
    if isinstance(result, str):
        return not result or result.startswith("[未找到]")
    if isinstance(result, dict):
        if not result:
            return True
        values = list(result.values())
        return bool(values) and isinstance(values[0], dict) and values[0].get("__not_found__")
    if isinstance(result, list) and not result:
        return True
    return False


def _is_suffix_of_any(failed_symbols: set, curr: str | None) -> str | None:
    """若 curr 是 failed_symbols 中某个符号加后缀的变体，返回那个基础符号；否则返回 None。"""
    if not curr:
        return None
    for base in failed_symbols:
        if len(curr) > len(base) and curr.startswith(base + "_"):
            return base
    return None


def agent_run(
    engine: AnalysisEngine,
    system_prompt: str,
    user_prompt: str,
    repo_path: str = "",
    level2_index=None,
    profile: dict | None = None,
    structure: dict | None = None,
) -> str:
    _repo_path = repo_path or engine.repo_path
    _ref_db = ReferenceOSDatabase(
        config.data.get("reference_db_dir", "data/reference_db")
    )
    mcp_tools = OSKernelMCPTools(
        _repo_path, engine, level2_index, profile or {}, structure, _ref_db
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]

    info = engine.get_engine_info()
    print(f"\n Agent 启动（引擎：{info['engine']}，路径{info['path']}，精度：{info['precision']}）")

    step = 1
    failed_symbols: set[str] = set()          # 所有已查询且未找到的符号名
    queried_cache: set[tuple] = set()         # (tool_name, symbol) 去重缓存
    queried_results: dict[tuple, str] = {}    # cache_key -> 首次结果（用于重复提示）
    hallucination_count = 0                   # 本轮幻觉扩展命中次数
    consecutive_dup_count = 0                 # 当前连续重复调用计数
    force_finish = False

    while True:
        if step > _MAX_STEPS:
            print(f"\n [保护] 已达最大步数 {_MAX_STEPS}，强制终止并输出报告")
            messages.append({
                "role":    "user",
                "content": f"已达工具调用上限（{_MAX_STEPS} 步）。请根据已收集到的信息直接输出最终分析报告，不要再调用任何工具。",
            })
            response = client.chat.completions.create(
                model=config.api["model"],
                messages=messages,
                tools=tools_schema,
                tool_choice="none",
                temperature=config.api["temperature"],
            )
            return response.choices[0].message.content

        if force_finish:
            print("\n [保护] 注入终止指令，要求 LLM 输出最终报告")
            if consecutive_dup_count >= _MAX_CONSECUTIVE_DUPS:
                stop_reason = (
                    "你已连续多次调用相同的工具且结果没有变化（可能是将目录路径当作文件，或重复查询无效符号）。"
                    "请根据已收集到的信息直接输出最终分析报告，不要再调用任何工具。"
                )
            else:
                stop_reason = (
                    "这些符号在代码库中均不存在，请勿继续猜测变体名称。"
                    "请根据已收集到的信息直接输出最终分析报告，不要再调用任何工具。"
                )
            messages.append({"role": "user", "content": stop_reason})
            force_finish = False
            hallucination_count = 0
            consecutive_dup_count = 0

        response = client.chat.completions.create(
            model=config.api["model"],
            messages=messages,
            tools=tools_schema,
            tool_choice="auto",
            temperature=config.api["temperature"],
        )

        response_message = response.choices[0].message

        if not response_message.tool_calls:
            print("\n Agent 分析完毕，输出最终报告：\n")
            return response_message.content

        messages.append(response_message)

        for tool_call in response_message.tool_calls:
            function_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            print(f"  [步骤 {step}] 调用工具：{function_name} → {args}")

            queried_symbol = (
                args.get("symbol_name")
                or args.get("function_name")
                or args.get("entry_function")
                or args.get("struct_name")
                or args.get("path")
            )

            # 重复调用检测：完全相同的 (工具名 + 全部参数) 组合已查询过
            # 用完整 args 序列化做 key，避免 read_file 不同行号被误判为重复
            cache_key = (function_name, json.dumps(args, sort_keys=True))
            if cache_key in queried_cache:
                consecutive_dup_count += 1
                prev_result = queried_results.get(cache_key, "")
                if function_name == "read_file":
                    if "[提示]" in prev_result and "是目录" in prev_result:
                        hint = (
                            f"{queried_symbol!r} 是目录，不能直接读取。"
                            f"请从上次返回的文件列表中选择一个具体文件路径再调用 read_file。"
                        )
                    else:
                        hint = f"已读取过 {queried_symbol!r}，结果同前，请勿重复调用。"
                else:
                    hint = f"符号 {queried_symbol!r} 已查询过，结果同前，请勿重复调用。"
                print(f"  [保护] 重复调用（连续第 {consecutive_dup_count} 次）跳过：{function_name}({queried_symbol!r})")
                messages.append({
                    "tool_call_id": tool_call.id,
                    "role":         "tool",
                    "name":         function_name,
                    "content":      hint,
                })
                step += 1
                if consecutive_dup_count >= _MAX_CONSECUTIVE_DUPS:
                    force_finish = True
                continue

            consecutive_dup_count = 0
            queried_cache.add(cache_key)

            # 幻觉扩展检测：仅对符号类工具生效，read_file 的 path 不参与
            base = (
                None if function_name == "read_file"
                else _is_suffix_of_any(failed_symbols, queried_symbol)
            )
            if base is not None:
                hallucination_count += 1
                print(f"  [保护] 幻觉扩展（{hallucination_count}/{_MAX_SUFFIX_HALLUCINATIONS}）：{queried_symbol!r} 是 {base!r} 的后缀变体")
                failed_symbols.add(queried_symbol)  # 也加入失败集，阻断更深层扩展
                messages.append({
                    "tool_call_id": tool_call.id,
                    "role":         "tool",
                    "name":         function_name,
                    "content":      f"符号 {queried_symbol!r} 不存在（是对不存在符号 {base!r} 的猜测变体）。请勿继续猜测。",
                })
                step += 1
                if hallucination_count >= _MAX_SUFFIX_HALLUCINATIONS:
                    force_finish = True
                continue

            result = mcp_tools.execute(function_name, args)

            if _is_not_found(result):
                # read_file 的 path 不是符号名，不加入幻觉检测集合
                if queried_symbol and function_name != "read_file":
                    failed_symbols.add(queried_symbol)
                result_str = (
                    f"文件 {queried_symbol!r} 不存在或无法读取。"
                    if function_name == "read_file"
                    else f"符号 {queried_symbol!r} 在代码库中不存在。"
                )
            else:
                result_str = str(result)

            queried_results[cache_key] = result_str
            messages.append({
                "tool_call_id": tool_call.id,
                "role":         "tool",
                "name":         function_name,
                "content":      result_str,
            })
            step += 1


#构建 structure dict（build_profile 和 build_repo_map 的输入)

def _build_structure(repo_path: Path) -> dict:
    source_roots_rel = find_source_roots(repo_path)
    source_roots_abs = [str(repo_path / r) for r in source_roots_rel]

    all_depths = [
        len(f.relative_to(repo_path).parts)
        for ext in ("*.c", "*.rs")
        for f in repo_path.rglob(ext)
    ]
    avg_depth = sum(all_depths) / len(all_depths) if all_depths else 0.0
    depth_label = "flat" if avg_depth <= 2 else ("shallow" if avg_depth <= 4 else "deep")

    structure = {
        "doc_files":           find_doc_files(repo_path),
        "subsystem_locations": classify_files_by_content(str(repo_path), source_roots_rel),
        "structure_depth":     depth_label,
        "avg_depth":           round(avg_depth, 1),
        "source_roots":        source_roots_abs,
        "source_roots_rel":    source_roots_rel,
        "naming_style":        detect_naming_style(repo_path),
    }
    structure["anomalies"] = detect_anomalies(structure, repo_path)
    return structure


def _resolve_repo_path(url: str | None, repo_path_arg: str | None,
                        repo_id: str | None) -> tuple[Path, str]:
    """根据命令行参数确定本地仓库路径，返回 (Path, repo_name)。"""
    if url:
        from fetch_single_repo import fetch_repo
        local = fetch_repo(url,
                           output_dir=config.data["repos_dir"],
                           meta_dir=config.data.get("metadata_dir", "data/metadata"))
        p = Path(local).resolve()
        return p, p.name
    if repo_path_arg:
        p = Path(repo_path_arg).resolve()
        return p, p.name
    name = repo_id or config.target["repo_id"]
    p = (Path(config.data["repos_dir"]) / name).resolve()
    return p, name


def _analyze_one(repo_path: Path, repo_name: str) -> tuple:
    """对单个仓库执行静态分析，返回 (structure, profile, level1_map, level2_index, engine)。"""
    print("正在执行静态结构分析...")
    structure = _build_structure(repo_path)
    profile = build_profile(str(repo_path), structure)
    level1_map, level2_index = build_repo_map(str(repo_path), structure, profile)
    profile["repo_name"] = repo_name
    engine = select_engine(str(repo_path), profile, level2_index)
    return structure, profile, level1_map, level2_index, engine


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="OS 内核代码分析智能体",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
用法示例：
  python agent.py                                      # 使用 config.toml 中的 repo_id
  python agent.py --repo-id T202510008995695-2259
  python agent.py --repo-path /absolute/path/to/repo
  python agent.py --url https://gitlab.example.com/repo.git
  python agent.py --repo-id REPO_A --output report.md
  python agent.py --compare --repo-id REPO_A --repo-id-b REPO_B
  python agent.py --compare --url URL_A --url-b URL_B
        """,
    )

    src = parser.add_mutually_exclusive_group()
    src.add_argument("--repo-id",   metavar="ID",
                     help="data/historical_repos/ 下的仓库文件夹名")
    src.add_argument("--repo-path", metavar="PATH",
                     help="仓库的完整本地路径")
    src.add_argument("--url",       metavar="URL",
                     help="克隆并分析远程仓库（自动执行 fetch）")

    parser.add_argument("--output", "-o", metavar="FILE",
                        help="将报告写入文件（默认打印到标准输出）")
    parser.add_argument("--model",  metavar="MODEL",
                        help="覆盖 config.toml 中的模型名称")
    parser.add_argument("--compare", action="store_true",
                        help="启用比较模式（需同时指定第二个仓库）")

    src_b = parser.add_argument_group("比较模式：第二个仓库（--compare 时使用）")
    bgroup = src_b.add_mutually_exclusive_group()
    bgroup.add_argument("--repo-id-b",   metavar="ID",
                        help="第二个仓库的文件夹名")
    bgroup.add_argument("--repo-path-b", metavar="PATH",
                        help="第二个仓库的完整本地路径")
    bgroup.add_argument("--url-b",       metavar="URL",
                        help="第二个仓库的远程地址")

    args = parser.parse_args()

    if args.compare and not any([args.repo_id_b, args.repo_path_b, args.url_b]):
        parser.error("--compare 需要同时指定第二个仓库（--repo-id-b / --repo-path-b / --url-b）")

    if args.model:
        config.api["model"] = args.model

    repo_path_a, repo_name_a = _resolve_repo_path(args.url, args.repo_path, args.repo_id)
    structure_a, profile_a, level1_map_a, level2_index_a, engine_a = _analyze_one(
        repo_path_a, repo_name_a
    )
    crate_roles_a = detect_crate_roles(str(repo_path_a), profile_a)

    if args.compare:
        repo_path_b, repo_name_b = _resolve_repo_path(
            args.url_b, args.repo_path_b, args.repo_id_b
        )
        structure_b, profile_b, level1_map_b, level2_index_b, engine_b = _analyze_one(
            repo_path_b, repo_name_b
        )
        crate_roles_b = detect_crate_roles(str(repo_path_b), profile_b)

        system_prompt = assemble_system_prompt(
            mode="compare",
            structure=structure_a,   profile=profile_a,   level1_map=level1_map_a,   engine=engine_a,
            structure_b=structure_b, profile_b=profile_b, level1_map_b=level1_map_b, engine_b=engine_b,
            crate_roles=crate_roles_a, crate_roles_b=crate_roles_b,
        )
        user_request = f"请对项目 {repo_name_a} 和 {repo_name_b} 展开完整的技术比较。"
        final_report = agent_run(
            engine_a, system_prompt, user_request,
            repo_path=str(repo_path_a),
            level2_index=level2_index_a,
            profile=profile_a,
            structure=structure_a,
        )
    else:
        system_prompt = assemble_system_prompt(
            mode="analyze",
            structure=structure_a,
            profile=profile_a,
            level1_map=level1_map_a,
            engine=engine_a,
            crate_roles=crate_roles_a,
        )
        user_request = f"请依照系统提示词的格式，对项目 {repo_name_a} 展开完整分析。"
        final_report = agent_run(
            engine_a, system_prompt, user_request,
            repo_path=str(repo_path_a),
            level2_index=level2_index_a,
            profile=profile_a,
            structure=structure_a,
        )

    if args.output:
        Path(args.output).write_text(final_report, encoding="utf-8")
        print(f"\n报告已保存至: {args.output}")
    else:
        print(final_report)
