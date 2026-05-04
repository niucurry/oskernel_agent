"""
MCP 工具注册表：定义 LLM 可见的工具 Schema，并提供格式转换工具。

工具列表：
  T1  read_file               读取文件内容（带行号，支持范围截取）
  T2  find_symbol_definition  查找符号定义（函数 / 结构体 / 类型）
  T3  find_symbol_references  查找符号的所有引用位置
  T4  list_implemented_syscalls  扫描已实现的 syscall 并计算覆盖率
  T5  get_subsystem_call_chain   从入口函数展开调用树
  T6  compare_with_reference_os  与参考 OS 进行函数级相似度比对
"""


def get_tool_definitions() -> list[dict]:
    """返回 MCP 风格工具定义列表，可直接注入 Anthropic API 的 tools 参数。"""
    return [
        # T1
        {
            "name": "read_file",
            "description": (
                "读取指定文件的源代码内容。"
                "可指定行号范围，避免一次性加载整个大文件。"
                "返回带行号的代码文本。"
                "何时使用：当你需要查看某个文件的完整内容或某个特定区域的代码时。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件的相对路径（相对于仓库根目录），如 os/src/task/mod.rs",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（可选，从1开始）",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（可选）",
                    },
                },
                "required": ["path"],
            },
        },
        # T2
        {
            "name": "find_symbol_definition",
            "description": (
                "查找一个函数、结构体或类型的跨文件定义位置，并返回其完整源码。"
                "底层使用 LSP 或 tree-sitter 引擎进行精确查找。"
                "何时使用：当你在地图中看到一个符号名（如 do_fork, TaskControlBlock），"
                "想了解它的具体实现细节时。"
                "注意：如果你没有调用此工具查询某个符号，就不应该在报告中描述它的实现细节。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol_name": {
                        "type": "string",
                        "description": "符号名称，如 do_fork, MemorySet, PAGE_SIZE",
                    },
                    "context_file": {
                        "type": "string",
                        "description": "（可选）当前分析的文件路径，用于辅助消歧",
                    },
                },
                "required": ["symbol_name"],
            },
        },
        # T3
        {
            "name": "find_symbol_references",
            "description": (
                "查找所有调用或引用了某个符号的位置。"
                "返回调用者函数名、文件和行号。"
                "何时使用：当你想知道一个函数被哪些地方调用（分析调用链），"
                "或想了解一个结构体在哪些地方被使用时。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol_name": {
                        "type": "string",
                        "description": "要查找引用的符号名称",
                    },
                },
                "required": ["symbol_name"],
            },
        },
        # T4
        {
            "name": "list_implemented_syscalls",
            "description": (
                "扫描仓库源码，列出所有已实现的系统调用函数，并与标准 Linux syscall 集合比对，"
                "计算覆盖率。"
                "何时使用：在分析的早期阶段调用一次，获取客观的功能完整性数据。"
                "此工具的结果是评审报告中'Syscall 实现情况'章节的事实依据。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        # T5
        {
            "name": "get_subsystem_call_chain",
            "description": (
                "从一个入口函数出发，展开调用树到指定深度，展示算法的完整执行路径。"
                "何时使用：当你需要回答'fork 是怎么实现的'、'内存分配的流程是什么'这类问题时，"
                "用此工具获取可视化的调用链。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "entry_function": {
                        "type": "string",
                        "description": "入口函数名，如 sys_fork, alloc_pages, schedule",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "展开深度（默认3层，最大5层）",
                        "default": 3,
                    },
                },
                "required": ["entry_function"],
            },
        },
        # T6
        {
            "name": "compare_with_reference_os",
            "description": (
                "将当前仓库的代码与历史参考 OS（rCore-Tutorial / xv6 / uCore）进行函数级相似度比对。"
                "返回高度相似的函数（疑似直接继承）、有修改的函数、以及当前仓库独有的函数（创新点候选）。"
                "何时使用：在分析的中后期调用，用于生成报告中的'原创性分析'和'创新点'章节。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "reference_name": {
                        "type": "string",
                        "description": "参考 OS 名称",
                        "enum": [
                            "rcore-tutorial-v3",
                            "rcore-tutorial-v2",
                            "xv6-riscv",
                            "ucore",
                        ],
                    },
                },
                "required": ["reference_name"],
            },
        },
    ]


def mcp_to_openai_schema(tools: list[dict]) -> list[dict]:
    """将 MCP 风格工具定义转换为 OpenAI function calling 格式。

    MCP:    {"name": ..., "description": ..., "input_schema": {...}}
    OpenAI: {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    """
    result = []
    for t in tools:
        func: dict = {
            "name": t["name"],
            "description": t["description"],
            "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
        }
        result.append({"type": "function", "function": func})
    return result
