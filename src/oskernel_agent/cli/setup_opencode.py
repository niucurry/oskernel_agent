"""注册本项目的 agent 与 MCP 工具到 OpenCode 全局配置。"""

import json
import sys
from pathlib import Path

# 项目根目录：src/oskernel_agent/cli/setup_opencode.py → ../../../
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
# 兼容 Linux (.venv/bin) 和 Windows (.venv/Scripts)
_VENV_PY_CANDIDATES = [
    _PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
    _PROJECT_ROOT / ".venv" / "Scripts" / "python",
    _PROJECT_ROOT / ".venv" / "bin" / "python",
]
_VENV_PY = next((str(p) for p in _VENV_PY_CANDIDATES if p.exists()),
                str(_PROJECT_ROOT / ".venv" / "bin" / "python"))
# opencode 全局配置路径：优先 opencode.jsonc（opencode npm 版本使用），
# 降级到 opencode.json（Linux 版本），都在 ~/.config/opencode/ 下。
_CFG_DIR = Path.home() / ".config" / "opencode"
_GLOBAL_CFG = (
    _CFG_DIR / "opencode.jsonc"
    if (_CFG_DIR / "opencode.jsonc").exists()
    else _CFG_DIR / "opencode.json"
)


# 共享：会话系统提示词构造（树状管道：3 个产出会话 + 1 个修复兜底）

def _build_session_prompt(session_type) -> str:
    """为树状管道某个 SessionType 构建静态系统提示词。"""
    from ..prompts.builder import (
        SessionType,
        _SESSION_CONFIG,
        LAYER_1_ROLE,
        LAYER_2_CONSTRAINTS,
        get_task_desc,
    )
    from ..prompts import skills

    _BAR = "━" * 30

    task_desc = get_task_desc(session_type)
    layer1 = LAYER_1_ROLE.format(task_description=task_desc)
    workflow_text, format_text = _SESSION_CONFIG[session_type]

    parts = [layer1, LAYER_2_CONSTRAINTS]
    if workflow_text:
        parts.append(f"{_BAR}\n【工作流】\n\n" + workflow_text)
    if format_text:
        parts.append(format_text)
    catalog = skills.build_catalog(session_type)
    if catalog:
        parts.append(catalog)
    return "\n\n".join(parts)


# os-kernel-plagiarism agent 系统提示词

_PLAGIARISM_SYSTEM_PROMPT = """\
你是代码原创性分析助手，专注于**语义级**（功能层面）的对比分析。

## 核心任务
分析 OS 内核新作品的功能借鉴情况，生成 HTML 分析报告片段。

## 工具使用
- **首选** bash 工具读取 JSON 数据文件（用 python -c 或 cat）
- **首选** write 工具写出 HTML 文件到指定路径
- **不要**调用 initialize_analysis（会超时）
- 可选：用 bash 或 read 工具读取新作品源文件以获取更多上下文

## 分析维度
对每个子模块，分析：
1. **功能借鉴**：借鉴了哪些算法/机制/数据结构（语义层面）
2. **借鉴程度**：直接复制 / 变量改名 / 结构保留逻辑改写 / 受启发重新实现
3. **代码证据**：引用 `文件:行号` 格式

## 输出格式
- 直接写 HTML 标签，不要写 Markdown
- 每个子模块用 `<section data-module="mod_tag">` 包裹
- 用 `<h3>`、`<p>`、`<ul>`、`<li>` 语义标签
- 文件引用写纯文本 `path:line`（如 `os/src/task/mod.rs:125`）
- **必须用 write 工具将 HTML 写入消息指定的 output_path**

## 示例
```html
<section data-module="sched">
  <h3>进程调度 (sched)</h3>
  <p>任务切换机制（os/src/task/mod.rs:132）与 rcore-tutorial-v3 高度一致，均采用协作式调度。</p>
  <ul>
    <li><strong>功能借鉴</strong>：任务队列管理、上下文切换（TaskContext 结构）</li>
    <li><strong>借鉴程度</strong>：结构保留，变量部分改名</li>
    <li><strong>证据</strong>：os/src/task/mod.rs:120-145</li>
  </ul>
</section>
```
"""


# 主逻辑

def setup() -> None:
    if not Path(_VENV_PY).exists():
        print(f"[错误] 虚拟环境未找到：{_VENV_PY}")
        print("请先运行：bash setup.sh")
        sys.exit(1)

    from .. import config

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

    # MCP server 通过 `python -m oskernel_agent.cli.mcp_server` 启动，
    # 需要项目以可编辑模式安装（pip install -e .），setup.sh 已自动处理。
    mcp_entry = {
        "type":    "local",
        "enabled": True,
        "command": [
            _VENV_PY, "-m", "oskernel_agent.cli.mcp_server",
            "--max-steps", str(max_steps),
        ],
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

    # 清理：已停用的旧 agent
    existing.setdefault("agent", {})
    for stale in (
        "os-kernel-analyzer",
        "os-kernel-overview",
        "os-kernel-subsys-core",
        "os-kernel-subsys-infra",
        "os-kernel-originality",
        "os-kernel-doc-quality",
        "os-kernel-format-check",
        "os-kernel-leaf",
        "os-kernel-dir",   # 目录式分层已废弃，改为按 OS 子系统分层
    ):
        existing["agent"].pop(stale, None)

    # 树状管道：注册 3 个产出会话 + 1 个 JSON 修复兜底
    from ..prompts.builder import SessionType, SESSION_AGENT_NAMES
    for session_type in SessionType:
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

    # 查重对比管道：注册 os-kernel-plagiarism agent
    plagiarism_system = _PLAGIARISM_SYSTEM_PROMPT
    existing["agent"]["os-kernel-plagiarism"] = {
        "model": MODEL,
        "system": plagiarism_system,
        "permission": {
            "bash":  {"type": "deny"},
            "edit":  {"type": "deny"},
            "write": {"type": "deny"},
        },
    }
    print(f"[配置] 注册 agent：os-kernel-plagiarism（{len(plagiarism_system)} 字符）")

    existing.setdefault("mcp", {})["os-kernel-tools"] = mcp_entry

    _GLOBAL_CFG.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[完成] 全局配置已写入：{_GLOBAL_CFG}")


if __name__ == "__main__":
    setup()
