"""
三个工具的具体实现：
  - read_file                读取文件内容（带行号，自动截断）
  - list_implemented_syscalls  扫描 sys_* 符号，与标准 Linux syscall 集合比对
  - compare_with_reference_os  从当前仓库提取函数名，与参考 OS 的代表性函数集对比
"""

import os
import re
from pathlib import Path

# 公共常量

_SKIP_DIRS = frozenset({"vendor", "third_party", "target", ".venv", "__pycache__"})

_BINARY_EXTS = frozenset({
    ".bin", ".img", ".o", ".elf", ".a", ".so", ".wasm",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".pdf",
})

# T1: read_file

_HEAD_LINES = 80    # 无范围截断时保留头部行数
_TAIL_LINES = 40    # 无范围截断时保留尾部行数
_MAX_RANGE_LINES = 200  # 指定范围时的单次最大行数

def read_file(
    repo_path: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """读取文件，返回带行号文本。

    - 二进制后缀：直接拒绝
    - 超大文件（>200KB）且未指定范围：提示先用 find_symbol_definition 定位行号
    - 指定了范围：精确返回，超过 _MAX_RANGE_LINES 行时截断并提示
    - 未指定范围且文件较短：全部返回
    - 未指定范围且文件较长：HEAD + 省略说明 + TAIL
    """
    full_path = Path(repo_path) / path.lstrip("/")

    if not full_path.exists():
        return (
            f"[错误] 文件不存在：{path}\n"
            f"请检查路径是否正确，可以先查看仓库结构地图确认。"
        )

    # 二进制文件保护
    if full_path.suffix.lower() in _BINARY_EXTS:
        size = full_path.stat().st_size
        return f"[跳过] {path} 是二进制文件（{size} 字节），无法以文本形式读取。"

    # 超大文件且未指定范围
    file_size = full_path.stat().st_size
    if file_size > 200_000 and start_line is None and end_line is None:
        return (
            f"[警告] {path} 文件过大（{file_size // 1024} KB），"
            f"请使用 start_line/end_line 参数指定范围读取。\n"
            f"建议：先调用 find_symbol_definition 定位目标函数的行号范围。"
        )

    try:
        all_lines = full_path.read_text(errors="replace").splitlines()
    except Exception as exc:
        return f"[错误] 读取失败：{exc}"

    total = len(all_lines)

    # 指定了范围
    if start_line is not None or end_line is not None:
        s = max(0, (start_line or 1) - 1)
        e = min(total, end_line if end_line else total)
        selected = all_lines[s:e]

        truncated = len(selected) > _MAX_RANGE_LINES
        if truncated:
            selected = selected[:_MAX_RANGE_LINES]

        body = "\n".join(f"{s + i + 1:5d} | {line}" for i, line in enumerate(selected))
        header = f"## {path}  行 {s + 1}-{s + len(selected)}（共 {total} 行）\n\n"
        footer = (
            f"\n... [已截断：共 {e - s} 行，仅显示前 {_MAX_RANGE_LINES} 行"
            f"（继续读取请用 start_line={s + _MAX_RANGE_LINES + 1}）]"
            if truncated
            else ""
        )
        return header + body + footer

    # 未指定范围：智能截断
    MAX_FULL = _HEAD_LINES + _TAIL_LINES   # 120 行以内直接全返回

    if total <= MAX_FULL:
        body = "\n".join(f"{i + 1:5d} | {line}" for i, line in enumerate(all_lines))
        return f"## {path}（共 {total} 行）\n\n" + body

    omitted = total - _HEAD_LINES - _TAIL_LINES
    head_part = "\n".join(
        f"{i + 1:5d} | {line}" for i, line in enumerate(all_lines[:_HEAD_LINES])
    )
    tail_part = "\n".join(
        f"{total - _TAIL_LINES + i + 1:5d} | {line}"
        for i, line in enumerate(all_lines[-_TAIL_LINES:])
    )
    return (
        f"## {path}（共 {total} 行，已截断）\n\n"
        + head_part
        + f"\n\n  ... 省略 {omitted} 行"
        + f"（使用 start_line/end_line 查看中间部分，如 start_line={_HEAD_LINES + 1}"
        + f", end_line={total - _TAIL_LINES}）...\n\n"
        + tail_part
    )


# T4: list_implemented_syscalls（旧版正则扫描，作为降级实现保留）

_SYS_RE = re.compile(r"\bsys_[a-zA-Z_][a-zA-Z0-9_]*")

_LINUX_SYSCALL_SET: frozenset[str] = frozenset({
    "sys_read", "sys_write", "sys_open", "sys_close",
    "sys_fork", "sys_execve", "sys_exit", "sys_wait4", "sys_waitpid",
    "sys_brk", "sys_mmap", "sys_munmap", "sys_mprotect",
    "sys_getpid", "sys_getppid",
    "sys_kill", "sys_signal", "sys_sigaction", "sys_sigreturn",
    "sys_pipe", "sys_dup", "sys_dup2",
    "sys_chdir", "sys_getcwd", "sys_mkdir", "sys_rmdir",
    "sys_unlink", "sys_rename", "sys_stat", "sys_fstat", "sys_lseek",
    "sys_ioctl", "sys_openat", "sys_mkdirat", "sys_fstatat",
    "sys_unlinkat", "sys_renameat", "sys_linkat", "sys_readlinkat",
    "sys_socket", "sys_bind", "sys_listen", "sys_accept", "sys_connect",
    "sys_send", "sys_recv", "sys_sendto", "sys_recvfrom",
    "sys_nanosleep", "sys_clock_gettime", "sys_gettimeofday", "sys_times",
    "sys_uname", "sys_getuid", "sys_getgid", "sys_setuid", "sys_setgid",
    "sys_clone", "sys_vfork", "sys_exit_group",
    "sys_set_tid_address", "sys_futex",
    "sys_sched_yield", "sys_sched_setscheduler", "sys_sched_getscheduler",
    "sys_symlinkat",
})


def list_implemented_syscalls(repo_path: str) -> str:
    """扫描 .c / .rs / .h 文件，收集 sys_* 符号，与标准集合比对后返回报告。"""
    found: dict[str, list[str]] = {}

    repo = Path(repo_path)
    for ext in ("*.c", "*.rs", "*.h"):
        for src in repo.rglob(ext):
            rel = str(src.relative_to(repo))
            if any(part in _SKIP_DIRS for part in Path(rel).parts):
                continue
            try:
                for lineno, line in enumerate(
                    src.read_text(errors="replace").splitlines(), 1
                ):
                    for m in _SYS_RE.finditer(line):
                        name = m.group()
                        locs = found.setdefault(name, [])
                        loc = f"{rel}:{lineno}"
                        if loc not in locs:
                            locs.append(loc)
            except Exception:
                continue

    impl = set(found)
    hit = impl & _LINUX_SYSCALL_SET
    extra = impl - _LINUX_SYSCALL_SET
    missing = _LINUX_SYSCALL_SET - impl

    lines = [
        f"## Syscall 实现情况（共扫描到 {len(impl)} 个 sys_* 符号）",
        f"- 标准 Linux syscall 覆盖：**{len(hit)}/{len(_LINUX_SYSCALL_SET)}**",
        f"- 项目独有 syscall：{len(extra)} 个",
        "",
        "### 已实现（与标准集合重合）",
    ]
    for name in sorted(hit):
        locs = "、".join(found[name][:3])
        lines.append(f"- `{name}` — {locs}")

    if extra:
        lines += ["", "### 项目独有 syscall"]
        for name in sorted(extra):
            locs = "、".join(found[name][:3])
            lines.append(f"- `{name}` — {locs}")

    if missing:
        lines += [
            "",
            f"### 未实现（标准集合中缺失，共 {len(missing)} 个）",
            ", ".join(f"`{n}`" for n in sorted(missing)),
        ]

    return "\n".join(lines)


# T6: compare_with_reference_os（降级版：函数名集合比对，无代码指纹库时使用）

_REFERENCE_OS_FUNCS: dict[str, frozenset[str]] = {
    "rcore-tutorial-v3": frozenset({
        # syscall 层
        "sys_read", "sys_write", "sys_open", "sys_close",
        "sys_fork", "sys_exec", "sys_exit", "sys_waitpid",
        "sys_yield", "sys_getpid", "sys_gettime", "sys_kill",
        "sys_sigaction", "sys_sigreturn", "sys_brk",
        "sys_mmap", "sys_munmap", "sys_pipe", "sys_dup", "sys_dup2",
        "sys_getcwd", "sys_chdir", "sys_mkdir", "sys_unlink",
        "sys_openat", "sys_fstatat", "sys_stat",
        # 进程管理
        "do_fork", "do_exec", "do_exit", "do_waitpid",
        "add_task", "fetch_task", "schedule", "run_tasks", "run_next_task",
        "task_current", "task_block_current", "wakeup_task",
        # 内存管理
        "alloc_frame", "frame_alloc", "frame_dealloc",
        "page_table_walk", "map_area", "unmap_area",
        "translated_byte_buffer", "translated_str", "translated_ref",
        # 陷入处理
        "trap_handler", "syscall", "set_kernel_trap_entry",
        "trap_from_kernel", "trap_return",
        # 文件系统
        "open_file", "file_read", "file_write",
        "inode_read_at", "inode_write_at",
        "OSInode", "ROOT_INODE", "easy_fs_init",
        # 核心数据结构
        "TaskControlBlock", "MemorySet", "MapArea", "PhysFrame", "VirtPage",
        "TrapContext", "TaskContext", "Pipe", "Inode",
    }),
    "rcore-tutorial-v2": frozenset({
        "sys_read", "sys_write", "sys_exit", "sys_fork", "sys_exec",
        "sys_yield", "sys_waitpid", "sys_getpid",
        "run_next_task", "switch_task", "__switch", "schedule",
        "alloc_frame", "dealloc_frame", "FrameAllocator",
        "trap_handler", "syscall", "__alltraps", "__restore",
        "TaskControlBlock", "MemorySet", "TrapContext", "TaskContext",
    }),
    "xv6-riscv": frozenset({
        "sys_read", "sys_write", "sys_open", "sys_close",
        "sys_fork", "sys_exec", "sys_exit", "sys_wait",
        "sys_pipe", "sys_dup", "sys_chdir", "sys_mkdir",
        "sys_unlink", "sys_fstat", "sys_kill", "sys_getpid",
        "sys_sleep", "sys_uptime", "sys_mmap", "sys_munmap", "sys_sbrk",
        "fork", "exec", "exit", "wait", "sleep", "wakeup", "kill",
        "scheduler", "swtch", "yield",
        "growproc", "uvmalloc", "uvmdealloc", "uvmcopy",
        "kalloc", "kfree", "kinit", "freerange",
        "usertrap", "usertrapret", "kerneltrap",
        "virtio_disk_rw", "bget", "bread", "bwrite", "brelse",
        "ialloc", "iget", "iput", "readi", "writei",
        "dirlookup", "dirlink", "namei",
        "proc", "trapframe", "context", "buf", "inode", "dirent",
    }),
    "ucore": frozenset({
        "sys_read", "sys_write", "sys_open", "sys_close",
        "sys_fork", "sys_exec", "sys_exit", "sys_wait",
        "sys_getpid", "sys_yield", "sys_kill", "sys_brk",
        "sys_mmap", "sys_munmap",
        "do_fork", "do_exec", "do_exit", "do_wait", "do_yield",
        "schedule", "run_timer_list", "add_timer",
        "alloc_page", "free_page", "alloc_pages", "free_pages",
        "get_pte", "page_insert", "page_remove", "tlb_invalidate",
        "trap", "trap_dispatch", "syscall", "exception_handler",
        "sfs_init", "sfs_lookup", "sfs_read_file", "sfs_write_file",
        "proc_struct", "mm_struct", "vma_struct", "page", "trapframe",
        "ide_read_secs", "ide_write_secs",
    }),
}

# 提取函数 / 结构体名的正则（C 和 Rust）
_FUNC_C_RE = re.compile(
    r"^\s*(?:static\s+)?(?:inline\s+)?(?:\w[\w\s*]+\s+)+(\w+)\s*\("
)
_FUNC_RS_RE = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)")
_STRUCT_RE = re.compile(r"\b(?:struct|enum|union|type)\s+(\w+)")


def _extract_repo_funcs(repo_path: str) -> set[str]:
    """从仓库源码中提取所有函数名和类型名（正则粗扫描，速度优先）。"""
    names: set[str] = set()
    repo = Path(repo_path)

    for ext, func_re in (
        ("*.c", _FUNC_C_RE),
        ("*.h", _FUNC_C_RE),
        ("*.rs", _FUNC_RS_RE),
    ):
        for src in repo.rglob(ext):
            rel = str(src.relative_to(repo))
            if any(part in _SKIP_DIRS for part in Path(rel).parts):
                continue
            try:
                for line in src.read_text(errors="replace").splitlines():
                    m = func_re.match(line)
                    if m:
                        names.add(m.group(1))
                    for sm in _STRUCT_RE.finditer(line):
                        names.add(sm.group(1))
            except Exception:
                continue

    return names


def compare_with_reference_os(repo_path: str, reference_name: str) -> str:
    """对比当前仓库与参考 OS 的函数集合，输出重叠率与创新点报告。"""
    if reference_name not in _REFERENCE_OS_FUNCS:
        valid = list(_REFERENCE_OS_FUNCS.keys())
        return f"错误：未知参考 OS '{reference_name}'。可选值：{valid}"

    ref_funcs = _REFERENCE_OS_FUNCS[reference_name]
    repo_funcs = _extract_repo_funcs(repo_path)

    hit = repo_funcs & ref_funcs
    unique = repo_funcs - ref_funcs
    missing_in_repo = ref_funcs - repo_funcs

    total_ref = len(ref_funcs)
    overlap_pct = len(hit) / total_ref * 100 if total_ref else 0.0

    if overlap_pct >= 80:
        similarity = "高（疑似直接继承）"
    elif overlap_pct >= 50:
        similarity = "中（有修改或改名移植）"
    else:
        similarity = "低（存在较多原创内容）"

    lines = [
        f"## 与 {reference_name} 对比结果",
        f"- 参考集函数数：{total_ref}",
        f"- 当前仓库提取函数数：{len(repo_funcs)}",
        f"- 重叠函数数：{len(hit)}（重叠率 {overlap_pct:.1f}%）",
        f"- 相似度评估：**{similarity}**",
        "",
        f"### 高度相似（疑似直接继承，共 {len(hit)} 个）",
        ", ".join(f"`{n}`" for n in sorted(hit)) or "（无）",
        "",
        f"### 当前仓库独有（创新点候选，共 {len(unique)} 个）",
    ]
    unique_sample = sorted(unique)[:50]
    lines.append(", ".join(f"`{n}`" for n in unique_sample))
    if len(unique) > 50:
        lines.append(f"... 等共 {len(unique)} 个（仅展示前 50 个）")

    if missing_in_repo:
        lines += [
            "",
            f"### 参考 OS 有但当前仓库未见（共 {len(missing_in_repo)} 个）",
            ", ".join(f"`{n}`" for n in sorted(missing_in_repo)),
        ]

    return "\n".join(lines)
