"""根目录入口 shim：转发至 oskernel_agent.cli.agent。

在未安装包（pip install -e .）的情况下也可直接运行：
    python agent.py --repo-id REPO_NAME
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from oskernel_agent.cli.agent import main

if __name__ == "__main__":
    main()
