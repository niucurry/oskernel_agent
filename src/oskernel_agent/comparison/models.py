"""查重引擎共享数据结构（所有模块统一引用）。

这里集中定义跨模块流转的 pydantic model。模块之间通过 JSON / SQLite 落盘解耦，
落盘时统一用 ``model_dump(mode="json")``，读回时用 ``Model.model_validate(...)``，
避免内存级耦合。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

# 基线仓库的 team 段前缀。baselines.yaml 里公共/模板/第三方库统一以 ``baseline_`` 命名
# （repo_id 形如 ``0/baseline_arceos``），各层据此判定「公共代码」而无需额外 schema 字段。
BASELINE_PREFIX = "baseline_"


def is_baseline_repo(repo_id: str) -> bool:
    """repo_id 是否指向显式基线仓库。

    正常值是 ``{year}/baseline_xxx``，但旧数据库或外部导入产物可能保留
    ``data/repos/...`` 前缀或 Windows 分隔符。只检查规范化后的最后一个路径段，
    既兼容这些表示形式，也不会把普通仓库路径中偶然出现的 ``baseline`` 字样误判。
    """
    if not repo_id:
        return False
    normalized = str(repo_id).strip().replace("\\", "/").rstrip("/")
    return bool(normalized) and normalized.rsplit("/", 1)[-1].startswith(BASELINE_PREFIX)


class ModuleTag(str, Enum):
    """内核子系统标签，用于按模块分桶比对与报告分类。

    除调度、内存、文件系统、异常、驱动与架构等核心大类外，还细分系统调用、信号、
    IPC、同步、时间、网络、安全及运行时支持，避免不同职责都落入 other。Rust 宏定义
    （macro_definition）单独成类。
    """

    SCHED = "sched"
    MM = "mm"
    FS = "fs"
    TRAP = "trap"
    SYSCALL = "syscall"
    SIGNAL = "signal"
    IPC = "ipc"
    SYNC = "sync"
    TIME = "time"
    NET = "net"
    DRIVER = "driver"
    ARCH = "arch"
    SECURITY = "security"
    RUNTIME = "runtime"
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
    code_simhash_distance: int | None = Field(
        default=None, description="归一化代码 shingle SimHash 海明距离；不经过 ANN top-k"
    )
    vector_similarity: float | None = Field(
        default=None, description="Layer2：整函数向量余弦相似度 [0,1]"
    )
    line_similarity: float | None = Field(
        default=None,
        description="Layer4：逐行匹配得到的原始相似度 [0,1]；不包含召回保底或分段重打分",
    )
    normalized_fingerprint_match: bool = Field(
        default=False,
        description="归一化代码 SHA-256 完全相同；不受 ANN top-k 限制的确定性命中",
    )
    function_name_recall: bool = Field(
        default=False,
        description="同名函数确定性补召回；仅作候选生成，仍需逐行相似度达到阈值",
    )
    structural_hash_recall: bool = Field(
        default=False,
        description="归一化代码 shingle SimHash 补召回；候选未经过 ANN top-k",
    )
    function_identity_score: float | None = Field(
        default=None,
        description="函数名、签名、行为 token 与控制流构成的具体函数身份兼容分 [0,1]",
    )
    function_identity_recall: bool = Field(
        default=False,
        description="候选由已命中文件内的函数身份邻域补召回",
    )
    function_name_exact: bool = Field(
        default=False, description="双方函数名是否完全一致；仅用于具体函数配对，不证明借鉴"
    )
    function_identity_relation: str | None = Field(
        default=None,
        description=("具体函数关系：exact_counterpart / same_name_code_clone / "
                     "same_name_only / compatible_renamed / code_clone_renamed / "
                     "family_neighbor / nonsemantic_stub"),
    )
    segment_hits: SegmentHits | None = Field(
        default=None, description="Layer3：分段向量比对结果（命中段数/覆盖/匹配段对）"
    )
    exact_match_lines: int = Field(
        default=0, ge=0, description="Layer4：逐字节相同的精确匹配行数"
    )
    renamed_match_lines: int = Field(
        default=0, ge=0, description="Layer4：仅在标识符/寄存器掩码后才相同的行数（改名复制）"
    )
    unique_string_matches: int = Field(
        default=0, ge=0, description="Layer4：命中的唯一字符串字面量数"
    )
    baseline_flag: bool = Field(
        default=False,
        description="是否疑似来自公共基线/模板代码（命中则降低查重权重）",
    )


class SuspectPair(BaseModel):
    """一对疑似借鉴/复制的函数及其证据与最终评分，是流向 review / report 的产物。"""

    query_func: FunctionRecord = Field(..., description="待查（新提交）函数")
    candidate_func: FunctionRecord = Field(..., description="历史库中的候选函数")
    evidence: Evidence = Field(default_factory=Evidence, description="各层累积证据")
    final_score: float = Field(
        default=0.0, ge=0.0, le=1.0, description="综合相似度评分 [0,1]，越高越可疑"
    )
    # --- Layer 4（oskernel_agent.comparison.exact）分流与精确比对证据 ---
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
