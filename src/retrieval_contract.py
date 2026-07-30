"""召回完整性契约：所有会把“未命中”用于报告的阶段共享同一套硬校验。"""

from __future__ import annotations

CONTRACT_VERSION = 4
REQUIRED_CHANNELS = frozenset({
    "vector",
    "feature_simhash",
    "normalized_fingerprint",
    "function_name",
    "normalized_code_simhash",
    "function_identity_neighbor",
})


def build_retrieval_contract(history_coverage: dict | None, *, complete: bool) -> dict:
    return {
        "version": CONTRACT_VERSION,
        "complete": bool(complete),
        "no_silent_candidate_truncation": True,
        "same_language_only": True,
        "channels": sorted(REQUIRED_CHANNELS),
        "history_coverage": history_coverage or {},
    }


def contract_errors(contract: dict | None) -> list[str]:
    """返回契约不满足完整查全要求的原因；空列表才允许产生“暂未检出”。"""
    c = contract or {}
    errors: list[str] = []
    if c.get("version") != CONTRACT_VERSION:
        errors.append(f"契约版本不是 {CONTRACT_VERSION}")
    if c.get("complete") is not True:
        errors.append("召回未声明 complete=true")
    if c.get("no_silent_candidate_truncation") is not True:
        errors.append("未承诺禁止静默截断候选")
    if c.get("same_language_only") is not True:
        errors.append("未承诺仅比较同一编程语言")
    missing_channels = REQUIRED_CHANNELS - set(c.get("channels") or [])
    if missing_channels:
        errors.append("缺少召回通道：" + ", ".join(sorted(missing_channels)))

    coverage = c.get("history_coverage") or {}
    configured = coverage.get("configured")
    covered = coverage.get("covered")
    if coverage.get("complete") is not True:
        errors.append("历史库覆盖不完整")
    if not isinstance(configured, int) or configured <= 0:
        errors.append("历史库配置作品数无效")
    if covered != configured:
        errors.append(f"历史库覆盖数不一致：{covered}/{configured}")
    if coverage.get("missing_repo_ids"):
        errors.append("历史库仍有缺失作品")
    return errors


def require_complete_contract(contract: dict | None, *, artifact: str = "召回产物") -> None:
    errors = contract_errors(contract)
    if errors:
        raise RuntimeError(f"{artifact}不满足完整性契约：" + "；".join(errors))
