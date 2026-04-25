import json
from openai import OpenAI
from os_tools import OSCodeTools

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
    
    agent_a_system_prompt = f"""
    你是一个严格的操作系统源码审查专家。现在需要对项目 {repo_id_test} 撰写结构化描述文档。
    
    执行策略：
    1. 你必须先调用 get_callees 工具，查看内核主调度函数 schedule 的调用链路。
    2. 如果链路中涉及了特定的结构体（如 task_struct 或 pcb），你必须调用 get_struct_definition 核实其定义。
    3. 只有经过代码核实的功能才能写入报告。坚决杜绝幻觉。
    
    强制输出 Markdown 模板：
    ### 1. 核心模块分析 - 进程调度
    - **调度算法推断**：(基于调用链路说明推断出的调度策略)
    - **关键函数调用链**：(列出 schedule 函数调用的子函数)
    - **核心数据结构**：(结合查到的 struct 源码进行简要分析)
    """
    
    user_request = f"请开始分析项目 {repo_id_test} 的进程调度模块。"
    
    # 运行 Agent A
    final_report = agent_run(agent_a_system_prompt, user_request)
    print(final_report)
    
    # ================= Agent B (查重引擎) 提示词框架示例 =================
    # 实际运行时，传入两个 repo_id 即可让其对比两者的调用链。
    agent_b_system_prompt = """
    你是一个代码查重与创新点评估专家。我将给你提供两个项目的 ID（Repo_New 和 Repo_Old）。
    请你调用工具分别获取两个项目中核心函数（如 trap 陷入处理函数）的调用链路。
    判断 Repo_New 的底层逻辑结构是否与 Repo_Old 完全一致，或者指明其在调用链上发生的实质性创新变更。
    """