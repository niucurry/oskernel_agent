"""Fail if tracked project files contain common live credential formats."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


PATTERNS = {
    "OpenAI-compatible API key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9_]{25,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Google API key": re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "Alibaba Cloud access key": re.compile(rb"\bLTAI[0-9A-Za-z]{12,}\b"),
    "Slack token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}


def main() -> int:
    paths = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
    findings = []
    for raw_path in paths:
        if not raw_path:
            continue
        path = Path(raw_path.decode("utf-8", "surrogateescape"))
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue
        if b"\0" in data or len(data) > 2_000_000:
            continue
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(data):
                line = data.count(b"\n", 0, match.start()) + 1
                findings.append((path, line, name))
    for path, line, name in findings:
        print(f"{path}:{line}: possible {name}; value hidden")
    if findings:
        print("Revoke any exposed credential and remove it from Git history.")
        return 1
    print("No credential patterns found in tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
