import json
import sys
from pathlib import Path

# 项目根目录（本脚本所在位置）
_PROJECT_ROOT = Path(__file__).parent.resolve()
_VENV_PY      = str(_PROJECT_ROOT / ".venv" / "bin" / "python")
_MCP_SRV      = str(_PROJECT_ROOT / "mcp_server.py")
_GLOBAL_CFG   = Path.home() / ".config" / "opencode" / "opencode.json"

sys.path.insert(0, str(_PROJECT_ROOT))


# 静态系统提示词（Layer 1 + 初始化指令 + Layer 3 + Layer 4 + Layer 5）

def _build_verifier_prompt() -> str:
    return """你是 OS 内核评审报告的"核验员"。

主 agent 已经完成了对某个仓库的技术评审并写出了报告。
你的任务是独立核验：
  1. 报告中引用的每个文件路径是否真实存在（可被 read_file 打开）
  2. 引用位置的实际内容是否支撑对应的技术结论
  3. 技术性断言是否都有 file:line 引用

【工作流程】

第一步：初始化
  调用 initialize_analysis(repo_path="<用户给出的仓库路径>") 进入仓库上下文。

第二步：提取报告中所有的 file:line 引用
  扫描报告全文，收集所有形如 path/to/file.ext:行号 的引用。
  去重后按文件分组，得到一个"引用文件清单"。

  同时，记录没有任何 file:line 引用但属于技术性断言的结论（B 类）：
    典型特征：直接断言"实现了 X"、"使用了 Y 算法"、"支持 Z 特性"，
    却没有括号里的文件:行号，也没有"未确认"之类的限定语。
    不算 B 类：syscall 覆盖率（来自工具汇总）、置信度说明、
    存疑项声明、"未找到相关实现"之类的否定性陈述。

第三步：逐文件验证路径可达性
  对"引用文件清单"中的每个文件：
    调用 read_file(path=<文件路径>, start_line=1, end_line=5) 做最小读取。
    - 读取成功 → 文件存在，标记为"路径有效"
    - 读取失败或返回错误 → 标记为"路径无效"，记录该文件的所有引用位置
  路径无效的文件，其所有引用结论均自动归入"需要修正的引用"。

第四步：对路径有效的引用逐条核验内容
  对每条 A 类结论（路径有效）：
    用 read_file(path, start_line=max(1,行号-5), end_line=行号+10) 读取上下文
    判断：源码是否支撑报告的断言？
      - 支撑：能看到结论描述的函数定义、字段、调用关系、算法关键字
      - 不支撑：内容无关、行号越界，或源码与结论矛盾
    不支撑时，用 search_code / find_symbol_definition 主动找真正的实现位置

  对 B 类结论：
    用 search_code(pattern="<关键字>") 或 find_symbol_definition("<符号>") 搜索
    找到实现 → 记录真实 file:line，视为"可验证，但报告缺引用"
    找不到 → 视为"无法验证"

第五步：将核验报告写入文件

  用户请求中会给出一个输出路径（形如 "核验报告路径：/tmp/verify_xxx.md"）。
  调用 write_report(content=<核验报告全文>, output_path=<该路径>) 保存结果。

  核验报告 Markdown 格式，严格按以下章节顺序输出（不得省略任何章节）：

  ## 无法访问的引用文件
  列出第三步中"路径无效"的文件，格式：
    - `路径` — 被以下结论引用：[结论摘要列表]
      建议：用 search_code 搜索函数名定位正确路径，或标注"文件不存在"
  若全部文件路径均有效，写"无"。

  ## 需要补充依据的结论
  合并列出以下情况，每条说明原因和补充建议：
  - 路径有效但内容不支撑的结论：说明该位置实际看到了什么，并给出更准确的位置
  - B 类完全没有引用的结论：给出搜索到的实现位置或"搜索无结果"
  若无此类问题，写"无"。

  ## 核验通过的结论
  逐条列出通过的结论，附 1-3 行关键源码摘录。

  ## 总体评价
  2-3 句话：通过了多少条、文件路径问题几处、内容不符问题几处。

【约束】
  - 必须调用 write_report 将核验报告写入用户指定的路径，不得遗漏
  - 不要重写报告正文，只输出核验结果
  - 以你实际读到的源码为准，不信报告的描述
  - A 类结论路径有效且内容吻合 → 直接通过，不需要深挖
  - 对 B 类结论，搜索 1-2 次找不到就记"搜索无结果"，不要反复尝试
""".strip()


def _build_static_prompt() -> str:
    from prompts import (
        LAYER_1_ROLE,
        LAYER_3_WORKFLOW_ANALYZE,
        LAYER_4_CONSTRAINTS,
        LAYER_4_DEGRADED_ENGINE_EXTRA,
        LAYER_5_FORMAT_ANALYZE,
    )

    _BAR = "━" * 30

    layer0 = f"""{_BAR}
【初始化指令（必须第一步执行）】

在执行任何分析前，调用 initialize_analysis 工具获取仓库代码地图：

  initialize_analysis(repo_path="<仓库绝对路径>")

工具返回的代码地图（含仓库结构、子系统文件定位、项目身份信息、引擎信息）
是后续所有工具调用的基础。在获取代码地图之前，不得调用任何其他分析工具。""".strip()

    layer1 = LAYER_1_ROLE.format(
        task_description=(
            "对参赛内核项目进行技术评审，"
            "生成结构化的评审报告。报告面向评审专家阅读，"
            "要求每条技术结论都有代码层面的证据支撑。"
        )
    )

    layer3 = (
        f"{_BAR}\n"
        "【分析工作流】\n\n"
        + LAYER_3_WORKFLOW_ANALYZE
    )

    parts = [
        layer1,
        layer0,
        layer3,
        LAYER_4_CONSTRAINTS,
        LAYER_4_DEGRADED_ENGINE_EXTRA,
        LAYER_5_FORMAT_ANALYZE,
    ]
    return "\n\n".join(parts)


# 主逻辑

def setup() -> None:
    if not Path(_VENV_PY).exists():
        print(f"[错误] 虚拟环境未找到：{_VENV_PY}")
        print("请先运行：bash setup.sh")
        sys.exit(1)

    import config
    model     = config.api["model"]           # e.g. "deepseek/deepseek-chat"
    api_key   = config.api.get("key", "")
    max_steps = config.engine.get("max_steps", 200)

    # 将 API 密钥写入 OpenCode 认证文件（按 provider 提取，如 "deepseek"）
    if api_key:
        _AUTH_FILE = Path.home() / ".local" / "share" / "opencode" / "auth.json"
        _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            auth = json.loads(_AUTH_FILE.read_text(encoding="utf-8")) if _AUTH_FILE.exists() else {}
        except json.JSONDecodeError:
            auth = {}
        provider_id = model.split("/")[0] if "/" in model else model
        auth[provider_id] = {"type": "api", "key": api_key}
        _AUTH_FILE.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[配置] 已写入 API 密钥到 {_AUTH_FILE}（provider: {provider_id}）")

    system_prompt = _build_static_prompt()
    print(f"[配置] 静态系统提示词长度：{len(system_prompt)} 字符")

    agent_entry = {
        "model":  model,
        "system": system_prompt,
        "permission": {
            "bash":  {"type": "deny"},
            "edit":  {"type": "deny"},
            "write": {"type": "deny"},
        },
    }

    mcp_entry = {
        "type":    "local",
        "enabled": True,
        "command": [_VENV_PY, _MCP_SRV, "--max-steps", str(max_steps)],
    }

    # 读取或初始化全局配置
    _GLOBAL_CFG.parent.mkdir(parents=True, exist_ok=True)
    if _GLOBAL_CFG.exists():
        try:
            existing = json.loads(_GLOBAL_CFG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        print(f"[配置] 读取已有全局配置：{_GLOBAL_CFG}")
    else:
        existing = {}
        print(f"[配置] 创建全局配置：{_GLOBAL_CFG}")

    # 更新我们的 agent 和 MCP 条目（model 始终更新，不覆盖其他用户配置）
    existing.setdefault("$schema", "https://opencode.ai/config.json")
    existing["model"] = model                                          # 始终更新
    verifier_entry = {
        "model":  model,
        "system": _build_verifier_prompt(),
        "permission": {
            "bash":  {"type": "deny"},
            "edit":  {"type": "deny"},
            "write": {"type": "deny"},
        },
    }

    existing.setdefault("agent", {})["os-kernel-analyzer"] = agent_entry
    existing.setdefault("agent", {})["os-kernel-verifier"] = verifier_entry
    existing.setdefault("mcp", {})["os-kernel-tools"] = mcp_entry

    _GLOBAL_CFG.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[完成] 全局配置已写入：{_GLOBAL_CFG}")
    print()
    print("现在可以直接使用 OpenCode：")
    print('  opencode run --agent os-kernel-analyzer "分析 /path/to/repo"')
    print()
    print("或使用 agent.py 封装（支持 --repo-id / --url 等参数）：")
    print("  python agent.py --repo-id REPO_NAME")
    print("  python agent.py --url https://gitlab.example.com/repo.git")


if __name__ == "__main__":
    setup()
