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
    focus_funcs: set[tuple[str, str]] | None = None,
) -> dict:
    """对单个仓库跑 AI 生成代码检测，返回结果 dict（并按需落盘）。

    Args:
        scorer: 注入的 LogRankProvider（单测用 mock）；为 None 时按 settings 加载真实模型，
                加载失败则返回 status="skipped" 而非抛错（VM 无模型/无磁盘时不阻塞流水线）。
        focus_funcs: 查重命中的可疑函数集合 {(相对文件路径 posix, 函数名)}。给定时（P2 限范围）
                检测范围收敛为「可疑清单 ∪ 大函数(LOC>=min_loc)」，不再按 max_functions 取前 N，
                跳过既不可疑又过小（必判 Uncertain）的函数，显著减少 Stage2 扰动开销。
    """
    repo = Path(repo)
    st = settings or load_ai_detect_settings()
    repo_id = repo_name or repo.resolve().name
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{repo_id}_ai_detect.json"

    def _emit(payload: dict) -> dict:
        payload.setdefault("repo_id", repo_id)
        payload.setdefault("model_id", st.model_id)
        payload.setdefault("config", _config_snapshot(st))
        if write:
            out_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            payload["output_path"] = str(out_path)
            logger.info("[ai_detect] 结果写入 {}（status={}）", out_path, payload["status"])
        return payload

    # 1) 抽取函数（复用 normalize 解析器）
    # 限范围模式（focus_funcs 给定）抽全量后按「可疑∪大函数」过滤，不再用 max_functions 取前 N
    extract_max = 0 if focus_funcs is not None else st.max_functions
    blocks = extract_blocks(repo, max_functions=extract_max)
    if not blocks:
        return _emit({"status": "skipped", "reason": "未抽取到 rust/c 函数"})

    if focus_funcs is not None:
        repo_root = repo.resolve()

        def _keep(b) -> bool:
            try:
                rel = Path(b.file_path).resolve().relative_to(repo_root).as_posix()
            except ValueError:
                rel = Path(b.file_path).name
            return b.loc >= st.min_loc or (rel, b.name) in focus_funcs

        kept = [b for b in blocks if _keep(b)]
        logger.info("[ai_detect] P2 限范围：可疑∪大函数(LOC>={}) {}/{} 个函数进入检测",
                    st.min_loc, len(kept), len(blocks))
        blocks = kept
        if not blocks:
            return _emit({"status": "skipped", "reason": "无可疑或大函数需检测",
                          "total_functions": 0})

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
