import re
import json
import subprocess
from pathlib import Path
from collections import defaultdict

import tree_sitter_c as tsc
import tree_sitter_rust as tsr
from tree_sitter import Language, Parser

c_parser = Parser(Language(tsc.language()))
rust_parser = Parser(Language(tsr.language()))

# 全局常量：所有需要跳过的噪声目录（供本文件内所有 rglob 循环复用）
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "target", "build", "node_modules",
    "__pycache__", ".cargo", "vendor",
    "third_party", "thirdparty", "external",
})

_SRC_EXTS: frozenset[str] = frozenset({".c", ".rs", ".h", ".S", ".asm"})


def _in_skip_dir(path: Path, base: Path) -> bool:
    """判断 path 是否位于 base 下的某个 skip 目录中。"""
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        parts = path.parts
    return any(part in _SKIP_DIRS for part in parts)


def _is_subpath_of(child: Path, parent: Path) -> bool:
    """判断 child 是否是 parent 的严格子路径（parent/... 形式）。"""
    try:
        child.relative_to(parent)
        return child != parent
    except ValueError:
        return False


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


def deduplicate_parent_paths(paths: list[str]) -> list[str]:
    """如果 A 是 B 的父路径，去掉 A，保留更精确的 B"""
    paths = sorted(paths, key=len, reverse=True)
    result = []
    for p in paths:
        if not any(kept.startswith(p + "/") for kept in result):
            result.append(p)
    return result


def find_source_roots(repo_path: Path) -> list[str]:
    """
    找到覆盖仓库内所有源代码文件的最小目录集合。

    算法：贪心最小覆盖集
      1. 遍历全部源文件（跳过 _SKIP_DIRS），收集其父目录
         - repo_path 本身也参与（处理根目录直接放文件的情况）
      2. 按路径深度升序排序，浅层目录优先处理
      3. 贪心选取：若某目录已被已选目录递归覆盖则跳过
      4. 最终每个选中目录用 ctags -R 扫描时能覆盖其下全部文件

    相比旧版改进：
      - 不再有 [:8] 硬截断，同级兄弟目录无论多少都能全部覆盖
      - 根目录下的直接源文件不再遗漏
      - 父目录有文件、子目录也有文件时，选父目录（-R 自动递归）
    """
    # 收集所有非 skip 源文件的直接父目录
    dirs_with_files: set[Path] = set()
    for f in repo_path.rglob("*"):
        if not f.is_file() or f.suffix not in _SRC_EXTS:
            continue
        if _in_skip_dir(f, repo_path):
            continue
        dirs_with_files.add(f.parent)

    if not dirs_with_files:
        return ["."]

    # 按深度升序贪心：浅层目录先处理，子目录若已被覆盖则跳过
    selected: list[Path] = []
    for d in sorted(dirs_with_files, key=lambda p: len(p.parts)):
        if not any(d == s or _is_subpath_of(d, s) for s in selected):
            selected.append(d)

    # 转为相对路径；repo_path 本身转为 "."
    result = []
    for d in selected:
        rel = str(d.relative_to(repo_path))
        result.append(rel if rel else ".")
    return result or ["."]


def run_ctags(repo_path: str, source_roots: list[str]) -> list[dict]:
    """调用 ctags 提取所有符号，返回原始 tag 列表（路径已转为相对于 repo_path 的相对路径）"""
    all_tags = []

    for root in source_roots:
        cmd = [
            "ctags",
            "--output-format=json",
            "--fields=+neStzK",   # n=行号 e=extras S=签名 t=类型 z=kind全名 K=kind全名备选
            "--extras=+fq",       # f=标记 file-scope 符号  q=产出全限定名
            "--kinds-c=+dfgmpstuvx",   # C: define/function/enum/macro/prototype/struct/typedef/union/variable
            "--kinds-rust=+fPMsgi",    # Rust: function/method/macro/struct/enum/trait
            *[f"--exclude={d}" for d in _SKIP_DIRS],
            "-R", root,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
                errors="replace",
                cwd=repo_path,
            )
        except FileNotFoundError:
            print("  警告：未找到 ctags 命令（需要 Universal Ctags）。符号索引为空，工具将无法查询符号。")
            break
        except subprocess.TimeoutExpired:
            print(f"  警告：ctags 扫描 {root} 超时，跳过")
            continue

        # 修复：防止 stdout 为 None 导致报错
        output = result.stdout or ""
        parsed_in_root = 0

        # 把原来的 result.stdout 改成 output
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                tag = json.loads(line)
                tag["rel_path"] = tag.get("path", "").replace(repo_path, "").lstrip("/")
                all_tags.append(tag)
                parsed_in_root += 1
            except json.JSONDecodeError:
                continue

        if parsed_in_root == 0 and result.returncode != 0:
            print(f"  警告：ctags 在 {root} 未产生任何 JSON 输出（returncode={result.returncode}）。"
                  f"可能是 Exuberant Ctags（不支持 --output-format=json），请安装 Universal Ctags。")

    return all_tags

#需要丢弃的噪声符号列表（常见库函数、宏、特殊标识符等）
_NOISE_NAMES = {
    "memset", "memcpy", "memmove", "strlen", "strcmp", "strncpy",
    "printf", "printk", "kprintf", "panic",
    "main",
}


def classify_symbol(tag: dict, primary_lang: str) -> str:
    """
    判断符号应进入哪一级：
      "level1"：第一级精简地图（注入 Prompt）
      "level2"：第二级完整索引
      "discard"：直接丢弃（噪声）
    """
    name      = tag.get("name", "")
    kind      = tag.get("kind", "")
    extras    = tag.get("extras", "")
    scope_kind = tag.get("scopeKind", "")

    if name in _NOISE_NAMES:
        return "discard"
    if name.startswith("__") and name.endswith("__"):
        return "discard"

    if primary_lang == "c":
        return _classify_c_symbol(name, kind, extras)
    if primary_lang == "rust":
        return _classify_rust_symbol(name, kind, scope_kind)
    return "level2"


def _classify_c_symbol(name: str, kind: str, extras: str) -> str:
    if kind == "function":
        if "fileScope" in extras:
            return "level2"
        if name.startswith("_") and not name.startswith("__"):
            return "level2"
        return "level1"

    if kind == "struct":
        return "level1"

    if kind == "macro":
        return "level1" if (name.isupper() and len(name) > 2) else "level2"

    if kind == "typedef":
        return "level1"

    if kind == "prototype":
        return "level1"

    # variable 及其他 kind → level2
    return "level2"


def _classify_rust_symbol(name: str, kind: str, scope_kind: str) -> str:
    if kind in ("function", "method"):
        if scope_kind in ("implementation", "impl"):
            if name in ("new", "run", "init", "exec", "spawn"):
                return "level1"
            return "level2"
        if name.startswith("sys_"):
            return "level1"
        if name.startswith("_"):
            return "level2"
        return "level1"

    if kind in ("struct", "enum", "trait", "macro"):
        return "level1"

    return "level2"



def _simplify_signature(sig: str) -> str:
    """
    简化函数签名以减少 token。
    (uint32_t clone_flags, uintptr_t stack, struct trapframe *tf)
    简化为：(uint32_t, uintptr_t, trapframe*)
    """
    params = sig.strip("()")
    if not params:
        return "()"

    simplified = []
    for param in params.split(","):
        param = param.strip()
        parts = param.split()
        if len(parts) >= 2:
            type_part = " ".join(parts[:-1]).replace("struct ", "").strip()
            if parts[-1].startswith("*"):
                type_part += "*"
            simplified.append(type_part)
        else:
            simplified.append(param)

    return "(" + ", ".join(simplified) + ")"


def _format_symbol(tag: dict, lang: str) -> str:
    """将 ctags tag 格式化为可读的单行符号描述。"""
    name = tag["name"]
    kind = tag.get("kind", "")
    line = tag.get("line", "?")
    signature = tag.get("signature", "")

    if kind == "function":
        short_sig = _simplify_signature(signature) if signature else "()"
        return f"fn {name}{short_sig}  (L{line})"

    if kind == "struct":
        return f"struct {name}  (L{line})"

    if kind == "macro":
        return f"#define {name}  (L{line})"

    if kind == "typedef":
        typeref = tag.get("typeref", "")
        return f"type {name} = {typeref}  (L{line})" if typeref else f"type {name}  (L{line})"

    if kind in ("enum", "trait"):
        return f"{kind} {name}  (L{line})"

    return f"{name}  (L{line})"


_SUBSYSTEM_ORDER = [
    "启动模块",
    "系统调用",
    "进程管理",
    "内存管理",
    "文件系统",
    "设备管理",
    "硬件抽象",
    "同步原语",
]


def generate_level1_map(
    tags: list[dict],
    structure: dict,
    profile: dict,
) -> str:
    """生成精简地图（纯文本），直接注入 System Prompt。"""

    primary_lang = profile["primary_lang"]

    #过滤，只保留 level1 符号
    level1_tags = [t for t in tags if classify_symbol(t, primary_lang) == "level1"]
    #构建文件路径到子系统的映射
    file_to_subsystem: dict[str, str] = {}
    for subsystem, files in structure["subsystem_locations"].items():
        for entry in files:
            file_to_subsystem[entry["file"]] = subsystem

    subsystem_groups: dict[str, list] = defaultdict(list)
    ungrouped: list[dict] = []

    for tag in level1_tags:
        sub = file_to_subsystem.get(tag["rel_path"])
        if sub:
            subsystem_groups[sub].append(tag)
        else:
            ungrouped.append(tag)

    #渲染
    lines = ["仓库结构地图（公开接口）", ""]

    for subsystem in _SUBSYSTEM_ORDER:
        group = subsystem_groups.get(subsystem)
        if not group:
            continue

        lines.append(f"### {subsystem}")

        by_file: dict[str, list] = defaultdict(list)
        for tag in group:
            by_file[tag["rel_path"]].append(tag)

        for file_path, file_tags in by_file.items():
            lines.append(f"{file_path}:")
            file_tags.sort(key=lambda t: t.get("line", 0))
            for tag in file_tags:
                lines.append(f"  {_format_symbol(tag, primary_lang)}")

        lines.append("")

    #未归类符号放到"其他"（最多 20 个）
    if ungrouped:
        lines.append("### 其他")
        for tag in ungrouped[:20]:
            lines.append(f"  {tag['rel_path']}: {_format_symbol(tag, primary_lang)}")

    return "\n".join(lines)


#子系统分类
SUBSYSTEM_FINGERPRINTS = {
    "启动模块": [
        "_start", "rust_main", "kernel_main", "start_kernel", "boot",
        "bootstrap", "early_init", "boot_stack", ".bss", "opensbi",
    ],
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
    "设备管理": [
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
            if _in_skip_dir(src_file, repo):
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
        root_path = Path(root)
        for f in root_path.rglob("*"):
            if not f.is_file():
                continue
            if _in_skip_dir(f, root_path):
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
        root_path = Path(root)
        for f in root_path.rglob("*"):
            if f.suffix not in {".rs", ".c", ".h"}:
                continue
            if _in_skip_dir(f, root_path):
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
        root_path = Path(root)
        for src_file in root_path.rglob("*"):
            if src_file.suffix not in {".rs", ".c", ".h"}:
                continue
            if _in_skip_dir(src_file, root_path):
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


#内核类型识别 
MICROKERNEL_SIGNATURES = [
    "ipc_send", "ipc_recv",
    "sys_ipc", "seL4", "L4",
    "capability", "cap_table",
    "server_process", "fs_server",
    "driver_process", "dev_server",
]

MONOLITHIC_SIGNATURES = [
    "vfs_read", "vfs_write",
    "kmalloc", "kfree",
    "task_struct", "proc_struct",
]


def detect_kernel_type(repo_path: str, structure: dict) -> dict:
    """
    通过特征签名和目录结构区分宏内核与微内核。
    返回：{"type": "monolithic"|"microkernel"|"unknown", "evidence": [...]}
    """
    micro_hits: list[str] = []
    mono_hits:  list[str] = []

    for root in structure["source_roots"]:
        root_path = Path(root)
        for f in root_path.rglob("*"):
            if f.suffix not in {".rs", ".c", ".h"}:
                continue
            if _in_skip_dir(f, root_path):
                continue
            try:
                content = f.read_text(errors="replace")
            except Exception:
                continue
            try:
                rel = str(f.relative_to(repo_path))
            except ValueError:
                rel = str(f)
            for sig in MICROKERNEL_SIGNATURES:
                if sig in content:
                    micro_hits.append(f"'{sig}' in {rel}")
            for sig in MONOLITHIC_SIGNATURES:
                if sig in content:
                    mono_hits.append(f"'{sig}' in {rel}")

    has_servers_dir = any(
        "server" in p.name.lower()
        for p in Path(repo_path).iterdir()
        if p.is_dir()
    )
    if has_servers_dir:
        micro_hits.append("存在 servers/ 类目录（微内核典型结构）")

    if len(micro_hits) >= 3:
        return {"type": "microkernel", "evidence": micro_hits[:3]}
    if len(mono_hits) >= 2 or not micro_hits:
        return {"type": "monolithic",  "evidence": mono_hits[:3]}
    return {"type": "unknown",         "evidence": (micro_hits + mono_hits)[:3]}


def detect_target_arch(repo_path: str) -> list[str]:
    """从 Cargo.toml / Makefile / 汇编文件中提取目标架构，支持双架构项目。"""
    path = Path(repo_path)
    found: set[str] = set()

    cargo_config = path / ".cargo" / "config.toml"
    if cargo_config.exists():
        content = cargo_config.read_text(errors="replace")
        if "riscv64" in content:
            found.add("riscv64")
        if "loongarch64" in content:
            found.add("loongarch64")

    makefile = path / "Makefile"
    if makefile.exists():
        content = makefile.read_text(errors="replace")
        if re.search(r'ARCH\s*[:?]?=\s*riscv', content) or \
                re.search(r'riscv64-unknown-elf', content):
            found.add("riscv64")
        if re.search(r'loongarch', content, re.IGNORECASE):
            found.add("loongarch64")

    for asm_file in path.rglob("*.S"):
        try:
            content = asm_file.read_text(errors="replace")
        except Exception:
            continue
        if "csrw" in content or "ecall" in content:
            found.add("riscv64")
        if "ertn" in content or "csrrd" in content:
            found.add("loongarch64")

    return sorted(found) if found else ["unknown"]


def detect_build_env(repo_path: str) -> str:
    """检测构建系统类型：cargo / cmake / make / unknown。"""
    path = Path(repo_path)
    if (path / "Cargo.toml").exists():
        return "cargo"
    if (path / "CMakeLists.txt").exists():
        return "cmake"
    if (path / "Makefile").exists():
        return "make"
    return "unknown"


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


def build_profile(repo_path: str, structure: dict) -> dict:
    """将三项识别任务的结果聚合为结构化 dict，供后续直接使用。"""
    lang_info   = detect_primary_language(structure["source_roots"])
    ref_os_info = detect_reference_os(repo_path, structure)
    kernel_info = detect_kernel_type(repo_path, structure)
    arch        = detect_target_arch(repo_path)

    return {
        "primary_lang":   lang_info["primary"],
        "secondary_lang": lang_info["secondary"],
        "has_assembly":   lang_info["has_assembly"],
        "loc":            lang_info["loc"],

        "reference_os":   ref_os_info["name"],
        "ref_confidence": ref_os_info["confidence"],
        "ref_evidence":   ref_os_info["evidence"],

        "kernel_type":    kernel_info["type"],
        "target_arch":    arch,

        "has_cargo":      any(
            f.name == "Cargo.toml"
            for f in Path(repo_path).rglob("Cargo.toml")
            if not any(p in {"vendor", "target", ".git"} for p in f.parts)
        ),
        "has_makefile":   (Path(repo_path) / "Makefile").exists(),
        "build_env":      detect_build_env(repo_path),
    }


