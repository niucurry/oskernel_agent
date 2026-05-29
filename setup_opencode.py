"""根目录入口 shim：转发至 oskernel_agent.cli.setup_opencode。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from oskernel_agent.cli.setup_opencode import setup

if __name__ == "__main__":
    setup()
