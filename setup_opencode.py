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

def _make_layer0() -> str:
    _BAR = "━" * 30
    return f"""{_BAR}
【初始化指令（必须第一步执行）】

在执行任何分析前，调用 initialize_analysis 工具获取仓库代码地图：

  initialize_analysis(repo_path="<仓库绝对路径>")

工具返回的代码地图（含仓库结构、子系统文件定位、项目身份信息、引擎信息）
是后续所有工具调用的基础。在获取代码地图之前，不得调用任何其他分析工具。""".strip()


def _build_static_prompt() -> str:
    from prompts import (
        LAYER_1_ROLE,
        LAYER_3_WORKFLOW_ANALYZE,
        LAYER_4_CONSTRAINTS,
        LAYER_4_DEGRADED_ENGINE_EXTRA,
        LAYER_5_FORMAT_ANALYZE,
    )

    _BAR = "━" * 30

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
        _make_layer0(),
        layer3,
        LAYER_4_CONSTRAINTS,
        LAYER_4_DEGRADED_ENGINE_EXTRA,
        LAYER_5_FORMAT_ANALYZE,
    ]
    return "\n\n".join(parts)


def _build_session_prompt(session_type) -> str:
    """为多会话模式构建某个 SessionType 的静态系统提示词。"""
    from prompts import (
        SessionType,
        _SESSION_CONFIG,
        _SESSION_TASK_DESC,
        LAYER_1_ROLE,
        LAYER_4_CONSTRAINTS,
        LAYER_4_DEGRADED_ENGINE_EXTRA,
    )

    _BAR = "━" * 30

    task_desc = _SESSION_TASK_DESC[session_type]
    layer1 = LAYER_1_ROLE.format(task_description=task_desc)
    layer3_text, layer5_text = _SESSION_CONFIG[session_type]
    layer3 = f"{_BAR}\n【分析工作流】\n\n" + layer3_text

    parts = [
        layer1,
        _make_layer0(),
        layer3,
        LAYER_4_CONSTRAINTS,
        LAYER_4_DEGRADED_ENGINE_EXTRA,
        layer5_text,
    ]
    return "\n\n".join(parts)


# 主逻辑

def setup() -> None:
    if not Path(_VENV_PY).exists():
        print(f"[错误] 虚拟环境未找到：{_VENV_PY}")
        print("请先运行：bash setup.sh")
        sys.exit(1)

    import config

    # 模型锁定：本项目固定使用 DeepSeek（deepseek/deepseek-chat）。
    # 用户在 config.toml 中只需配置 key 与 base_url。
    PROVIDER_ID = "deepseek"
    MODEL       = f"{PROVIDER_ID}/deepseek-chat"

    api_key   = config.api.get("key", "")
    base_url  = config.api.get("base_url", "").strip()
    max_steps = config.engine.get("max_steps", 200)

    if not api_key:
        print("[错误] config.toml [api].key 未配置，请填写 DeepSeek API Key。",
              file=sys.stderr)
        sys.exit(1)

    # 将 API 密钥写入 OpenCode 认证文件
    _AUTH_FILE = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        auth = json.loads(_AUTH_FILE.read_text(encoding="utf-8")) if _AUTH_FILE.exists() else {}
    except json.JSONDecodeError:
        auth = {}
    auth[PROVIDER_ID] = {"type": "api", "key": api_key}
    _AUTH_FILE.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[配置] 已写入 API 密钥到 {_AUTH_FILE}（provider: {PROVIDER_ID}）")

    system_prompt = _build_static_prompt()
    print(f"[配置] 静态系统提示词长度：{len(system_prompt)} 字符")

    agent_entry = {
        "model":  MODEL,
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
    existing["model"] = MODEL                                          # 始终更新

    # 若用户配置了自定义 base_url（非 DeepSeek 官方），写入 provider 覆盖，
    # 这样 OpenCode 会用配置的 URL 发请求；官方地址则不需要覆盖。
    if base_url and "api.deepseek.com" not in base_url:
        existing.setdefault("provider", {})[PROVIDER_ID] = {
            "options": {"baseURL": base_url},
        }
        print(f"[配置] 写入自定义 base_url 覆盖：{base_url}")
    elif base_url:
        # 即使是官方地址也写入一份，确保 OpenCode 使用用户期望的版本路径
        existing.setdefault("provider", {})[PROVIDER_ID] = {
            "options": {"baseURL": base_url},
        }

    existing.setdefault("agent", {})["os-kernel-analyzer"] = agent_entry

    # 多会话模式：为每个 SessionType 注册独立 agent
    from prompts import SessionType, SESSION_AGENT_NAMES
    for session_type in SessionType:
        if session_type == SessionType.FULL:
            continue  # FULL 已由 os-kernel-analyzer 覆盖
        agent_name = SESSION_AGENT_NAMES[session_type]
        prompt = _build_session_prompt(session_type)
        existing["agent"][agent_name] = {
            "model": MODEL,
            "system": prompt,
            "permission": {
                "bash":  {"type": "deny"},
                "edit":  {"type": "deny"},
                "write": {"type": "deny"},
            },
        }
        print(f"[配置] 注册 agent：{agent_name}（{len(prompt)} 字符）")

    existing.setdefault("mcp", {})["os-kernel-tools"] = mcp_entry

    _GLOBAL_CFG.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[完成] 全局配置已写入：{_GLOBAL_CFG}")


if __name__ == "__main__":
    setup()
