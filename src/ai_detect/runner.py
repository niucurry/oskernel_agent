"""AI 生成代码检测编排：抽取 → log-rank/NPR 检测 → 仓库级聚合 → 落盘 JSON。

与其他模块一致：独立 CLI、中间产物落盘、可注入 mock（scorer）做单测、缺模型时优雅降级。

输出 `{repo}_ai_detect.json`：
    {
      "status": "ok" | "skipped",
      "reason": <skipped 时的原因>,
      "repo_id": <仓库名>,
      "model_id": <参考模型>,
      "config": {<阈值快照>},
      "aggregated": {<AggregatedReport.to_dict()>}   # status==ok 时
    }
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from .extract import extract_blocks
from .settings import AIDetectSettings, load_ai_detect_settings
from .vendor.ai_code_detector.aggregator import RepoAggregator
from .vendor.ai_code_detector.detector import DetectCodeGPT, LogRankProvider
from .vendor.ai_code_detector.pipeline import DetectionPipeline, ThresholdConfig

DEFAULT_OUTPUT_DIR = "data/output"


def _threshold_config(st: AIDetectSettings) -> ThresholdConfig:
    return ThresholdConfig(
        log_rank_llm_threshold=st.log_rank_llm_threshold,
        log_rank_human_threshold=st.log_rank_human_threshold,
        detect_score_threshold=st.detect_score_threshold,
        loc_uncertain_below=st.min_loc,
    )


def _config_snapshot(st: AIDetectSettings) -> dict:
    return {
        "model_id": st.model_id,
        "engine": st.engine,
        "device": st.device,
        "k_perturbations": st.k_perturbations,
        "min_loc": st.min_loc,
        "log_rank_llm_threshold": st.log_rank_llm_threshold,
        "log_rank_human_threshold": st.log_rank_human_threshold,
        "detect_score_threshold": st.detect_score_threshold,
    }


def _build_scorer(st: AIDetectSettings):
    """加载参考模型（重量级，14GB+）。失败时抛异常，由调用方降级。"""
    from .vendor.ai_code_detector.perplexity import create_calculator

    return create_calculator(
        engine=st.engine,
        model_id=st.model_id,
        device=st.device,
        batch_size=st.batch_size,
    )


def run_ai_detect(
    repo: str | Path,
    *,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    repo_name: str | None = None,
    settings: AIDetectSettings | None = None,
    scorer: LogRankProvider | None = None,
    show_progress: bool = True,
    write: bool = True,
    exclude_files: set[str] | None = None,
    exclude_funcs: set[tuple[str, str]] | None = None,
) -> dict:
    """对单个仓库跑 AI 生成代码检测，返回结果 dict（并按需落盘）。

    Args:
        scorer: 注入的 LogRankProvider（单测用 mock）；为 None 时按 settings 加载真实模型，
                加载失败则返回 status="skipped" 而非抛错（VM 无模型/无磁盘时不阻塞流水线）。
        exclude_files: 文件级借鉴的相对路径集合（posix）。命中整文件的函数全部跳过。
        exclude_funcs: 函数级借鉴集合 {(相对文件路径 posix, 函数名)}。命中的单个函数跳过。

    给定 exclude_files / exclude_funcs（排除借鉴模式）时：抽全量函数后剔除文件级 / 函数级
    借鉴代码，**只对未匹配上的原创代码**做检测（AI 生成检测只对作者自己写的代码才有意义，
    借鉴自参考 OS 的代码无论是否疑似 AI 生成都不应计入）。否则按 max_functions 取前 N。
    """
    repo = Path(repo)
    st = settings or load_ai_detect_settings()
    repo_id = repo_name or repo.resolve().name
    out_dir = Path(output_dir)
    out_path = out_dir / f"{repo_id}_ai_detect.json"

    def _emit(payload: dict) -> dict:
        payload.setdefault("repo_id", repo_id)
        payload.setdefault("model_id", st.model_id)
        payload.setdefault("config", _config_snapshot(st))
        if write:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            payload["output_path"] = str(out_path)
            logger.info("[ai_detect] 结果写入 {}（status={}）", out_path, payload["status"])
        return payload

    # 1) 抽取函数（复用 normalize 解析器）
    # 排除借鉴模式（exclude_files/exclude_funcs 给定）抽全量后剔除借鉴代码，再按 max_functions 截断
    exclude_mode = exclude_files is not None or exclude_funcs is not None
    extract_max = 0 if exclude_mode else st.max_functions
    blocks = extract_blocks(repo, max_functions=extract_max)
    if not blocks:
        return _emit({"status": "skipped", "reason": "未抽取到 rust/c 函数"})

    if exclude_mode:
        ex_files = exclude_files or set()
        ex_funcs = exclude_funcs or set()
        repo_root = repo.resolve()

        def _rel(b) -> str:
            try:
                return Path(b.file_path).resolve().relative_to(repo_root).as_posix()
            except ValueError:
                return Path(b.file_path).name

        def _keep(b) -> bool:
            rel = _rel(b)
            if rel in ex_files:            # 文件级借鉴：整文件跳过
                return False
            if (rel, b.name) in ex_funcs:  # 函数级借鉴：该函数跳过
                return False
            return True

        kept = [b for b in blocks if _keep(b)]
        logger.info("[ai_detect] 排除借鉴代码：文件级 {} / 函数级 {} → 跳过 {} 个借鉴函数，"
                    "{}/{} 个未匹配函数进入检测",
                    len(ex_files), len(ex_funcs), len(blocks) - len(kept), len(kept), len(blocks))
        blocks = kept
        if not blocks:
            return _emit({"status": "skipped",
                          "reason": "全部函数均为借鉴代码，无未匹配原创函数需检测",
                          "total_functions": 0})
        if st.max_functions and len(blocks) > st.max_functions:
            logger.info("[ai_detect] 未匹配函数 {} 个超过 max_functions={}，截断",
                        len(blocks), st.max_functions)
            blocks = blocks[:st.max_functions]

    # 2) 取得 log-rank provider（注入优先；否则加载真实模型，失败则降级跳过）
    if scorer is None:
        try:
            scorer = _build_scorer(st)
        except Exception as exc:  # noqa: BLE001 — 缺模型/磁盘/GPU 都降级，不阻塞流水线
            logger.warning("[ai_detect] 参考模型加载失败，跳过检测：{!r}", exc)
            return _emit({
                "status": "skipped",
                "reason": f"参考模型不可用（{type(exc).__name__}）：{exc}",
                "total_functions": len(blocks),
            })

    # 3) 两阶段检测
    pipeline = DetectionPipeline(
        provider=scorer,
        detector=DetectCodeGPT(provider=scorer, k=st.k_perturbations),
        cfg=_threshold_config(st),
    )
    results = pipeline.process_batch(blocks, show_progress=show_progress)

    # 4) 仓库级聚合
    agg = RepoAggregator(
        min_confidence_llm=st.suspicious_min_confidence,
        enable_git_blame=st.git_blame,
    )
    report = agg.aggregate(results, repo.resolve())

    return _emit({"status": "ok", "aggregated": report.to_dict()})
