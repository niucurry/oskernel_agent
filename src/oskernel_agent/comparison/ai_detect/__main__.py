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
from pathlib import Path

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
    p.add_argument(
        "--exclude-file", default=None,
        help="排除清单 JSON 文件：{\"files\": [...posix 相对路径], \"funcs\": [[posix 路径, 函数名], ...]}。"
             "命中文件/函数全部跳过检测（借鉴代码与第三方复用不入 AI 检测口径）",
    )
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

    exclude_files: set[str] | None = None
    exclude_funcs: set[tuple[str, str]] | None = None
    if args.exclude_file:
        try:
            exclusions = json.loads(Path(args.exclude_file).read_text(encoding="utf-8"))
            if not isinstance(exclusions, dict):
                raise ValueError("排除清单必须是 JSON 对象")
            raw_files = exclusions.get("files") or []
            raw_funcs = exclusions.get("funcs") or []
            if (not isinstance(raw_files, list)
                    or not isinstance(raw_funcs, list)
                    or not all(isinstance(x, str) for x in raw_files)
                    or not all(isinstance(x, list) and len(x) == 2
                               and all(isinstance(y, str) for y in x)
                               for x in raw_funcs)):
                raise ValueError("排除清单字段格式无效")
            exclude_files = set(raw_files)
            exclude_funcs = {(str(p), str(n)) for p, n in raw_funcs}
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.error("[ai_detect] 排除清单不可用，拒绝降低检测口径：{}", exc)
            return 2

    res = run_ai_detect(
        args.repo,
        output_dir=args.output_dir,
        repo_name=args.name,
        settings=st,
        show_progress=not args.no_progress,
        exclude_files=exclude_files,
        exclude_funcs=exclude_funcs,
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
