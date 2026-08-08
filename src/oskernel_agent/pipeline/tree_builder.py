"""
按 OS 概念分层的报告构建器：repo → tree.json（单一真相源）。

树结构（3 层）：
  Level 0  根节点（VERDICT 顶层评判）
  Level 1  OS 子系统：进程管理 / 内存管理 / 文件系统 / 系统调用 /
           设备驱动 / 硬件抽象 / 其他
  Level 2  模块：由 SUBSYS agent 在分析阶段动态识别（如 文件系统 →
           VFS 层 / inode 层 / 块缓存 / 日志层）

阶段：
  A. 子系统枚举：按 SUBSYSTEM_FINGERPRINTS 把所有源文件归类到子系统
  B. SUBSYS 聚合（并行）：每个子系统一次 LLM 会话，agent 决定模块拆分
     + 写子系统总览 .md + 写各模块 .md + 写结构化 JSON
  C. VERDICT：综合所有子系统摘要 + repo_facts → 顶层评判

调用方：cli/agent.py:_run_tree_mode
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

SCHEMA_VERSION = "tree-v3"

MAX_MODULES_PER_SUBSYS = 8   # 每个子系统至多 N 个模块槽位
MAX_FILES_IN_SUBSYS_PROMPT = 80

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

# 与 code_parser.SUBSYSTEM_FINGERPRINTS 同口径；保留显示顺序
_SUBSYS_DISPLAY_ORDER = [
    "进程管理", "内存管理", "文件系统",
    "系统调用", "设备驱动", "硬件抽象", "其他",
]


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
    扫描仓库源文件，按 SUBSYSTEM_FINGERPRINTS 归类到 6 个 OS 子系统；
    没有命中任何子系统的归到"其他"。
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
        subsys = file_to_subsys.get(f["path"], "其他")
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
        "JSON schema：\n"
        '{\n'
        '  "name":"...","role":"...","summary":"...",\n'
        '  "highlights":[{"path":"...","quote":"..."}],\n'
        '  "issues":[{"path":"...","severity":"low|medium|high","quote":"..."}],\n'
        '  "modules":[\n'
        '    {"slot":1,"name":"模块名","summary":"≤200字",'
        '"file_paths":["..."]}\n'
        '  ]\n'
        '}\n\n'
        "modules[].slot 是 1..8 之间的整数，对应你用 outputs.module_paths[slot-1] "
        "写出的那份 .md（slot 从 1 开始计数）。\n\n"
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


def _validate_subsys_result(parsed: dict, subsystem_name: str) -> None:
    """子系统分析必须有真实总览和至少一个完整模块，否则拒绝继续生成报告。"""
    if parsed.get("_error"):
        raise RuntimeError(f"{subsystem_name} 语义聚合失败：{parsed['_error']}")
    if not str(parsed.get("summary") or "").strip():
        raise RuntimeError(f"{subsystem_name} 缺少子系统语义摘要")
    if not str(parsed.get("content") or "").strip():
        raise RuntimeError(f"{subsystem_name} 缺少子系统分析正文")
    modules = parsed.get("modules") or []
    if not isinstance(modules, list) or not modules:
        raise RuntimeError(f"{subsystem_name} 未形成任何真实模块分析")
    for index, module in enumerate(modules, start=1):
        if not isinstance(module, dict):
            raise RuntimeError(f"{subsystem_name} 第 {index} 个模块格式无效")
        missing = [field for field in ("name", "summary", "content")
                   if not str(module.get(field) or "").strip()]
        if not module.get("file_paths"):
            missing.append("file_paths")
        if missing:
            raise RuntimeError(
                f"{subsystem_name} 第 {index} 个模块缺少：{'、'.join(missing)}")


def _process_one_subsys(subsys_node: dict, repo_path: Path,
                         work_dir: Path, facts: dict | None) -> None:
    """处理一个子系统节点：跑 LLM、读回所有 .md、填充节点与模块子节点。"""
    files     = subsys_node["files"]
    outputs   = _build_subsys_outputs(subsys_node, work_dir)
    out_path  = Path(outputs["json_path"])
    module_paths = outputs["module_paths"]

    def _enrich(parsed: dict) -> dict:
        """把 agent 落盘的 HTML 正文读进 parsed，使其随 JSON 一起进缓存。"""
        parsed["content"] = _read_md_if_exists(Path(outputs["content_path"]))
        for i, m in enumerate(parsed.get("modules") or [], start=1):
            slot = int(m.get("slot") or i)
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
            _validate_subsys_result(parsed, subsys_node["name"])
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
    _validate_subsys_result(parsed, subsys_node["name"])

    # 填子系统字段（子系统/模块不打分，评分只在顶层 VERDICT）
    # 正文已由 enrich 读入 parsed（含缓存命中场景）
    from finals.readability import concise_module_summary, explain_terms_on_first_use

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


def _normalize_adjacent_module_paths(paths: list, repo_path: Path) -> list[str]:
    """把模型省略目录的相邻文件名补成仓库内真实路径。"""
    normalized: list[str] = []
    for value in paths or []:
        raw = str(value or "").strip().replace("\\", "/")
        if not raw:
            continue
        candidate = raw
        if "/" not in raw and normalized:
            parent = Path(normalized[-1]).parent
            adjacent = (parent / raw).as_posix()
            if (repo_path / Path(adjacent)).exists():
                candidate = adjacent
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
        "3. 对设计不完整或不合理的问题，必须说明具体模块、性能/正确性影响、真实 path:line；"
        "若某种不合理设计会对特定测试有利，也要明确写出获益条件。\n"
        "4. 必要时 compare_with_reference_os(facts.meta.reference_os) / read_file / search_code 验证关键判断\n"
        "5. 工具调用 ≤5 次；其余判断可使用 findings 已附的代码摘录，但不得跳过任何扫描线索\n\n"
        "**写出顺序**：\n"
        "  a. 详细评判 HTML 片段（不含图表）→ 写到 outputs.content_path\n"
        "  b. 结构化 JSON → 写到 outputs.json_path\n\n"
        "JSON schema：\n"
        '{\n'
        '  "score_total":int,\n'
        '  "dimensions":[6 items: 原创性/架构合理性/代码质量/文档质量/完整性/功能性,\n'
        '    each {"name":"...","score":int,"reason":"..."}],\n'
        '  "highlights":[{"path":"...","quote":"..."}],\n'
        '  "issues":[{"path":"...","severity":"low|medium|high","quote":"..."}],\n'
        '  "hardcode_reviews":[{\n'
        '    "signal_id":"原 signal_id；AI 主动发现时用 ai-new-N",\n'
        '    "category":"四类方法之一","path":"真实相对路径","line":int,\n'
        '    "status":"confirmed|suspected|cleared",\n'
        '    "method":"具体作弊或获益方法；cleared 时写未构成原因",\n'
        '    "reason":"结合代码上下文的中文判断","confidence":0到100,\n'
        '    "excerpt":"不超过200字的关键代码摘录"\n'
        '  }],\n'
        '  "one_line":"... ≤40 字"\n'
        '}\n\n'
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
    scale = 10 if present and max(present) <= 10 else 1
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


def _validate_verdict_result(parsed: dict) -> None:
    """总评必须包含真实正文、六维评分和理由，不允许用固定分数补位。"""
    if parsed.get("_error"):
        raise RuntimeError(f"顶层评判失败：{parsed['_error']}")
    if not str(parsed.get("content") or "").strip():
        raise RuntimeError("顶层评判缺少详细正文")
    if not str(parsed.get("one_line") or "").strip():
        raise RuntimeError("顶层评判缺少一句话结论")
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


def _validate_hardcode_reviews(parsed: dict, facts: dict | None) -> None:
    """确保每条规则线索都经过 AI 复核，且结论能回到真实代码位置。"""
    signals = ((((facts or {}).get("integrity") or {}).get("hardcode") or {})
               .get("findings") or [])
    reviews = parsed.get("hardcode_reviews") or []
    if not isinstance(reviews, list):
        raise RuntimeError("顶层评判 hardcode_reviews 格式无效")

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
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"硬编码复核 {signal_id} 缺少置信度") from exc
        if not 0 <= confidence <= 100:
            raise RuntimeError(f"硬编码复核 {signal_id} 的置信度不在 0–100")
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
        if str(review.get("path")) != str(signal.get("path")):
            raise RuntimeError(f"硬编码复核 {signal_id} 的路径与扫描证据不一致")
        if int(review.get("line") or 0) != int(signal.get("line") or 0):
            raise RuntimeError(f"硬编码复核 {signal_id} 的行号与扫描证据不一致")
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


def run_verdict_stage(tree_root: dict, facts: dict | None,
                       work_dir: Path,
                       repo_path: Path | None = None) -> dict:
    outputs = {
        "json_path":    str(work_dir / "verdict.json"),
        "content_path": str(work_dir / "verdict.content.md"),
    }
    out_path = Path(outputs["json_path"])

    subsys_summaries = _collect_subsys_summaries(tree_root)
    def _enrich(parsed: dict) -> dict:
        """把 verdict 详细正文（HTML，含强制图表）读进 parsed，随 JSON 一起进缓存。"""
        parsed["content"] = _read_md_if_exists(Path(outputs["content_path"]))
        # 正常情况下提示词已直接生成中文；只有检测出英文正文时才调用翻译兜底。
        from .lang_guard import normalize_tree_language
        normalize_tree_language(parsed)
        return parsed

    from .lang_guard import language_output_complete

    def _delivery_complete(parsed: dict) -> bool:
        if not language_output_complete(parsed):
            return False
        try:
            _validate_verdict_result(parsed)
            _validate_hardcode_reviews(parsed, facts)
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
    _validate_verdict_result(parsed)
    _validate_hardcode_reviews(parsed, facts)
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
    print(f"\n[tree] 按 OS 子系统归类源文件 ...")
    tree_root, file_count = enumerate_subsystems(repo_path)
    print(f"[tree] 命中 {file_count} 个源文件，"
          f"{len(tree_root['children'])} 个子系统：" +
          " / ".join(c["name"] for c in tree_root["children"]))

    if file_count == 0:
        print("[tree] 仓库未找到可索引源文件，放弃。", file=sys.stderr)
        return _empty_tree(repo_name, ts, facts)

    # B. SUBSYS 并发分析
    run_subsys_stage(tree_root, repo_path, out_dir, facts)

    # C. VERDICT 综合
    print(f"[tree] VERDICT 阶段 ...")
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
