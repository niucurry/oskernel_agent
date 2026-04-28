import json
from pathlib import Path
from openai import OpenAI

import config
from engines.base import AnalysisEngine
from parser.code_parser import (
    build_repo_profile, build_profile,
    find_source_roots, classify_files_by_content,
    detect_naming_style, find_doc_files,
)
from parser.os_tools import build_repo_map

client = OpenAI(api_key=config.api["key"], base_url=config.api["base_url"])

#大模型可见的工具 Schema

tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "get_call_chain",
            "description": "从入口函数展开调用树（最多指定深度），用于分析内核函数的执行逻辑和调用链路。",
            "parameters": {
                "type": "object",
                "properties": {
                    "function_name": {"type": "string", "description": "C/Rust 函数名"},
                    "max_depth":     {"type": "integer", "description": "调用树层数，默认 3"}
                },
                "required": ["function_name"]
            }
        }
    },
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
                "required": ["struct_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": "查找所有调用了指定函数或使用了指定符号的位置，返回调用方列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol_name": {"type": "string", "description": "函数名或结构体名"}
                },
                "required": ["symbol_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "go_to_definition",
            "description": "查找符号的定义位置，返回完整源码和所在文件路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol_name": {"type": "string", "description": "函数名或结构体名"}
                },
                "required": ["symbol_name"]
            }
        }
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
_MAX_STEPS = 100                 # 单次 agent_run 最大工具调用步数


def _is_not_found(result) -> bool:
    if result is None:
        return True
    if isinstance(result, dict):
        if not result:           # 空 dict
            return True
        values = list(result.values())
        return bool(values) and isinstance(values[0], dict) and values[0].get("__not_found__")
    if isinstance(result, (list, str)) and not result:  # 空 list / 空字符串
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


def agent_run(engine: AnalysisEngine, system_prompt: str, user_prompt: str) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]

    info = engine.get_engine_info()
    print(f"\n Agent 启动（引擎：{info['engine']}，路径{info['path']}，精度：{info['precision']}）")

    step = 1
    failed_symbols: set[str] = set()          # 所有已查询且未找到的符号名
    queried_cache: set[tuple] = set()         # (tool_name, symbol) 去重缓存
    hallucination_count = 0                   # 本轮幻觉扩展命中次数
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
            messages.append({
                "role":    "user",
                "content": "这些符号在代码库中均不存在，请勿继续猜测变体名称。请根据已收集到的信息直接输出最终分析报告，不要再调用任何工具。",
            })
            force_finish = False
            hallucination_count = 0

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

            queried_symbol = (args.get("function_name")
                              or args.get("symbol_name")
                              or args.get("struct_name"))

            # 重复调用检测：完全相同的 (工具, 符号) 组合已查询过
            cache_key = (function_name, queried_symbol)
            if cache_key in queried_cache:
                print(f"  [保护] 重复调用跳过：{function_name}({queried_symbol!r}) 已查询过")
                messages.append({
                    "tool_call_id": tool_call.id,
                    "role":         "tool",
                    "name":         function_name,
                    "content":      f"符号 {queried_symbol!r} 已查询过，结果同前，请勿重复调用。",
                })
                step += 1
                continue

            queried_cache.add(cache_key)

            # 幻觉扩展检测：当前符号是已失败符号集合中任意一个的后缀变体
            base = _is_suffix_of_any(failed_symbols, queried_symbol)
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

            if function_name == "get_call_chain":
                result = engine.get_call_chain(args["function_name"], args.get("max_depth", 3))
            elif function_name == "get_struct_fields":
                result = engine.get_struct_fields(args["struct_name"])
            elif function_name == "find_references":
                result = engine.find_references(args["symbol_name"])
            elif function_name == "go_to_definition":
                result = engine.go_to_definition(args["symbol_name"])
            else:
                result = f"Error: 未知工具 {function_name}"

            if _is_not_found(result):
                if queried_symbol:
                    failed_symbols.add(queried_symbol)
                result_str = f"符号 {queried_symbol!r} 在代码库中不存在。"
            else:
                result_str = str(result)

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

    return {
        "doc_files":           find_doc_files(repo_path),
        "subsystem_locations": classify_files_by_content(str(repo_path), source_roots_rel),
        "structure_depth":     depth_label,
        "source_roots":        source_roots_abs,
        "naming_style":        detect_naming_style(repo_path),
    }


if __name__ == "__main__":
    repo_id_test = config.target["repo_id"]
    repo_path    = Path(config.data["repos_dir"]) / repo_id_test

    #静态结构分析（注入 System Prompt)
    print("正在执行静态结构分析...")
    repo_profile_text = build_repo_profile(repo_path)
    print(repo_profile_text)

    #构建两级索引
    structure        = _build_structure(repo_path)
    profile          = build_profile(str(repo_path), structure)
    level1_map, level2_index = build_repo_map(str(repo_path), structure, profile)

    #按优先级选择引擎
    engine      = select_engine(str(repo_path), profile, level2_index)
    engine_info = engine.get_engine_info()

    limitations_text = (
        "引擎限制（分析结论请结合精度评估）：\n"
        + "\n".join(f"- {l}" for l in engine_info.get("limitations", []))
    ) if engine_info.get("limitations") else ""

    #Agent A：完整性与原创性评估
    agent_a_system_prompt = (
        repo_profile_text + "\n\n" + level1_map + f"""

---
你是一个严格的操作系统课程项目审查专家，负责对学生提交的 OS 内核实现进行完整性与原创性评估。
项目 ID：{repo_id_test}
当前分析引擎：{engine_info['engine']}（{engine_info['path']}，精度：{engine_info['precision']}）
{limitations_text}

上方【仓库结构探索结果】和【仓库结构地图】由确定性静态分析工具生成，是已知事实，不得质疑或忽略。

## 工具使用规则

你有四个工具：
- `get_call_chain(function_name, max_depth)`：从入口函数展开调用树
- `get_struct_fields(struct_name)`：获取结构体完整字段列表
- `find_references(symbol_name)`：查找所有调用该符号的位置
- `go_to_definition(symbol_name)`：查找符号定义，返回完整源码

**必须遵守：**
1. 只分析上方"子系统文件定位"中标注为已找到的子系统，未找到的直接标注"未实现"，不得调用工具猜测
2. 查询函数名时根据"命名风格"适配符号名：
   - snake_case → `schedule` / `task_struct` / `sys_fork` / `page_fault` 等
   - CamelCase  → `run_tasks` / `TaskControlBlock` / `MemorySet` / `TrapContext` 等
   - mixed      → 两种形式各尝试一次
3. 每个已识别子系统至少调用一次 `get_call_chain` 核实其入口函数
4. 调用链中出现结构体名（首字母大写或含 `_t` 后缀）时，必须调用 `get_struct_fields` 核实
5. 工具返回"未找到"时，区分两种情况：
   - 若静态分析已在该子系统发现了相关源文件，则注明"符号查询受引擎限制，无法核实"，**不得**判定为"未实现"
   - 若静态分析也未发现任何相关文件，才可判定为"未实现"

## 强制输出格式（Markdown）

### 仓库概览
- 命名风格 / 目录风格 / 已识别子系统列表（直接引用静态分析结论，不改写）

### 各子系统分析
对每个已识别子系统，依次输出：

#### [子系统名]
- **入口函数调用链**：`函数名 → 子函数1, 子函数2, ...`（来自 get_call_chain 结果）
- **核心数据结构**：结构体名 + 关键字段摘要（来自 get_struct_fields 结果；若无结构体则注明）
- **实现完整度**：`完整` / `基本完整` / `欠缺` — 一句话说明判断依据

### 文档质量
- 逐一列出静态分析找到的文档文件及类型；若无文档，注明影响

### 异常说明
- 逐条回应上方"异常警告"，说明本次分析如何处置该异常
"""
    )

    user_request = f"请依照系统提示词的格式，对项目 {repo_id_test} 展开完整分析。"
    final_report = agent_run(engine, agent_a_system_prompt, user_request)
    print(final_report)

    # ── Agent B：查重引擎（需要两个仓库各自的引擎实例）──
    # 使用方式：分别为 repo_new 和 repo_old 调用 select_engine，
    # 将两个引擎的查询结果拼入 system_prompt 后传给 agent_run
    agent_b_system_prompt = """
你是一个代码查重与创新点评估专家。我将给你提供两个项目的调用链和结构体对比数据。

## 查重策略

1. 选取以下核心函数作为比对锚点（依次尝试，直到在两个项目中都找到为止）：
   - 进程调度：`schedule` / `run_tasks` / `task_switch`
   - 陷入处理：`trap_handler` / `handle_trap` / `__alltraps`
   - 内存分配：`page_alloc` / `alloc_frame` / `frame_alloc`
2. 对每个锚点函数，分别对 Repo_New 和 Repo_Old 调用 `get_call_chain`，记录完整调用集合
3. 若调用链中出现相同结构体名，用 `get_struct_fields` 对两个项目各查一次，对比字段定义
4. 相似度判定标准：
   - 调用集合重合度 ≥ 80% → 高度相似（疑似抄袭）
   - 重合度 50%–80%，函数名不同但结构一致 → 疑似改名移植
   - 重合度 < 50% 且存在新增调用路径 → 记录为创新点

## 强制输出格式（Markdown）

### 整体相似度评估
- 相似度等级：高 / 中 / 低，附简要说明

### 逐函数调用链对比
| 锚点函数 | Repo_New 调用集合 | Repo_Old 调用集合 | 重合度 | 结论 |
|---------|-----------------|-----------------|--------|------|

### 创新点（Repo_New 独有的实质性差异）
- ...

### 查重疑点（高度相似或改名移植的证据）
- ...
"""
