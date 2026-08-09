"""AI 生成代码检测 CLI：

  python -m oskernel_agent.comparison.ai_detect --repo <路径> [-o data/output] [--name 仓库名]
                          [--model codellama/CodeLlama-7b-hf] [--device cuda]
                          [--engine transformers|vllm] [--max-functions N]

重量级参考模型（CodeLlama-7B ~14GB）建议在带 GPU 的宿主机单独运行；产出
`{name}_ai_detect.json` 后，主流水线 report 步骤会自动并入「AI 生成代码检测」章节。
缺模型/磁盘/GPU 时本命令不报错，落盘 status=skipped 的诊断状态；
交付报告会拒绝把该状态渲染成检测模块。
"""

from __future__ import annotations

import argparse
import json
import sys

from loguru import logger

from .runner import DEFAULT_OUTPUT_DIR, run_ai_detect
from .settings import load_ai_detect_settings


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m oskernel_agent.comparison.ai_detect", description="AI 生成代码检测（DetectCodeGPT）。")
    p.add_argument("--repo", required=True, help="待检测仓库本地路径")
    p.add_argument("-o", "--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--name", default=None, help="仓库标识（默认取目录名）")
    p.add_argument("--model", default=None, help="覆盖参考模型 ID")
    p.add_argument("--device", default=None, help="auto / cpu / cuda / mps")
    p.add_argument("--engine", default=None, choices=["transformers", "vllm"])
    p.add_argument("--max-functions", type=int, default=None, help=">0 时只检测前 N 个函数")
    p.add_argument("--no-progress", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    args = build_parser().parse_args(argv)

    st = load_ai_detect_settings()
    overrides = {}
    if args.model is not None:
        overrides["model_id"] = args.model
    if args.device is not None:
        overrides["device"] = args.device
    if args.engine is not None:
        overrides["engine"] = args.engine
    if args.max_functions is not None:
        overrides["max_functions"] = args.max_functions
    if overrides:
        st = st.model_copy(update=overrides)

    res = run_ai_detect(
        args.repo,
        output_dir=args.output_dir,
        repo_name=args.name,
        settings=st,
        show_progress=not args.no_progress,
    )

    summary = {k: res.get(k) for k in ("status", "reason", "repo_id", "output_path")}
    if res.get("status") == "ok":
        overall = res["aggregated"]["overall"]
        summary["overall"] = {
            "total_functions": overall["total_functions"],
            "llm_count": overall["llm_count"],
            "human_count": overall["human_count"],
            "uncertain_count": overall["uncertain_count"],
            "llm_ratio_by_count": round(overall["llm_ratio_by_count"], 4),
        }
    logger.info("[ai_detect] 完成")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
