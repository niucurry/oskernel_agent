from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_agent_help_without_config_file():
    r = subprocess.run([sys.executable, "-m", "oskernel_agent.cli.agent", "--help"], cwd=ROOT,
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert "--repo-path" in r.stdout
