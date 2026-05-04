"""
参考 OS 指纹库：用于 T6 compare_with_reference_os 的代码级相似度分析。

公开 API：
  normalize_code(code)            代码归一化（消除注释 / 变量名 / 空白差异）
  compute_similarity(code_a, b)   三指标加权相似度（0.0-1.0）
  ReferenceOSDatabase             按需加载 / 离线构建指纹 JSON 文件
"""

import hashlib
import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path

# 代码归一化

def normalize_code(code: str) -> str:
    """消除表面差异，只保留控制流 + 调用结构特征。

    消除：行注释 / 块注释 / 字符串字面量 / 数字（保留 0/1）/ 多余空白
    保留：关键字 / 运算符 / 函数名 / 控制流结构
    """
    # 行注释（C/Rust 都适用）
    code = re.sub(r"//[^\n]*", "", code)
    # 块注释
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    # 字符串字面量
    code = re.sub(r'"(?:[^"\\]|\\.)*"', '"STR"', code)
    code = re.sub(r"'(?:[^'\\]|\\.)*'", "'STR'", code)
    # 数字（保留 0 / 1，其余替换为 NUM）
    code = re.sub(r"\b(?!0\b|1\b)\d+\b", "NUM", code)
    # 归一化空白
    code = re.sub(r"\s+", " ", code).strip()
    return code


# 相似度计算

_MAX_CHARS_FOR_SEQ = 8_000   # 超过此长度截断，防止 SequenceMatcher 过慢

def compute_similarity(code_a: str, code_b: str) -> float:
    """三指标加权相似度：字符序列 × 0.4 + token Jaccard × 0.3 + 调用序列 × 0.3。"""
    norm_a = normalize_code(code_a)
    norm_b = normalize_code(code_b)

    if not norm_a and not norm_b:
        return 1.0
    if not norm_a or not norm_b:
        return 0.0

    # 指标 1：字符级编辑距离（长代码截断）
    a_trunc = norm_a[:_MAX_CHARS_FOR_SEQ]
    b_trunc = norm_b[:_MAX_CHARS_FOR_SEQ]
    seq_sim = SequenceMatcher(None, a_trunc, b_trunc).ratio()

    # 指标 2：token 集合 Jaccard
    tok_a = set(norm_a.split())
    tok_b = set(norm_b.split())
    union = tok_a | tok_b
    jaccard = len(tok_a & tok_b) / len(union) if union else 0.0

    # 指标 3：调用序列相似度（保留调用顺序）
    calls_a = re.findall(r"\b(\w+)\s*\(", norm_a)
    calls_b = re.findall(r"\b(\w+)\s*\(", norm_b)
    call_sim = SequenceMatcher(None, calls_a, calls_b).ratio()

    return seq_sim * 0.4 + jaccard * 0.3 + call_sim * 0.3


# ReferenceOSDatabase

class ReferenceOSDatabase:
    """
    参考 OS 指纹库：按需加载各参考 OS 的函数级代码摘要。

    每个参考 OS 对应一个 JSON 文件，格式：
    {
        "func_name": {
            "body_normalized": "...",   # normalize_code 后的结果
            "body_hash":       "md5...",
            "calls":           [...],   # 调用的函数名列表
            "file":            "...",   # 相对路径
            "line_count":      42
        },
        ...
    }

    使用流程：
      1. 离线调用 build_from_repo() 为每个参考 OS 生成 JSON
      2. 运行时 load(reference_name) 按需加载
    """

    SUPPORTED: tuple[str, ...] = (
        "rcore-tutorial-v3",
        "rcore-tutorial-v2",
        "xv6-riscv",
        "ucore",
    )

    def __init__(self, db_dir: str):
        self.db_dir = db_dir
        self._cache: dict[str, dict] = {}

    def is_available(self, reference_name: str) -> bool:
        return (Path(self.db_dir) / f"{reference_name}.json").exists()

    def load(self, reference_name: str) -> dict:
        """加载指定参考 OS 的指纹数据，未找到返回空字典。"""
        if reference_name in self._cache:
            return self._cache[reference_name]

        path = Path(self.db_dir) / f"{reference_name}.json"
        if not path.exists():
            return {}

        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self._cache[reference_name] = data
        print(f"[ReferenceDB] 已加载 {reference_name}：{len(data)} 个函数")
        return data

    @staticmethod
    def build_from_repo(ref_name: str, repo_path: str, output_path: str) -> int:
        """
        离线构建指纹库（对每个参考 OS 运行一次即可）。

        参数：
          ref_name    参考 OS 名称（仅用于日志）
          repo_path   参考 OS 的本地路径
          output_path 输出 JSON 文件路径

        返回构建的函数数量。
        """
        from engines.path_c import TreeSitterEngine

        # 探测语言
        c_files  = len(list(Path(repo_path).rglob("*.c")))
        rs_files = len(list(Path(repo_path).rglob("*.rs")))
        lang = "rust" if rs_files > c_files else "c"
        print(f"[Build] {ref_name}：探测语言={lang}，"
              f"C 文件={c_files}，RS 文件={rs_files}")

        engine = TreeSitterEngine(repo_path, lang)

        fingerprints: dict[str, dict] = {}
        for key, entry in engine._func_index.items():
            body = entry.get("body", "")
            if not body:
                continue
            name = entry.get("name", key)
            fingerprints[name] = {
                "body_normalized": normalize_code(body),
                "body_hash":       hashlib.md5(
                    body.encode(errors="replace")
                ).hexdigest(),
                "calls":           entry.get("calls", []),
                "file":            entry.get("file", ""),
                "line_count":      body.count("\n") + 1,
            }

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(fingerprints, f, ensure_ascii=False, indent=2)

        print(f"[Build] {ref_name}：写入 {len(fingerprints)} 个函数 → {output_path}")
        return len(fingerprints)
