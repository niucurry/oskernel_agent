"""Regression coverage for the project-private OpenCode setup command."""

from __future__ import annotations

import json

from oskernel_agent import config
from oskernel_agent.cli import setup_opencode


def test_setup_writes_project_private_opencode_files(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    python_bin = project_root / ".venv" / "Scripts" / "python.exe"
    python_bin.parent.mkdir(parents=True)
    python_bin.touch()

    monkeypatch.setattr(setup_opencode, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(setup_opencode, "SOURCE_ROOT", project_root / "src")
    monkeypatch.setattr(setup_opencode, "_VENV_PY", str(python_bin))
    monkeypatch.setattr(config, "api", {"key": "test-key", "base_url": ""})
    monkeypatch.setattr(config, "engine", {"max_steps": 20})

    setup_opencode.setup()

    auth_file = project_root / "data" / "opencode" / "data" / "opencode" / "auth.json"
    config_file = project_root / "data" / "opencode" / "config" / "opencode" / "opencode.json"
    assert json.loads(auth_file.read_text(encoding="utf-8"))["deepseek"]["key"] == "test-key"
    assert json.loads(config_file.read_text(encoding="utf-8"))["mcp"]["os-kernel-tools"]
