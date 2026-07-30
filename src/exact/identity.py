"""仓库无关的函数身份兼容度。

用于区分“同一功能簇中的具体对应函数”和“只是使用相同模板的邻近函数”。特征只来自
函数名分词、参数数量、调用/成员访问以及控制流，不依赖仓库、路径或预设函数名。
"""

from __future__ import annotations

import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

MIN_IDENTITY_SCORE = 0.62
MIN_IDENTITY_LINE_SIM = 0.15
MIN_IDENTITY_MATCH_LINES = 3

_IDENT = re.compile(r"[A-Za-z_]\w*")
_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*(?=\()")
_MEMBER = re.compile(r"(?:\.|->)\s*([A-Za-z_]\w*)")
_CONSTANT = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
_LINE_COMMENT = re.compile(r"//[^\n]*|#[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_CONTROL_WORDS = {
    "if", "else", "for", "while", "loop", "match", "switch", "case", "return",
    "break", "continue", "try", "catch", "throw", "await", "yield",
}
_NON_CALL_WORDS = _CONTROL_WORDS | {
    "fn", "def", "function", "sizeof", "alignof", "typeof", "unsafe",
}


@dataclass(frozen=True, slots=True)
class FunctionIdentityFeatures:
    """可跨候选复用的函数身份特征。"""

    name: str
    name_tokens: frozenset[str]
    parameter_count: int | None
    behavior_tokens: frozenset[str]
    control_tokens: frozenset[str]


def _name_tokens(name: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name or "")
    return {token.lower() for token in re.split(r"[^A-Za-z0-9]+", expanded) if token}


def _jaccard(left: AbstractSet[str], right: AbstractSet[str]) -> float:
    if not left and not right:
        return 0.5
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _mask_comments_and_literals(code: str) -> str:
    """Mask comments/literals while preserving offsets used by signature scanning."""
    chars = list(code)
    index = 0
    length = len(code)

    def mask(start: int, stop: int) -> None:
        for offset in range(start, min(stop, length)):
            if chars[offset] not in "\r\n":
                chars[offset] = " "

    while index < length:
        if code.startswith("//", index):
            stop = code.find("\n", index + 2)
            stop = length if stop < 0 else stop
            mask(index, stop)
            index = stop
            continue
        if code.startswith("/*", index):
            stop = code.find("*/", index + 2)
            stop = length if stop < 0 else stop + 2
            mask(index, stop)
            index = stop
            continue
        if code.startswith(("#[", "#!["), index):
            depth = 0
            stop = index
            while stop < length:
                if code[stop] == "[":
                    depth += 1
                elif code[stop] == "]":
                    depth -= 1
                    if depth == 0:
                        stop += 1
                        break
                stop += 1
            mask(index, stop)
            index = stop
            continue
        if code[index] == "#":
            stop = code.find("\n", index + 1)
            stop = length if stop < 0 else stop
            mask(index, stop)
            index = stop
            continue
        if code[index] in {'"', "'", "`"}:
            quote = code[index]
            delimiter = quote * 3 if code.startswith(quote * 3, index) else quote
            stop = index + len(delimiter)
            while stop < length:
                if code.startswith(delimiter, stop):
                    stop += len(delimiter)
                    break
                if code[stop] == "\\" and len(delimiter) == 1:
                    stop += 2
                else:
                    stop += 1
            mask(index, stop)
            index = stop
            continue
        index += 1
    return "".join(chars)


def _signature_parameter_open(code: str, func_name: str) -> int | None:
    """Locate the parameter list belonging to ``func_name``, not an annotation."""
    masked = _mask_comments_and_literals(code)
    if not func_name:
        open_at = masked.find("(")
        return open_at if open_at >= 0 else None

    escaped = re.escape(func_name)

    def after_name(name_end: int) -> int | None:
        cursor = name_end
        while cursor < len(masked) and masked[cursor].isspace():
            cursor += 1
        # Rust/C++ generic parameter lists may sit between the name and ``(``.
        if cursor < len(masked) and masked[cursor] == "<":
            depth = 0
            while cursor < len(masked):
                if masked[cursor] == "<":
                    depth += 1
                elif masked[cursor] == ">":
                    depth -= 1
                    if depth == 0:
                        cursor += 1
                        break
                cursor += 1
            if depth:
                return None
            while cursor < len(masked) and masked[cursor].isspace():
                cursor += 1
        return cursor if cursor < len(masked) and masked[cursor] == "(" else None

    # Prefer declaration keywords for languages that have them. This prevents a
    # recursive call later in a Python/JavaScript body from being mistaken for
    # the declaration when no brace delimits the header.
    declaration_patterns = (
        re.compile(rf"\b(?:fn|def|function)\s+(?P<name>{escaped})\b"),
        re.compile(rf"\bfunc\s*(?:\([^{{}};]*\)\s*)?(?P<name>{escaped})\b"),
    )
    for pattern in declaration_patterns:
        for match in reversed(list(pattern.finditer(masked))):
            open_at = after_name(match.end("name"))
            if open_at is not None:
                return open_at

    # C/C++ and macro-generated declarations have no universal keyword. The
    # declaration name is the last matching token before the function body.
    body_at = masked.find("{")
    header = masked if body_at < 0 else masked[:body_at]
    matches = list(re.finditer(rf"\b{escaped}\b", header))
    for match in reversed(matches):
        open_at = after_name(match.end())
        if open_at is not None:
            return open_at
    return None


def _parameter_count(code: str, func_name: str = "") -> int | None:
    """Estimate top-level parameter count from the actual function declaration."""
    open_at = _signature_parameter_open(code, func_name)
    if open_at is None:
        return None
    depth = 0
    close_at = -1
    for index in range(open_at, len(code)):
        char = code[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_at = index
                break
    if close_at < 0:
        return None
    body = code[open_at + 1:close_at].strip()
    if not body or body == "void":
        return 0
    depth = 0
    count = 1
    for char in body:
        if char in "([{<":
            depth += 1
        elif char in ")]}>" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
    return count


def _behavior_tokens(code: str, func_name: str) -> set[str]:
    calls = {
        token.lower() for token in _CALL.findall(code)
        if token.lower() not in _NON_CALL_WORDS and token != func_name
    }
    members = {token.lower() for token in _MEMBER.findall(code)}
    constants = {token.lower() for token in _CONSTANT.findall(code)}
    strings = {value for value in _STRING.findall(code) if len(value) >= 8}
    return calls | members | constants | strings


def _control_tokens(code: str) -> set[str]:
    words = {token.lower() for token in _IDENT.findall(code)}
    return words & _CONTROL_WORDS


def function_identity_features(name: str, code: str) -> FunctionIdentityFeatures:
    """一次解析函数身份特征，供一个函数与多个候选比较时复用。"""
    clean_name = (name or "").strip()
    clean_code = code or ""
    return FunctionIdentityFeatures(
        name=clean_name,
        name_tokens=frozenset(_name_tokens(clean_name)),
        parameter_count=_parameter_count(clean_code, clean_name),
        behavior_tokens=frozenset(_behavior_tokens(clean_code, clean_name)),
        control_tokens=frozenset(_control_tokens(clean_code)),
    )


def compare_function_identity_features(
    query: FunctionIdentityFeatures,
    candidate: FunctionIdentityFeatures,
) -> dict[str, float | bool]:
    """比较预计算身份特征；结果与 :func:`function_identity` 完全一致。"""
    exact_name = bool(query.name and query.name == candidate.name)
    name_score = (
        1.0 if exact_name
        else _jaccard(query.name_tokens, candidate.name_tokens)
    )

    if query.parameter_count is None or candidate.parameter_count is None:
        signature_score = 0.5
    else:
        signature_score = 1.0 / (
            1.0 + abs(query.parameter_count - candidate.parameter_count)
        )

    behavior_score = _jaccard(
        query.behavior_tokens, candidate.behavior_tokens,
    )
    control_score = _jaccard(
        query.control_tokens, candidate.control_tokens,
    )
    score = (
        0.45 * name_score
        + 0.20 * signature_score
        + 0.25 * behavior_score
        + 0.10 * control_score
    )
    return {
        "score": round(min(1.0, max(0.0, score)), 4),
        "exact_name": exact_name,
        "name": round(name_score, 4),
        "signature": round(signature_score, 4),
        "behavior": round(behavior_score, 4),
        "control": round(control_score, 4),
    }


def is_trivial_constant_stub(code: str) -> bool:
    """是否为只返回常量（或空体）的短占位实现。

    这种函数在系统调用表、接口适配层和未实现功能中很常见。不同职责的函数可能仅因
    多行签名、括号和固定返回值而获得很高逐行相似度；它们没有可支持“改名复制”的
    行为证据。判定只看函数体形态，不枚举函数名、仓库或具体错误码。
    """
    text = _BLOCK_COMMENT.sub("", code or "")
    text = _LINE_COMMENT.sub("", text)
    open_at = text.find("{")
    close_at = text.rfind("}")
    if open_at < 0 or close_at <= open_at:
        return False
    body = text[open_at + 1:close_at].strip()
    if not body:
        return True
    # Rust/C/C++ 常见占位体：``-38``、``return ENOSYS;``、``false``、``None``。
    constant = r"(?:[+-]?\d+(?:[A-Za-z0-9_]*)?|[A-Z][A-Z0-9_]*|true|false|null|nullptr|None)"
    return re.fullmatch(rf"(?:return\s+)?{constant}\s*;?", body) is not None


def function_identity(
    query_name: str, query_code: str, candidate_name: str, candidate_code: str,
) -> dict[str, float | bool]:
    """返回 0～1 身份兼容分及可解释分量；该分不等价于代码借鉴概率。"""
    return compare_function_identity_features(
        function_identity_features(query_name, query_code),
        function_identity_features(candidate_name, candidate_code),
    )


def identity_can_rescue(line_similarity: float, matched_lines: int, identity_score: float) -> bool:
    """身份兼容只负责救回可能配错的候选，仍要求至少存在少量共同代码。"""
    return bool(
        identity_score >= MIN_IDENTITY_SCORE
        and line_similarity >= MIN_IDENTITY_LINE_SIM
        and matched_lines >= MIN_IDENTITY_MATCH_LINES
    )


def identity_relation(exact_name: bool, identity_score: float,
                      line_similarity: float) -> str:
    """区分具体对应、可信改名和仅同功能族邻居。"""
    if exact_name:
        if identity_score >= MIN_IDENTITY_SCORE:
            return "exact_counterpart"
        if line_similarity >= 0.70:
            return "same_name_code_clone"
        return "same_name_only"
    if identity_score >= 0.72:
        return "compatible_renamed"
    if line_similarity >= 0.70:
        return "code_clone_renamed"
    return "family_neighbor"
