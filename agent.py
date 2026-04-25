import json
from pathlib import Path
from openai import OpenAI
from os_tools import OSCodeTools
from code_parser import build_repo_profile

API_KEY = "sk-8baedbf35e474021a8d923eac557aca8"
BASE_URL = "https://api.deepseek.com/v1"
MODEL_NAME = "deepseek-chat"

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
tools_executor = OSCodeTools()

# 1. 定义大模型可见的工具 Schema
tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "get_callees",
            "description": "获取指定函数内部调用的所有其他函数名，用于分析内核函数的底层执行逻辑和调用图谱。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "项目仓库ID"},
                    "function_name": {"type": "string", "description": "C/Rust函数名"}
                },
                "required": ["repo_id", "function_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_struct_definition",
            "description": "获取操作系统中核心数据结构（Struct）的完整代码定义，用于核实内存结构或寄存器设计。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "项目仓库ID"},
                    "struct_name": {"type": "string", "description": "结构体名称"}
                },
                "required": ["repo_id", "struct_name"]
            }
        }
    }
]

# 2. 核心执行引擎
def agent_run(system_prompt: str, user_prompt: str):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    
    print("\n Agent 启动，开始分析...")
    
    #  ReAct
    step = 1
    while True:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tools=tools_schema,
            tool_choice="auto",
            temperature=0.1 # 严谨的分析任务需要低随机性
        )
        
        response_message = response.choices[0].message
        
        # 退出条件：模型没有再调用工具，直接输出了最终报告
        if not response_message.tool_calls:
            print("\n Agent 分析完毕，输出最终报告：\n")
            return response_message.content

        # 模型决定调用工具，将模型的决策加入历史
        messages.append(response_message)
        
        # 解析并执行工具
        for tool_call in response_message.tool_calls:
            function_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)
            
            print(f"  [步骤 {step}]  决定调用工具: {function_name} -> 参数: {args}")
            
            # 路由到本地 Python 函数执行 SQL 查询
            if function_name == "get_callees":
                result = tools_executor.get_callees(args["repo_id"], args["function_name"])
            elif function_name == "get_struct_definition":
                result = tools_executor.get_struct_definition(args["repo_id"], args["struct_name"])
            else:
                result = "Error: Tool not found."
                
            # 将工具执行结果 (即 SQL 查出的代码或列表) 返回给大模型
            messages.append({
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": function_name,
                "content": str(result)
            })
            step += 1


if __name__ == "__main__":
    repo_id_test = "T202510008995695-2259"
    repo_path = Path(f"./data/historical_repos/{repo_id_test}")

    print(" 正在执行静态结构分析...")
    repo_profile = build_repo_profile(repo_path)
    print(repo_profile)

    agent_a_system_prompt = repo_profile + f"""

---
你是一个严格的操作系统课程项目审查专家，负责对学生提交的 OS 内核实现进行完整性与原创性评估。
项目 ID：{repo_id_test}

上方【仓库结构探索结果】由确定性静态分析工具生成，是已知事实，不得质疑或忽略。

## 工具使用规则

你有两个工具：
- `get_callees(repo_id, function_name)`：返回某函数直接调用的所有函数名
- `get_struct_definition(repo_id, struct_name)`：返回某结构体的完整源码定义

**必须遵守：**
1. 只分析上方"子系统文件定位"中标注为已找到的子系统，未找到的子系统直接标注"未实现"，不得调用工具猜测
2. 查询函数名时，根据上方"命名风格"适配符号名：
   - snake_case → 查 `schedule` / `task_struct` / `sys_fork` / `page_fault` 等
   - CamelCase  → 查 `run_tasks` / `TaskControlBlock` / `MemorySet` / `TrapContext` 等
   - mixed      → 两种形式各尝试一次
3. 每个已识别子系统至少调用一次 `get_callees` 核实其入口函数
4. 调用链中出现结构体名（首字母大写或含 `_t` 后缀）时，必须调用 `get_struct_definition` 核实
5. 工具返回"未找到"时，如实记录该函数缺失，不得替换为推测内容

## 强制输出格式（Markdown）

### 仓库概览
- 命名风格 / 目录风格 / 已识别子系统列表（直接引用静态分析结论，不改写）

### 各子系统分析
对每个已识别子系统，依次输出：

#### [子系统名]
- **入口函数调用链**：`函数名 → 子函数1, 子函数2, ...`（来自 get_callees 结果）
- **核心数据结构**：结构体名 + 关键字段摘要（来自 get_struct_definition 结果；若无结构体则注明）
- **实现完整度**：`完整` / `基本完整` / `欠缺` — 一句话说明判断依据

### 文档质量
- 逐一列出静态分析找到的文档文件及类型；若无文档，注明影响

### 异常说明
- 逐条回应上方"异常警告"，说明本次分析如何处置该异常
"""

    user_request = f"请依照系统提示词的格式，对项目 {repo_id_test} 展开完整分析。"

    # 运行 Agent A
    final_report = agent_run(agent_a_system_prompt, user_request)
    print(final_report)

    # Agent B (查重引擎)
    # 传入两个 repo_id 即可驱动对比分析。
    agent_b_system_prompt = """
你是一个代码查重与创新点评估专家。我将给你提供两个项目的 ID：Repo_New 和 Repo_Old。

## 查重策略

1. 选取以下核心函数作为比对锚点（依次尝试，直到在两个项目中都找到为止）：
   - 进程调度：`schedule` / `run_tasks` / `task_switch`
   - 陷入处理：`trap_handler` / `handle_trap` / `__alltraps`
   - 内存分配：`page_alloc` / `alloc_frame` / `frame_alloc`
2. 对每个锚点函数，分别对 Repo_New 和 Repo_Old 调用 `get_callees`，记录完整调用集合
3. 若调用链中出现相同结构体名，用 `get_struct_definition` 对两个项目各查一次，对比字段定义
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