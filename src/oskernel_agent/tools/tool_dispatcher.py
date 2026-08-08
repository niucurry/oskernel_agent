"""
ToolDispatcher：将 engine + level2_index + repo_path + profile + structure 聚合在一起，
为需要多方资源的工具提供统一的调用入口。

承载的工具：
  T2  find_symbol_definition      查找符号定义（含消歧逻辑 + 截断保护）
  T3  find_symbol_references      查找引用位置（按子系统分组 + 调用密度分析）
  T4  list_implemented_syscalls   多策略扫描 syscall 实现情况
  T5  get_subsystem_call_chain    树形渲染调用链（带位置信息 + 外部函数标注）
  T6  compare_with_reference_os   代码级指纹比对（指纹库异常时自动重建）
"""

import os
import re
from collections import defaultdict
from pathlib import Path

from .reference_db import ReferenceOSDatabase, compute_similarity

# T4 模块级常量

_STANDARD_SYSCALLS: frozenset[str] = frozenset({
    # 进程管理
    "fork", "clone", "exec", "execve", "exit", "exit_group",
    "wait", "wait4", "waitpid",
    "getpid", "getppid", "gettid",
    "kill", "sigaction", "sigprocmask", "sigreturn",
    "sched_yield", "nanosleep", "clock_gettime",
    # 文件操作
    "read", "write", "open", "openat", "close",
    "lseek", "stat", "fstat", "fstatat",
    "mkdir", "mkdirat", "unlink", "unlinkat",
    "rename", "renameat",
    "getcwd", "chdir", "getdents64",
    "dup", "dup2", "dup3", "fcntl", "ioctl",
    # 内存管理
    "mmap", "munmap", "mprotect", "brk",
    # IPC
    "pipe", "pipe2",
    # 网络
    "socket", "bind", "listen", "accept", "connect",
    "sendto", "recvfrom",
    # 系统信息
    "uname", "times", "gettimeofday",
    "getuid", "getgid",
})

_SYSCALL_CATEGORIES: dict[str, frozenset[str]] = {
    "进程管理": frozenset({
        "fork", "clone", "exec", "execve", "exit", "exit_group",
        "wait", "wait4", "waitpid", "getpid", "getppid", "gettid",
        "kill", "sigaction", "sigprocmask", "sigreturn", "sched_yield",
    }),
    "文件系统": frozenset({
        "read", "write", "open", "openat", "close", "lseek",
        "stat", "fstat", "fstatat", "mkdir", "mkdirat", "unlink",
        "unlinkat", "rename", "renameat", "getcwd", "chdir",
        "getdents64", "dup", "dup2", "dup3", "fcntl", "ioctl",
    }),
    "内存管理": frozenset({"mmap", "munmap", "mprotect", "brk"}),
    "IPC":      frozenset({"pipe", "pipe2"}),
    "网络":     frozenset({"socket", "bind", "listen", "accept", "connect",
                           "sendto", "recvfrom"}),
    "系统信息": frozenset({"uname", "times", "gettimeofday", "clock_gettime",
                           "nanosleep", "getuid", "getgid"}),
}

#分发表中的 SYSCALL_XXX 常量
_DISPATCH_CONST_RE = re.compile(r"(?:SYSCALL_|SYS_|NR_)([A-Z0-9_]+)", re.IGNORECASE)

#宏定义风格
_MACRO_PATTERNS: list[re.Pattern] = [
    re.compile(r"syscall_define!\s*\(\s*(\w+)"),
    re.compile(r"define_syscall!\s*\(\s*(\w+)"),
    re.compile(r"#\[syscall\].*?fn\s+(\w+)"),
]

#SYS_xxx 常量定义
_CONST_DEF_RE = re.compile(
    r"(?:pub\s+)?(?:const|static)\s+(?:SYSCALL_|SYS_|NR_)([A-Z0-9_]+)\s*[=:]",
    re.IGNORECASE,
)


# ToolDispatcher

class ToolDispatcher:

    def __init__(
        self,
        engine,
        level2_index,
        repo_path: str,
        profile: dict,
        structure: dict | None = None,
        ref_db_dir: str = "reference_db",
    ):
        self.engine = engine
        self.level2_index = level2_index        # 可为 None（降级模式）
        self.repo_path = repo_path
        self.profile = profile or {}
        self.structure = structure or {}
        self._src_ext = ".rs" if self.profile.get("primary_lang") == "rust" else ".c"
        self.ref_database = ReferenceOSDatabase(ref_db_dir)

        # 构建 file_path → subsystem 映射（T3 分组使用）
        self._file_to_subsystem: dict[str, str] = {}
        if level2_index and hasattr(level2_index, "_by_subsystem"):
            for sub, entries in level2_index._by_subsystem.items():
                for e in entries:
                    self._file_to_subsystem[e["file"]] = sub
        elif structure:
            for sub, files in structure.get("subsystem_locations", {}).items():
                for f in files:
                    self._file_to_subsystem[f["file"]] = sub

    # T2: find_symbol_definition

    def find_symbol_definition(
        self, symbol_name: str, context_file: str | None = None
    ) -> str:
        #level2_index 精确 / 模糊查找候选
        candidates: list[dict] = []
        if self.level2_index:
            candidates = self.level2_index.lookup_symbol(symbol_name)
            if not candidates:
                candidates = self.level2_index.search_symbols(symbol_name)

        cached_defn: dict | None = None
        if not candidates:
            cached_defn = self.engine.go_to_definition(symbol_name)
            if not cached_defn:
                return (
                    f"[未找到] 符号 '{symbol_name}' 不在索引中。\n"
                    f"可能原因：\n"
                    f"  1. 该符号通过宏生成（宏展开后的符号 tree-sitter 无法索引）\n"
                    f"  2. 拼写不准确\n"
                    f"建议：使用 read_file 直接查看你怀疑包含该符号的文件。"
                )
            candidates = [{
                "file":  cached_defn["file"],
                "line":  cached_defn["start_line"],
                "kind":  "function",
                "level": "level1",
            }]

        #消歧
        best = (
            self._disambiguate(candidates, symbol_name, context_file)
            if len(candidates) > 1
            else dict(candidates[0])
        )
        ambiguous = best.pop("_ambiguous", False)
        other_files = best.pop("_other_candidates", [])

        #引擎跳转拿完整源码
        # 优先：用索引位置直接读文件（无 LSP 等待，适用于 99% 的情况）
        defn = cached_defn
        if defn is None:
            file_rel = best.get("file", "")
            start_ln = best.get("line", 0)
            if file_rel and start_ln:
                full_path = str(Path(self.repo_path) / file_rel)
                if hasattr(self.engine, "_extract_code_block"):
                    raw_body = self.engine._extract_code_block(full_path, start_ln)
                else:
                    raw_body = self._read_lines_around(file_rel, start_ln, context_lines=80)
                if raw_body:
                    defn = {
                        "file":       file_rel,
                        "start_line": start_ln,
                        "end_line":   start_ln + raw_body.count("\n"),
                        "body":       raw_body,
                    }
            # 降级：LSP 跳转（精度高但可能阻塞，仅在直接读取失败时使用）
            if defn is None:
                defn = self.engine.go_to_definition(symbol_name)

        if defn:
            body       = defn["body"]
            file_path  = defn["file"]
            start_line = defn["start_line"]
            end_line   = defn["end_line"]
        else:
            file_path  = best["file"]
            start_line = best["line"]
            body       = self._read_lines_around(file_path, start_line, context_lines=50)
            end_line   = start_line + body.count("\n")

        #截断保护
        _MAX_BODY = 120
        lines = body.splitlines()
        if len(lines) > _MAX_BODY:
            body = (
                "\n".join(lines[:40])
                + f"\n\n  // ... 省略 {len(lines) - 60} 行 "
                + f"（使用 read_file(\"{file_path}\", "
                + f"{start_line + 40}, {end_line - 20}) 查看）...\n\n"
                + "\n".join(lines[-20:])
            )

        #组装返回文本
        engine_info = self.engine.get_engine_info()
        notes: list[str] = []
        if engine_info.get("precision", "high") != "high":
            notes.append(
                f"[精度提示] 当前使用 {engine_info['engine']}，"
                f"结果基于名称匹配，可能存在同名函数混淆。"
            )
        if ambiguous and other_files:
            notes.append(
                f"[消歧提示] 存在同名符号，已选择 {file_path}。"
                f"其他候选：{', '.join(other_files)}"
            )
        note_block = ("\n" + "\n".join(notes)) if notes else ""
        lang = self._get_lang_tag()
        return (
            f"## {symbol_name} 的定义\n"
            f"文件：{file_path}  行：{start_line}-{end_line}\n"
            f"类型：{best.get('kind', '未知')}"
            f"{note_block}\n\n"
            f"```{lang}\n{body}\n```"
        )

    # T3: find_symbol_references

    def find_symbol_references(self, symbol_name: str) -> str:
        refs = self.engine.find_references(symbol_name)

        if not refs:
            return (
                f"[未找到] '{symbol_name}' 没有被其他函数引用。\n"
                f"可能原因：\n"
                f"  1. 它是顶层入口函数（如 main / _start），只被硬件或引导代码调用\n"
                f"  2. 它通过函数指针间接调用（当前引擎无法追踪函数指针）\n"
                f"  3. 它通过宏展开调用（宏内部的调用无法被静态分析发现）\n"
                f"建议：使用 read_file 手动检查你怀疑存在调用的文件。"
            )

        # 按子系统分组
        by_subsystem: dict[str, list[dict]] = defaultdict(list)
        for ref in refs:
            sub = self._classify_file_to_subsystem(ref.get("file", ""))
            by_subsystem[sub].append(ref)

        engine_info = self.engine.get_engine_info()
        precision_note = ""
        if engine_info.get("precision", "high") != "high":
            precision_note = (
                "\n[精度提示] 当前引擎基于函数名文本匹配查找引用，"
                "可能遗漏通过函数指针或宏的间接调用，"
                "也可能误报同名但不同作用域的符号。"
            )

        lines = [
            f"## '{symbol_name}' 的引用位置（共 {len(refs)} 处）{precision_note}\n"
        ]
        for subsystem, sub_refs in by_subsystem.items():
            lines.append(f"### {subsystem}")
            for ref in sub_refs:
                tag = " [名称匹配]" if ref.get("_precision") == "name_match_only" else ""
                lines.append(
                    f"  调用方：{ref.get('caller', '?')}()"
                    f"  在 {ref.get('file', '?')}:{ref.get('line', '?')}{tag}"
                )
            lines.append("")

        lines.append(f"**调用密度**：被 {len(refs)} 个函数调用")
        if len(refs) > 10:
            lines.append("  注：这是一个高频使用的核心函数，修改它会影响大量模块")
        elif len(refs) == 1:
            lines.append("  注：只有单一调用者，可能是特定流程的专用函数")

        return "\n".join(lines)

    # T4: list_implemented_syscalls

    def list_implemented_syscalls(self) -> str:
        """多策略扫描已实现的 syscall，生成覆盖率分类报告。"""
        found: dict[str, dict] = {}   # canonical_name → {source, file, line, strategy}

        self._scan_function_names(found)
        self._scan_dispatch_table(found)
        self._scan_macro_definitions(found)
        self._scan_constants(found)

        return self._format_syscall_report(found)

    def _scan_function_names(self, found: dict) -> None:
        """直接用 sys_xxx / syscall_xxx / do_xxx 命名的函数。"""
        func_syms = self._get_func_symbols()
        for func_name, entry in func_syms.items():
            canonical: str | None = None

            if func_name.startswith("sys_"):
                canonical = func_name[4:]
            elif func_name.startswith("syscall_"):
                canonical = func_name[8:]
            elif func_name.startswith("do_"):
                # do_xxx 只在疑似被 syscall 分发器调用时才计入
                if self._is_called_by_syscall_handler(func_name, entry.get("file", "")):
                    canonical = func_name[3:]

            if canonical:
                key = canonical.lower()
                if key not in found:
                    found[key] = {
                        "source":   func_name,
                        "file":     entry.get("file", ""),
                        "line":     entry.get("start_line", 0),
                        "evidence": f"函数定义 {func_name}()",
                        "strategy": "function_name",
                    }

    def _scan_dispatch_table(self, found: dict) -> None:
        """在 syscall 分发函数所在文件里搜索 SYSCALL_XXX / SYS_XXX 常量引用。"""
        func_syms = self._get_func_symbols()
        dispatch_files: set[str] = set()
        for name, entry in func_syms.items():
            if any(kw in name.lower() for kw in ("syscall", "sys_call", "trap_handler")):
                dispatch_files.add(entry.get("file", ""))

        for rel_path in dispatch_files:
            if not rel_path:
                continue
            full = Path(self.repo_path) / rel_path
            try:
                content = full.read_text(errors="replace")
            except Exception:
                continue
            for m in _DISPATCH_CONST_RE.finditer(content):
                key = m.group(1).lower()
                if key not in found:
                    lineno = content[: m.start()].count("\n") + 1
                    found[key] = {
                        "source":   m.group(0),
                        "file":     rel_path,
                        "line":     lineno,
                        "evidence": f"分发表常量 {m.group(0)}",
                        "strategy": "dispatch_table",
                    }

    def _scan_macro_definitions(self, found: dict) -> None:
        """搜索宏定义风格的 syscall（Rust 宏 / 属性宏）。"""
        repo = Path(self.repo_path)
        for src in repo.rglob(f"*{self._src_ext}"):
            rel = str(src.relative_to(repo))
            if any(p in Path(rel).parts for p in ("vendor", "third_party", "target")):
                continue
            try:
                content = src.read_text(errors="replace")
            except Exception:
                continue
            for pattern in _MACRO_PATTERNS:
                for m in pattern.finditer(content):
                    name = m.group(1).lower()
                    if name.startswith("sys_"):
                        name = name[4:]
                    if name not in found:
                        lineno = content[: m.start()].count("\n") + 1
                        found[name] = {
                            "source":   m.group(0)[:40],
                            "file":     rel,
                            "line":     lineno,
                            "evidence": f"宏定义 {m.group(0)[:40]}",
                            "strategy": "macro",
                        }

    def _scan_constants(self, found: dict) -> None:
        """搜索 SYS_xxx 常量定义（仅有声明，无函数实现，标记为 constant_only）。"""
        repo = Path(self.repo_path)
        for src in repo.rglob(f"*{self._src_ext}"):
            rel = str(src.relative_to(repo))
            if any(p in Path(rel).parts for p in ("vendor", "third_party", "target")):
                continue
            try:
                content = src.read_text(errors="replace")
            except Exception:
                continue
            for m in _CONST_DEF_RE.finditer(content):
                key = m.group(1).lower()
                if key not in found:
                    lineno = content[: m.start()].count("\n") + 1
                    found[key] = {
                        "source":   m.group(0)[:40],
                        "file":     rel,
                        "line":     lineno,
                        "evidence": f"常量定义 {m.group(0)[:40]}（可能只有声明无实现）",
                        "strategy": "constant_only",
                    }

    def _format_syscall_report(self, found: dict) -> str:
        implemented = {k for k, v in found.items() if v["strategy"] != "constant_only"}
        constant_only = {k for k, v in found.items()
                         if v["strategy"] == "constant_only"} - implemented

        covered = implemented & _STANDARD_SYSCALLS
        missing = _STANDARD_SYSCALLS - implemented - constant_only
        extra   = implemented - _STANDARD_SYSCALLS

        pct = len(covered) / len(_STANDARD_SYSCALLS) * 100
        lines = [
            "## Syscall 实现情况\n",
            f"标准覆盖率：**{len(covered)} / {len(_STANDARD_SYSCALLS)}**"
            f"（{pct:.1f}%）\n",
            "### 已实现（标准 syscall）",
        ]
        for name in sorted(covered):
            info = found[name]
            lines.append(
                f"  [OK] {name:<18}  来源：{info['evidence']}"
                f"  ({info['file']}:{info['line']})"
            )

        if missing:
            lines.append(f"\n### 未实现（共 {len(missing)} 个）")
            for cat, cat_set in _SYSCALL_CATEGORIES.items():
                cat_missing = missing & cat_set
                if cat_missing:
                    lines.append(f"  [{cat}]")
                    for name in sorted(cat_missing):
                        lines.append(f"    [缺失] {name}")
            # 不在任何分类中的剩余项
            uncategorized = missing - set().union(*_SYSCALL_CATEGORIES.values())
            if uncategorized:
                lines.append("  [其他]")
                for name in sorted(uncategorized):
                    lines.append(f"    [缺失] {name}")

        if extra:
            lines.append(f"\n### 超出标准集的实现（{len(extra)} 个，可能是创新点）")
            for name in sorted(extra):
                info = found[name]
                lines.append(
                    f"  [独有] {name:<18}  来源：{info['evidence']}"
                    f"  ({info['file']}:{info['line']})"
                )

        if constant_only:
            lines.append(f"\n### 仅有常量声明（可能未真正实现，共 {len(constant_only)} 个）")
            for name in sorted(constant_only):
                info = found[name]
                lines.append(f"  [仅声明] {name:<18}  来源：{info['evidence']}")

        return "\n".join(lines)

    # T5a: find_entry_symbol — 轻量"符号存在性检查"

    def find_entry_symbol(self, name: str) -> str:
        """只确认符号是否存在并返回位置，不展开调用链。

        与 find_symbol_definition 的区别：后者读取并返回完整源码（可能数十行），
        本工具仅返回 file:line + kind（<100 token），让 LLM 决定是否要再调用
        expand_callees 展开调用树。配合 expand_callees 使用，可避免对不存在的
        符号做昂贵的调用树展开。
        """
        func_syms = self._get_func_symbols()
        info = func_syms.get(name)
        if info:
            return (
                f"## 符号 {name} 存在\n"
                f"位置：{info['file']}:{info['start_line']}\n"
                f"类型：function\n\n"
                f"下一步：用 expand_callees('{name}') 展开调用树，"
                f"或 find_symbol_definition('{name}') 查看完整源码。"
            )

        # 降级：level2_index 模糊查找
        if self.level2_index:
            candidates = self.level2_index.lookup_symbol(name)
            if candidates:
                c = candidates[0]
                return (
                    f"## 符号 {name} 存在\n"
                    f"位置：{c['file']}:{c['line']}\n"
                    f"类型：{c.get('kind', '未知')}\n\n"
                    f"下一步：用 expand_callees('{name}') 展开调用树，"
                    f"或 find_symbol_definition('{name}') 查看完整源码。"
                )
            similar = self.level2_index.search_symbols(name)
            if similar:
                hints = ", ".join(f"`{s['name']}`" for s in similar[:5])
                return (
                    f"[未找到] '{name}' 不在索引中。"
                    f"相似符号：{hints}"
                )

        return (
            f"[未找到] '{name}' 不在索引中。"
            f"可能是宏生成、外部依赖或拼写有误。"
        )

    # T5b: expand_callees — 已知存在前提下展开调用树

    def expand_callees(self, name: str, max_depth: int = 3) -> str:
        """假定符号存在，专注调用树展开（最多 5 层）。"""
        max_depth = min(max_depth, 5)
        chain = self.engine.get_call_chain(name, max_depth)

        entry_val = chain.get(name) if chain else None
        if not chain or entry_val is None or (
            isinstance(entry_val, dict) and entry_val.get("__not_found__")
        ):
            return (
                f"[未找到] 入口函数 '{name}' 不在索引中。"
                f"建议先用 find_entry_symbol('{name}') 确认它是否存在。"
            )

        return self._render_call_tree(name, chain, max_depth)

    # T5: get_subsystem_call_chain（保留为外壳，内部转调 find_entry_symbol + expand_callees）

    def get_subsystem_call_chain(
        self, entry_function: str, max_depth: int = 3
    ) -> str:
        """旧入口：判存 + 展开打包。新代码请用 find_entry_symbol + expand_callees。"""
        max_depth = min(max_depth, 5)
        chain = self.engine.get_call_chain(entry_function, max_depth)

        if not chain:
            return (
                f"[未找到] 入口函数 '{entry_function}' 不在索引中。\n"
                f"建议：先调用 find_symbol_definition('{entry_function}') 确认它是否存在。"
            )
        entry_val = chain.get(entry_function)
        if entry_val is None or (isinstance(entry_val, dict) and entry_val.get("__not_found__")):
            return (
                f"[未找到] 入口函数 '{entry_function}' 不在索引中。\n"
                f"建议：先调用 find_symbol_definition('{entry_function}') 确认它是否存在。"
            )
        return self._render_call_tree(entry_function, chain, max_depth)

    def _render_call_tree(self, entry_function: str, chain: dict, max_depth: int) -> str:
        """共享的调用树渲染逻辑（同时被 get_subsystem_call_chain 和 expand_callees 使用）。"""
        func_syms = self._get_func_symbols()
        tree_lines: list[str] = []
        total_nodes = [0]

        def render(subtree: dict, prefix: str = "") -> None:
            items = [(k, v) for k, v in subtree.items() if not k.startswith("_")]
            for i, (func_name, children) in enumerate(items):
                if total_nodes[0] > 60:
                    tree_lines.append(f"{prefix}... [节点过多，已截断]")
                    return
                is_last = (i == len(items) - 1)
                sym = func_syms.get(func_name, {})
                if sym:
                    loc = f"  ({sym['file']}:{sym['start_line']})"
                else:
                    loc = ""

                not_found = isinstance(children, dict) and children.get("__not_found__")
                if not_found:
                    loc += "  [外部/未索引]"

                tree_lines.append(f"{prefix}- {func_name}(){loc}")
                total_nodes[0] += 1

                if children and not not_found:
                    ext = prefix + "  "
                    render(children, ext)

        render(chain)

        engine_info = self.engine.get_engine_info()
        precision_note = ""
        if engine_info.get("precision", "high") != "high":
            precision_note = (
                "\n[精度提示] 调用关系基于函数名文本匹配，"
                "可能遗漏通过函数指针或宏的间接调用。\n"
            )

        result = (
            f"## {entry_function}() 调用链（深度={max_depth}）\n"
            f"共展开 {total_nodes[0]} 个节点{precision_note}\n\n"
            + "\n".join(tree_lines)
        )

        # 列出直接子节点中未索引的函数
        not_found_funcs = [
            k for k, v in chain.get(entry_function, {}).items()
            if isinstance(v, dict) and v.get("__not_found__")
        ]
        if not_found_funcs:
            result += (
                f"\n\n**未索引的直接调用**：{', '.join(not_found_funcs)}\n"
                f"这些可能是标准库函数、外部依赖或宏生成的函数。"
            )
        return result

    # T6: compare_with_reference_os

    def compare_with_reference_os(self, reference_name: str) -> str:
        # 正式原创性判断禁止退化成函数名集合重叠。指纹库缺失、损坏或内容不完整时，
        # load_or_rebuild 会先从固定参考源码版本自动重建；只有重建成功后才继续比较。
        ref_db = self.ref_database.load_or_rebuild(reference_name)
        return self._compare_with_db(reference_name, ref_db)

    def _compare_with_db(self, reference_name: str, ref_db: dict | None = None) -> str:
        ref_db = ref_db or self.ref_database.load_or_rebuild(reference_name)

        # 获取当前仓库函数
        current_funcs: dict[str, dict] = {}
        if hasattr(self.engine, "_func_index"):
            for key, entry in self.engine._func_index.items():
                name = entry.get("name", key)
                if name not in current_funcs:
                    current_funcs[name] = entry
        elif self.level2_index:
            ref_names_set = set(ref_db)
            # 以参考库符号为驱动：仅对交集函数调用 go_to_definition（获取函数体做代码级比对）。
            # 旧实现遍历仓库全量符号（N+1 SQL + N×LSP），这里降为至多 len(ref_db) 次。
            for sym_name in ref_names_set:
                entries = self.level2_index._by_name.get(sym_name)
                if not entries:
                    continue
                if not any(e.get("kind") == "function" for e in entries):
                    continue
                defn = self.engine.go_to_definition(sym_name)
                if defn:
                    current_funcs[sym_name] = {
                        "body": defn["body"],
                        "file": defn["file"],
                        "calls": [],
                    }
            # new_funcs 需要仓库全量函数名；用单次 SQL 批量取，不再逐符号查询
            try:
                _sym_db = getattr(self.level2_index, "_db", None)
                if _sym_db is not None:
                    for row in _sym_db.conn.execute(
                        "SELECT DISTINCT name, file FROM symbols WHERE kind='function'"
                    ):
                        fn_name, fn_file = row[0], row[1]
                        if fn_name not in current_funcs:
                            current_funcs[fn_name] = {
                                "body": "",
                                "file": fn_file or "?",
                                "calls": [],
                            }
            except Exception:
                pass

        identical: list[dict] = []
        modified:  list[dict] = []
        reimpl:    list[dict] = []
        new_funcs: list[dict] = []
        _TRIVIAL = {"main", "memset", "memcpy", "memmove", "strlen", "panic",
                    "abort", "assert", "new", "drop"}

        current_names = set(current_funcs)
        ref_names = set(ref_db)

        for name in current_names & ref_names:
            cur_body = current_funcs[name].get("body", "")
            ref_norm = ref_db[name].get("body_normalized", "")
            sim = compute_similarity(cur_body, ref_norm)
            entry = {
                "name":         name,
                "similarity":   sim,
                "current_file": current_funcs[name].get("file", "?"),
                "current_lines": cur_body.count("\n") + 1,
                "ref_lines":    ref_db[name].get("line_count", 0),
            }
            if sim > 0.90:
                identical.append(entry)
            elif sim > 0.50:
                modified.append(entry)
            else:
                reimpl.append(entry)

        for name in current_names - ref_names:
            if name in _TRIVIAL:
                continue
            new_funcs.append({
                "name": name,
                "file": current_funcs[name].get("file", "?"),
            })

        ref_only = sorted(ref_names - current_names)

        total_shared = len(identical) + len(modified) + len(reimpl)
        overall_sim = (
            sum(e["similarity"] for e in identical + modified + reimpl) / total_shared
            if total_shared else 0.0
        )

        lines = [
            f"## 与 {reference_name} 的相似度分析（代码指纹库）\n",
            f"综合相似度：**{overall_sim * 100:.1f}%**",
            f"共有函数 {total_shared} 个，当前仓库独有 {len(new_funcs)} 个\n",
        ]

        if identical:
            lines.append(
                f"### 高度相似（>90%）——疑似直接继承（{len(identical)} 个）"
            )
            for e in sorted(identical, key=lambda x: -x["similarity"]):
                lines.append(
                    f"  [高相似]  {e['name']:<20}  "
                    f"相似度={e['similarity']:.0%}  "
                    f"({e['current_file']}，{e['current_lines']}行 vs 参考{e['ref_lines']}行)"
                )
            lines.append("")

        if modified:
            lines.append(
                f"### 基于参考修改（50-90%）——（{len(modified)} 个）"
            )
            for e in sorted(modified, key=lambda x: -x["similarity"]):
                lines.append(
                    f"  [已修改]  {e['name']:<20}  "
                    f"相似度={e['similarity']:.0%}  ({e['current_file']})"
                )
            lines.append("")

        if reimpl:
            lines.append(
                f"### 同名但差异显著（<50%）——可能独立重实现（{len(reimpl)} 个）"
            )
            for e in sorted(reimpl, key=lambda x: -x["similarity"]):
                lines.append(
                    f"  [重实现]  {e['name']:<20}  "
                    f"相似度={e['similarity']:.0%}  ({e['current_file']})"
                )
            lines.append("")

        if new_funcs:
            lines.append(
                f"### 当前仓库独有函数（创新点候选，{len(new_funcs)} 个）"
            )
            for e in sorted(new_funcs, key=lambda x: x["name"])[:40]:
                lines.append(f"  [新增]  {e['name']:<20}  ({e['file']})")
            if len(new_funcs) > 40:
                lines.append(f"  ... 等共 {len(new_funcs)} 个（仅展示前 40 个）")
            lines.append(
                "\n  注意：以上为初筛结果。请对标注新增的函数调用 "
                "find_symbol_definition 查看实际内容，确认是否有实质性创新再写入报告。"
            )
            lines.append("")

        if ref_only:
            lines.append(
                f"### 参考 OS 中有但当前缺失的函数（{len(ref_only)} 个，仅供参考）"
            )
            for name in ref_only[:20]:
                lines.append(f"  [缺失]  {name}")
            if len(ref_only) > 20:
                lines.append(f"  ... 还有 {len(ref_only) - 20} 个")
            lines.append("")

        lines.append(
            "**免责声明**：相似度基于归一化代码计算，存在局限：同名不代表同逻辑；"
            "重命名后的函数无法匹配；宏生成代码无法分析。最终判断需结合人工审查。"
        )
        return "\n".join(lines)

    # 内部辅助

    def _get_func_symbols(self) -> dict[str, dict]:
        """返回 {canonical_name: {"file": ..., "start_line": ...}} 字典。

        优先使用 TreeSitter engine 的 _func_index（已解析 AST）；
        降级到 level2_index（ctags 结果，跨引擎通用）。

        注意：_func_index 的 key 可能是 "name@file" 去重形式，
        此处统一使用 info["name"] 作为规范键，只保留第一次出现。
        """
        if hasattr(self.engine, "_func_index"):
            result: dict[str, dict] = {}
            for key, info in self.engine._func_index.items():
                canonical = info.get("name", key)
                if canonical not in result:          # 同名函数取第一个
                    result[canonical] = {
                        "file":       info.get("file", ""),
                        "start_line": info.get("start_line", 0),
                    }
            return result
        if self.level2_index:
            result = {}
            for name, entries in self.level2_index._by_name.items():
                for e in entries:
                    if e.get("kind") in ("function", "f", "fn", "m"):
                        result[name] = {
                            "file":       e["file"],
                            "start_line": e["line"],
                        }
                        break
            return result
        return {}

    def _is_called_by_syscall_handler(self, func_name: str, file_path: str) -> bool:
        """判断 do_xxx 函数是否位于 syscall 分发器的调用链中。

        启发式：与该函数同文件的其他符号中，是否存在 syscall / trap_handler 等分发函数。
        """
        if not file_path or not self.level2_index:
            return False
        file_syms = self.level2_index._by_file.get(file_path, [])
        for sym in file_syms:
            if any(kw in sym["name"].lower()
                   for kw in ("syscall", "sys_call", "trap_handler", "dispatch")):
                return True
        return False

    def _classify_file_to_subsystem(self, file_path: str) -> str:
        """将文件路径映射到子系统名称，优先查索引，降级走路径启发。"""
        if not file_path:
            return "其他"
        sub = self._file_to_subsystem.get(file_path)
        if sub:
            return sub
        # 路径分量启发式
        _PATH_HINTS = {
            "task": "进程管理", "process": "进程管理", "proc": "进程管理",
            "sched": "进程管理", "signal": "进程管理",
            "memory": "内存管理", "mm": "内存管理", "mem": "内存管理", "page": "内存管理",
            "fs": "文件系统", "filesystem": "文件系统", "vfs": "文件系统",
            "inode": "文件系统", "fat": "文件系统",
            "trap": "陷入/中断", "interrupt": "陷入/中断", "irq": "陷入/中断",
            "net": "网络", "network": "网络", "socket": "网络",
            "syscall": "Syscall", "sys": "Syscall",
        }
        for part in Path(file_path).parts:
            hint = _PATH_HINTS.get(part.lower())
            if hint:
                return hint
        return "其他"

    def _disambiguate(
        self,
        candidates: list[dict],
        symbol_name: str,
        context_file: str | None,
    ) -> dict:
        """多候选消歧，返回最优候选的副本。

        优先级（分值越高越优先）：
          context_file 同文件 +10 / 同目录 +5
          _kernel_crate_dirs 内 +3（可选，子类设置）
          level1 符号 +2
          kind == function +1
        """
        scored: list[tuple[int, dict]] = []
        for c in candidates:
            score = 0
            if context_file:
                if c["file"] == context_file:
                    score += 10
                elif os.path.dirname(c["file"]) == os.path.dirname(context_file):
                    score += 5
            if getattr(self, "_kernel_crate_dirs", None):
                if any(c["file"].startswith(d) for d in self._kernel_crate_dirs):
                    score += 3
            if c.get("level") == "level1":
                score += 2
            if c.get("kind") == "function":
                score += 1
            scored.append((score, c))

        scored.sort(key=lambda x: -x[0])
        best = dict(scored[0][1])
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            best["_ambiguous"] = True
            best["_other_candidates"] = [s[1]["file"] for s in scored[1:3]]
        return best

    def _read_lines_around(
        self, file_path: str, start_line: int, context_lines: int = 50
    ) -> str:
        """引擎跳转失败时的降级方案：直接读文件 start_line 附近内容。"""
        full = Path(self.repo_path) / file_path
        try:
            all_lines = full.read_text(errors="replace").splitlines()
        except Exception as exc:
            return f"[读取失败：{exc}]"
        s = max(0, start_line - 1)
        e = min(len(all_lines), start_line - 1 + context_lines)
        return "\n".join(
            f"{s + i + 1:5d} | {line}" for i, line in enumerate(all_lines[s:e])
        )

    def _get_lang_tag(self) -> str:
        return {"rust": "rust", "c": "c"}.get(
            self.profile.get("primary_lang", ""), ""
        )

    # T8: analyze_subtree — 子树范围内符号 + 调用图（按需）

    def analyze_subtree(self, subtree_path: str = "",
                        max_call_graph_funcs: int = 30) -> str:
        """
        枚举子树内（subtree_path 及其所有子目录）的所有符号，
        构建子树内调用图，标注跨子树的外部依赖。

        - 复用全局 SymbolDB（无需重新解析）
        - 调用图边内部 = 子树文件之间；外部 = 调用了子树外的符号
        - 跨层级查询应通过 find_symbol_definition / read_file 等全局工具
        """
        db = getattr(self.level2_index, "db", None) or self.level2_index
        if db is None:
            return "[错误] 符号索引未就绪，请先调用 initialize_analysis。"

        all_files = db.all_files()
        if subtree_path:
            prefix = subtree_path.rstrip("/") + "/"
            subtree_files = [
                f for f in all_files
                if f == subtree_path or f.startswith(prefix)
            ]
        else:
            subtree_files = list(all_files)

        if not subtree_files:
            return (
                f"[未找到] 子树 '{subtree_path or '<root>'}' 下无可索引文件。"
                f"全仓库共 {len(all_files)} 个索引文件。"
            )

        # 收集子树内所有符号，按 kind 分组、按 name 索引
        symbols_by_kind: dict[str, list[dict]] = {}
        symbols_by_name: dict[str, dict] = {}
        file_symbol_count: dict[str, int] = {}
        for f in subtree_files:
            syms = db.list_file_symbols(f)
            file_symbol_count[f] = len(syms)
            for s in syms:
                kind = s.get("kind") or "other"
                symbols_by_kind.setdefault(kind, []).append(s)
                # 第一次出现的同名符号占位（用于内部/外部判断）
                symbols_by_name.setdefault(s["name"], s)

        total_syms = sum(len(v) for v in symbols_by_kind.values())

        # 构建子树内调用图（限制函数数，避免 LSP 阻塞）
        internal_calls: list[tuple[str, str, str]] = []
        external_calls: dict[str, set[str]] = {}
        graph_note = ""
        engine_ready = (not hasattr(self.engine, "is_ready")
                         or self.engine.is_ready())
        if not engine_ready:
            graph_note = "[注意] 语义引擎尚未就绪，调用图省略。"
        else:
            funcs = symbols_by_kind.get("function", [])
            sample = funcs[:max_call_graph_funcs]
            for fn in sample:
                try:
                    chain = self.engine.get_call_chain(fn["name"], 1)
                except Exception:
                    continue
                callees = (chain or {}).get(fn["name"])
                if not isinstance(callees, dict):
                    continue
                for callee_name in callees.keys():
                    if callee_name.startswith("_") or callee_name == fn["name"]:
                        continue
                    info = symbols_by_name.get(callee_name)
                    if info:
                        internal_calls.append((
                            fn["name"], callee_name,
                            f"{info['file']}:{info['line']}",
                        ))
                    else:
                        external_calls.setdefault(fn["name"], set()).add(callee_name)
            if len(funcs) > max_call_graph_funcs:
                graph_note = (
                    f"[注意] 子树函数数 {len(funcs)} 个，仅对前 "
                    f"{max_call_graph_funcs} 个构建调用图（按符号顺序）。"
                    f"如需更多，对感兴趣的函数单独调用 expand_callees(name)。"
                )

        # 渲染
        lines: list[str] = []
        title = subtree_path or "<repo_root>"
        lines.append(f"## 子树分析：{title}")
        lines.append(
            f"文件数：{len(subtree_files)} ／ 符号总数：{total_syms} ／ "
            f"函数：{len(symbols_by_kind.get('function', []))}"
        )
        if graph_note:
            lines.append(graph_note)
        lines.append("")

        # 文件列表
        lines.append("### 子树文件列表")
        for f in subtree_files[:50]:
            n = file_symbol_count.get(f, 0)
            lines.append(f"- {f}  ({n} 个符号)")
        if len(subtree_files) > 50:
            lines.append(f"  ... 还有 {len(subtree_files) - 50} 个文件")

        # 符号清单（按 kind 分组）
        lines.append("\n### 符号清单（按类型分组）")
        kind_order = ["function", "struct", "typedef", "enum", "macro",
                       "variable", "trait", "impl", "other"]
        sorted_kinds = sorted(
            symbols_by_kind.items(),
            key=lambda kv: (kind_order.index(kv[0]) if kv[0] in kind_order
                            else len(kind_order)),
        )
        for kind, items in sorted_kinds:
            lines.append(f"\n**{kind}**（{len(items)} 个）：")
            for s in items[:30]:
                sig = s.get("signature") or ""
                sig_short = f"  {sig}" if sig and len(sig) < 80 else ""
                lines.append(f"- `{s['name']}`  ({s['file']}:{s['line']}){sig_short}")
            if len(items) > 30:
                lines.append(f"  ... 还有 {len(items) - 30} 个 {kind}")

        # 内部调用图
        if internal_calls:
            lines.append("\n### 子树内调用关系（caller → callee）")
            by_caller: dict[str, list[tuple[str, str]]] = {}
            for caller, callee, loc in internal_calls:
                by_caller.setdefault(caller, []).append((callee, loc))
            for caller, callees in list(by_caller.items())[:25]:
                lines.append(f"- **{caller}** →")
                for callee, loc in callees[:8]:
                    lines.append(f"    - {callee}  ({loc})")
                if len(callees) > 8:
                    lines.append(f"    - ... 还有 {len(callees) - 8} 个内部调用")
        elif engine_ready:
            lines.append("\n### 子树内调用关系")
            lines.append("（未发现子树内的函数间调用）")

        # 外部依赖（跨子树调用）
        if external_calls:
            lines.append("\n### 跨子树外部依赖（caller → 外部符号）")
            for caller, callees in list(external_calls.items())[:20]:
                shown = list(callees)[:10]
                extra = "" if len(callees) <= 10 else f"  ... 还有 {len(callees) - 10} 个"
                lines.append(f"- **{caller}** → {', '.join(shown)}{extra}")
            lines.append(
                "\n[提示] 如需查看外部符号定义，调用 "
                "find_symbol_definition(name)；"
                "如需读取外部文件内容，调用 read_file(path)。"
            )

        return "\n".join(lines)


    # T7: get_index_status — 暴露 SQLite/FTS 索引健康度

    def get_index_status(self) -> str:
        """返回当前索引的统计信息（符号数、FTS 行数、磁盘占用、上次索引时间）。"""
        db = getattr(self.level2_index, "db", None)
        if db is None:
            return (
                "[未启用持久化索引] 当前 Level2Index 未挂接 SymbolDB。"
                "可能是 build_repo_map 未被正常调用。"
            )
        s = db.stats()
        db_kb = (s.get("db_size", 0) or 0) // 1024
        lines = [
            "## 索引状态",
            f"符号总数：{s['symbols']}",
            f"FTS 行数：{s['fts_rows']}",
            f"已索引文件：{s['files']}",
            f"数据库大小：{db_kb} KB（{s['db_path']}）",
            f"主语言：{s['primary_lang'] or '未知'}",
            f"上次索引：{s['indexed_at'] or '未知'}",
        ]
        engine_info = self.engine.get_engine_info()
        lines.append(f"语义引擎：{engine_info.get('engine', '?')}"
                     f"（精度 {engine_info.get('precision', '?')}）")
        return "\n".join(lines)
