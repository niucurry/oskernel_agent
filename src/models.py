"""查重引擎共享数据结构（所有模块统一引用）。

这里集中定义跨模块流转的 pydantic model。模块之间通过 JSON / SQLite 落盘解耦，
落盘时统一用 ``model_dump(mode="json")``，读回时用 ``Model.model_validate(...)``，
避免内存级耦合。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ModuleTag(str, Enum):
    """内核子系统标签，用于按模块分桶比对与报告分类。

    在 CLAUDE.md 最初的 6 类（sched/mm/fs/trap/driver/other）之外，normalize 阶段额外引入：
    - ARCH：架构/启动/汇编相关代码；
    - MACRO：Rust 宏定义（macro_definition）单独成类。
    """

    SCHED = "sched"
    MM = "mm"
    FS = "fs"
    TRAP = "trap"
    DRIVER = "driver"
    ARCH = "arch"
    MACRO = "macro"
    OTHER = "other"


class FunctionRecord(BaseModel):
    """单个函数的规范化记录，是后续所有层（simhash/embed/segment/exact）的基本单元。"""

    repo_id: str = Field(..., description="所属仓库标识，通常为 {year}/{team_name}")
    file_path: str = Field(..., description="相对仓库根目录的源文件路径")
    start_line: int = Field(..., ge=1, description="函数起始行（1-based，含）")
    end_line: int = Field(..., ge=1, description="函数结束行（1-based，含）")
    func_name: str = Field(..., description="函数名")
    module_tag: ModuleTag = Field(
        default=ModuleTag.OTHER, description="所属内核子系统"
    )
    lang: str = Field(..., description="源语言：rust / c / asm")
    raw_code: str = Field(..., description="原始函数源码")
    normalized_code: str = Field(
        default="", description="归一化后源码（去注释/统一标识符等），供哈希与嵌入使用"
    )


class SegmentHits(BaseModel):
    """Layer3 分段向量验证结果。"""

    hits: int = Field(default=0, ge=0, description="命中（cosine>阈值 且最优匹配）的段对数")
    q_total: int = Field(default=0, ge=0, description="query 函数分段总数")
    c_total: int = Field(default=0, ge=0, description="candidate 函数分段总数")
    matched_segment_pairs: list[dict] = Field(
        default_factory=list,
        description="命中段对：[{q_lines:[s,e], c_lines:[s,e], sim:float}, ...]（绝对行号）",
    )


class Evidence(BaseModel):
    """一对嫌疑函数在各层累积的证据信号。

    各字段在对应层被填充，未经过该层时保持默认值（None / 0 / False）。
    """

    simhash_distance: int | None = Field(
        default=None, description="Layer1：SimHash 海明距离，越小越相似"
    )
    vector_similarity: float | None = Field(
        default=None, description="Layer2：整函数向量余弦相似度 [0,1]"
    )
    segment_hits: SegmentHits | None = Field(
        default=None, description="Layer3：分段向量比对结果（命中段数/覆盖/匹配段对）"
    )
    exact_match_lines: int = Field(
        default=0, ge=0, description="Layer4：精确匹配的行数"
    )
    unique_string_matches: int = Field(
        default=0, ge=0, description="Layer4：命中的唯一字符串字面量数"
    )
    baseline_flag: bool = Field(
        default=False,
        description="是否疑似来自公共基线/模板代码（命中则降低查重权重）",
    )


class SuspectPair(BaseModel):
    """一对疑似抄袭的函数及其证据与最终评分，是流向 review / report 的产物。"""

    query_func: FunctionRecord = Field(..., description="待查（新提交）函数")
    candidate_func: FunctionRecord = Field(..., description="历史库中的候选函数")
    evidence: Evidence = Field(default_factory=Evidence, description="各层累积证据")
    final_score: float = Field(
        default=0.0, ge=0.0, le=1.0, description="综合相似度评分 [0,1]，越高越可疑"
    )
    # --- Layer 4（src.exact）分流与精确比对证据 ---
    tier: str = Field(
        default="",
        description="分流档位：confirmed(>0.95) / review(0.7-0.95) / weak(0.5-0.7)",
    )
    matched_spans: list[tuple[int, int, int, int]] = Field(
        default_factory=list,
        description="精确匹配的行区间（绝对文件行号）：(a_start, a_end, b_start, b_end)",
    )
    match_type_per_span: list[str] = Field(
        default_factory=list, description="每个 span 的类型：exact / renamed"
    )
