"""Inspect a trusted evidence checklist or exercise its read-repair loop."""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

from ..analysis.evidence_plan import check_evidence_plan, load_evidence_plan
from ..analysis.evaluation_assertions import assertion_shape_errors, check_assertions
from ..analysis.claim_review import REVIEW_PROMPT
from ..engines.evidence_plan import bind_evidence_plan
from ..engines.llm_batch import BatchTask, run_batch_task
from .read_audit import _environment, _trial_permissions


def run_plan(repo: Path, plan_path: Path, output: Path, model: str, *,
             timeout: int = 300, repair_rounds: int = 2, omit_first: str = "",
             with_assertions: bool = False) -> dict:
    from .. import config

    plan = load_evidence_plan(repo, plan_path)
    if with_assertions and omit_first:
        raise ValueError("断言实验不同时使用局部预览故障设置；缺证据场景通过独立清单和测试覆盖")
    if omit_first and not any(f"{i['id']}/{e['id']}" == omit_first
                              for i in plan["items"] for e in i["evidence"]):
        raise ValueError("omit-first 必须是清单内的 评价项ID/证据ID")
    if not config.api.get("key"):
        raise ValueError("请在本机 config.toml 配置 [api].key 和 base_url")
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("当前 Python 缺少项目 MCP 依赖") from exc
    repo, output = repo.resolve(), output.resolve()
    if output.exists() or output == repo or repo in output.parents:
        raise ValueError("输出必须是被评价仓库外的新目录")
    output.mkdir(parents=True, mode=0o700)
    runtime = output / "runtime-config"
    runtime.mkdir()
    permissions = _trial_permissions(repo)
    cfg = {"model": f"evidence/{model}", "small_model": f"evidence/{model}", "share": "disabled",
           "provider": {"evidence": {"npm": "@ai-sdk/openai-compatible", "name": "Evidence checklist validation",
               "options": {"baseURL": config.api["base_url"], "apiKey": config.api["key"]},
               "models": {model: {"name": model, "limit": {"context": 65536, "output": 8192}}}}},
           "agent": {"evidence-check": {"mode": "primary", "permission": permissions,
               "prompt": "依据实际读取的源码给出中文事实说明，保留证据不足的范围。不要改源码。"}},
           "mcp": {"os-kernel-tools": {"type": "local", "enabled": True,
                     "command": [sys.executable, "-m", "oskernel_agent.cli.mcp_server"]}}}
    cfg_path = runtime / "opencode.json"
    if with_assertions:
        cfg["agent"]["os-kernel-claim-review"] = {"mode": "primary", "permission": permissions, "prompt": REVIEW_PROMPT}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    cfg_path.chmod(0o600)
    (output / "plan.snapshot.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = output / "report.json"
    task = BatchTask(batch_id="evidence-plan-validation", agent_name="evidence-check",
        user_request=f"分析仓库 {repo}，按评价清单说明实现和尚未确认的内容。"
                     '输出 JSON：{"summary":"带相对路径:行号的简短事实说明"}。'
                     "请使用原生 read 和 MCP read_file 两种读取工具以验证记录通道。"
                     f"用 MCP write_report 写到 {report_path}。",
        output_path=report_path, cache_dir=output, cache_key="", cache_enabled=False,
        repo_path=repo, audit_source_reads=True, read_repair_rounds=repair_rounds,
        cache_validator=lambda result: isinstance(result.get("summary"), str) and bool(result["summary"].strip()))
    base_request = task.user_request
    bind_evidence_plan(task, plan, with_assertions=with_assertions)
    if omit_first:
        preview = copy.deepcopy(plan["items"])
        for item in preview:
            item["evidence"] = [e for e in item["evidence"] if f"{item['id']}/{e['id']}" != omit_first]
        # Hide one target in the first model-facing checklist only. The host
        # snapshot/required_source_ranges remain complete, and repair uses them.
        task.user_request = base_request + (
            "\n【首轮局部预览实验】本轮只读取以下目标对应的文件，描述这些源码能确认的事实。"
            "不要检索、读取或引用其他文件；缺失上下文直接说明，不补充猜测。"
            "若收到后续的源码读取补证要求，取消此预览限制，重新读取全部指定范围。\n"
            + json.dumps(preview, ensure_ascii=False)
        )
    values = {"AGENT_OPENCODE_SOURCE_CONFIG_DIR": str(runtime),
              "AGENT_OPENCODE_SOURCE_DATA_DIR": str(output / "auth"),
              "AGENT_READ_AUDIT_DIR": str(output / "receipts"),
              "AGENT_CLAIM_REVIEW_DIR": str(output / "reviews"),
              "OPENCODE_DISABLE_PROJECT_CONFIG": "true", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "true",
              "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true", "OPENCODE_DISABLE_AUTOUPDATE": "true",
              "OPENCODE_DISABLE_MODELS_FETCH": "true", "OPENCODE_DISABLE_LSP_DOWNLOAD": "true"}
    start = time.monotonic()
    with _environment(values):
        report = run_batch_task(task, timeout=timeout)
        review = None
        if with_assertions:
            errors = assertion_shape_errors(plan, report)
            if report.get("_error") or errors:
                raise RuntimeError("未形成有效评价断言：" + "；".join(errors))
            from ..engines.claim_review import review_and_revise
            report, review = review_and_revise(task, report, timeout=timeout)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            (output / "review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    evaluation = check_evidence_plan(repo, plan, task.result_read_audit)
    evaluation.update(model=model, elapsed_seconds=round(time.monotonic() - start, 3),
                      read_evidence=task.read_evidence, response_error=report.get("_error"),
                      repair_budget=repair_rounds, omit_first=omit_first)
    if with_assertions:
        evaluation["assertions"] = check_assertions(repo, plan, report, task.result_read_audit, review)
        (output / "assertions.json").write_text(json.dumps(evaluation["assertions"], ensure_ascii=False, indent=2), encoding="utf-8")
    path = output / "evaluation.json"
    path.write_text(json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"report": str(report_path), "evaluation": str(path), "status": evaluation["status"],
            "repair_rounds": task.read_evidence.get("repair_rounds"),
            "assertion_statuses": [i["status"] for i in evaluation.get("assertions", {}).get("items", [])], "items": [
                {"id": i["id"], "returned": i["returned_count"], "required": i["required_count"]}
                for i in evaluation["items"]]}


def main() -> None:
    parser = argparse.ArgumentParser(description="评价项证据清单：离线检查或真实模型补读验证")
    parser.add_argument("command", choices=["inspect", "run"])
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--repair-rounds", type=int, choices=[0, 1, 2], default=2)
    parser.add_argument("--omit-first", default="", help="仅实验：首轮遗漏 评价项ID/证据ID 对应文件")
    parser.add_argument("--claims", action="store_true", help="逐项生成带范围的断言，并运行独立复核与有限修订")
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = load_evidence_plan(args.repo, args.plan)
        else:
            if not args.output or not args.model:
                raise ValueError("run 必须提供 --output 和 --model；会消耗配置接口的模型用量")
            result = run_plan(args.repo, args.plan, args.output, args.model, timeout=args.timeout,
                              repair_rounds=args.repair_rounds, omit_first=args.omit_first,
                              with_assertions=args.claims)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"评价清单处理失败：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
