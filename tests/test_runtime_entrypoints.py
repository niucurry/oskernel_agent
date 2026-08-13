from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_agent_help_without_config_file():
    # 子进程 stdout 用 locale 编码（中文 Windows 上为 GBK），测试按 utf-8 解码会崩；
    # 强制子进程以 utf-8 输出，使断言与解码一致。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([sys.executable, "-m", "oskernel_agent.cli.agent", "--help"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", timeout=20, env=env)
    assert r.returncode == 0, r.stderr
    assert "--repo-path" in r.stdout
