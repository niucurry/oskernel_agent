"""
按 OS 概念分层的报告构建器：repo → tree.json（单一真相源）。

树结构（3 层）：
  Level 0  根节点（VERDICT 顶层评判）
  Level 1  OS 子系统：启动模块 / 进程管理 / 内存管理 / 文件系统 / 系统调用 /
           设备管理 / 硬件抽象 / 其他
  Level 2  模块：由 SUBSYS agent 在分析阶段动态识别（如 文件系统 →
           VFS 层 / inode 层 / 块缓存 / 日志层）

阶段：
  A. 子系统枚举：按 SUBSYSTEM_FINGERPRINTS 把所有源文件归类到子系统
  B. SUBSYS 聚合（并行）：每个子系统一次 LLM 会话，agent 决定模块拆分
     + 写子系统总览 .md + 写各模块 .md + 写结构化 JSON
  C. VERDICT：综合所有子系统摘要 + repo_facts → 顶层评判

调用方：oskernel_agent.cli.agent:_run_tree_mode
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .. import config
from ..parsers.symbol_db import iter_source_files
from ..parsers.code_parser import (
    classify_files_by_content,
    find_source_roots,
)
from ..engines.llm_batch import (
    BatchTask, opencode_serial_enabled, run_batch_task,
)
from ..finals.readability import is_dependency_scope_only_issue

SCHEMA_VERSION = "tree-v3"

MAX_MODULES_PER_SUBSYS = 8   # 每个子系统至多 N 个模块槽位
MAX_FILES_IN_SUBSYS_PROMPT = 80

_DEPENDENCY_PATH_PARTS = {
    "vendor", "third_party", "thirdparty", "external", "node_modules", "target",
    "example", "examples", "test", "tests", "bench", "benches", "demo", "demos",
}


def _is_dependency_evidence_path(location: str) -> bool:
    path = str(location or "").split(":", 1)[0].replace("\\", "/")
    return any(part.casefold() in _DEPENDENCY_PATH_PARTS for part in path.split("/"))


def _keep_system_level_issue(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    if _is_dependency_evidence_path(str(item.get("path") or "")):
        return False
    return not is_dependency_scope_only_issue(str(item.get("quote") or ""))

# 顶层评判 6 维度及其在总分中的权重（默认等权；如需侧重可调）。
# score_total 由这些维度加权平均确定性算出，不再采信 LLM 自填的总分。
VERDICT_DIMENSIONS = ["原创性", "架构合理性", "代码质量", "文档质量", "完整性", "功能性"]
VERDICT_WEIGHTS = {
    "原创性":     1.0,
    "架构合理性": 1.0,
    "代码质量":   1.0,
    "文档质量":   1.0,
    "完整性":     1.0,
    "功能性":     1.0,
}

_LANG_BY_EXT = {
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    ".rs": "rust",
    ".s": "asm", ".S": "asm", ".asm": "asm",
    ".py": "python", ".sh": "bash",
    ".md": "markdown", ".txt": "text", ".rst": "rst",
    ".toml": "toml",
}
_SKIP_LANGS = {"markdown", "text", "rst", "toml", "other"}

_VERDICT_DUPLICATE_SCORE_RE = re.compile(
    r"<p>\s*<strong>\s*(?:总?评分|综合分)[：:].*?</p>",
    re.IGNORECASE | re.DOTALL,
)

# 与 code_parser.SUBSYSTEM_FINGERPRINTS 同口径；保留显示顺序
_SUBSYS_DISPLAY_ORDER = [
    "启动模块", "内存管理", "进程管理", "文件系统", "\u7f51\u7edc",
    "设备管理", "系统调用", "硬件抽象", "其他",
]


# 内容指纹适合确认一个文件“做了什么”，但入口薄封装、模块导出文件和仅含类型
# 声明的文件经常达不到命中阈值。只在内容分类没有结论时，按明确的目录/文件名
# 语义回退，避免把 mm、task、fs、driver、arch 等一股脑归入“其他”。
_PATH_SUBSYSTEM_TOKENS = [
    ("启动模块", {"boot", "bootstrap", "entry", "init", "loader", "startup"}),
    ("内存管理", {"alloc", "allocator", "heap", "memory", "mm", "page", "paging", "vm"}),
    ("进程管理", {
        "ipc", "process", "proc", "sched", "scheduler", "signal", "sync", "task", "thread",
    }),
    ("文件系统", {"file", "filesystem", "fs", "inode", "vfs"}),
    ("\u7f51\u7edc", {"net", "network", "socket", "tcp", "udp", "vsock"}),
    ("设备管理", {"device", "devices", "driver", "drivers"}),
    ("系统调用", {"syscall", "syscalls"}),
    ("硬件抽象", {"arch", "hal", "platform"}),
]


def _fallback_subsystem_for_path(path: str) -> str | None:
    """Infer a subsystem from unambiguous path components only."""
    normalized = path.replace("\\", "/").lower()
    parts: set[str] = set()
    for component in normalized.split("/"):
        parts.update(token for token in re.split(r"[^a-z0-9]+", component) if token)
    for subsystem, tokens in _PATH_SUBSYSTEM_TOKENS:
        if parts & tokens:
            return subsystem
    return None


def _detect_lang(path: Path) -> str:
    return _LANG_BY_EXT.get(path.suffix.lower(), "other")


def _safe_filename_part(s: str) -> str:
    """转为安全的文件名片段。"""
    keep = []
    for ch in s:
        if ch.isalnum() or ch in "-_":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep) or "root"


def _read_md_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# 并发度

_DEFAULT_SUBSYS_CONCURRENCY = 10


def _subsys_concurrency() -> int:
    raw = os.environ.get("AGENT_SUBSYS_CONCURRENCY", "").strip()
    if not raw:
        return _DEFAULT_SUBSYS_CONCURRENCY
    try:
        return max(1, min(int(raw), 16))
    except ValueError:
        return _DEFAULT_SUBSYS_CONCURRENCY


# 阶段 A：按 OS 子系统归类

def enumerate_subsystems(repo_path: Path) -> tuple[dict, int]:
    """
    扫描仓库源文件，优先按 SUBSYSTEM_FINGERPRINTS 归类；内容指纹没有结论时
    再按明确的路径组件回退，仍无法判断的归到“其他”。
    返回 (tree_root, total_file_count)。
    """
    repo_path = Path(repo_path).resolve()

    # 1. 枚举所有源文件
    all_files: list[dict] = []
    for abs_p in iter_source_files(str(repo_path)):
        try:
            st = abs_p.stat()
            rel = abs_p.resolve().relative_to(repo_path)
        except (OSError, ValueError):
            continue
        if _is_dependency_evidence_path(rel.as_posix()):
            continue
        lang = _detect_lang(abs_p)
        if lang in _SKIP_LANGS:
            continue
        all_files.append({
            "path":  str(rel),
            "name":  abs_p.name,
            "lang":  lang,
            "size":  st.st_size,
            "mtime": int(st.st_mtime_ns),
        })

    # 2. 调内容指纹分类
    source_roots = find_source_roots(repo_path)
    subsys_locations = classify_files_by_content(str(repo_path), source_roots)
    # 把 (file → primary subsystem) 映射建出来
    file_to_subsys: dict[str, str] = {}
    for subsys, entries in subsys_locations.items():
        for e in entries:
            if e.get("is_primary"):
                # 已分配过的不覆盖（按 _SUBSYS_DISPLAY_ORDER 顺序优先）
                file_to_subsys.setdefault(e["file"], subsys)

    # 3. 按子系统分组
    by_subsys: dict[str, list[dict]] = {s: [] for s in _SUBSYS_DISPLAY_ORDER}
    for f in all_files:
        subsys = (
            file_to_subsys.get(f["path"])
            or _fallback_subsystem_for_path(f["path"])
            or "其他"
        )
        by_subsys.setdefault(subsys, []).append(f)

    # 4. 装配 tree
    root: dict = {
        "type":     "root",
        "name":     repo_path.name,
        "path":     "",
        "children": [],
    }
    for subsys_name in _SUBSYS_DISPLAY_ORDER:
        files = by_subsys.get(subsys_name, [])
        if not files:
            continue
        subsys_node: dict = {
            "type":     "subsystem",
            "name":     subsys_name,
            "path":     f"<subsys>/{subsys_name}",
            "files":    files,         # 该子系统的源文件清单（不作为 children 渲染）
            "children": [],            # SUBSYS agent 写出的模块节点会填到这里
        }
        root["children"].append(subsys_node)

    return root, len(all_files)


# 阶段 B：SUBSYS 聚合

def _build_subsys_outputs(subsys_node: dict, work_dir: Path) -> dict:
    """预分配子系统的所有输出路径（子系统总览 + N 个模块槽位）。"""
    safe = _safe_filename_part(subsys_node["name"])
    base = f"subsys-{safe}"
    return {
        "json_path":        str(work_dir / f"{base}.json"),
        "content_path":     str(work_dir / f"{base}.content.md"),
        "module_paths":     [
            str(work_dir / f"{base}.module-{i:03d}.md")
            for i in range(1, MAX_MODULES_PER_SUBSYS + 1)
        ],
    }


def _files_for_subsys_prompt(files: list[dict]) -> list[dict]:
    """控制 user message 长度，避免 Windows CreateProcess 命令行长度限制。"""
    limit_raw = os.environ.get("AGENT_SUBSYS_PROMPT_FILE_LIMIT", "").strip()
    try:
        limit = int(limit_raw) if limit_raw else MAX_FILES_IN_SUBSYS_PROMPT
    except ValueError:
        limit = MAX_FILES_IN_SUBSYS_PROMPT
    limit = max(20, min(limit, 200))
    if len(files) <= limit:
        picked = files
    else:
        # 覆盖更多目录和语言，比简单取前 N 个更利于 agent 建立子系统轮廓。
        ranked: list[tuple[tuple[str, str, str], dict]] = []
        for f in files:
            path = str(f.get("path", ""))
            top_dir = path.split("/", 1)[0]
            ranked.append(((top_dir, str(f.get("lang", "")), path), f))
        picked = [f for _, f in sorted(ranked)[:limit]]
    return [
        {"path": f["path"], "name": f["name"], "lang": f["lang"]}
        for f in picked
    ]


def _build_subsys_request(subsys_node: dict, repo_path: Path,
                           outputs: dict, facts: dict | None) -> str:
    """构造 SUBSYS agent 的 user message。"""
    files = subsys_node["files"]
    prompt_files = _files_for_subsys_prompt(files)
    payload = {
        "repo_path":      str(repo_path),
        "subsystem":      subsys_node["name"],
        "reference_os":   (facts or {}).get("meta", {}).get("reference_os"),
        "syscall_facts":  (facts or {}).get("syscall", {}),
        "file_count":     len(files),
        "files_truncated": len(prompt_files) < len(files),
        "files":          prompt_files,
        "outputs":        outputs,
    }
    return (
        "【最终交付语言】必须一次性生成完整的简体中文报告。标题、表头、列表项、"
        "JSON 描述字段和所有自然语言句子都必须是中文；函数名、类型名、路径、代码标识符"
        "及约定的英文技术术语保持原文。禁止出现整句英文或整节英文。\n\n"
        "你负责分析仓库中的某一个 OS 子系统（如文件系统、内存管理）。"
        "请阅读这些代码，**自己决定该子系统内部的模块拆分**"
        "（典型 2–5 个模块，最多 8 个），然后产出：\n\n"
        "  a. 子系统总览 **HTML 片段**（写到 outputs.content_path）—— 总评、模块列表\n"
        "  b. 每个模块的详细 **HTML 片段**（写到 outputs.module_paths[i] 中你选用的槽位）\n"
        "  c. 结构化 JSON（写到 outputs.json_path）—— 含模块清单与各模块槽位号\n\n"
        "若 subsystem 是系统调用，syscall_facts.standard_count 只是函数定义正则统计。"
        "工具统计与其不一致时必须同时说明口径差异；任何静态数量都不得写成语义可用或测试通过。\n"
        "除非已经逐项核验源码与测试证据，不得使用“完整”“全部”“完全一致”“确保兼容”等绝对化能力宣称；"
        "应改写为源码能够直接证明的模块、机制或兼容目标。\n"
        "第三方库（如 smoltcp、lwIP、LittleFS）本身不作为参赛作品的缺陷或自研亮点；只评价作品自有适配层"
        "及操作系统可见行为，例如驱动接入、系统调用 ABI、阻塞/非阻塞、信号、超时、poll/epoll、路由和真实设备收发。"
        "不得仅依据注释或‘不是完整 Linux 协议栈/文件系统’作负面结论；问题必须给出项目自有代码中的具体行为"
        "以及对系统调用、应用运行或硬件路径的可验证影响。\n\n"
        "内容直接写 HTML（不要 Markdown）：用 `<h3>/<p>/<ul>/<table>` 等语义标签；"
        "**不要画架构图/流程图**（不要 `<pre class=\"mermaid\">`），用文字说明模块关系；"
        "文件引用写纯文本 path:line（自动变链接）。\n\n"
        "工作步骤：\n"
        "1. 不要调用 initialize_analysis；工具会根据 repo_path 自动初始化\n"
        "2. files 可能是截断索引；完整代码以 MCP 工具看到的仓库为准\n"
        "3. 直接调用 analyze_subtree('') 或针对 files 中目录调 analyze_subtree(dir)\n"
        "4. 浏览 files 列表与符号清单，识别模块拆分\n"
        "5. read_file / find_symbol_definition 看关键模块的实现细节\n"
        "6. 工具调用总数 ≤12 次\n\n"
        "**写出顺序**：先写各模块 HTML → 再写子系统总览 HTML → **最后**写 JSON。\n"
        "**不要给子系统或模块打分**（JSON 无 score 字段）。\n\n"
        "**不要对子系统或模块打分**——评分只在顶层 VERDICT 会话产出。\n\n"
        "最终描述报告会将这些事实组合成每个并列模块不超过 300 字的评委分析。"
        "summary 应为 140–200 字，覆盖结构、关键机制和已完成能力；每个 "
        "modules[].summary 应为 80–160 字且不要重复总摘要原句。highlights 写 2–6 条"
        "有 file:line 的实现能力或设计优点，并尽量覆盖不同子模块。若子系统名为“其他”或"
        "“未分类”，必须按定时器、日志、随机数、进程间通信等真实职责拆成独立模块，"
        "不得再使用“其他/杂项”作为模块名。\n\n"
        "JSON schema：\n"
        '{\n'
        '  "name":"...","role":"...","summary":"140–200字",\n'
        '  "highlights":[{"path":"...","quote":"..."}],\n'
        '  "issues":[{"path":"...","severity":"low|medium|high","quote":"..."}],\n'
        '  "modules":[\n'
        '    {"slot":1,"name":"模块名","summary":"80–160字",'
        '"file_paths":["..."]}\n'
        '  ]\n'
        '}\n\n'
        "modules[].slot 是 1..8 之间的整数，对应你用 outputs.module_paths[slot-1] "
        "写出的那份 .md（slot 从 1 开始计数）。\n\n"
        "证据与模块路径硬性自检：highlights/issues 的 path 必须是实际文件并精确到 "
        "file:line；modules[].file_paths 只能填写实际存在的源文件，严禁填写目录。"
        "files 只是候选索引，无须为了覆盖全部候选而用目录代替文件；每个模块选择 1–4 个"
        "最具代表性的真实文件即可。写 JSON 前逐项核对。\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n"
        "写入前做最后一次语言自检：若任何标题、段落、表格单元格或 JSON 描述仍是英文，"
        "先改写成简体中文再调用写入工具；不要输出英文版后等待后续翻译。"
    )


def _subsys_fallback(subsys_node: dict) -> dict:
    return {
        "name":       subsys_node["name"],
        "role":       subsys_node["name"],
        "summary":    "",
        "highlights": [],
        "issues":     [],
        "modules":    [],
        "_error":     "subsystem_analysis_failed",
    }


def _validate_subsys_result(
    parsed: dict,
    subsystem_name: str,
    repo_path: Path | None = None,
) -> None:
    """子系统分析必须有真实总览和至少一个完整模块，否则拒绝继续生成报告。"""
    if parsed.get("_error"):
        raise RuntimeError(f"{subsystem_name} 语义聚合失败：{parsed['_error']}")
    if not str(parsed.get("summary") or "").strip():
        raise RuntimeError(f"{subsystem_name} 缺少子系统语义摘要")
    if not str(parsed.get("content") or "").strip():
        raise RuntimeError(f"{subsystem_name} 缺少子系统分析正文")
    parsed["highlights"] = [
        item for item in (parsed.get("highlights") or [])
        if isinstance(item, dict)
        and not _is_dependency_evidence_path(str(item.get("path") or ""))
    ]
    parsed["issues"] = [
        item for item in (parsed.get("issues") or []) if _keep_system_level_issue(item)
    ]
    modules = parsed.get("modules") or []
    if not isinstance(modules, list) or not modules:
        raise RuntimeError(f"{subsystem_name} 未形成任何真实模块分析")
    for index, module in enumerate(modules, start=1):
        if not isinstance(module, dict):
            raise RuntimeError(f"{subsystem_name} 第 {index} 个模块格式无效")
        module["file_paths"] = [
            path for path in (module.get("file_paths") or [])
            if not _is_dependency_evidence_path(str(path))
        ]
        missing = [field for field in ("name", "summary", "content")
                   if not str(module.get(field) or "").strip()]
        if not module.get("file_paths"):
            missing.append("file_paths")
        if missing:
            raise RuntimeError(
                f"{subsystem_name} 第 {index} 个模块缺少：{'、'.join(missing)}")
        if repo_path is not None:
            normalized_files: list[str] = []
            for file_path in module.get("file_paths") or []:
                canonical_path, _ = _validate_repo_location(
                    repo_path, str(file_path),
                    label=f"{subsystem_name} 第 {index} 个模块文件", require_line=False,
                )
                normalized_files.append(canonical_path)
            module["file_paths"] = normalized_files
    if repo_path is not None:
        _validate_structured_evidence(
            parsed.get("highlights") or [], repo_path,
            label=f"{subsystem_name}亮点",
        )
        _validate_structured_evidence(
            parsed.get("issues") or [], repo_path,
            label=f"{subsystem_name}问题",
        )


def _process_one_subsys(subsys_node: dict, repo_path: Path,
                         work_dir: Path, facts: dict | None) -> None:
    """处理一个子系统节点：跑 LLM、读回所有 .md、填充节点与模块子节点。"""
    outputs   = _build_subsys_outputs(subsys_node, work_dir)
    out_path  = Path(outputs["json_path"])
    module_paths = outputs["module_paths"]

    def _enrich(parsed: dict) -> dict:
        """把 agent 落盘的 HTML 正文读进 parsed，使其随 JSON 一起进缓存。"""
        parsed["content"] = _read_md_if_exists(Path(outputs["content_path"]))
        for i, m in enumerate(parsed.get("modules") or [], start=1):
            slot = int(m.get("slot") or i)
            m["file_paths"] = _normalize_adjacent_module_paths(
                m.get("file_paths") or [], repo_path,
            )
            if 1 <= slot <= MAX_MODULES_PER_SUBSYS:
                m["content"] = _read_md_if_exists(Path(module_paths[slot - 1]))
        # 正常情况下提示词已直接生成中文；只有检测出英文正文时才调用翻译兜底。
        from .lang_guard import normalize_tree_language, normalize_tree_titles
        normalize_tree_language(parsed)
        normalize_tree_titles(parsed)
        return parsed

    from .lang_guard import language_output_complete

    def _delivery_complete(parsed: dict) -> bool:
        if not language_output_complete(parsed):
            return False
        try:
            _validate_subsys_result(parsed, subsys_node["name"], repo_path)
        except RuntimeError:
            return False
        return True

    task = BatchTask(
        batch_id=f"subsys-{_safe_filename_part(subsys_node['name'])}",
        agent_name="os-kernel-subsys",
        user_request=_build_subsys_request(
            subsys_node, repo_path, outputs, facts),
        output_path=out_path,
        cache_dir=work_dir,  # cache_enabled=False；该路径仅满足 BatchTask 接口
        cache_key="",
        cache_enabled=False,
        fallback=_subsys_fallback(subsys_node),
        repo_path=repo_path,
        enrich=_enrich,
        cache_validator=_delivery_complete,
    )
    parsed = run_batch_task(
        task,
        schema_hint='{"name":str,"role":str,"summary":str,'
                    '"highlights":[...],"issues":[...],'
                    '"modules":[{slot:int,name:str,summary:str,'
                    'file_paths:[...]}]}',
        timeout=600,
    )
    _validate_subsys_result(parsed, subsys_node["name"], repo_path)

    _apply_subsys_result(subsys_node, parsed, repo_path)


def _apply_subsys_result(subsys_node: dict, parsed: dict, repo_path: Path) -> None:
    """把已经验证并补齐正文的子系统结果装配到报告树。"""

    # 填子系统字段（子系统/模块不打分，评分只在顶层 VERDICT）
    # 正文已由 enrich 读入 parsed（含缓存命中场景）
    from oskernel_agent.finals.readability import concise_module_summary, explain_terms_on_first_use

    subsys_node["role"]       = parsed.get("role", subsys_node["name"])
    subsys_node["summary"]    = parsed.get("summary", "")
    subsys_node["content"]    = parsed.get("content", "")
    subsys_node["brief"]      = concise_module_summary(
        parsed.get("summary") or parsed.get("content") or subsys_node["name"]
    )
    subsys_node["highlights"] = parsed.get("highlights", [])
    subsys_node["issues"]     = parsed.get("issues", [])
    # 把 modules 转成 children
    subsys_node["children"] = []
    for i, m in enumerate(parsed.get("modules") or [], start=1):
        slot = int(m.get("slot") or i)
        if not (1 <= slot <= MAX_MODULES_PER_SUBSYS):
            continue
        module_paths = _normalize_adjacent_module_paths(
            m.get("file_paths", []), repo_path
        )
        module_summary = _normalize_module_content_paths(
            m.get("summary", ""), module_paths
        )
        module_content = _normalize_module_content_paths(
            m.get("content", ""), module_paths
        )
        subsys_node["children"].append({
            "type":       "module",
            "name":       explain_terms_on_first_use(
                m.get("name", f"模块 {slot}")
            ),
            "path":       f"{subsys_node['path']}/m{slot:03d}",
            "summary":    module_summary,
            "brief":      concise_module_summary(
                module_summary or module_content or m.get("name", f"模块 {slot}")
            ),
            "file_paths": module_paths,
            "content":    module_content,
        })


def load_subsys_stage_from_artifacts(
    tree_root: dict,
    repo_path: Path,
    work_dir: Path,
) -> None:
    """从已通过 AI 生成的阶段产物恢复子系统树，不再次调用模型。

    顶层阶段校验失败不应迫使所有子系统重新分析。JSON 修复产物优先于模型写出的
    原始 JSON；恢复时仍执行与正常流水线相同的语言、路径和完整性校验。
    """
    from .lang_guard import normalize_tree_language, normalize_tree_titles

    for subsys_node in tree_root.get("children", []):
        if subsys_node.get("type") != "subsystem":
            continue
        outputs = _build_subsys_outputs(subsys_node, work_dir)
        original_path = Path(outputs["json_path"])
        repaired_path = original_path.with_name(f"{original_path.stem}.repair.json")
        artifact_path = repaired_path if repaired_path.exists() else original_path
        try:
            parsed = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"无法恢复{subsys_node['name']}阶段产物：{artifact_path}：{exc}"
            ) from exc

        parsed["content"] = _read_md_if_exists(Path(outputs["content_path"]))
        for index, module in enumerate(parsed.get("modules") or [], start=1):
            slot = int(module.get("slot") or index)
            module["file_paths"] = _normalize_adjacent_module_paths(
                module.get("file_paths") or [], repo_path,
            )
            if 1 <= slot <= MAX_MODULES_PER_SUBSYS:
                module["content"] = _read_md_if_exists(
                    Path(outputs["module_paths"][slot - 1])
                )
        normalize_tree_language(parsed)
        normalize_tree_titles(parsed)
        _validate_subsys_result(parsed, subsys_node["name"], repo_path)
        _apply_subsys_result(subsys_node, parsed, repo_path)


def _normalize_adjacent_module_paths(paths: list, repo_path: Path) -> list[str]:
    """把模型省略目录的相邻文件名补成仓库内真实路径。"""
    normalized: list[str] = []
    repo_root = repo_path.resolve()
    for value in paths or []:
        raw = str(value or "").strip().replace("\\", "/")
        if not raw:
            continue
        candidate = raw
        exact = repo_root / Path(raw)
        if not exact.is_file() and "/" not in raw and normalized:
            parent = Path(normalized[-1]).parent
            adjacent = (parent / raw).as_posix()
            if (repo_root / Path(adjacent)).is_file():
                candidate = adjacent
        if not exact.is_file() and candidate == raw:
            suffix = "/" + raw.lstrip("./")
            matches: list[str] = []
            for match in repo_root.rglob(Path(raw).name):
                if not match.is_file() or ".git" in match.parts:
                    continue
                relative = match.relative_to(repo_root).as_posix()
                if relative == raw or relative.endswith(suffix):
                    matches.append(relative)
                    if len(matches) > 1:
                        break
            if len(matches) == 1:
                candidate = matches[0]
        normalized.append(candidate)
    return normalized


def _normalize_module_content_paths(content: str, paths: list[str]) -> str:
    """用模块真实文件列表补全正文中的裸 ``文件名:行号``。"""
    by_name: dict[str, list[str]] = {}
    for path in paths:
        by_name.setdefault(Path(path).name, []).append(path)
    normalized = str(content or "")
    for name, candidates in by_name.items():
        if len(candidates) != 1 or "/" not in candidates[0]:
            continue
        normalized = re.sub(
            rf"(?<![/\\A-Za-z0-9_.-]){re.escape(name)}(?=$|[^A-Za-z0-9_.-])",
            candidates[0],
            normalized,
        )
    return normalized


def _normalize_verdict_content_paths(parsed: dict) -> None:
    """用顶层结构化证据补全总评正文中的裸 ``文件名:行号``。"""
    evidence_files: list[str] = []
    for field in ("highlights", "issues"):
        for item in parsed.get(field) or []:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("path") or "").strip()
            match = _SOURCE_LOCATION_RE.match(raw)
            evidence_files.append(match.group(1) if match else raw)
    for item in parsed.get("hardcode_reviews") or []:
        if isinstance(item, dict) and item.get("path"):
            evidence_files.append(str(item["path"]))
    parsed["content"] = _normalize_module_content_paths(
        str(parsed.get("content") or ""), evidence_files,
    )


def run_subsys_stage(tree_root: dict, repo_path: Path,
                      work_dir: Path, facts: dict | None) -> None:
    """对每个子系统并发跑一次 SUBSYS agent，原地填子系统与模块节点。"""
    subsys_nodes = [c for c in tree_root.get("children", [])
                    if c.get("type") == "subsystem"]
    if not subsys_nodes:
        return

    requested_workers = _subsys_concurrency()
    serial_opencode = opencode_serial_enabled()
    workers = 1 if serial_opencode else requested_workers
    scheduled_workers = min(workers, len(subsys_nodes))
    serial_note = ""
    if serial_opencode:
        serial_note = "；OpenCode CLI 串行执行以避免本地数据库锁"
    print(f"[tree] SUBSYS 阶段：{len(subsys_nodes)} 个子系统"
          f"（并发 {scheduled_workers}{serial_note}）",
          file=sys.stderr, flush=True)

    with ThreadPoolExecutor(max_workers=scheduled_workers) as ex:
        futs = {
            ex.submit(_process_one_subsys, n, repo_path,
                      work_dir, facts): n
            for n in subsys_nodes
        }
        failures: list[str] = []
        for fut in as_completed(futs):
            n = futs[fut]
            try:
                fut.result()
            except Exception as e:
                failures.append(f"{n['name']}：{e}")
                print(f"[tree] {n['name']} 异常：{e}",
                      file=sys.stderr, flush=True)
        if failures:
            raise RuntimeError("子系统语义分析未完整完成，拒绝生成占位报告：" + "；".join(failures))


# 阶段 C：VERDICT（接收子系统总结 + facts 综合评判）

def _build_verdict_request(facts: dict | None, subsys_summaries: list[dict],
                             outputs: dict, repo_path: Path) -> str:
    payload = {
        "repo_path":         str(repo_path),
        "facts":             facts or {},
        "subsys_summaries":  subsys_summaries,
        "outputs":           outputs,
    }
    return (
        "【最终交付语言】必须一次性生成完整的简体中文总评。标题、表头、列表项、"
        "评分理由和所有自然语言句子都必须是中文；函数名、类型名、路径、代码标识符"
        "及约定的英文技术术语保持原文。禁止出现整句英文或整节英文。\n\n"
        "你是仓库顶层评判会话，综合下面"
        "facts + 各 OS 子系统的总结，产出整体评判结论。\n"
        "**详细正文写成独立 HTML 片段，JSON 只放结构化字段**。\n"
        "正文直接写 HTML（不要 Markdown）：不要输出 ECharts / Mermaid / SVG 图表，"
        "六维雷达图由最终渲染器根据 JSON 评分自动生成；文件引用写纯文本 path:line（自动变链接）。\n\n"
        "工作步骤：\n"
        "1. 不要调用 initialize_analysis；工具会根据 repo_path 自动初始化\n"
        "2. 必须逐条复核 facts.integrity.hardcode.findings，并主动搜索四类实现："
        "按测试名/ELF 名分支、针对测试的 cache 替换、直接打印预期输出、修改脚本旁路失败。"
        "规则命中不是作弊结论；结合上下文给 confirmed/suspected/cleared，说明实现方法、影响和依据。\n"
        "3. facts.integrity.build_verification.requested=true 时，必须原样采用比赛镜像双架构"
        "编译结果；passed 才能写实际编译通过，environment_error/timeout 不能归因于作品。"
        "未请求真实编译时再依据 build_log，没有编译材料则写未实测。run_log 未提供时不要"
        "额外讨论本地无法复现的运行环境，也不得因此扣分。结构化 issues 只列可回溯到"
        "仓库源码 path:line 的设计或实现问题，避免与首屏日志事实重复。\n"
        "4. 检查 facts.integrity.build_interface：它只静态判断根目录 Makefile 是否声明"
        "kernel-rv 与 kernel-la。双目标完整只能写入口完整；是否编译通过完全服从"
        "build_verification 或正式 build_log。缺少 Dockerfile 不是问题，"
        "不得作为扣分依据。partial/missing 只能写静态检查未识别到规定入口，不能外推为源码"
        "编译失败。只有仓库自带容器辅助命令与 Dockerfile 明确矛盾时才作为低优先级补充。"
        "对设计不完整或不合理的问题，必须说明具体模块、"
        "性能/正确性影响、真实 path:line；"
        "若某种不合理设计会对特定测试有利，也要明确写出获益条件。\n"
        "5. 最终主报告不设问题数量上限：全部高/中风险、作弊和破坏语义正确性的缺失必须保留；"
        "其余低风险项进入紧凑清单。构建接口、作弊、正确性优先，无实测支撑的性能推断靠后。"
        "构建或测试未执行时，禁止声称功能完整或可用。\n"
        "6. 必要时 compare_with_reference_os(facts.meta.reference_os) / read_file / search_code 验证关键判断\n"
        "7. 工具调用 ≤20 次；必须为分散在不同文件的硬编码线索读取足够上下文，不得仅凭摘录猜测\n\n"
        "**写出顺序**：\n"
        "  a. 详细评判 HTML 片段（不含图表）→ 写到 outputs.content_path\n"
        "  b. 结构化 JSON → 写到 outputs.json_path\n\n"
        "JSON schema：\n"
        '{\n'
        '  "score_total":int,\n'
        '  "dimensions":[6 items: 原创性/架构合理性/代码质量/文档质量/完整性/功能性,\n'
        '    each {"name":"...","score":int 0到100（禁止使用0到10制）,"reason":"..."}],\n'
        '  "highlights":[{"path":"...","quote":"..."}],\n'
        '  "issues":[{"path":"...","severity":"low|medium|high","quote":"...","confidence":0到100}],\n'
        '  "hardcode_reviews":[{\n'
        '    "signal_id":"原 signal_id；AI 主动发现时用 ai-new-N",\n'
        '    "category":"四个固定类别之一",'
        '"path":"真实相对路径","line":int,\n'
        '    "status":"confirmed|suspected|cleared",\n'
        '    "method":"具体作弊或获益方法；cleared 时写未构成原因",\n'
        '    "reason":"结合代码上下文的中文判断","confidence":0到100,\n'
        '    "excerpt":"不超过200字的关键代码摘录"\n'
        '  }],\n'
        '  "similarity":{"reference_os":"...","overlap_pct":0到100,'
        '"level":"low|medium|high","summary":"...","borrowed":[],"original":[]},\n'
        '  "one_line":"≤80字；逐项写明编译状态、运行状态、硬编码结论和最严重设计问题"\n'
        '}\n\n'
        "one_line 必须使用自然、完整的中文；confirmed/suspected/cleared 只允许出现在 status "
        "枚举字段，禁止写入 one_line。若全部复核为 cleared，one_line 明确写“未发现硬编码”。\n\n"
        "详细 HTML 正文禁止重复写总分、评分制或雷达图；最终页面只显示 JSON dimensions "
        "生成的唯一评分卡，避免出现两套分数。\n\n"
        "hardcode_reviews[].category 只能逐项填写以下一个固定值：按测试名或 ELF 名称分支、"
        "测试专用缓存策略、疑似写死测试结果、脚本强制忽略失败。\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n"
        "写入前做最后一次语言自检：若任何标题、段落、表格单元格或 JSON 描述仍是英文，"
        "先改写成简体中文再调用写入工具；不要输出英文版后等待后续翻译。"
        "hardcode_reviews 必须覆盖每个原 signal_id；即使结论为 cleared 也不能省略。"
    )


def _verdict_fallback() -> dict:
    return {
        "score_total": 0,
        "dimensions": [],
        "highlights": [],
        "issues":     [],
        "hardcode_reviews": [],
        "similarity": {},
        "one_line":   "",
        "_error":     "verdict_fallback",
    }


def _normalize_verdict(parsed: dict) -> dict:
    """统一评分尺度：维度分 clamp 到 0–100，总分=维度加权平均（确定性）。

    LLM 偶尔把维度分按 0–10 制给（如 7/6/5），且自填的 score_total 与维度分
    脱节。这里做两件事，保证落盘结果在固定 0–100 尺度上自洽：
      1. 尺度纠偏：若所有维度分 ≤10，判定为误用 0–10 制，统一 ×10。
      2. 重算总分：score_total = round(Σ w·score / Σ w)，覆盖 LLM 自填值。
    幂等：对已归一化的结果再跑一次不变。
    """
    dims = [d for d in (parsed.get("dimensions") or []) if isinstance(d, dict)]
    raw_scores: list[float | None] = []
    for d in dims:
        try:
            raw_scores.append(float(d.get("score")))
        except (TypeError, ValueError):
            raw_scores.append(None)

    present = [s for s in raw_scores if s is not None]
    # 尺度判定：不要仅凭"所有维度分≤10"就猜成 0–10 制——那会把 0–100 制里
    # 真实拿低分的差作品 ×10 抬成高分。用 LLM 自填的 score_total 判别：
    #   - 总分更接近 均值×10（0–10 制下总分≈维度均值×10）→ 判为 0–10 制，×10；
    #   - 总分更接近裸均值（0–100 制真实低分）→ 保持 0–100 原样，不放大。
    # 无总分可判时退回旧启发式（≤10 视为 0–10 制）。
    scale = 1
    if present and max(present) <= 10:
        try:
            total_v = float(parsed.get("score_total"))
        except (TypeError, ValueError):
            total_v = None
        if total_v is not None:
            mean = sum(present) / len(present)
            scale = 10 if abs(total_v - mean * 10) <= abs(total_v - mean) else 1
        else:
            scale = 10
    by_name: dict[str, dict] = {}
    for d, raw in zip(dims, raw_scores):
        if raw is None:
            continue
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        score = max(0, min(100, int(round(raw * scale))))
        nd = dict(d)
        nd["name"] = name
        nd["score"] = score
        by_name[name] = nd

    ordered_dims: list[dict] = []
    for name in VERDICT_DIMENSIONS:
        if name not in by_name:
            raise RuntimeError(f"顶层评判缺少评分维度：{name}")
        ordered_dims.append(by_name[name])
    parsed["dimensions"] = ordered_dims

    num = den = 0.0
    for d in ordered_dims:
        score = int(d.get("score") or 0)
        w = VERDICT_WEIGHTS.get(d.get("name"), 1.0)
        num += w * score
        den += w

    if den > 0:
        parsed["score_total"] = int(round(num / den))
    return parsed


def _effective_build_status(integrity: dict) -> str:
    """真实镜像编译一旦请求，就覆盖外部日志和静态入口判断。"""
    build_interface = integrity.get("build_interface") or {}
    verification = (
        integrity.get("build_verification")
        or build_interface.get("verification")
        or {}
    )
    if verification.get("requested"):
        return str(verification.get("status") or "unknown")
    return str((integrity.get("build_log") or {}).get("status") or "not_provided")


def _validate_verdict_integrity_conclusion(parsed: dict, facts: dict | None) -> None:
    """确保首屏一句话与唯一生效的编译、运行和硬编码事实一致。"""
    if facts is None:
        return
    text = str(parsed.get("one_line") or "")
    integrity = (facts.get("integrity") or {}) if isinstance(facts, dict) else {}
    status_phrases = {
        "passed": ("通过", "成功"),
        "failed": ("失败",),
        "unknown": ("未确认", "无法确认", "未能确认"),
        "not_provided": ("未提供", "未实测", "未核验", "无法核验"),
        "missing": ("缺失", "不存在"),
        "skipped": ("不适用", "未执行"),
        "partial": ("部分", "仅", "一个架构", "未全部通过"),
        "timeout": ("超时", "未形成结论", "无法确认"),
        "environment_error": ("环境异常", "环境错误", "未形成结论", "无法确认"),
    }
    clauses = [clause for clause in re.split(r"[，,；;。！？!?]", text) if clause.strip()]
    checks = [("编译", _effective_build_status(integrity), ("编译", "构建"))]
    run_status = str((integrity.get("run_log") or {}).get("status") or "not_provided")
    if run_status not in {"not_provided", "missing", "skipped"}:
        checks.append(("运行", run_status, ("运行",)))
    else:
        unavailable_phrases = ("未提供", "未实测", "未核验", "无法核验", "环境不可用")
        if any(
            "运行" in clause and any(phrase in clause for phrase in unavailable_phrases)
            for clause in clauses
        ):
            raise RuntimeError("未提供正式运行日志时，顶层一句话不应展示运行环境缺口")

    for subject, status, labels in checks:
        matching_clauses = [
            clause for clause in clauses if any(label in clause for label in labels)
        ]
        if not matching_clauses:
            raise RuntimeError(f"顶层一句话结论未说明{subject}状态")
        phrases = status_phrases.get(status, status_phrases["unknown"])
        if not any(phrase in clause for clause in matching_clauses for phrase in phrases):
            raise RuntimeError(f"顶层一句话结论与{subject}事实不一致：{status}")

    reviews = [item for item in (parsed.get("hardcode_reviews") or []) if isinstance(item, dict)]
    if "硬编码" not in text:
        raise RuntimeError("顶层一句话结论未说明硬编码检查结果")
    active = [item for item in reviews if item.get("status") in {"confirmed", "suspected"}]
    if active:
        if any(phrase in text for phrase in ("未发现硬编码", "无硬编码")):
            raise RuntimeError("顶层一句话结论与硬编码复核结果不一致")
        if not any(phrase in text for phrase in ("发现", "存在", "确认", "疑似", "未确认")):
            raise RuntimeError("顶层一句话结论未明确说明硬编码复核结论")
    if not active and not any(
        phrase in text for phrase in (
            "未发现", "未确认", "无硬编码", "无作弊", "不构成作弊", "已排除", "排除",
        )
    ):
        raise RuntimeError("顶层一句话结论未明确说明未形成硬编码问题")


def _verdict_one_line_requires_repair(parsed: dict, facts: dict | None) -> bool:
    one_line = str(parsed.get("one_line") or "").strip()
    if not one_line or len(one_line) > 80:
        return True
    try:
        _validate_verdict_integrity_conclusion(parsed, facts)
    except RuntimeError:
        return True
    return False


def _validate_verdict_result(
    parsed: dict,
    repo_path: Path | None = None,
    facts: dict | None = None,
    *,
    enforce_one_line_length: bool = True,
) -> None:
    """总评必须包含真实正文、六维评分和理由，不允许用固定分数补位。"""
    parsed["highlights"] = [
        item for item in (parsed.get("highlights") or [])
        if isinstance(item, dict)
        and not _is_dependency_evidence_path(str(item.get("path") or ""))
    ]
    parsed["issues"] = [
        item for item in (parsed.get("issues") or []) if _keep_system_level_issue(item)
    ]
    if parsed.get("_error"):
        raise RuntimeError(f"顶层评判失败：{parsed['_error']}")
    if not str(parsed.get("content") or "").strip():
        raise RuntimeError("顶层评判缺少详细正文")
    if not str(parsed.get("one_line") or "").strip():
        raise RuntimeError("顶层评判缺少一句话结论")
    if enforce_one_line_length and len(str(parsed.get("one_line") or "")) > 80:
        raise RuntimeError("顶层评判一句话结论超过 80 字")
    _validate_verdict_integrity_conclusion(parsed, facts)
    dimensions = parsed.get("dimensions") or []
    if not isinstance(dimensions, list):
        raise RuntimeError("顶层评判 dimensions 格式无效")
    by_name = {
        str(item.get("name") or "").strip(): item
        for item in dimensions if isinstance(item, dict)
    }
    missing = [name for name in VERDICT_DIMENSIONS if name not in by_name]
    if missing:
        raise RuntimeError("顶层评判缺少评分维度：" + "、".join(missing))
    for name in VERDICT_DIMENSIONS:
        item = by_name[name]
        try:
            float(item.get("score"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"顶层评判 {name} 缺少有效分数") from exc
        if not str(item.get("reason") or "").strip():
            raise RuntimeError(f"顶层评判 {name} 缺少评分理由")
    if repo_path is not None:
        _validate_structured_evidence(
            parsed.get("highlights") or [], repo_path, label="顶层亮点",
        )
        _validate_structured_evidence(
            parsed.get("issues") or [], repo_path, label="顶层问题",
            require_confidence=True,
        )
        similarity = parsed.get("similarity") or {}
        if isinstance(similarity, dict):
            _normalize_similarity_evidence(similarity)
            _validate_structured_evidence(
                similarity.get("borrowed") or [], repo_path, label="参考实现沿用证据",
            )
            _validate_structured_evidence(
                similarity.get("original") or [], repo_path, label="候选创新证据",
            )


def _validate_similarity_result(parsed: dict, facts: dict | None) -> None:
    """识别到参考 OS 时必须交付代码指纹结果，禁止函数名或主观估算降级。"""
    reference_os = str(((facts or {}).get("meta") or {}).get("reference_os") or "").strip()
    if not reference_os:
        return
    similarity = parsed.get("similarity")
    if not isinstance(similarity, dict):
        raise RuntimeError("顶层评判缺少参考 OS 代码指纹比对结果")
    if str(similarity.get("reference_os") or "").strip() != reference_os:
        raise RuntimeError("顶层评判的参考 OS 与事实档案不一致")
    try:
        overlap_pct = float(similarity.get("overlap_pct"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("顶层评判缺少代码指纹综合相似度") from exc
    if not 0 <= overlap_pct <= 100:
        raise RuntimeError("顶层评判的代码指纹综合相似度不在 0–100")
    if not str(similarity.get("summary") or "").strip():
        raise RuntimeError("顶层评判缺少代码指纹比对摘要")


def _ensure_reference_database(facts: dict | None) -> None:
    """在启动顶层 AI 评判前校验指纹库；异常时先自动重建。"""
    reference_os = str(((facts or {}).get("meta") or {}).get("reference_os") or "").strip()
    if not reference_os:
        return
    from .. import config as agent_config
    from ..tools.reference_db import ReferenceOSDatabase

    ReferenceOSDatabase(
        agent_config.data.get("reference_db_dir", "resources/reference_db"),
        source_config=agent_config.data.get(
            "reference_sources_config", "config/reference_sources.yaml"
        ),
        source_cache_dir=agent_config.data.get(
            "reference_sources_dir", "data/reference_sources"
        ),
    ).load_or_rebuild(reference_os)


def _norm_path(p: str) -> str:
    """硬编码复核路径归一化：统一分隔符、去 ./、忽略大小写。"""
    return p.replace("\\", "/").replace("./", "").strip("/").lower()


_SOURCE_LOCATION_RE = re.compile(r"^(.*?)(?::|#L)(\d+)(?:-L?\d+)?$")
_SOURCE_LOCATION_IN_TEXT_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.+@-]+/)+[A-Za-z0-9_.+@-]+(?::|#L)\d+(?:-L?\d+)?)"
)


def _normalize_similarity_evidence(similarity: dict) -> None:
    """兼容模型把借鉴/创新证据写成字符串，同时不为裸路径编造分析。"""
    for key in ("borrowed", "original"):
        raw_items = similarity.get(key) or []
        if not isinstance(raw_items, list):
            continue
        normalized: list[dict] = []
        for item in raw_items:
            if isinstance(item, dict):
                normalized.append(item)
                continue
            if not isinstance(item, str):
                continue
            text = item.strip().strip("` ")
            if text.startswith("{"):
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, dict):
                    normalized.append(decoded)
                    continue
            match = _SOURCE_LOCATION_IN_TEXT_RE.search(text)
            if not match:
                continue
            quote = (text[:match.start()] + " " + text[match.end():]).strip(
                " \t\r\n—–-：:，,；;。"
            )
            # 只有位置而没有分析时按提示契约丢弃；空数组比伪造说明更可靠。
            if not quote:
                continue
            normalized.append({"path": match.group("path"), "quote": quote})
        similarity[key] = normalized


def _validate_repo_location(
    repo_path: Path,
    path_value: str,
    *,
    label: str,
    line: int | None = None,
    require_line: bool = True,
) -> tuple[str, int | None]:
    """验证 AI 引用确实位于仓库内，并且行号落在文件范围内。"""
    raw = str(path_value or "").strip().strip("`'")
    parsed_line = line
    if parsed_line is None:
        match = _SOURCE_LOCATION_RE.match(raw)
        if match:
            raw = match.group(1)
            parsed_line = int(match.group(2))
    if not raw or (require_line and not parsed_line):
        raise RuntimeError(f"{label}缺少真实相对 path:line")
    relative = Path(raw.replace("\\", "/"))
    if relative.is_absolute():
        raise RuntimeError(f"{label}必须使用仓库相对路径：{path_value}")
    root = Path(repo_path).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{label}路径越出仓库：{path_value}") from exc
    if not candidate.is_file():
        raise RuntimeError(f"{label}引用的文件不存在：{path_value}")
    if parsed_line is not None:
        if parsed_line < 1:
            raise RuntimeError(f"{label}行号无效：{path_value}")
        try:
            line_count = len(candidate.read_text(
                encoding="utf-8", errors="replace",
            ).splitlines())
        except OSError as exc:
            raise RuntimeError(f"{label}引用文件无法读取：{path_value}") from exc
        if parsed_line > line_count:
            raise RuntimeError(
                f"{label}行号超出文件范围：{path_value}（共 {line_count} 行）"
            )
    return candidate.relative_to(root).as_posix(), parsed_line


def _validate_structured_evidence(
    items: list,
    repo_path: Path,
    *,
    label: str,
    require_confidence: bool = False,
) -> None:
    if not isinstance(items, list):
        raise RuntimeError(f"{label}格式无效")
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"{label}第 {index} 项格式无效")
        canonical_path, canonical_line = _validate_repo_location(
            repo_path, str(item.get("path") or ""),
            label=f"{label}第 {index} 项",
        )
        item["path"] = f"{canonical_path}:{canonical_line}"
        if not str(item.get("quote") or "").strip():
            raise RuntimeError(f"{label}第 {index} 项缺少 AI 分析")
        if require_confidence:
            try:
                confidence = float(item.get("confidence"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"{label}第 {index} 项缺少置信度") from exc
            if not 0 <= confidence <= 100:
                raise RuntimeError(f"{label}第 {index} 项置信度不在 0–100")
            # 兼容模型偶发返回 0–1 比例；落盘统一为 0–100，避免 HTML 把 0.91 显示成 1%。
            item["confidence"] = confidence * 100 if 0 < confidence <= 1 else confidence


def _actual_source_excerpt(repo_path: Path, relative_path: str, line: int) -> str:
    """从已验证的位置提取真实代码窗口，替换模型可能改写过的 excerpt。"""
    source_lines = (Path(repo_path).resolve() / relative_path).read_text(
        encoding="utf-8", errors="replace",
    ).splitlines()
    start = max(0, line - 2)
    end = min(len(source_lines), line + 1)
    return " ".join(part.strip() for part in source_lines[start:end] if part.strip())[:500]


def _hardcode_source_context(
    repo_path: Path,
    signal: dict,
    *,
    radius: int = 24,
) -> str:
    """读取规则命中附近的真实源码，供高风险语义校验和定向 AI 复核使用。"""
    relative = str(signal.get("path") or "").replace("\\", "/").lstrip("./")
    try:
        line = int(signal.get("line") or 0)
        lines = (Path(repo_path).resolve() / relative).read_text(
            encoding="utf-8", errors="replace",
        ).splitlines()
    except (OSError, TypeError, ValueError):
        return ""
    start = max(0, line - radius - 1)
    end = min(len(lines), line + radius)
    return "\n".join(
        f"{index + 1}: {lines[index]}" for index in range(start, end)
    )


def _semantic_hardcode_risk(signal: dict, repo_path: Path) -> str:
    """识别不能仅以“兼容性代码”为由清除的系统调用语义兜底。"""
    if str(signal.get("category") or "") != "按测试名或 ELF 名称分支":
        return ""
    context = _hardcode_source_context(repo_path, signal).casefold()
    if not context or "/musl/busybox" not in context:
        return ""
    if (
        ('starts_with("/musl/")' in context or 'path == "/musl"' in context)
        and "return 0" in context
        and ("open_inode" in context or "open_file" in context)
    ):
        return (
            "目标路径访问失败后，仅因 /musl/busybox 存在便返回成功，可能把 "
            "ENOENT 等失败改写为成功，改变 faccessat/faccessat2 语义。"
        )
    excerpt = str(signal.get("excerpt") or "").casefold()
    if (
        "is_err()" in excerpt
        and 'open_inode("/musl/busybox"' in context
        and "is_path_search" in context
    ):
        return (
            "待执行文件打开失败后，代码按固定目录把程序替换为 /musl/busybox；"
            "这可能让缺失程序执行另一 ELF，必须作为系统级定向路径复核。"
        )
    return ""


def _hardcode_reviews_needing_repair(
    parsed: dict,
    facts: dict | None,
    repo_path: Path,
) -> list[dict]:
    hardcode = (((facts or {}).get("integrity") or {}).get("hardcode") or {})
    reviews = {
        str(item.get("signal_id") or ""): item
        for item in (parsed.get("hardcode_reviews") or [])
        if isinstance(item, dict)
    }
    targets: list[dict] = []
    for signal in hardcode.get("findings") or []:
        signal_id = str(signal.get("signal_id") or "")
        review = reviews.get(signal_id)
        risk = _semantic_hardcode_risk(signal, repo_path)
        if review is None or (risk and review.get("status") == "cleared"):
            targets.append({
                **signal,
                "semantic_risk": risk,
                "source_context": _hardcode_source_context(repo_path, signal),
                "previous_review": review or {},
            })
    return targets


def _validate_hardcode_reviews(
    parsed: dict,
    facts: dict | None,
    repo_path: Path | None = None,
) -> None:
    """确保每条规则线索都经过 AI 复核，且结论能回到真实代码位置。"""
    hardcode = (((facts or {}).get("integrity") or {}).get("hardcode") or {})
    signals = hardcode.get("findings") or []
    if hardcode.get("truncated"):
        raise RuntimeError(
            "硬编码候选超过复核上限，拒绝生成不完整报告："
            f"候选 {hardcode.get('candidate_count', '?')} 条，"
            f"当前上限 {len(signals)} 条；请提高 AGENT_HARDCODE_SIGNAL_LIMIT 后重跑"
        )
    reviews = parsed.get("hardcode_reviews") or []
    if not isinstance(reviews, list):
        raise RuntimeError("顶层评判 hardcode_reviews 格式无效")

    from oskernel_agent.finals.integrity import REQUIRED_HARDCODE_CATEGORIES

    by_signal_id = {
        str(signal.get("signal_id") or (
            f"{signal.get('path')}:{signal.get('line')}:{signal.get('category')}"
        )): signal
        for signal in signals
    }
    by_id: dict[str, dict] = {}
    for item in reviews:
        if not isinstance(item, dict):
            raise RuntimeError("顶层评判包含无效的硬编码复核项")
        signal_id = str(item.get("signal_id") or "").strip()
        if not signal_id or signal_id in by_id:
            raise RuntimeError("硬编码复核项缺少唯一 signal_id")
        if item.get("status") not in {"confirmed", "suspected", "cleared"}:
            raise RuntimeError(f"硬编码复核 {signal_id} 的 status 无效")
        if not str(item.get("path") or "").strip() or not item.get("line"):
            raise RuntimeError(f"硬编码复核 {signal_id} 缺少真实 path:line")
        if not str(item.get("method") or "").strip() or not str(item.get("reason") or "").strip():
            raise RuntimeError(f"硬编码复核 {signal_id} 缺少方法或分析")
        if not str(item.get("excerpt") or "").strip():
            raise RuntimeError(f"硬编码复核 {signal_id} 缺少关键代码摘录")
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"硬编码复核 {signal_id} 缺少置信度") from exc
        if not 0 <= confidence <= 100:
            raise RuntimeError(f"硬编码复核 {signal_id} 的置信度不在 0–100")
        item["confidence"] = confidence * 100 if 0 < confidence <= 1 else confidence
        original = by_signal_id.get(signal_id)
        category = str(item.get("category") or "").strip()
        if original is not None:
            if category != str(original.get("category") or "").strip():
                raise RuntimeError(f"硬编码复核 {signal_id} 的类别与扫描证据不一致")
        else:
            if not re.fullmatch(r"ai-new-\d+", signal_id):
                raise RuntimeError(f"AI 主动发现的硬编码复核 ID 无效：{signal_id}")
            if category not in REQUIRED_HARDCODE_CATEGORIES:
                raise RuntimeError(f"硬编码复核 {signal_id} 的类别无效")
        if repo_path is not None:
            canonical_path, canonical_line = _validate_repo_location(
                repo_path, str(item.get("path") or ""),
                line=int(item.get("line") or 0), label=f"硬编码复核 {signal_id}",
            )
            item["path"] = canonical_path
            item["line"] = canonical_line
            item["excerpt"] = _actual_source_excerpt(
                repo_path, canonical_path, int(canonical_line or 0),
            )
        by_id[signal_id] = item

    missing: list[str] = []
    for signal in signals:
        signal_id = str(signal.get("signal_id") or (
            f"{signal.get('path')}:{signal.get('line')}:{signal.get('category')}"
        ))
        review = by_id.get(signal_id)
        if review is None:
            missing.append(signal_id)
            continue
        # 位置容错：路径归一化（去 ./、统一正反斜杠、忽略大小写）后比较，
        # 行号允许 ±2 行偏差。LLM 复核时行号/路径写法差一点不应中止整份报告。
        if _norm_path(str(review.get("path") or "")) != _norm_path(str(signal.get("path") or "")):
            raise RuntimeError(f"硬编码复核 {signal_id} 的路径与扫描证据不一致")
        if abs(int(review.get("line") or 0) - int(signal.get("line") or 0)) > 2:
            raise RuntimeError(f"硬编码复核 {signal_id} 的行号与扫描证据不一致")
        if repo_path is not None:
            semantic_risk = _semantic_hardcode_risk(signal, repo_path)
            if semantic_risk and review.get("status") == "cleared":
                raise RuntimeError(
                    f"硬编码复核 {signal_id} 涉及系统调用语义兜底，不能直接标为 cleared："
                    f"{semantic_risk}"
                )
            # 对规则扫描命中以扫描器的真实位置为准；AI 只负责解释上下文和作出结论。
            canonical_path, canonical_line = _validate_repo_location(
                repo_path, str(signal.get("path") or ""),
                line=int(signal.get("line") or 0), label=f"硬编码复核 {signal_id}",
            )
            review["path"] = canonical_path
            review["line"] = canonical_line
            review["excerpt"] = _actual_source_excerpt(
                repo_path, canonical_path, int(canonical_line or 0),
            )
    if missing:
        raise RuntimeError("以下硬编码线索未经 AI 复核：" + "、".join(missing[:6]))


def _collect_subsys_summaries(tree_root: dict) -> list[dict]:
    out = []
    for c in tree_root.get("children", []):
        if c.get("type") != "subsystem":
            continue
        out.append({
            "name":       c["name"],
            "role":       c.get("role", ""),
            "summary":    c.get("summary", ""),
            "highlights": c.get("highlights", []),
            "issues":     c.get("issues", []),
            "modules":    [
                {"name": m["name"], "summary": m.get("summary", "")}
                for m in c.get("children", [])
            ],
        })
    return out


def _deterministic_verdict_one_line(
    parsed: dict,
    facts: dict | None,
    *,
    limit: int = 80,
) -> str:
    """在模型压缩失败时，从已校验事实构造可再次校验的短结论。"""
    integrity = ((facts or {}).get("integrity") or {})
    status_text = {
        "passed": "通过",
        "failed": "失败",
        "unknown": "未确认",
        "not_provided": "日志未提供",
        "missing": "缺失",
        "skipped": "未执行",
        "partial": "未全部通过",
        "timeout": "超时",
        "environment_error": "环境异常",
    }

    build_status = _effective_build_status(integrity)
    clauses = [f"编译{status_text.get(build_status, '无法确认')}"]

    run_status = str((integrity.get("run_log") or {}).get("status") or "not_provided")
    run_available = run_status not in {"not_provided", "missing", "skipped"}
    if run_available:
        clauses.append(f"运行{status_text.get(run_status, '无法确认')}")

    reviews = [
        item for item in (parsed.get("hardcode_reviews") or [])
        if isinstance(item, dict)
    ]
    confirmed = sum(item.get("status") == "confirmed" for item in reviews)
    suspected = sum(item.get("status") == "suspected" for item in reviews)
    if confirmed and suspected:
        clauses.append(f"发现{confirmed}项确认、{suspected}项疑似硬编码")
    elif confirmed:
        clauses.append(f"确认发现{confirmed}项硬编码")
    elif suspected:
        clauses.append(f"发现{suspected}项疑似硬编码")
    else:
        clauses.append("未发现硬编码")

    issues = [item for item in (parsed.get("issues") or []) if isinstance(item, dict)]
    issue = str((issues[0] if issues else {}).get("quote") or "最严重设计问题未说明")
    issue = re.sub(r"<[^>]+>", "", issue)
    issue = re.sub(r"\s+", " ", issue).strip(" ，,；;。")
    if not run_available:
        issue = re.sub(
            r"运行(?:日志)?(?:未提供|未实测|未核验|无法核验|环境不可用)[，,；;。]?",
            "",
            issue,
        ).strip(" ，,；;。")
    if not issue:
        issue = "最严重设计问题未说明"

    prefix = "；".join(clauses)
    label = "；主要问题："
    budget = limit - len(prefix) - len(label)
    if budget <= 0:
        return prefix[:limit]
    shortened = issue[:budget].rstrip(" ，,；;。")
    return f"{prefix}{label}{shortened}" if shortened else prefix


def _normalize_hardcode_repair_items(
    candidate: dict,
    targets: list[dict],
) -> list[dict]:
    """把模型常见的 impact/semantic_risk 字段漂移归一为正式复核 schema。"""
    returned = candidate.get("hardcode_reviews") or []
    if not isinstance(returned, list):
        return []
    sources = {str(item.get("signal_id") or ""): item for item in targets}
    normalized_items: list[dict] = []
    seen: set[str] = set()
    for item in returned:
        if not isinstance(item, dict):
            return []
        signal_id = str(item.get("signal_id") or "")
        source = sources.get(signal_id)
        if source is None or signal_id in seen:
            return []
        seen.add(signal_id)
        normalized = dict(item)
        normalized.update({
            "signal_id": signal_id,
            "category": source.get("category"),
            "path": source.get("path"),
            "line": source.get("line"),
            "excerpt": source.get("excerpt"),
        })
        if not str(normalized.get("reason") or "").strip():
            normalized["reason"] = "；".join(
                str(normalized.get(field) or "").strip()
                for field in ("semantic_risk", "impact")
                if str(normalized.get(field) or "").strip()
            )
        if normalized.get("confidence") is None:
            try:
                confidence = float(source.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0
            normalized["confidence"] = confidence * 100 if 0 < confidence <= 1 else confidence
        normalized_items.append(normalized)
    return normalized_items if seen == set(sources) else []


def repair_verdict_hardcode_reviews(
    parsed: dict,
    facts: dict | None,
    work_dir: Path,
    repo_path: Path,
) -> dict:
    """只让 AI 重审缺失项及被错误清除的系统级高风险硬编码线索。"""
    targets = _hardcode_reviews_needing_repair(parsed, facts, repo_path)
    if not targets:
        return parsed
    out_path = work_dir / "verdict-hardcode.repair.json"
    target_ids = {str(item.get("signal_id") or "") for item in targets}
    risk_ids = {
        str(item.get("signal_id") or "")
        for item in targets if str(item.get("semantic_risk") or "").strip()
    }

    def _merged_reviews(candidate: dict) -> list[dict]:
        returned = _normalize_hardcode_repair_items(candidate, targets)
        if not returned:
            return []
        replacements: dict[str, dict] = {}
        for item in returned:
            signal_id = str(item.get("signal_id") or "")
            if signal_id not in target_ids or signal_id in replacements:
                return []
            if signal_id in risk_ids and item.get("status") == "cleared":
                return []
            replacements[signal_id] = item
        if set(replacements) != target_ids:
            return []
        existing = {
            str(item.get("signal_id") or ""): item
            for item in (parsed.get("hardcode_reviews") or [])
            if isinstance(item, dict)
        }
        signals = ((((facts or {}).get("integrity") or {}).get("hardcode") or {}).get(
            "findings"
        ) or [])
        merged = [
            replacements.get(str(signal.get("signal_id") or ""))
            or existing.get(str(signal.get("signal_id") or ""))
            for signal in signals
        ]
        if any(item is None for item in merged):
            return []
        merged.extend(
            item for signal_id, item in existing.items()
            if signal_id.startswith("ai-new-")
        )
        return merged

    def _complete(candidate: dict) -> bool:
        merged_reviews = _merged_reviews(candidate)
        if not merged_reviews:
            return False
        merged = dict(parsed)
        merged["hardcode_reviews"] = merged_reviews
        try:
            _validate_hardcode_reviews(merged, facts, repo_path)
        except RuntimeError:
            return False
        return True

    payload = {
        "复核规则": (
            "先判断代码是否会针对固定程序/路径改变正常失败或执行语义，再区分 confirmed、"
            "suspected、cleared。semantic_risk 非空的项目不得写 cleared；证据足以确定行为时"
            "写 confirmed，仍需运行验证影响范围时写 suspected。兼容性目的不能抵消语义变化。"
        ),
        "待复核线索": targets,
        "输出路径": str(out_path),
    }
    task = BatchTask(
        batch_id="verdict-hardcode-repair",
        agent_name="os-kernel-verdict",
        user_request=(
            "你是操作系统内核赛评委，只重审输入中的硬编码线索，不修改报告其他内容。"
            "逐项阅读 source_context，说明触发方法、实际输出/返回值变化及其对正常内核语义的"
            "影响。必须原样返回每个 signal_id/category/path/line；status 只能是 confirmed、"
            "suspected、cleared，semantic_risk 非空时不得 cleared。不得把普通第三方库实现当作"
            "作品问题。调用 write_report，把仅含 hardcode_reviews 的 JSON 写到指定路径。\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        ),
        output_path=out_path,
        cache_dir=work_dir,
        cache_key="",
        cache_enabled=False,
        fallback={"hardcode_reviews": []},
        repo_path=repo_path,
        cache_validator=_complete,
    )
    candidate = run_batch_task(
        task,
        schema_hint=(
            '{"hardcode_reviews":[{"signal_id":str,"category":str,"path":str,'
            '"line":int,"status":"confirmed|suspected|cleared","method":str,'
            '"reason":str,"confidence":int,"excerpt":str}]}'
        ),
        timeout=300,
    )
    if not _complete(candidate):
        raise RuntimeError("AI 未能完成系统级高风险硬编码线索的定向复核")
    parsed["hardcode_reviews"] = _merged_reviews(candidate)
    return parsed


def repair_verdict_one_line(
    parsed: dict,
    facts: dict | None,
    work_dir: Path,
    repo_path: Path,
) -> dict:
    """让 AI 只压缩不合规的顶层一句话，不重写整份已验证总评。"""
    out_path = work_dir / "verdict-one-line.repair.json"
    integrity = ((facts or {}).get("integrity") or {})
    run_status = str((integrity.get("run_log") or {}).get("status") or "not_provided")
    run_available = run_status not in {"not_provided", "missing", "skipped"}
    issues = [item for item in (parsed.get("issues") or []) if isinstance(item, dict)]
    issue_hint = str((issues[0] if issues else {}).get("quote") or "未说明")
    payload = {
        "原句": str(parsed.get("one_line") or ""),
        "编译状态": _effective_build_status(integrity),
        "运行状态": run_status if run_available else "不展示",
        "硬编码复核状态": [
            str(item.get("status") or "")
            for item in (parsed.get("hardcode_reviews") or [])
            if isinstance(item, dict)
        ],
        "最严重设计问题候选": issue_hint,
        "输出路径": str(out_path),
    }

    def _complete(candidate: dict) -> bool:
        one_line = str(candidate.get("one_line") or "").strip()
        if not one_line or len(one_line) > 80:
            return False
        merged = dict(parsed)
        merged["one_line"] = one_line
        try:
            _validate_verdict_integrity_conclusion(merged, facts)
        except RuntimeError:
            return False
        return True

    task = BatchTask(
        batch_id="verdict-one-line-repair",
        agent_name="os-kernel-verdict",
        user_request=(
            "只修复顶层报告的一句话结论，不修改其他任何结论。根据下面数据生成一条不超过 "
            "70 个字符的自然中文句子；必须出现“编译”“硬编码”并准确说明状态。只有“运行状态”"
            "不是“不展示”时才写“运行”；为“不展示”时不得提运行环境或未实测。"
            "末尾点出一个最严重设计问题。status 枚举值不得写入句子；全部 cleared 时写"
            "“未发现硬编码”。不要解释。调用 write_report，把仅含 one_line 字段的 JSON 写入"
            f"指定输出路径。\n\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
        ),
        output_path=out_path,
        cache_dir=work_dir,
        cache_key="",
        cache_enabled=False,
        fallback={"one_line": ""},
        repo_path=repo_path,
        cache_validator=_complete,
    )
    candidate = run_batch_task(
        task,
        schema_hint='{"one_line":"不超过70个字符的中文结论"}',
        timeout=180,
    )
    if not _complete(candidate):
        candidate = {
            "one_line": _deterministic_verdict_one_line(parsed, facts),
        }
    if not _complete(candidate):
        raise RuntimeError("AI 未能把顶层一句话结论压缩到 80 字以内")
    parsed["one_line"] = str(candidate["one_line"]).strip()
    return parsed


def repair_verdict_similarity(
    parsed: dict,
    facts: dict | None,
    work_dir: Path,
    repo_path: Path,
) -> dict:
    """只补生成缺失/无效的参考 OS 指纹比对字段。"""
    reference_os = str(((facts or {}).get("meta") or {}).get("reference_os") or "").strip()
    if not reference_os:
        return parsed
    out_path = work_dir / "verdict-similarity.repair.json"
    # MCP 会为这个工具等待完整语义引擎初始化，在大仓库中可能超过请求超时；这里用
    # 同一 TreeSitterEngine + ToolDispatcher 直接取得完全相同的代码指纹结果，再交给
    # AI 只做中文摘要与结构化，不让模型主观估算数值。
    from .. import config as agent_config
    from ..engines.path_c import TreeSitterEngine
    from ..tools.tool_dispatcher import ToolDispatcher

    primary_lang = str(((facts or {}).get("profile_lite") or {}).get("primary_lang") or "rust")
    engine_lang = "c" if primary_lang.lower() in {"c", "cpp", "c++"} else "rust"
    engine = TreeSitterEngine(
        str(repo_path), engine_lang,
        skip_dirs=agent_config.engine.get("skip_dirs"),
    )
    dispatcher = ToolDispatcher(
        engine, None, str(repo_path), {"primary_lang": engine_lang},
        ref_db_dir=agent_config.data.get("reference_db_dir", "resources/reference_db"),
    )
    comparison = dispatcher.compare_with_reference_os(reference_os)
    salient_lines = [
        line for line in comparison.splitlines()
        if ("综合相似度" in line or "共有函数" in line or line.startswith("### "))
    ]
    payload = {
        "repo_path": str(repo_path),
        "reference_os": reference_os,
        "指纹工具结果": salient_lines,
        "output_path": str(out_path),
    }

    def _complete(candidate: dict) -> bool:
        similarity = candidate.get("similarity")
        if not isinstance(similarity, dict):
            return False
        _normalize_similarity_evidence(similarity)
        merged = dict(parsed)
        merged["similarity"] = similarity
        try:
            _validate_similarity_result(merged, facts)
            _validate_structured_evidence(
                similarity.get("borrowed") or [], repo_path, label="参考实现沿用证据",
            )
            _validate_structured_evidence(
                similarity.get("original") or [], repo_path, label="候选创新证据",
            )
        except RuntimeError:
            return False
        return True

    task = BatchTask(
        batch_id="verdict-similarity-repair",
        agent_name="os-kernel-verdict",
        user_request=(
            "只补生成顶层报告的代码指纹相似度字段，不修改其他结论。代码指纹工具已执行，"
            "必须原样使用输入里的综合相似度，禁止重新估算；不需要再调用任何分析工具。"
            "调用 write_report，将 JSON 写入 output_path；JSON 只能有 similarity "
            "顶层字段，内含 reference_os、overlap_pct、level、summary、borrowed、original。"
            "borrowed/original 若无可靠 path:line 证据就使用空数组。所有描述使用中文。\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        ),
        output_path=out_path,
        cache_dir=work_dir,
        cache_key="",
        cache_enabled=False,
        fallback={"similarity": {}},
        repo_path=repo_path,
        cache_validator=_complete,
    )
    candidate = run_batch_task(
        task,
        schema_hint=(
            '{"similarity":{"reference_os":str,"overlap_pct":int,'
            '"level":"low|medium|high","summary":str,"borrowed":[],"original":[]}}'
        ),
        timeout=300,
    )
    if not _complete(candidate):
        raise RuntimeError("AI 未能补齐参考 OS 代码指纹比对结果")
    parsed["similarity"] = candidate["similarity"]
    return parsed


def run_verdict_stage(tree_root: dict, facts: dict | None,
                       work_dir: Path,
                       repo_path: Path | None = None) -> dict:
    # 指纹库故障必须在顶层模型开始评分前修复，避免模型看不到工具结果后自行估算原创性。
    _ensure_reference_database(facts)
    outputs = {
        "json_path":    str(work_dir / "verdict.json"),
        "content_path": str(work_dir / "verdict.html"),
    }
    out_path = Path(outputs["json_path"])

    subsys_summaries = _collect_subsys_summaries(tree_root)
    def _enrich(parsed: dict) -> dict:
        """把 verdict 详细正文（HTML，含强制图表）读进 parsed，随 JSON 一起进缓存。"""
        parsed["content"] = _read_md_if_exists(Path(outputs["content_path"]))
        parsed["content"] = _VERDICT_DUPLICATE_SCORE_RE.sub("", parsed["content"])
        _normalize_verdict_content_paths(parsed)
        # 正常情况下提示词已直接生成中文；只有检测出英文正文时才调用翻译兜底。
        from .lang_guard import normalize_tree_language
        normalize_tree_language(parsed)
        return parsed

    from .lang_guard import language_output_complete

    def _delivery_complete(parsed: dict) -> bool:
        if not language_output_complete(parsed):
            return False
        try:
            _validate_verdict_result(
                parsed, repo_path or Path("."), facts,
                enforce_one_line_length=False,
            )
        except RuntimeError:
            return False
        return True

    task = BatchTask(
        batch_id="verdict",
        agent_name="os-kernel-verdict",
        user_request=_build_verdict_request(
            facts, subsys_summaries, outputs, repo_path or Path(".")),
        output_path=out_path,
        cache_dir=work_dir,  # cache_enabled=False；该路径仅满足 BatchTask 接口
        cache_key="",
        cache_enabled=False,
        fallback=_verdict_fallback(),
        repo_path=repo_path or Path("."),
        enrich=_enrich,
        cache_validator=_delivery_complete,
    )
    parsed = run_batch_task(
        task,
        schema_hint='{"score_total":int,"dimensions":[...6 items...],'
                    '"highlights":[...],"issues":[...],'
                    '"hardcode_reviews":[{signal_id,category,path,line,status,method,reason,confidence,excerpt}],'
                    '"similarity":{reference_os:str,overlap_pct:int,level:str,'
                    'summary:str,borrowed:[...],original:[...]},'
                    '"one_line":str}',
        timeout=600,
    )
    if _hardcode_reviews_needing_repair(parsed, facts, repo_path or Path(".")):
        repair_verdict_hardcode_reviews(
            parsed, facts, work_dir, repo_path or Path("."),
        )
    if _verdict_one_line_requires_repair(parsed, facts):
        repair_verdict_one_line(
            parsed, facts, work_dir, repo_path or Path("."),
        )
    try:
        _validate_similarity_result(parsed, facts)
    except RuntimeError:
        repair_verdict_similarity(
            parsed, facts, work_dir, repo_path or Path("."),
        )
    _validate_verdict_result(parsed, repo_path or Path("."), facts)
    _validate_similarity_result(parsed, facts)
    _validate_hardcode_reviews(parsed, facts, repo_path or Path("."))
    # 正文已由 enrich 读入 parsed（含缓存命中场景）
    # 归一化评分：维度统一到 0–100，总分=维度加权平均（覆盖 LLM 自填值）
    return _normalize_verdict(parsed)


# 工具函数

def write_tree_json(tree: dict, path: Path) -> None:
    path.write_text(json.dumps(tree, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _clean_tree(node: dict) -> dict:
    """删除临时/内部字段。"""
    _INTERNAL = {"abs_path", "mtime", "size", "_dir_index", "files"}
    out = {k: v for k, v in node.items() if k not in _INTERNAL}
    if "children" in node:
        out["children"] = [_clean_tree(c) for c in node["children"]]
    return out


def _empty_tree(repo_name: str, ts: str, facts: dict | None) -> dict:
    return {
        "meta": {
            "repo":           repo_name,
            "ts":             ts,
            "indexed_files":  0,
            "schema_version": SCHEMA_VERSION,
        },
        "facts":   facts or {},
        "verdict": {
            "score_total": 0,
            "dimensions":  [],
            "highlights":  [],
            "issues":      [],
            "one_line":    "仓库未找到可索引源文件。",
        },
        "tree": {
            "type": "root", "path": "", "name": repo_name,
            "summary": "", "children": [],
        },
    }


# 总入口

def build_tree(repo_path: Path, repo_name: str, ts: str,
               facts: dict | None = None,
               output_dir: Path | None = None) -> dict:
    """构建完整 tree.json（按 OS 子系统分层）。"""
    repo_path = Path(repo_path).resolve()
    out_dir = output_dir or (
        Path(config.data.get("reports_dir", "./data/output")).resolve()
        / f"{repo_name}_{ts}_tree"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    # A. 按子系统枚举
    print("\n[tree] 按 OS 子系统归类源文件 ...")
    tree_root, file_count = enumerate_subsystems(repo_path)
    print(f"[tree] 命中 {file_count} 个源文件，"
          f"{len(tree_root['children'])} 个子系统：" +
          " / ".join(c["name"] for c in tree_root["children"]))

    if file_count == 0:
        raise RuntimeError("仓库未找到可索引源文件，拒绝生成空的作品描述报告")

    # B. SUBSYS 并发分析
    run_subsys_stage(tree_root, repo_path, out_dir, facts)

    # C. VERDICT 综合
    print("[tree] VERDICT 阶段 ...")
    verdict = run_verdict_stage(tree_root, facts, out_dir, repo_path)

    result = {
        "meta": {
            "repo":           repo_name,
            "ts":             ts,
            "indexed_files":  file_count,
            "schema_version": SCHEMA_VERSION,
        },
        "facts":   facts or {},
        "verdict": verdict,
        "tree":    _clean_tree(tree_root),
    }

    # D. 语言护栏：把 LLM 漏出的英文正文确定性地改成中文（提示词约束之外的兜底）
    try:
        from .lang_guard import normalize_tree_language
        print("[tree] 语言护栏：检测并中文化英文正文 ...")
        lang_stats = normalize_tree_language(result)
        result["meta"]["language_guard"] = lang_stats
    except Exception as e:
        print(f"[警告] 语言护栏失败：{e}（继续）", file=sys.stderr)
        result["meta"]["language_guard"] = {"enabled": True, "complete": False,
                                                    "remaining": None, "error": str(e)}

    # D2. 标题护栏：module 节点英文 name 中文化（渲染在树节点头 + 目录，正文护栏覆盖不到）
    try:
        from .lang_guard import normalize_tree_titles
        print("[tree] 标题护栏：检测并中文化英文模块标题 ...")
        title_stats = normalize_tree_titles(result)
        result["meta"]["title_language_guard"] = title_stats
    except Exception as e:
        print(f"[警告] 标题护栏失败：{e}（继续）", file=sys.stderr)
        result["meta"]["title_language_guard"] = {"enabled": True, "complete": False,
                                                          "remaining": None, "error": str(e)}

    result["meta"]["language_incomplete"] = not (
        result["meta"].get("language_guard", {}).get("complete", False)
        and result["meta"].get("title_language_guard", {}).get("complete", False)
    )

    # E. quote 护栏：把亮点/槽点里粘贴的源码摘录改写成中文一句话点评
    try:
        from .lang_guard import normalize_tree_quotes
        print("[tree] quote 护栏：代码摘录改中文点评 ...")
        normalize_tree_quotes(result)
    except Exception as e:
        print(f"[警告] quote 护栏失败：{e}（继续）", file=sys.stderr)

    return result
