"""
用于自底向上树状报告管道的提示词构建。

会话类型（2 个产出会话 + 1 个修复兜底）：
  DIR         — 目录级聚合摘要（agent 用工具自主探索文件）
  VERDICT     — 顶层评判性结论
  JSON_REPAIR — JSON 损坏时的单 turn 修复

第 1 层：固定角色与任务声明
第 2 层：硬约束（保 JSON 合法 + 拒绝评判 / 中性差异）
第 3 层 + 第 4 层：会话专属工作流与输出格式（从 prompts/tree/*.md 加载）
"""

import tomllib
from enum import Enum
from pathlib import Path


class SessionType(str, Enum):
    """树状管道的会话类型。"""
    SUBSYS      = "subsys"
    VERDICT     = "verdict"
    DEVELOPMENT = "development"
    JSON_REPAIR = "json_repair"


# 第 1 层：角色与任务声明

LAYER_1_ROLE = """你是一位代码仓库分析助手，擅长在大型 C / Rust 项目（尤其是
操作系统内核）中做事实摘要和评判分析。

你的职责是：
{task_description}""".strip()


_SESSION_TASK_DESC: dict[SessionType, str] = {
    SessionType.SUBSYS: (
        "分析仓库中的某一个 OS 子系统（如文件系统 / 内存管理 / 进程管理）。"
        "用工具读取该子系统的源文件，**自主决定**该子系统内部的模块拆分，"
        "产出 1 份子系统总览 .md + N 份模块详细 .md + 1 份结构化 JSON。"
    ),
    SessionType.VERDICT: (
        "基于仓库根目录摘要 + repo_facts + 一级子系统摘要，"
        "综合产出顶层评判性结论："
        "5 维度评分（原创性 / 架构合理性 / 代码质量 / 文档质量 / 完整性）+ "
        "亮点 / 槽点 + 一句话总评。"
    ),
    SessionType.DEVELOPMENT: (
        "只依据用户消息提供的 Git 提交证据，复核开发过程问题候选并归纳连续开发阶段。"
        "不得编造提交、日期、代码行数或文件；问题判断和阶段结论必须给出依据与置信度。"
    ),
    SessionType.JSON_REPAIR: (
        "把损坏的 LLM 输出文本修复成合法 JSON，并通过 write_report 写入指定路径。"
        "不增加 / 不删除内容，只做语法修复。"
    ),
}


# 第 2 层：硬约束（树状管道通用版）

LAYER_2_CONSTRAINTS = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【硬性约束 — 违反任何一条则整个输出无效】

〔约束 1：语言规范——中文写作 + 技术术语英文原文〕
所有产出（JSON 字段、Markdown 正文、注释、说明）一律使用**简体中文**书写。
这里的“一律”包括标题、表头、列表项、模块名、总结、理由和每一个自然语言句子；
不得因为输入代码、仓库文档或术语是英文，就切换成英文段落或英文章节。
但以下类别**必须保留英文原文**，不做翻译：
  - 函数名 / 结构体名 / 变量名 / 文件名 / 路径（如 usertrap、TaskControlBlock）
  - 关键字 / 类型名 / 宏（如 unsafe、static、SYSCALL_FORK）
  - 通用技术术语：syscall、page table、buddy system、scheduler、trap、
    interrupt、mutex、semaphore、IPC、VFS、inode、page fault、TLB、MMU、
    kernel/user mode、context switch、ELF、ABI、FFI、SMP、cache、heap、
    stack、bootstrap、trait、impl、enum、struct、typedef
  - 工具 / 库 / 协议 / 标准名：xv6、rCore、Linux、POSIX、RISC-V、LoongArch、
    Cargo、ctags、LSP、Mermaid、ECharts
反面示例（禁止）：把 syscall 翻成"系统调用"、把 page table 翻成"页表"、
                  把 trait 翻成"特征"、把 scheduler 翻成"调度器"。
正面示例：本目录实现 syscall 分发（kernel/trap.c:42），ecall 触发后
          通过 scause 路由到对应 handler。
写入文件前必须逐项自检：除上述技术词、代码标识符、路径和专有名词外，所有自然语言
叙述均应是完整的简体中文句子；发现英文标题或英文句子时，先自行改写成中文再写出。

〔约束 2：JSON 不嵌长 Markdown〕
JSON 中所有字符串字段（summary / role / quote / reason / note）≤200 字符。
长说明、代码块、表格、mermaid / echarts 一律写到独立 .md 文件，
JSON 仅记录结构化摘要。

〔约束 3：路径与行号必须真实〕
所有 path / file / line 字段必须来自工具实际返回（read_file、
find_symbol_definition、analyze_subtree 等）或 user message 输入数据中
已有的路径。禁止编造文件路径，禁止把行号写成约数。

〔约束 4：评判 vs 中性的硬分割〕
- SUBSYS 会话的 JSON summary 字段保持**中性事实**：描述"做了什么、在何处"，
  不写"亮点 / 槽点 / 优秀 / 糟糕"等评判词。（highlights / issues 字段允许列出
  强项弱项；content .md 中可包含轻度评价；但 JSON 的 summary 保持中性。）
- VERDICT 会话产出**评判性结论**：必须明确表达项目的优劣，必须给出亮点和
  槽点，必须给一句话总评。
""".strip()


# 第 3 层 + 第 4 层：会话专属工作流（从 prompts/tree/ 加载）

def _load_session_prompts() -> "dict[SessionType, tuple[str, str]]":
    prompts_dir = Path(__file__).parent / "templates"
    result: dict[SessionType, tuple[str, str]] = {}
    for st in SessionType:
        path = prompts_dir / f"{st.value}.md"
        if not path.exists():
            result[st] = ("", "")
            continue
        content = path.read_text(encoding="utf-8")
        p1 = content.split("<!-- format -->", 1)
        workflow = p1[0].replace("<!-- workflow -->", "").strip()
        fmt = p1[1].strip() if len(p1) > 1 else ""
        result[st] = (workflow, fmt)
    return result


_SESSION_CONFIG: "dict[SessionType, tuple[str, str]]" = _load_session_prompts()


# 多会话模式的公共元数据（供 agent.py 和 setup_opencode.py 共用）

SESSION_AGENT_NAMES: dict[SessionType, str] = {
    SessionType.SUBSYS:      "os-kernel-subsys",
    SessionType.VERDICT:     "os-kernel-verdict",
    SessionType.DEVELOPMENT: "os-kernel-development",
    SessionType.JSON_REPAIR: "os-kernel-json-repair",
}


# Rust 工作区：crate 角色检测（VERDICT 阶段在事实档案里可能用到）

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
    "trap":           "中断/异常处理",
    "interrupt":      "中断/异常处理",
    "irq":            "中断/异常处理",
    "ipc":            "进程间通信",
    "pipe":           "管道（IPC）",
    "signal":         "信号机制（IPC）",
    "sync":           "同步原语",
    "lock":           "同步原语",
    "spinlock":       "自旋锁（同步原语）",
    "mutex":          "互斥锁（同步原语）",
    "smp":            "多核支持",
    "cpu":            "CPU/Hart 管理",
    "hart":           "Hart 管理（RISC-V 多核）",
    "boot":           "启动初始化",
    "startup":        "启动序列",
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


# 任务描述查询（供 setup_opencode.py 用）

def get_task_desc(session_type: SessionType) -> str:
    return _SESSION_TASK_DESC[session_type]


# 以下 build_layer_2 / format_* 辅助函数保留给 mcp_server.py 的
# initialize_analysis 工具使用：即使我们的 3 个树会话不调 MCP 工具，
# MCP server 本身仍由 OpenCode 注册并加载（其它代码可能仍引用）。


_BAR = "━" * 30


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


_DOC_LABELS = {
    "readme":     "README",
    "design_doc": "设计文档",
    "report":     "技术报告",
    "slides":     "幻灯片",
    "changelog":  "更新日志",
}


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


def _format_doc_files(doc_files: dict) -> str:
    if not doc_files:
        return "  （未找到任何文档文件）"
    lines = []
    for doc_type, entries in doc_files.items():
        label = _DOC_LABELS.get(doc_type, doc_type)
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
