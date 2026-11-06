"""Reference checking must work in the same uninitialized fast mode as reads."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _check_refs(repo_path, content):
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["OSKERNEL_AGENT_REPO_PATH"] = str(repo_path) if repo_path else ""
    env["PYTHONPATH"] = str(source_root) + os.pathsep + env.get("PYTHONPATH", "")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "oskernel_agent.cli.mcp_server", "--max-steps", "1"],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()  # MCP handshake, no initialize_analysis.
            if repo_path:
                await client.call_tool("analyze_subtree", {"subtree_path": ""})
            response = await client.call_tool("validate_refs", {"content": content})
            return "\n".join(part.text for part in response.content if hasattr(part, "text"))


def test_fast_mode_checks_real_and_missing_references_after_analysis_budget(tmp_path):
    source = tmp_path / "kernel" / "ipc.c"
    source.parent.mkdir()
    source.write_text("void send(void) {}\n")
    # One analysis query has exhausted the configured allowance. Reference
    # checks still work and cannot require the forbidden analysis initializer.
    result = asyncio.run(_check_refs(tmp_path, "kernel/ipc.c:1 missing.c:8"))
    assert "1 个有效" in result and "1 个无法在磁盘找到" in result
    assert "断链：missing.c" in result
    assert "initialize_analysis" not in result


def test_fast_mode_valid_references_allow_report_writing(tmp_path):
    (tmp_path / "ipc.c").write_text("void send(void) {}\n")
    result = asyncio.run(_check_refs(tmp_path, "ipc.c:1"))
    assert "全部 1 个文件引用路径均可访问" in result
    assert "可以调用 write_report" in result


def test_missing_repository_stays_an_explicit_configuration_error():
    result = asyncio.run(_check_refs(None, "ipc.c:1"))
    assert "未配置有效仓库目录" in result
    assert "initialize_analysis" not in result
