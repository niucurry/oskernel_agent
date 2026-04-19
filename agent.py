import json
import os
from openai import OpenAI

# LLM API 配置
API_KEY = "sk-e81919dd75ff4c7c88161485f82a76c9"
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODEL_NAME = "qwen3.5-35b-a3b"

# LLM 客户端
class LLMClient:
    def __init__(self):
        self.client = OpenAI(
            api_key=API_KEY, 
            base_url=BASE_URL
        )
        self.model = MODEL_NAME

    def ask_agent(self, system_prompt, user_input):
        # 调用大模型 API
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input},
            ],
            temperature=0.1, 
            max_tokens=2048
        )
        return response.choices[0].message.content

def run_demo():
    # 加载项目元数据
    meta_file = "./data/metadata/all_repos_info.json"
    if not os.path.exists(meta_file):
        print(" 找不到元数据文件")
        return

    with open(meta_file, 'r', encoding='utf-8') as f:
        repos = json.load(f)

    if not repos:
        print("数据集为空！")
        return

    # 取第一个项目进行分析
    test_repo = repos[0]
    system_prompt = "你是一个操作系统专家。我会给你一个 GitLab 项目的描述和最近的 Commit 记录，请你总结该项目的主要技术栈（如：RISC-V, Rust, 微内核）以及它的开发阶段。"
    recent_logs = test_repo.get("recent_commits", [])[:5] 
    
    # 构造请求
    user_input = f"""
    项目名：{test_repo['name']}
    描述：{test_repo['description']}
    最近日志：{recent_logs}
    """

    # 发送请求并输出结果
    llm = LLMClient()
    result = llm.ask_agent(system_prompt, user_input)
    print(result)

if __name__ == "__main__":
    run_demo()