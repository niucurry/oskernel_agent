import os
import re
import sqlite3
import subprocess
from pathlib import Path
from collections import defaultdict

import tree_sitter_c as tsc
import tree_sitter_rust as tsr
from tree_sitter import Language, Parser

# 指向克隆下来的具体代码库路径
TARGET_REPO_DIR = './data/historical_repos/T202510008995695-2259'
REPO_ID = 'T202510008995695-2259' 
DB_PATH = './data/os_knowledge_graph.db'

# 1. 初始化 C 语言和 Rust 语言解析器
c_parser = Parser(Language(tsc.language()))
rust_parser = Parser(Language(tsr.language()))

def init_db():
    """初始化 SQLite 数据库"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id TEXT,
            file_path TEXT,
            symbol_type TEXT,
            symbol_name TEXT,
            code_content TEXT,
            start_line INTEGER
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS CallGraph (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id TEXT,
            caller_name TEXT,
            callee_name TEXT
        )
    ''')
    
    # 为了防止重复运行导致数据叠加，每次运行时先清理当前项目的数据
    cursor.execute("DELETE FROM Symbols WHERE repo_id=?", (REPO_ID,))
    cursor.execute("DELETE FROM CallGraph WHERE repo_id=?", (REPO_ID,))
    
    conn.commit()
    return conn

def find_function_calls(func_code_bytes, lang_type):
    """局部解析，提取内部调用的子函数 (兼容 C 和 Rust)"""
    parser = c_parser if lang_type == 'c' else rust_parser
    tree = parser.parse(func_code_bytes)
    calls = set()
    
    def traverse(node):
        # 普通函数调用 a() 或 a.b() 或 a::b()
        if node.type == 'call_expression':
            func_node = node.child_by_field_name('function')
            if func_node:
                callee = func_code_bytes[func_node.start_byte:func_node.end_byte].decode('utf-8')
                calls.add(callee)
                
        # Rust 特有的宏调用，比如 println!()
        elif lang_type == 'rust' and node.type == 'macro_invocation':
            macro_node = node.child(0) # 获取宏名称节点
            if macro_node:
                macro_name = func_code_bytes[macro_node.start_byte:macro_node.end_byte].decode('utf-8')
                calls.add(macro_name)
                
        for child in node.children:
            traverse(child)
            
    traverse(tree.root_node)
    return list(calls)

def get_nodes_of_types(node, target_types):
    """递归获取所有指定类型的节点，解决 Rust 中函数包裹在 impl 块内的问题"""
    result = []
    if node.type in target_types:
        result.append(node)
    for child in node.children:
        result.extend(get_nodes_of_types(child, target_types))
    return result

def parse_file_and_store(file_path, conn):
    """解析单个 C/Rust 文件并将数据入库"""
    cursor = conn.cursor()
    rel_path = os.path.relpath(file_path, TARGET_REPO_DIR)
    ext = os.path.splitext(file_path)[1]
    
    # 动态匹配语言解析策略
    if ext == '.c':
        parser, lang_type = c_parser, 'c'
        struct_type, func_type = 'struct_specifier', 'function_definition'
    elif ext == '.rs':
        parser, lang_type = rust_parser, 'rust'
        struct_type, func_type = 'struct_item', 'function_item'
    else:
        return

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            source_code = f.read()
        source_bytes = source_code.encode('utf-8')
    except Exception as e:
        print(f" 跳过无法读取的文件 {rel_path}: {e}")
        return

    tree = parser.parse(source_bytes)
    
    # 递归提取所有目标节点
    target_nodes = get_nodes_of_types(tree.root_node, [struct_type, func_type])

    for child in target_nodes:
        # 1. 处理结构体定义
        if child.type == struct_type:
            struct_name_node = child.child_by_field_name('name')
            if struct_name_node:
                struct_name = source_bytes[struct_name_node.start_byte:struct_name_node.end_byte].decode('utf-8')
                struct_body = source_bytes[child.start_byte:child.end_byte].decode('utf-8')
                
                cursor.execute('''
                    INSERT INTO Symbols (repo_id, file_path, symbol_type, symbol_name, code_content, start_line)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (REPO_ID, rel_path, 'struct', struct_name, struct_body, child.start_point[0]))

        # 2. 处理函数定义
        elif child.type == func_type:
            func_name = None
            
            if lang_type == 'c':
                declarator = child.child_by_field_name('declarator')
                if declarator:
                    while declarator.type != 'identifier' and declarator.child_by_field_name('declarator'):
                        declarator = declarator.child_by_field_name('declarator')
                    if declarator.type == 'identifier':
                        func_name = source_bytes[declarator.start_byte:declarator.end_byte].decode('utf-8')
                        
            elif lang_type == 'rust':
                name_node = child.child_by_field_name('name')
                if name_node:
                    func_name = source_bytes[name_node.start_byte:name_node.end_byte].decode('utf-8')
            
            if func_name:
                func_body_bytes = source_bytes[child.start_byte:child.end_byte]
                func_body_str = func_body_bytes.decode('utf-8')
                
                cursor.execute('''
                    INSERT INTO Symbols (repo_id, file_path, symbol_type, symbol_name, code_content, start_line)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (REPO_ID, rel_path, 'function', func_name, func_body_str, child.start_point[0]))
                
                # 3. 提取调用图
                callees = find_function_calls(func_body_bytes, lang_type)
                for callee in callees:
                    cursor.execute('''
                        INSERT INTO CallGraph (repo_id, caller_name, callee_name)
                        VALUES (?, ?, ?)
                    ''', (REPO_ID, func_name, callee))
                    
    conn.commit()

def deduplicate_parent_paths(paths: list[str]) -> list[str]:
    """如果 A 是 B 的父路径，去掉 A，保留更精确的 B"""
    paths = sorted(paths, key=len, reverse=True)
    result = []
    for p in paths:
        if not any(kept.startswith(p + "/") for kept in result):
            result.append(p)
    return result


def find_source_roots(repo_path: Path) -> list[str]:
    """找到仓库中源代码文件最密集的目录"""
    SKIP_DIRS = {".git", "target", "build", "node_modules", "__pycache__", ".cargo", "vendor"}
    SRC_EXTS = {".c", ".rs", ".h", ".S", ".asm"}
    KNOWN_NAMES = {"src", "kernel", "kern", "os", "core", "code"}

    candidates = []
    for entry in repo_path.rglob("*"):
        if not entry.is_dir():
            continue
        if any(skip in entry.parts for skip in SKIP_DIRS):
            continue

        src_count = sum(
            1 for f in entry.iterdir()
            if f.is_file() and f.suffix in SRC_EXTS
        )
        if src_count > 0:
            rel = str(entry.relative_to(repo_path))
            candidates.append((rel, src_count, entry.name.lower()))

    candidates.sort(key=lambda x: (x[2] not in KNOWN_NAMES, -x[1]))

    return deduplicate_parent_paths([c[0] for c in candidates[:8]])


SUBSYSTEM_FINGERPRINTS = {
    "进程管理": [
        "fork", "exec", "waitpid", "do_fork", "task_struct",
        "proc_struct", "TaskControlBlock", "switch_to", "schedule()"
    ],
    "内存管理": [
        "page_alloc", "alloc_pages", "PageTable", "mmap", "buddy",
        "page_fault", "MemorySet", "MapArea", "PhysPageNum", "brk"
    ],
    "文件系统": [
        "fat32", "ext4", "inode", "dentry", "vfs", "open_file",
        "FileDescriptor", "FAT", "superblock", "block_device"
    ],
    "系统调用": [
        "sys_read", "sys_write", "sys_fork", "ecall",
        "trap_handler", "syscall_handler", "SYSCALL_"
    ],
    "设备驱动": [
        "virtio", "uart", "mmio", "disk_read", "disk_write",
        "PLIC", "interrupt", "block_device"
    ],
    "硬件抽象": [
        "riscv", "loongarch", "satp", "stvec", "TrapContext",
        "sret", "mret", "CSR_", "__riscv"
    ],
}


def classify_files_by_content(repo_path: str, source_roots: list[str]) -> dict:
    """按内容指纹将源文件归类到子系统"""
    result = {s: [] for s in SUBSYSTEM_FINGERPRINTS}
    SRC_EXTS = {".c", ".rs", ".h", ".S"}

    repo = Path(repo_path)
    for root in source_roots:
        for src_file in (repo / root).rglob("*"):
            if not src_file.is_file():
                continue
            if src_file.suffix not in SRC_EXTS:
                continue
            if src_file.stat().st_size > 500_000:
                continue

            content = src_file.read_text(errors="replace").lower()
            rel_path = str(src_file.relative_to(repo_path))

            scores = {}
            for subsystem, keywords in SUBSYSTEM_FINGERPRINTS.items():
                hits = sum(1 for kw in keywords if kw.lower() in content)
                if hits >= 2:
                    scores[subsystem] = hits

            if not scores:
                continue

            max_score = max(scores.values())
            for subsystem, score in scores.items():
                result[subsystem].append({
                    "file": rel_path,
                    "score": score,
                    "is_primary": (score == max_score),
                })

    for s in result:
        result[s].sort(key=lambda x: -x["score"])

    return result


DOC_PATTERNS = {
    "readme":     r"readme(\.\w+)?$",
    "design_doc": r"(design|arch|architecture|设计|架构).+\.(md|pdf|docx|txt)$",
    "report":     r"(report|总结|报告|技术报告).+\.(md|pdf|docx|txt)$",
    "slides":     r"\.(pptx|ppt|key)$",
    "changelog":  r"(changelog|history)\.(md|txt)$",
}

_EXT_TO_READER = {
    ".pdf":  "pdf_reader",
    ".docx": "docx_reader",
    ".pptx": "pptx_reader",
    ".ppt":  "pptx_reader",
    ".key":  "pptx_reader",
}


def find_doc_files(repo_path: Path) -> dict[str, list[str]]:
    """递归扫描仓库，按类型收集文档文件"""
    found: dict[str, list[str]] = {k: [] for k in DOC_PATTERNS}
    SKIP = {".git", "target", "build"}

    for f in repo_path.rglob("*"):
        if not f.is_file():
            continue
        if any(s in f.parts for s in SKIP):
            continue

        name_lower = f.name.lower()
        rel = str(f.relative_to(repo_path))

        for doc_type, pattern in DOC_PATTERNS.items():
            if re.search(pattern, name_lower, re.IGNORECASE):
                found[doc_type].append(rel)

    return {k: v for k, v in found.items() if v}


def annotate_doc_readers(doc_files: dict[str, list[str]]) -> dict[str, list[dict]]:
    """为每个文档条目附加读取器类型，返回 {doc_type: [{path, reader}]}"""
    result: dict[str, list[dict]] = {}
    for doc_type, paths in doc_files.items():
        result[doc_type] = [
            {
                "path": path,
                "reader": _EXT_TO_READER.get(Path(path).suffix.lower(), "text_reader"),
            }
            for path in paths
        ]
    return result


def detect_naming_style(repo_path: Path) -> str:
    """采样最多 20 个源文件，判断仓库整体命名风格"""
    snake_count = camel_count = 0

    samples = list(repo_path.rglob("*.rs"))[:10] + \
              list(repo_path.rglob("*.c"))[:10]

    for f in samples:
        try:
            content = f.read_text(errors="replace")
        except Exception:
            continue
        snake_count += len(re.findall(r'\b(fn|void|int)\s+[a-z][a-z0-9_]+\s*\(', content))
        camel_count += len(re.findall(r'\b[A-Z][a-zA-Z0-9]{3,}\b', content))

    if camel_count > snake_count * 2:
        return "CamelCase"
    if snake_count > camel_count * 2:
        return "snake_case"
    return "mixed"


def detect_primary_language(source_roots: list[str]) -> dict:
    """
    按代码行数加权识别主语言，排除汇编和头文件干扰。
    返回：{
      "primary": "rust",
      "secondary": "c",
      "has_assembly": True,
      "loc": {"rust": 8420, "c": 312, "asm": 89}
    }
    """
    loc: dict[str, int] = defaultdict(int)

    LANG_MAP = {
        ".rs":  "rust",
        ".c":   "c",
        ".h":   "c",
        ".cpp": "cpp",
        ".S":   "asm",
        ".s":   "asm",
        ".asm": "asm",
    }

    for root in source_roots:
        for f in Path(root).rglob("*"):
            if not f.is_file():
                continue
            lang = LANG_MAP.get(f.suffix.lower())
            if not lang:
                continue
            try:
                lines = sum(
                    1 for line in f.read_text(errors="replace").splitlines()
                    if line.strip()
                )
                loc[lang] += lines
            except Exception:
                continue

    substantive = {k: v for k, v in loc.items() if k != "asm"}

    if not substantive:
        primary = "unknown"
    else:
        primary = max(substantive, key=substantive.__getitem__)

    secondary_langs = [k for k in substantive if k != primary and substantive[k] > 100]
    secondary = secondary_langs[0] if secondary_langs else None

    return {
        "primary":      primary,
        "secondary":    secondary,
        "has_assembly": loc.get("asm", 0) > 0,
        "loc":          dict(loc),
    }


# 参考 OS 溯源：三层判断
STRUCT_FINGERPRINTS: dict[str, list[str]] = {
    "rcore-tutorial-v3": [
        "TaskControlBlock",
        "MemorySet",
        "MapArea",
        "TrapContext",
        "AppManager",
    ],
    "rcore-tutorial-v2": [
        "AppManager",
        "TaskContext",
        "__switch",
    ],
    "xv6-riscv": [
        "struct proc",
        "struct spinlock",
        "struct inode",
        "kalloc",
        "kinit",
    ],
    "ucore": [
        "proc_struct",
        "pmm_manager",
        "run_queue",
        "struct Page",
        "le_to_struct",
    ],
    "titanix": [
        "Titanix",
        "ProcessInner",
        "FrameTracker",
    ],
}

GIT_FIRST_COMMIT_HINTS: dict[str, list[str]] = {
    "rcore-tutorial": ["rcore", "rcore", "tutorial", "chapter"],
    "xv6":            ["xv6", "mit", "6.828", "6.s081"],
    "ucore":          ["ucore", "ucore", "tsinghua"],
}

FUNC_FINGERPRINTS: dict[str, list[str]] = {
    "rcore-tutorial-v3": ["trap_handler", "sys_fork", "translated_byte_buffer"],
    "xv6-riscv":         ["usertrap", "kernelvec", "uservec", "forkret"],
    "ucore":             ["do_fork", "copy_mm", "load_icode"],
}


def _search_in_repo(repo_path: str, keyword: str, source_roots: list[str]) -> bool:
    """在所有源文件中搜索关键词，找到即返回 True。"""
    for root in source_roots:
        for f in Path(root).rglob("*"):
            if f.suffix not in {".rs", ".c", ".h"}:
                continue
            try:
                if keyword in f.read_text(errors="replace"):
                    return True
            except Exception:
                continue
    return False


def detect_reference_os(repo_path: str, structure: dict) -> dict:
    """
    三层指纹叠加判断参考 OS 来源。
    返回：{
      "name": "rcore-tutorial-v3",   # None 表示独立实现
      "confidence": "high",          # high / medium / low
      "evidence": [...],
      "all_scores": {...},
    }
    """
    scores: dict[str, int] = defaultdict(int)
    evidence: dict[str, list[str]] = defaultdict(list)

    # 第一层：数据结构名称（权重 3）
    for root in structure["source_roots"]:
        for src_file in Path(root).rglob("*"):
            if src_file.suffix not in {".rs", ".c", ".h"}:
                continue
            try:
                content = src_file.read_text(errors="replace")
            except Exception:
                continue
            try:
                rel = str(src_file.relative_to(repo_path))
            except ValueError:
                rel = str(src_file)
            for ref_name, keywords in STRUCT_FINGERPRINTS.items():
                for kw in keywords:
                    if kw in content:
                        scores[ref_name] += 3
                        evidence[ref_name].append(f"数据结构 '{kw}' 出现在 {rel}")

    # 第二层：Git 历史早期提交（权重 5）
    try:
        git_log = subprocess.run(
            ["git", "log", "--oneline", "--reverse", "--max-count=5"],
            cwd=repo_path, capture_output=True, text=True, timeout=10
        ).stdout.lower()
        for ref_name, hints in GIT_FIRST_COMMIT_HINTS.items():
            for hint in hints:
                if hint.lower() in git_log:
                    scores[ref_name] += 5
                    evidence[ref_name].append(f"Git 早期提交包含关键词 '{hint}'")
    except Exception:
        pass

    # 第三层：特征函数名（权重 1，容易被重命名）
    for ref_name, func_names in FUNC_FINGERPRINTS.items():
        for func in func_names:
            if _search_in_repo(repo_path, func, structure["source_roots"]):
                scores[ref_name] += 1
                evidence[ref_name].append(f"特征函数 '{func}' 存在")

    if not scores:
        return {
            "name":       None,
            "confidence": "high",
            "evidence":   ["无已知参考OS特征，疑似独立实现"],
            "all_scores": {},
        }

    best = max(scores, key=scores.__getitem__)
    best_score = scores[best]

    if best_score >= 8:
        confidence = "high"
    elif best_score >= 4:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "name":       best,
        "confidence": confidence,
        "evidence":   evidence[best][:5],
        "all_scores": dict(scores),
    }


def analyze_structure_depth(repo_path: Path) -> str:
    """根据源文件的平均目录深度判断项目结构层级"""
    depths = [
        len(f.relative_to(repo_path).parts)
        for ext in ("*.c", "*.rs")
        for f in repo_path.rglob(ext)
    ]
    if not depths:
        return "unknown"
    avg = sum(depths) / len(depths)
    if avg <= 2:
        return "flat"
    if avg <= 4:
        return "shallow"
    return "deep"


def detect_anomalies(structure: dict, repo_path: Path) -> list[str]:
    """根据预分析结构体生成需注入 System Prompt 的异常警告列表"""
    anomalies = []

    if not structure["doc_files"]:
        anomalies.append("未找到任何文档文件（README/设计文档/技术报告），文档评分可能为零")

    if not structure["subsystem_locations"].get("系统调用"):
        anomalies.append("未找到 syscall 处理代码，请检查是否使用了极为非常规的命名")

    if structure["structure_depth"] == "flat":
        anomalies.append("目录结构极度扁平（平均深度≤2），可能存在代码堆砌，缺乏模块划分")

    for root in structure["source_roots"]:
        for f in Path(root).rglob("*.c"):
            try:
                size = f.stat().st_size
                if size > 100_000:
                    rel = str(f.relative_to(repo_path))
                    anomalies.append(f"超大源文件：{rel}（{size // 1024}KB），疑似代码堆砌")
            except OSError:
                continue
        for f in Path(root).rglob("*.rs"):
            try:
                line_count = f.read_text(errors="replace").count("\n")
                if line_count > 800:
                    rel = str(f.relative_to(repo_path))
                    anomalies.append(f"超长源文件：{rel}（{line_count}行），建议人工核查模块划分")
            except OSError:
                continue

    if structure["naming_style"] == "mixed":
        anomalies.append("命名风格混杂（CamelCase 与 snake_case 并存），代码可能来自多个不同来源")

    return anomalies


_DOC_TYPE_LABELS: dict[str, str] = {
    "readme":     "README",
    "design_doc": "设计文档",
    "report":     "技术报告",
    "slides":     "幻灯片",
    "changelog":  "更新日志",
}

_READER_NOTES: dict[str, str] = {
    "pdf_reader":  "← 需使用 PDF 读取工具",
    "docx_reader": "← 需使用 DOCX 读取工具",
    "pptx_reader": "← 需使用 PPTX 读取工具",
}


def build_repo_profile(repo_path: Path) -> str:
    """组装所有静态分析结果，返回可直接注入 System Prompt 的结构描述文本"""
    rt = repo_path if isinstance(repo_path, Path) else Path(repo_path)

    source_roots_rel = find_source_roots(rt)
    source_roots_abs = [str(rt / r) for r in source_roots_rel]

    all_depths = [
        len(f.relative_to(rt).parts)
        for ext in ("*.c", "*.rs")
        for f in rt.rglob(ext)
    ]
    avg_depth = sum(all_depths) / len(all_depths) if all_depths else 0.0
    if not all_depths:
        depth_label = "unknown"
    elif avg_depth <= 2:
        depth_label = "flat"
    elif avg_depth <= 4:
        depth_label = "shallow"
    else:
        depth_label = "deep"

    naming = detect_naming_style(rt)
    lang_info = detect_primary_language(source_roots_abs)
    subsystem_map = classify_files_by_content(str(rt), source_roots_rel)
    doc_files = find_doc_files(rt)
    annotated = annotate_doc_readers(doc_files)

    structure = {
        "doc_files":           doc_files,
        "subsystem_locations": subsystem_map,
        "structure_depth":     depth_label,
        "source_roots":        source_roots_abs,
        "naming_style":        naming,
    }
    anomalies = detect_anomalies(structure, rt)
    ref_os = detect_reference_os(str(rt), structure)

    output: list[str] = ["【仓库结构探索结果（确定性分析，非 LLM 推断）】", ""]

    if source_roots_rel:
        root_parts = [f"{source_roots_rel[0]}（主要）"] + \
                     [f"{r}（次要）" for r in source_roots_rel[1:]]
        output.append(f"源码根目录：{'，'.join(root_parts)}")
    else:
        output.append("源码根目录：（未检测到）")

    output.append(f"目录风格：{depth_label}（平均深度 {avg_depth:.1f} 层）")
    output.append(f"命名风格：{naming}")

    primary = lang_info["primary"]
    secondary = lang_info["secondary"]
    has_asm = lang_info["has_assembly"]
    loc_parts = "、".join(f"{k} {v}行" for k, v in lang_info["loc"].items() if k != "asm")
    asm_note = f"（含汇编 {lang_info['loc'].get('asm', 0)} 行）" if has_asm else ""
    lang_line = f"主语言：{primary}"
    if secondary:
        lang_line += f"（次要：{secondary}）"
    lang_line += f"  {loc_parts}{asm_note}"
    output.append(lang_line)
    output.append("")

    ref_name = ref_os["name"] or "独立实现"
    ref_conf = ref_os["confidence"]
    output.append(f"参考OS溯源：{ref_name}（置信度：{ref_conf}）")
    for ev in ref_os["evidence"]:
        output.append(f"  · {ev}")
    output.append("")

    output.append("子系统文件定位（按内容关键词识别，非路径名）：")
    for subsystem, files in subsystem_map.items():
        if not files:
            output.append(f"  {subsystem} → 未找到明显的{subsystem}代码")
            continue
        for i, entry in enumerate(files[:3]):
            score = entry["score"]
            confidence = "高" if score >= 5 else "中" if score >= 3 else "低"
            if i == 0:
                output.append(f"  {subsystem} → {entry['file']}（置信度：{confidence}，命中{score}个关键词）")
            else:
                output.append(f"    └─ {entry['file']}（置信度：{confidence}，命中{score}个关键词）")
    output.append("")

    output.append("文档文件：")
    if annotated:
        for doc_type, entries in annotated.items():
            label = _DOC_TYPE_LABELS.get(doc_type, doc_type)
            for entry in entries:
                note = _READER_NOTES.get(entry["reader"], "")
                note_str = f"  {note}" if note else ""
                output.append(f"  {label:<8}→ {entry['path']}{note_str}")
    else:
        output.append("  （未找到任何文档文件）")
    output.append("")

    if anomalies:
        output.append("异常警告（分析时请特别注意）：")
        for anomaly in anomalies:
            output.append(f"  - {anomaly}")

    return "\n".join(output)


def build_knowledge_graph(repo_path: str | None = None):
    target = repo_path or TARGET_REPO_DIR
    rt = Path(target)
    source_roots = find_source_roots(rt)

    if source_roots:
        scan_dirs = [str(rt / r) for r in source_roots]
        print(f"  源码根目录检测结果: {source_roots}")
    else:
        scan_dirs = [target]

    print(f" 开始构建代码知识图谱 (支持 C/Rust)，目标仓库: {REPO_ID}")
    conn = init_db()

    processed_count = 0
    for scan_dir in scan_dirs:
        for root, dirs, files in os.walk(scan_dir):
            for file in files:
                if file.endswith(('.c', '.rs')):
                    file_path = os.path.join(root, file)
                    print(f"正在解析: {os.path.relpath(file_path, target)}...")
                    parse_file_and_store(file_path, conn)
                    processed_count += 1

    conn.close()
    print(f"\n 解析完成！共处理了 {processed_count} 个 C/Rust 源码文件。")
    print(f" 图谱数据已保存至 SQLite 数据库: {DB_PATH}")


    print(f"\n子系统分类结果")
    classification = classify_files_by_content(repo_path=str(rt), source_roots=source_roots)
    for subsystem, files in classification.items():
        if files:
            print(f"\n{subsystem} → {files}")

if __name__ == "__main__":
    build_knowledge_graph()