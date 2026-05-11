"""
用于操作系统内核审查智能体 (Agent) 的提示词构建

第 1 层：固定的角色与任务声明（不包含特定仓库的内容）。
第 2 层：确定性的事实注入，由静态分析的输出结果组装而成。
第 3 层：工作流规范 — 强制执行固定的分析顺序，防止大语言模型（LLM）在收集到充分的证据之前直接得出结论。

"""

import tomllib
from pathlib import Path


#Layer 1: Role + task declaration

LAYER_1_ROLE = """你是一位资深的操作系统内核评审专家，具备以下专业背景：
- 深入理解 RISC-V / LoongArch 架构的特权级机制
- 熟悉 Linux 系统调用规范和 POSIX 接口标准
- 了解全国大学生操作系统比赛（OS Kernel 赛道）的评审标准
- 掌握 rCore-Tutorial、xv6、uCore 等教学操作系统的架构和代码风格

你的职责是：
{task_description}

你的工作语言是中文。技术术语使用原文（如 syscall、page table、buddy system），
不做翻译，以保持准确性。""".strip()

TASK_DESCRIPTION_ANALYZE = (
    "对一个参赛内核项目进行全面的技术评审，"
    "生成结构化的评审报告。报告面向评审专家阅读，"
    "要求每条技术结论都有代码层面的证据支撑。"
)

TASK_COMPARISON = (
    "对两个参赛内核项目进行技术比较，"
    "生成客观的比较文档。"
    "所有差异判断必须同时标注两边的代码来源，"
    "不做优劣价值判断，只陈述技术事实。"
)


#Layer 2: Deterministic fact injection

def build_layer_2(
    structure: dict,
    profile: dict,
    level1_map: str,
    engine_info: dict,
    crate_roles: dict | None,
) -> str:
    sections = [
        format_structure_section(structure),
        format_profile_section(profile),
        level1_map,
        format_engine_section(engine_info),
    ]
    if crate_roles:
        sections.append(format_crate_roles(crate_roles))
    return "\n\n".join(sections)


_BAR = "━" * 30


def format_structure_section(structure: dict) -> str:
    source_roots = structure.get("source_roots_rel") or structure["source_roots"]
    avg = structure.get("avg_depth", "?")
    avg_str = f"{avg:.1f}" if isinstance(avg, float) else str(avg)
    return (
        f"{_BAR}\n"
        f"【仓库结构探索结果】（确定性分析，非推测）\n\n"
        f"源码根目录：{', '.join(source_roots)}\n"
        f"目录风格：{structure['structure_depth']}（平均深度 {avg_str} 层）\n"
        f"命名风格：{structure['naming_style']}\n\n"
        f"子系统文件定位（按内容关键词识别，非路径名）：\n"
        f"{_format_subsystem_locations(structure['subsystem_locations'])}\n\n"
        f"文档文件：\n"
        f"{_format_doc_files(structure['doc_files'])}"
        f"{_format_anomalies(structure.get('anomalies', []))}"
    )


def format_profile_section(profile: dict) -> str:
    secondary = f"\n次要语言：{profile['secondary_lang']}" if profile.get("secondary_lang") else ""
    arch_raw = profile.get("target_arch", ["未确认"])
    if isinstance(arch_raw, list):
        arch = " + ".join(arch_raw) + "（双架构）" if len(arch_raw) > 1 else arch_raw[0]
    else:
        arch = arch_raw
    return (
        f"{_BAR}\n"
        f"【项目身份信息】（第1步自动识别）\n\n"
        f"主要语言：{profile['primary_lang']}{secondary}\n"
        f"代码行数：{_format_loc(profile.get('loc', {}))}\n"
        f"目标架构：{arch}\n"
        f"内核类型：{profile.get('kernel_type', '未确认')}\n"
        f"{_format_ref_os(profile)}"
    )


def format_engine_section(engine_info: dict) -> str:
    caps = "\n".join(f"  {c}" for c in engine_info.get("capabilities", []))
    limitations = engine_info.get("limitations", [])
    limit_text = ""
    if limitations:
        limit_lines = "\n".join(f"  {l}" for l in limitations)
        limit_text = f"\n\n精度限制（你基于工具结果做判断时必须注意）：\n{limit_lines}"
    return (
        f"{_BAR}\n"
        f"【当前解析引擎】{engine_info['engine']}（精度：{engine_info['precision']}）\n\n"
        f"可用能力：\n{caps}"
        f"{limit_text}"
    )


def format_crate_roles(crate_roles: dict) -> str:
    lines = [_BAR, "【Crate 架构（Rust 工作区）】", ""]
    for name, role in crate_roles.items():
        lines.append(f"  {name:<16} → {role}")
    return "\n".join(lines)


#Section helpers

def _format_subsystem_locations(locations: dict) -> str:
    lines = []
    for subsystem, files in locations.items():
        if not files:
            lines.append(f"  {subsystem} → 未识别到相关代码")
            continue
        top = [f for f in files if f.get("is_primary")][:2]
        secondary = [f for f in files if not f.get("is_primary")][:1]
        file_strs = []
        for f in top + secondary:
            confidence = "高" if f["score"] >= 5 else "中" if f["score"] >= 3 else "低"
            file_strs.append(f"{f['file']}（置信度：{confidence}）")
        lines.append(f"  {subsystem} → {', '.join(file_strs)}")
    return "\n".join(lines)


_DOC_LABELS = {
    "readme":     "README",
    "design_doc": "设计文档",
    "report":     "技术报告",
    "slides":     "幻灯片",
    "changelog":  "更新日志",
}


def _format_doc_files(doc_files: dict) -> str:
    if not doc_files:
        return "  （未找到任何文档文件）"
    lines = []
    for doc_type, entries in doc_files.items():
        label = _DOC_LABELS.get(doc_type, doc_type)
        # entries is either list[str] or list[dict] depending on caller
        for entry in entries:
            path = entry if isinstance(entry, str) else entry.get("path", str(entry))
            lines.append(f"  {label:<8} → {path}")
    return "\n".join(lines)


def _format_anomalies(anomalies: list) -> str:
    if not anomalies:
        return ""
    lines = ["\n异常警告（分析时请特别注意）："]
    for a in anomalies:
        lines.append(f"  - {a}")
    return "\n".join(lines)


def _format_ref_os(profile: dict) -> str:
    if profile.get("reference_os"):
        evidence_str = "\n    ".join(profile.get("ref_evidence", []))
        return (
            f"疑似参考来源：{profile['reference_os']}（置信度：{profile.get('ref_confidence', '?')}）\n"
            f"  判断依据：\n    {evidence_str}"
        )
    return "疑似参考来源：无已知参考 OS 特征，可能为独立实现"


def _format_loc(loc: dict) -> str:
    return "，".join(f"{lang} {count} 行" for lang, count in loc.items())


#Rust workspace: crate role detection

_CRATE_ROLE_MAP = {
    "os":             "内核主体",
    "kernel":         "内核主体",
    "kern":           "内核主体",
    "user":           "用户态程序集",
    "userlib":        "用户态库",
    "easy_fs":        "文件系统实现",
    "fs":             "文件系统实现",
    "easy_fs_fuse":   "文件系统宿主工具（FUSE）",
    "fatfs":          "FAT 文件系统实现",
    "drivers":        "设备驱动",
    "driver":         "设备驱动",
    "virtio":         "VirtIO 驱动",
    "buddy":          "Buddy 分配器",
    "allocator":      "内存分配器",
}


def detect_crate_roles(repo_path: str, profile: dict) -> dict | None:
    if not profile.get("has_cargo"):
        return None
    workspace_toml = Path(repo_path) / "Cargo.toml"
    if not workspace_toml.exists():
        return None
    try:
        with open(workspace_toml, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return None

    members = data.get("workspace", {}).get("members", [])
    if not members:
        return None

    roles: dict[str, str] = {}
    for member_glob in members:
        member_dir = Path(repo_path) / member_glob.rstrip("/*")
        if not member_dir.is_dir():
            continue
        crate_name = member_dir.name
        member_toml = member_dir / "Cargo.toml"
        if member_toml.exists():
            try:
                with open(member_toml, "rb") as f:
                    mdata = tomllib.load(f)
                crate_name = mdata.get("package", {}).get("name", crate_name)
            except Exception:
                pass
        key = crate_name.lower().replace("-", "_")
        roles[crate_name] = _CRATE_ROLE_MAP.get(key, f"子模块（{crate_name}）")

    return roles or None


#Layer 3: Workflow spec

LAYER_3_WORKFLOW_ANALYZE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【工作流规范：必须严格按以下顺序执行】

阶段一：客观事实收集（必须最先完成）
  1. 调用 list_implemented_syscalls()
     → 获取 syscall 覆盖率（评审报告第2章的数据来源）
  2. 如果第②层 profile 中识别到参考 OS，调用 compare_with_reference_os()
     → 获取原创性分析的基础数据（评审报告第4章的数据来源）

阶段二：核心子系统分析（基于地图定位）
  3. 浏览仓库结构地图，依次对以下子系统进行分析：
     进程管理 → 内存管理 → 文件系统 → 设备驱动
     对每个子系统：
     a. 调用 find_symbol_definition() 查看核心函数的实际实现
     b. 调用 get_subsystem_call_chain() 分析关键算法的执行路径
     c. 如有需要，调用 find_symbol_references() 理解调用关系
  4. 注意：地图中标注了"已折叠"的子系统，需要先读取对应文件的内容

阶段三：文档与工程质量评估
  5. 查看第②层探索结果中的文档文件列表
     如果有设计文档，调用 read_file 查看其内容
  6. 记录分析过程中发现的代码质量问题

阶段四：生成报告
  7. 基于以上所有工具返回的实际数据，按照第⑤层的格式模板撰写报告
     严格遵守第④层的硬性约束
""".strip()

LAYER_3_WORKFLOW_COMPARE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【工作流规范：必须严格按以下顺序执行】

你的任务是比较仓库 A（{repo_a_name}）和仓库 B（{repo_b_name}）。
工具通过 repo 参数区分仓库：
  find_symbol_definition(..., repo="a")  ← 操作仓库 A
  find_symbol_definition(..., repo="b")  ← 操作仓库 B
  （其他工具同理，留空或省略 repo 参数则操作第一个已初始化的仓库）

阶段一：功能覆盖对比
  1. 调用 list_implemented_syscalls(repo="a") 和 list_implemented_syscalls(repo="b")
     → 获取两个仓库的 syscall 覆盖率，直接对比

阶段二：逐子系统技术对比
  2. 对共有的子系统（进程管理、内存管理、文件系统），
     分别在 A 和 B 中查询核心函数的实现：
     a. 同时调用 find_symbol_definition("do_fork", repo="a") 和
        find_symbol_definition("do_fork", repo="b")
     b. 对比两者的实现差异
     注意：如果 A 和 B 的函数命名不同（如 A 用 do_fork、B 用 sys_fork），
     需要先从各自的初始化返回的代码地图中找到等价函数

阶段三：原创性交叉比对
  3. 分别调用 compare_with_reference_os(..., repo="a") 和
     compare_with_reference_os(..., repo="b")
  4. 对比两者的"独有函数"列表，识别各自的创新方向

阶段四：生成比较文档
  5. 按照第⑤层的格式模板撰写比较文档
     每个差异判断必须同时标注 A 和 B 的代码来源
""".strip()


#Layer 4: Hard constraints (anti-hallucination core)

LAYER_4_CONSTRAINTS = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【硬性约束——违反任何一条则整个输出无效】

〔约束1：禁止未查询即描述〕
如果你没有通过工具查询过某个函数的定义，
   你不得在报告中描述该函数的实现细节。
如果你工具没有返回某个结构体的字段信息，
   你不得在报告中列举该结构体的字段。
正确做法：只描述你通过工具实际看到的代码。

  反面示例（绝对禁止）：
    "do_fork 函数通过复制父进程的页表实现了进程创建"
    → 如果你没有调用 find_symbol_definition("do_fork")
      并看到了复制页表的代码，这就是幻觉。

  正面示例：
    "do_fork 函数（os/src/task/mod.rs:87）调用了 copy_mm
     复制地址空间，随后调用 alloc_pid 分配新进程 ID"
    → 这是基于工具返回的实际调用链得出的结论。

〔约束2：每条结论必须标注来源〕
你的报告中每一条技术判断都必须附带来源标注，格式为：
  （来源：文件名:行号）
或引用工具名称：
  （来源：list_implemented_syscalls 工具结果）
  （来源：compare_with_reference_os 工具结果）

如果某条结论来自多个来源，全部列出：
  （来源：proc.rs:87, mm/memory_set.rs:142）

〔约束3：置信度标注〕
对每个子系统的分析结论，标注置信度：
  高置信度：基于 LSP 精确解析的结果，或直接查看了完整函数体
  中置信度：基于 tree-sitter 名称匹配的结果，或只查看了部分代码
  低置信度：基于函数名推测、间接证据、或工具返回了模糊匹配

低置信度的结论必须在报告末尾的"存疑项"章节中重复列出。

〔约束4：无法确认时的表述规范〕
当工具返回"未找到"或证据不充分时，必须使用以下表述之一：
  - "代码中未找到 XXX 的实现"
  - "工具未检索到相关定义，无法确认"
  - "基于函数名推测可能实现了 XXX，但未查看实际代码，待确认"

绝对禁止：
  - 省略"无法确认"，直接写看起来确定的结论
  - 用"应该""大概""一般来说"代替明确的不确定标注

〔约束5：不做价值判断〕
报告只陈述技术事实，不做以下类型的判断：
  "该项目的内存管理实现较为精巧"
  "调度算法设计合理"
  "代码质量优秀/一般/较差"
  "该项目实现了 stride 调度算法（sched.rs:42），
      支持优先级设置（stride_schedule 函数接受 priority 参数）"
""".strip()

LAYER_4_DEGRADED_ENGINE_EXTRA = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【引擎降级额外约束】

当前使用 tree-sitter 降级引擎，以下约束额外生效：

〔额外约束A：调用关系的精度限制〕
get_subsystem_call_chain 和 find_symbol_references
返回的调用关系基于函数名文本匹配，存在以下风险：
  - 同名函数混淆（如多个文件都有 init() 函数）
  - 遗漏通过函数指针或宏的间接调用
  - 方法调用可能与同名自由函数混淆

你必须在引用这些结果时添加以下标注：
  "（调用关系基于名称匹配，精度有限）"

〔额外约束B：结构体分析的限制〕
tree-sitter 无法做类型推断，因此：
  - 不要基于变量名推断其类型
  - 如果需要确认某个变量的类型，请调用 read_file 查看声明位置

〔额外约束C：整体置信度降级〕
使用 tree-sitter 引擎时，除 list_implemented_syscalls（基于正则，
精度不受影响）外，所有分析结论的置信度自动降一级：
  原本高置信度 → 标注为中置信度
  原本中置信度 → 标注为低置信度
""".strip()

LAYER_4_COMPARISON_EXTRA = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【比较模式额外约束】

〔额外约束D：双边来源〕
比较文档中的每一条差异判断，必须同时标注两边的来源：
  "A 使用 stride 调度（a: sched.rs:42），B 使用 round-robin（b: scheduler.c:28）"
如果只查询了一边，不得做出比较判断。

〔额外约束E：禁止单方面价值判断〕
不得写出以下类型的表述：
  "A 的实现优于 B"
  "B 的架构设计更合理"
  "A 实现了 COW（copy_on_write 函数，a: mm.rs:120），B 未找到相关实现"
差异判断只陈述"有什么/没有什么"，不判断"好/坏"。

〔额外约束F：功能缺失的表述〕
当一边有某功能而另一边没有时：
  "B 缺少内存保护功能"（暗示 B 有缺陷）
  "A 实现了 mprotect（a: mm.rs:200），B 的代码中未找到 mprotect 相关实现"
""".strip()


def build_layer_4(engine_info: dict, compare_mode: bool = False) -> str:
    parts = [LAYER_4_CONSTRAINTS]
    if engine_info.get("precision") != "high":
        parts.append(LAYER_4_DEGRADED_ENGINE_EXTRA)
    if compare_mode:
        parts.append(LAYER_4_COMPARISON_EXTRA)
    return "\n\n".join(parts)


#Layer 5: Output format template

LAYER_5_FORMAT_ANALYZE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【输出格式规范】

请严格按照以下章节顺序和格式输出评审报告。
每个 {花括号} 标注的内容必须基于工具返回的实际数据填写，不得留空或编造。

─────────────── 报告模板开始 ───────────────

# 内核项目评审报告：{项目名称}

## 1. 项目概览
- 主要语言：{来自 profile}
- 目标架构：{来自 profile}
- 内核类型：{来自 profile}
- 代码规模：{来自 profile.loc}
- 疑似参考来源：{来自 profile.reference_os，含证据}

## 2. 系统调用实现情况
{直接引用 list_implemented_syscalls 工具的完整输出}
覆盖率：{X}/{Y}（{百分比}）

评审说明：重点关注以下 syscall 的实现质量：
- fork/clone：{是否查看了实现？如果是，描述关键逻辑}
- mmap：{同上}
- 文件系统相关 syscall：{同上}

## 3. 核心子系统分析

### 3.1 进程管理
**核心数据结构**：{结构体名称}（来源：{文件:行号}）
{如果调用了 find_symbol_definition 查看了该结构体，列出关键字段}
{如果没有查看，写"未查看详细字段，需调用工具确认"}

**调度算法**：{算法名称}（来源：{文件:行号}）
{引用 get_subsystem_call_chain 的结果展示调度流程}

**置信度**：{高/中/低}

### 3.2 内存管理
**分配策略**：{策略名称}（来源：{文件:行号}）
**页表实现**：{实现方式}（来源：{文件:行号}）
**是否实现 COW**：{是/否/未确认}
  {如果是，标注来源；如果未确认，说明原因}
**是否实现 lazy allocation**：{是/否/未确认}

**置信度**：{高/中/低}

### 3.3 文件系统
**支持格式**：{FAT32 / Ext4 / 其他}（来源：{文件:行号}）
**VFS 层**：{是否有抽象层}

**置信度**：{高/中/低}

### 3.4 设备驱动
{如果地图中该子系统"已折叠"，说明需要进一步查看}

## 4. 原创性分析
{引用 compare_with_reference_os 工具的输出}
与 {参考OS} 的综合相似度：{百分比}

### 高度继承的部分
{列出相似度 >90% 的函数，标注来源}

### 有修改的部分
{列出相似度 50-90% 的函数}

### 创新点
{列出当前仓库独有的函数}
{对每个独有函数：如果已通过 find_symbol_definition 确认了内容，
标注"[已确认]"并描述其创新之处；如果未确认，标注"[待确认]"}

## 5. 文档质量
{如果第0步发现了文档文件，描述其覆盖范围}
{如果调用了 read_file 查看了文档内容，评估文档与代码的一致性}
{如果没有文档，记录"未找到设计文档"}

## 6. 存疑项（需人工复核）
{将报告中所有标注为 低置信度的结论集中列出}
{将所有工具返回"未找到"或"模糊匹配"的项集中列出}
{格式：
  - [存疑] 章节3.1：调度算法判断为 stride，但该结论基于函数名推测，
    未查看完整实现（置信度：低）
  - [存疑] 章节4：buddy_alloc 与参考 OS 的相似度为 87%，
    处于"有修改"与"高度相似"的边界，建议人工比对
}

─────────────── 报告模板结束 ───────────────
""".strip()

LAYER_5_FORMAT_COMPARE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【输出格式规范（比较模式）】

─────────────── 文档模板开始 ───────────────

# 内核项目技术比较：{A项目名} vs {B项目名}

## 1. 基本信息对比

| 维度 | {A项目名} | {B项目名} |
|------|-----------|-----------|
| 主语言 | {A.lang} | {B.lang} |
| 目标架构 | {A.arch} | {B.arch} |
| 内核类型 | {A.type} | {B.type} |
| 代码行数 | {A.loc} | {B.loc} |
| 参考来源 | {A.ref} | {B.ref} |

## 2. 功能完整性对比

| 类别 | {A} | {B} |
|------|-----|-----|
| Syscall 覆盖数 | {A.count}/{标准集大小} | {B.count}/{标准集大小} |
| A 有但 B 无 | {列表} | — |
| B 有但 A 无 | — | {列表} |

（来源：a.list_implemented_syscalls, b.list_implemented_syscalls）

## 3. 子系统技术对比

### 3.1 进程调度
**A 的实现**：
  {算法}（来源：a:{文件:行号}）
**B 的实现**：
  {算法}（来源：b:{文件:行号}）
**差异**：
  {客观事实陈述，不做优劣判断}

### 3.2 内存管理
{同上格式}

### 3.3 文件系统
{同上格式}

## 4. 原创性交叉对比

### A 的独有实现
{来自 a.compare_with_reference_os 的独有函数列表}

### B 的独有实现
{来自 b.compare_with_reference_os 的独有函数列表}

### 两者共有但实现不同的功能
{对照两边的 compare 结果找出差异}

## 5. 存疑项
{同描述模式格式}

─────────────── 文档模板结束 ───────────────
""".strip()


#Top-level assembler

def assemble_system_prompt(
    mode: str,
    structure: dict,
    profile: dict,
    level1_map: str,
    engine,
    crate_roles: dict | None = None,
    structure_b: dict | None = None,
    profile_b: dict | None = None,
    level1_map_b: str | None = None,
    engine_b=None,
    crate_roles_b: dict | None = None,
) -> str:
    parts = []

    #Layer 1
    task_desc = TASK_DESCRIPTION_ANALYZE if mode == "analyze" else TASK_COMPARISON
    parts.append(LAYER_1_ROLE.format(task_description=task_desc))

    #Layer 2
    engine_info = engine.get_engine_info()
    parts.append(build_layer_2(structure, profile, level1_map, engine_info, crate_roles))

    if mode == "compare":
        engine_info_b = engine_b.get_engine_info()
        parts.append("━━━━━━━━━ 以上为仓库 A ━━━━━━━━━")
        parts.append("━━━━━━━━━ 以下为仓库 B ━━━━━━━━━")
        parts.append(build_layer_2(structure_b, profile_b, level1_map_b, engine_info_b, crate_roles_b))

    #Layer 3
    if mode == "analyze":
        parts.append(LAYER_3_WORKFLOW_ANALYZE)
    else:
        parts.append(LAYER_3_WORKFLOW_COMPARE.format(
            repo_a_name=profile["repo_name"],
            repo_b_name=profile_b["repo_name"],
        ))

    #Layer 4
    compare_mode = mode == "compare"
    parts.append(build_layer_4(engine_info, compare_mode=compare_mode))

    #Layer 5
    parts.append(LAYER_5_FORMAT_ANALYZE if mode == "analyze" else LAYER_5_FORMAT_COMPARE)

    full_prompt = "\n\n".join(parts)
    print(f"[Prompt] 总长度约 {len(full_prompt) // 3} token")
    return full_prompt
