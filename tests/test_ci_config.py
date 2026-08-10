from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_gitlab_ci_has_frontend_production_build_gate():
    config = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    job = config["frontend-build"]
    commands = "\n".join(job["script"])
    assert job["stage"] == "test"
    assert "npm ci" in commands
    assert "npm run build" in commands


def test_github_ci_has_frontend_production_build_gate():
    config = yaml.safe_load((ROOT / ".github/workflows/eval.yml").read_text(encoding="utf-8"))
    job = config["jobs"]["frontend-build"]
    setup = next(step for step in job["steps"] if "setup-node" in step.get("uses", ""))
    build = next(step for step in job["steps"] if step.get("name") == "Build frontend")
    assert "frontend/package-lock.json" in setup["with"]["cache-dependency-path"]
    assert "npm ci" in build["run"]
    assert "npm run build" in build["run"]
