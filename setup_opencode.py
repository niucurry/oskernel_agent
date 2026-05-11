"""
OpenCode 全局配置一次性注册脚本。

将 os-kernel-analyzer agent 和 os-kernel-tools MCP server 写入
~/.config/opencode/opencode.json，使 OpenCode 在任意目录均可直接使用：

  opencode run --agent os-kernel-analyzer "分析 /path/to/repo"
  opencode run --agent os-kernel-analyzer "比较 /path/to/repo_a 和 /path/to/repo_b"

或通过 agent.py 封装调用（自动解析 --repo-id / --url 等参数）：

  python agent.py --repo-id REPO_NAME
  python agent.py --url https://gitlab.example.com/repo.git

注意：只需运行一次。重复运行会更新 system prompt 和 MCP 命令路径，不会破坏其他配置。
"""

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

def _build_static_prompt() -> str:
    from prompts import (
        LAYER_1_ROLE,
        LAYER_3_WORKFLOW_ANALYZE,
        LAYER_3_WORKFLOW_COMPARE,
        LAYER_4_CONSTRAINTS,
        LAYER_4_DEGRADED_ENGINE_EXTRA,
        LAYER_5_FORMAT_ANALYZE,
        LAYER_5_FORMAT_COMPARE,
    )

    _BAR = "━" * 30

    layer0 = f"""{_BAR}
【初始化指令（必须第一步执行）】

在执行任何分析前，调用 initialize_analysis 工具获取仓库代码地图：

单仓库分析模式：
  initialize_analysis(repo_path="<仓库绝对路径>")

比较模式（两个仓库）：
  initialize_analysis(repo_path="<仓库A路径>", label="a")
  initialize_analysis(repo_path="<仓库B路径>", label="b")

工具返回的代码地图（含仓库结构、子系统文件定位、项目身份信息、引擎信息）
是后续所有工具调用的基础。在获取代码地图之前，不得调用任何其他分析工具。""".strip()

    layer1 = LAYER_1_ROLE.format(
        task_description=(
            "根据用户请求，对一个或两个参赛内核项目进行技术评审，"
            "生成结构化的评审报告或技术比较文档。报告面向评审专家阅读，"
            "要求每条技术结论都有代码层面的证据支撑。"
        )
    )

    layer3_analyze = (
        f"{_BAR}\n"
        "【单仓库分析工作流】（当用户请求分析单个仓库时使用）\n\n"
        + LAYER_3_WORKFLOW_ANALYZE
    )

    layer3_compare = (
        f"{_BAR}\n"
        "【比较模式工作流】（当用户请求比较两个仓库时使用）\n\n"
        + LAYER_3_WORKFLOW_COMPARE.format(repo_a_name="仓库A", repo_b_name="仓库B")
    )

    parts = [
        layer1,
        layer0,
        layer3_analyze,
        layer3_compare,
        LAYER_4_CONSTRAINTS,
        LAYER_4_DEGRADED_ENGINE_EXTRA,
        LAYER_5_FORMAT_ANALYZE,
        LAYER_5_FORMAT_COMPARE,
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
    existing.setdefault("agent", {})["os-kernel-analyzer"] = agent_entry
    existing.setdefault("mcp", {})["os-kernel-tools"] = mcp_entry

    _GLOBAL_CFG.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[完成] 全局配置已写入：{_GLOBAL_CFG}")
    print()
    print("现在可以直接使用 OpenCode：")
    print(f'  opencode run --agent os-kernel-analyzer "分析 /path/to/repo"')
    print()
    print("或使用 agent.py 封装（支持 --repo-id / --url 等参数）：")
    print("  python agent.py --repo-id REPO_NAME")
    print("  python agent.py --url https://gitlab.example.com/repo.git")


if __name__ == "__main__":
    setup()
