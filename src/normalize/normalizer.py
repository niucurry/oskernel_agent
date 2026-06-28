"""基于 AST 的代码归一化：生成 normalized_code 并收集字符串字面量。

归一化目标：剥离与抄袭无关的表层差异，使「改了变量名/注释/空白」后的等价代码产生
**完全相同**的 normalized_code。规则（见任务说明）：
  a. 用户标识符（变量/参数/局部函数名）脱敏为 VAR_n / FUNC_n（同名同号，按首现顺序）；
     类型名、保留符号（std/core/已知 crate/C 标准库）原样保留。
  b. 删除全部注释。
  c. 字符串字面量替换为 STR；长度 >= 8 的原文收集到 unique_strings。
  d. 数字字面量按量级替换：0-10→INT_S，其余十进制→INT_M，十六进制→INT_HEX。
  e. 压缩空白：以 ; { } 为界，每条语句一行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .keep_symbols import load_keep_symbols
from .ts import parse

# --- 各语言的节点类型集合 ---
COMMENT_TYPES = {"line_comment", "block_comment", "comment"}
STRING_TYPES = {"string_literal", "raw_string_literal"}
CHAR_TYPES = {"char_literal", "char"}
INT_TYPES = {"integer_literal", "float_literal", "number_literal"}
# 这些「类型/字段」标识符无条件保留
KEEP_NODE_TYPES = {"type_identifier", "primitive_type", "field_identifier"}
# 语句分隔符：在其后断行
STMT_DELIMS = {";", "{", "}"}

MIN_STRING_LEN = 8

_NUM_CORE = re.compile(r"[0-9][0-9_.]*")


@dataclass
class NormResult:
    code: str
    strings: list[str] = field(default_factory=list)


def _num_token(text: str) -> str:
    s = text.strip().lower()
    if s.startswith("0x"):
        return "INT_HEX"
    if s.startswith(("0b", "0o")):
        return "INT_M"
    m = _NUM_CORE.match(s)
    core = m.group(0).replace("_", "") if m else ""
    if "." in core:
        return "INT_M"
    try:
        v = int(core)
    except ValueError:
        return "INT_M"
    return "INT_S" if 0 <= v <= 10 else "INT_M"


def _func_names(node, lang: str) -> set[str]:
    """收集本函数子树内「被当作函数使用」的标识符名（定义名 + 调用名 + 宏名）。"""
    names: set[str] = set()

    def visit(n):
        t = n.type
        if t == "call_expression":
            f = n.child_by_field_name("function")
            if f is not None and f.type == "identifier":
                names.add(f.text.decode("utf-8", "replace"))
        elif lang == "rust":
            if t == "macro_invocation":
                m = n.child(0)
                if m is not None and m.type == "identifier":
                    names.add(m.text.decode("utf-8", "replace"))
            elif t in ("function_item", "function_signature_item"):
                nm = n.child_by_field_name("name")
                if nm is not None:
                    names.add(nm.text.decode("utf-8", "replace"))
        elif lang == "c" and t == "function_declarator":
            d = n.child_by_field_name("declarator")
            if d is not None and d.type == "identifier":
                names.add(d.text.decode("utf-8", "replace"))
        for c in n.children:
            visit(c)

    visit(node)
    return names


class _Renamer:
    """把用户标识符映射为 VAR_n / FUNC_n（同名同号，按首现顺序）。"""

    def __init__(self, keep: frozenset[str], func_names: set[str]):
        self.keep = keep
        self.func_names = func_names
        self.mapping: dict[str, str] = {}
        self.counters = {"VAR": 0, "FUNC": 0}

    def rename(self, name: str) -> str:
        if name in self.keep:
            return name
        if name in self.mapping:
            return self.mapping[name]
        kind = "FUNC" if name in self.func_names else "VAR"
        token = f"{kind}_{self.counters[kind]}"
        self.counters[kind] += 1
        self.mapping[name] = token
        return token


def normalize_node(node, lang: str, keep: frozenset[str] | None = None) -> NormResult:
    """归一化单个函数（语法树节点），返回 normalized_code 与收集到的字符串。"""
    keep = keep if keep is not None else load_keep_symbols()
    renamer = _Renamer(keep, _func_names(node, lang))
    tokens: list[str] = []
    strings: list[str] = []

    def emit(n):
        t = n.type
        if t in COMMENT_TYPES:
            return
        if t in STRING_TYPES:
            tokens.append("STR")
            raw = n.text.decode("utf-8", "replace")
            inner = raw.strip('"').strip("#").strip('"').strip("r")
            if len(inner) >= MIN_STRING_LEN:
                strings.append(inner)
            return
        if t in CHAR_TYPES:
            tokens.append("STR")
            return
        if t in INT_TYPES:
            tokens.append(_num_token(n.text.decode("utf-8", "replace")))
            return
        if n.child_count == 0:
            text = n.text.decode("utf-8", "replace")
            if t in KEEP_NODE_TYPES:
                tokens.append(text)
            elif t == "identifier":
                tokens.append(renamer.rename(text))
            else:  # 关键字 / 运算符 / 标点
                tokens.append(text)
            return
        for c in n.children:
            emit(c)

    emit(node)

    # 以语句分隔符断行，压缩空白
    lines: list[str] = []
    cur: list[str] = []
    for tok in tokens:
        cur.append(tok)
        if tok in STMT_DELIMS:
            lines.append(" ".join(cur))
            cur = []
    if cur:
        lines.append(" ".join(cur))
    code = "\n".join(ln.strip() for ln in lines if ln.strip())
    return NormResult(code=code, strings=strings)


def normalize_snippet(code: str, lang: str, keep: frozenset[str] | None = None) -> NormResult:
    """便捷入口：解析一段源码并归一化其根节点（用于单元测试）。"""
    # 仅 rust/c 有 tree-sitter grammar；asm 等无 grammar 的语言不解析，
    # 返回空结果（unique_strings 信号通道对它们自然为空，不影响流水线）。
    if lang not in ("rust", "c"):
        return NormResult(code=code, strings=[])
    tree = parse(code.encode("utf-8"), lang)
    return normalize_node(tree.root_node, lang, keep)


# ---- 特征 token 提取（供 Layer 1 SimHash 粗筛使用） ----
_CF_TYPES = {
    "rust": {
        "if_expression": "if",
        "while_expression": "while",
        "for_expression": "for",
        "loop_expression": "loop",
        "match_expression": "match",
    },
    "c": {
        "if_statement": "if",
        "while_statement": "while",
        "for_statement": "for",
        "do_statement": "loop",
        "switch_statement": "match",
    },
}


def extract_feature_tokens(node, lang: str, keep: frozenset[str] | None = None) -> list[str]:
    """从函数 AST 抽取特征 token 集合（去重排序）。

    类别：cf:<控制流>（if/while/for/loop/match）、ty:<类型名>、
    lib:<保留库符号调用>、call:<其它函数/宏调用名>。
    """
    keep = keep if keep is not None else load_keep_symbols()
    cf = _CF_TYPES.get(lang, {})
    feats: set[str] = set()

    def _call_token(name: str) -> str:
        return f"lib:{name}" if name in keep else f"call:{name}"

    def visit(n):
        t = n.type
        if t in cf:
            feats.add(f"cf:{cf[t]}")
        elif t in ("type_identifier", "primitive_type"):
            feats.add(f"ty:{n.text.decode('utf-8', 'replace')}")
        elif t == "call_expression":
            f = n.child_by_field_name("function")
            if f is not None and f.type == "identifier":
                feats.add(_call_token(f.text.decode("utf-8", "replace")))
        elif lang == "rust" and t == "macro_invocation":
            m = n.child(0)
            if m is not None and m.type == "identifier":
                feats.add(_call_token(m.text.decode("utf-8", "replace")))
        for c in n.children:
            visit(c)

    visit(node)
    return sorted(feats)


def extract_asm_feature_tokens(text: str) -> list[str]:
    """汇编特征 token：每条指令的助记符 op:<mnemonic>（去标号/指示符/注释）。"""
    feats: set[str] = set()
    for raw in text.splitlines():
        line = raw
        for marker in ("//", "#", ";"):
            idx = line.find(marker)
            if idx != -1:
                line = line[:idx]
        line = line.strip()
        if not line or line.endswith(":") or line.startswith("."):
            continue
        op = re.split(r"[ \t,]", line, maxsplit=1)[0]
        if op:
            feats.add(f"op:{op.lower()}")
    return sorted(feats)


def normalize_asm(text: str) -> NormResult:
    """汇编无 tree-sitter grammar，做轻量文本归一化：去注释、数字分级、压缩空白。

    汇编不做标识符脱敏（寄存器/助记符并非用户自定义标识符）。
    """
    strings: list[str] = []
    out_lines: list[str] = []
    for raw in text.splitlines():
        line = raw
        # 去注释：# 行注释、// 行注释、; 行注释（GAS / 各家汇编混用）
        for marker in ("//", "#", ";"):
            idx = line.find(marker)
            if idx != -1:
                line = line[:idx]
        line = line.strip()
        if not line:
            continue
        # 字符串收集 + 替换
        def _str_sub(m):
            inner = m.group(1)
            if len(inner) >= MIN_STRING_LEN:
                strings.append(inner)
            return "STR"

        line = re.sub(r'"([^"]*)"', _str_sub, line)
        # 数字分级
        line = re.sub(r"0[xX][0-9a-fA-F]+", "INT_HEX", line)
        line = re.sub(r"\b\d+\b", lambda m: "INT_S" if 0 <= int(m.group(0)) <= 10 else "INT_M", line)
        line = re.sub(r"\s+", " ", line)
        out_lines.append(line)
    return NormResult(code="\n".join(out_lines), strings=strings)
