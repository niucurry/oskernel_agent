"""Attach an evaluator-owned checklist to existing audited batch tasks."""

from __future__ import annotations

import copy
import os
from pathlib import Path

from ..analysis.evidence_plan import load_evidence_plan, plan_prompt, plan_requirements


def configured_plan(repo: Path) -> dict | None:
    path = os.environ.get("AGENT_EVIDENCE_PLAN", "").strip()
    return load_evidence_plan(repo, Path(path)) if path else None


def bind_evidence_plan(task, plan: dict | None, *, with_assertions: bool = False) -> None:
    if not plan:
        return
    if task.repo_path is None or str(task.repo_path.resolve()) != plan["repo_path"]:
        raise ValueError("评价清单绑定的仓库与任务不一致")
    task.evidence_plan = copy.deepcopy(plan)
    task.required_source_ranges = task.required_source_ranges + plan_requirements(plan)
    task.audit_source_reads = True
    task.cache_enabled = False
    task.user_request += plan_prompt(plan)
    if with_assertions:
        from ..analysis.evaluation_assertions import ASSERTION_PROMPT, assertion_shape_errors
        previous = task.cache_validator
        frozen = task.evidence_plan

        def complete(result):
            original = previous(result) if previous else True
            if original is not True and original is not None:
                return original
            errors = assertion_shape_errors(frozen, result)
            return "评价断言格式无效：" + "；".join(errors) if errors else True

        task.cache_validator = complete
        task.evaluation_assertions_enabled = True
        task.user_request += ASSERTION_PROMPT
