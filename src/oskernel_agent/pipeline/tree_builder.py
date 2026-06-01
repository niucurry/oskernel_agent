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
    BatchTask, cache_key, run_batch_task,
)

SCHEMA_VERSION = "tree-v3"
PROMPT_VERSION_SUBSYS  = "subsys-v3"
PROMPT_VERSION_VERDICT = "verdict-v8"

MAX_MODULES_PER_SUBSYS = 8   # 每个子系统至多 N 个模块槽位

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

_DEFAULT_SUBSYS_CONCURRENCY = 4


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


def _build_subsys_request(subsys_node: dict, repo_path: Path,
                           outputs: dict, facts: dict | None) -> str:
    """构造 SUBSYS agent 的 user message。"""
    files = subsys_node["files"]
    payload = {
        "repo_path":      str(repo_path),
        "subsystem":      subsys_node["name"],
        "reference_os":   (facts or {}).get("meta", {}).get("reference_os"),
        "files":          [
            {"path": f["path"], "name": f["name"], "lang": f["lang"]}
            for f in files
        ],
        "outputs":        outputs,
    }
    return (
        "你负责分析仓库中的某一个 OS 子系统（如文件系统、内存管理）。"
        "请阅读这些代码，**自己决定该子系统内部的模块拆分**"
        "（典型 2–5 个模块，最多 8 个），然后产出：\n\n"
        "  a. 子系统总览 **HTML 片段**（写到 outputs.content_path）—— 总评、"
        "模块列表、整体架构图（Mermaid）\n"
        "  b. 每个模块的详细 **HTML 片段**（写到 outputs.module_paths[i] 中你选用的槽位）\n"
        "  c. 结构化 JSON（写到 outputs.json_path）—— 含模块清单与各模块槽位号\n\n"
        "内容直接写 HTML（不要 Markdown）：图表用 `<pre class=\"mermaid\">…</pre>` 或 "
        "`<div class=\"echarts-chart\" style=\"height:360px\"><script type=\"application/json\">{…}"
        "</script></div>`；文件引用写纯文本 path:line（自动变链接）。\n\n"
        "工作步骤：\n"
        "1. initialize_analysis(repo_path)\n"
        "2. analyze_subtree('') 或针对 files 中目录调 analyze_subtree(dir)\n"
        "3. 浏览 files 列表与符号清单，识别模块拆分\n"
        "4. read_file / find_symbol_definition 看关键模块的实现细节\n"
        "5. 工具调用总数 ≤12 次\n\n"
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
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )


def _subsys_fallback(subsys_node: dict) -> dict:
    n = len(subsys_node["files"])
    return {
        "name":       subsys_node["name"],
        "role":       subsys_node["name"],
        "summary":    f"本子系统包含 {n} 个文件（LLM 聚合失败，使用规则兜底）。",
        "highlights": [],
        "issues":     [],
        "modules":    [],
    }


def _process_one_subsys(subsys_node: dict, repo_path: Path,
                         work_dir: Path, cache_dir: Path,
                         facts: dict | None) -> None:
    """处理一个子系统节点：跑 LLM、读回所有 .md、填充节点与模块子节点。"""
    files     = subsys_node["files"]
    outputs   = _build_subsys_outputs(subsys_node, work_dir)
    out_path  = Path(outputs["json_path"])
    sub_cache = cache_dir / "subsys"
    sub_cache.mkdir(parents=True, exist_ok=True)

    # 缓存键：子系统名 + 所有文件 mtime
    file_keys = [f"{f['path']}:{f['mtime']}" for f in files]
    ck = cache_key(PROMPT_VERSION_SUBSYS, subsys_node["name"], *file_keys)

    module_paths = outputs["module_paths"]

    def _enrich(parsed: dict) -> dict:
        """把 agent 落盘的 HTML 正文读进 parsed，使其随 JSON 一起进缓存。"""
        parsed["content"] = _read_md_if_exists(Path(outputs["content_path"]))
        for i, m in enumerate(parsed.get("modules") or [], start=1):
            slot = int(m.get("slot") or i)
            if 1 <= slot <= MAX_MODULES_PER_SUBSYS:
                m["content"] = _read_md_if_exists(Path(module_paths[slot - 1]))
        return parsed

    task = BatchTask(
        batch_id=f"subsys-{_safe_filename_part(subsys_node['name'])}",
        agent_name="os-kernel-subsys",
        user_request=_build_subsys_request(
            subsys_node, repo_path, outputs, facts),
        output_path=out_path,
        cache_dir=sub_cache,
        cache_key=ck,
        fallback=_subsys_fallback(subsys_node),
        enrich=_enrich,
    )
    parsed = run_batch_task(
        task,
        schema_hint='{"name":str,"role":str,"summary":str,'
                    '"highlights":[...],"issues":[...],'
                    '"modules":[{slot:int,name:str,summary:str,'
                    'file_paths:[...]}]}',
        timeout=600,
    )

    # 填子系统字段（子系统/模块不打分，评分只在顶层 VERDICT）
    # 正文已由 enrich 读入 parsed（含缓存命中场景）
    subsys_node["role"]       = parsed.get("role", subsys_node["name"])
    subsys_node["summary"]    = parsed.get("summary", "")
    subsys_node["content"]    = parsed.get("content", "")
    subsys_node["highlights"] = parsed.get("highlights", [])
    subsys_node["issues"]     = parsed.get("issues", [])

    # 把 modules 转成 children
    subsys_node["children"] = []
    for i, m in enumerate(parsed.get("modules") or [], start=1):
        slot = int(m.get("slot") or i)
        if not (1 <= slot <= MAX_MODULES_PER_SUBSYS):
            continue
        subsys_node["children"].append({
            "type":       "module",
            "name":       m.get("name", f"模块 {slot}"),
            "path":       f"{subsys_node['path']}/m{slot:03d}",
            "summary":    m.get("summary", ""),
            "file_paths": m.get("file_paths", []),
            "content":    m.get("content", ""),
        })


def run_subsys_stage(tree_root: dict, repo_path: Path,
                      work_dir: Path, cache_dir: Path,
                      facts: dict | None) -> None:
    """对每个子系统并发跑一次 SUBSYS agent，原地填子系统与模块节点。"""
    subsys_nodes = [c for c in tree_root.get("children", [])
                    if c.get("type") == "subsystem"]
    if not subsys_nodes:
        return

    workers = _subsys_concurrency()
    print(f"[tree] SUBSYS 阶段：{len(subsys_nodes)} 个子系统"
          f"（并发 {min(workers, len(subsys_nodes))}）",
          file=sys.stderr, flush=True)

    with ThreadPoolExecutor(max_workers=min(workers, len(subsys_nodes))) as ex:
        futs = {
            ex.submit(_process_one_subsys, n, repo_path,
                      work_dir, cache_dir, facts): n
            for n in subsys_nodes
        }
        for fut in as_completed(futs):
            n = futs[fut]
            try:
                fut.result()
            except Exception as e:
                print(f"[tree] {n['name']} 异常：{e}",
                      file=sys.stderr, flush=True)


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
        "你是仓库顶层评判会话，综合下面"
        "facts + 各 OS 子系统的总结，产出整体评判结论。\n"
        "**详细正文写成独立 HTML 片段，JSON 只放结构化字段**。\n"
        "正文直接写 HTML（不要 Markdown）：雷达图用 "
        "`<div class=\"echarts-chart\" style=\"height:380px\"><script type=\"application/json\">{…}"
        "</script></div>`；文件引用写纯文本 path:line（自动变链接）。\n\n"
        "工作步骤：\n"
        "1. initialize_analysis(repo_path)\n"
        "2. 必要时 compare_with_reference_os(facts.meta.reference_os) "
        "/ search_code 验证关键判断\n"
        "3. 工具调用 ≤5 次\n\n"
        "**写出顺序**：\n"
        "  a. 详细评判 HTML 片段（含强制雷达图）→ 写到 outputs.content_path\n"
        "  b. 结构化 JSON → 写到 outputs.json_path\n\n"
        "JSON schema：\n"
        '{\n'
        '  "score_total":int,\n'
        '  "dimensions":[5 items: 原创性/架构合理性/代码质量/文档质量/完整性,\n'
        '    each {"name":"...","score":int,"reason":"..."}],\n'
        '  "highlights":[{"path":"...","quote":"..."}],\n'
        '  "issues":[{"path":"...","severity":"low|medium|high","quote":"..."}],\n'
        '  "one_line":"... ≤40 字"\n'
        '}\n\n'
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )


def _verdict_fallback() -> dict:
    return {
        "score_total": 60,
        "dimensions": [
            {"name": "原创性",     "score": 60, "reason": "LLM 评判失败。"},
            {"name": "架构合理性", "score": 60, "reason": "LLM 评判失败。"},
            {"name": "代码质量",   "score": 60, "reason": "LLM 评判失败。"},
            {"name": "文档质量",   "score": 60, "reason": "LLM 评判失败。"},
            {"name": "完整性",     "score": 60, "reason": "LLM 评判失败。"},
        ],
        "highlights": [],
        "issues":     [],
        "one_line":   "LLM 顶层评判失败，使用规则兜底。",
        "_error":     "verdict_fallback",
    }


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
                       work_dir: Path, cache_dir: Path,
                       repo_path: Path | None = None) -> dict:
    verdict_cache = cache_dir / "verdict"
    verdict_cache.mkdir(parents=True, exist_ok=True)

    outputs = {
        "json_path":    str(work_dir / "verdict.json"),
        "content_path": str(work_dir / "verdict.content.md"),
    }
    out_path = Path(outputs["json_path"])

    subsys_summaries = _collect_subsys_summaries(tree_root)
    facts_str = json.dumps(facts or {}, ensure_ascii=False, sort_keys=True)
    subsys_str = json.dumps(
        [s["name"] for s in subsys_summaries],
        ensure_ascii=False, sort_keys=True,
    )
    ck = cache_key(PROMPT_VERSION_VERDICT, facts_str[:2048], subsys_str)

    def _enrich(parsed: dict) -> dict:
        """把 verdict 详细正文（HTML，含强制图表）读进 parsed，随 JSON 一起进缓存。"""
        parsed["content"] = _read_md_if_exists(Path(outputs["content_path"]))
        return parsed

    task = BatchTask(
        batch_id="verdict",
        agent_name="os-kernel-verdict",
        user_request=_build_verdict_request(
            facts, subsys_summaries, outputs, repo_path or Path(".")),
        output_path=out_path,
        cache_dir=verdict_cache,
        cache_key=ck,
        fallback=_verdict_fallback(),
        enrich=_enrich,
    )
    parsed = run_batch_task(
        task,
        schema_hint='{"score_total":int,"dimensions":[...5 items...],'
                    '"highlights":[...],"issues":[...],"one_line":str}',
        timeout=600,
    )
    # 正文已由 enrich 读入 parsed（含缓存命中场景）
    return parsed


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
        Path(config.data.get("reports_dir", "./data/reports")).resolve()
        / f"{repo_name}_{ts}_tree"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_root = Path(config.data.get("cache_dir", "./data/cache")).resolve()
    cache_dir  = cache_root / f"{repo_name}_tree"
    cache_dir.mkdir(parents=True, exist_ok=True)

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
    run_subsys_stage(tree_root, repo_path, out_dir, cache_dir, facts)

    # C. VERDICT 综合
    print(f"[tree] VERDICT 阶段 ...")
    verdict = run_verdict_stage(tree_root, facts, out_dir, cache_dir, repo_path)

    return {
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
