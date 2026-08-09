"""仓库文件发现：遍历、语言识别、目录排除、第三方库识别。"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .ts import lang_of

# 始终排除的目录名（构建产物、明确的 vendored 依赖目录）
EXCLUDE_DIRS = {
    "target", "build", ".git", "vendor", "third_party", "third-party",
    "node_modules", ".cargo", "deps", "dependencies",
    # 竞赛通用基础设施（各队签入的同一份，非内核原创代码）：官方测试集 / 用户态测试程序
    # / benchmark。不排除会让 L0 文件指纹把它们当「整文件复制」批量误报（实测占跨仓命中绝大多数）。
    "libc-test", "oscomp", "user_C_program", "testsuits", "testsuites",
    "lmbench", "busybox", "iozone", "ltp", "lua",
    # 性能/压力测试工具（各队签入同一份基准套件，非内核原创代码）
    "netperf", "iperf", "unixbench", "rt-tests", "byte-unixbench",
    # 构建产物根文件系统 / sysroot（编译出来的 libc/libstdc++ 头与库，非源码）
    "sysroot", "rootfs",
}

# 目录名后缀模式：随仓库签入的预编译交叉工具链 / sysroot（musl-cross 等，整棵都是 libc 头/库）。
EXCLUDE_DIR_SUFFIXES = ("-musl-cross", "-gcc-cross", "-elf-cross")

# 目录名子串模式：第三方项目名常被改前缀签入（chcore-busybox、busybox_lua_testsuites 等），
# 精确名匹配会漏；这些 token 在内核源码目录名里不会出现，用子串兜底是安全的。
EXCLUDE_DIR_SUBSTRINGS = ("busybox", "ltp-full", "ltp_full")

# 各队签入的第三方目录大小写不一（UnixBench vs unixbench），统一按小写比对。
_EXCLUDE_DIRS_LC = {d.lower() for d in EXCLUDE_DIRS}
_EXCLUDE_SUFFIXES_LC = tuple(s.lower() for s in EXCLUDE_DIR_SUFFIXES)
_EXCLUDE_SUBSTRINGS_LC = tuple(s.lower() for s in EXCLUDE_DIR_SUBSTRINGS)

# 测试和基准代码不属于参赛内核实现。这里仅识别完整目录组件或由 ``-_.``
# 分隔的文件名 token，避免把 contest/latest/testament/benchpress 等普通名称误判。
_TEST_BENCH_TOKENS = frozenset({
    "test", "tests", "testing", "testcase", "testcases", "testdata",
    "bench", "benches", "benchmark", "benchmarks",
})
_TEST_BENCH_COMPONENTS = frozenset({
    *_TEST_BENCH_TOKENS,
    "libctest", "libc-test", "test-suite", "test-suites", "testsuite", "testsuites",
})
_PATH_TOKEN_SPLIT_RE = re.compile(r"[-_.]+")


def _normalized_path_components(path: str | Path) -> tuple[str, ...]:
    value = unicodedata.normalize("NFKC", str(path or "")).casefold()
    return tuple(part for part in value.replace("\\", "/").split("/") if part not in ("", ".", ".."))


def _is_test_bench_component(component: str, *, filename: bool = False) -> bool:
    value = component
    if filename and "." in value:
        # 保留 foo.test.rs 中的中间 ``test``，只去掉最后一个扩展名。
        value = value.rsplit(".", 1)[0]
    if value in _TEST_BENCH_COMPONENTS:
        return True
    return any(token in _TEST_BENCH_TOKENS for token in _PATH_TOKEN_SPLIT_RE.split(value))


def is_test_or_benchmark_path(path: str | Path) -> bool:
    """路径是否明确指向测试/benchmark 资产。

    目录支持 ``test``、``tests``、``bench``、``benchmark`` 及组合名（如
    ``integration-tests``）；文件支持 ``test_sched.rs``、``sched_test.rs`` 等
    规范命名。匹配基于完整 token，不会因名称中偶然包含 ``test`` 而排除生产代码。
    """
    parts = _normalized_path_components(path)
    if not parts:
        return False
    if any(_is_test_bench_component(part) for part in parts[:-1]):
        return True
    return _is_test_bench_component(parts[-1], filename=True)


def _is_excluded_dir(name: str) -> bool:
    d = name.lower()
    return (d in _EXCLUDE_DIRS_LC
            or any(d.endswith(s) for s in _EXCLUDE_SUFFIXES_LC)
            or any(s in d for s in _EXCLUDE_SUBSTRINGS_LC)
            or is_test_or_benchmark_path(d))


@dataclass(frozen=True)
class DiscoveredFile:
    path: Path          # 绝对/可读路径
    rel_path: str       # 相对仓库根（用于 file_path 与归类）
    lang: str           # rust / c / asm


def discover_files(repo: str | Path) -> list[DiscoveredFile]:
    """遍历仓库，返回待处理的源码文件。

    排除：EXCLUDE_DIRS 中的目录名所对应的子树。

    注意：早期版本曾把「含 LICENSE/COPYING 的子目录」整棵当第三方库跳过，但 Rust 工程惯例是
    每个 crate（包括参赛队自己写的）都带 LICENSE，该规则会误杀整个作品源码（实测某作品 480 个
    rust 文件只剩 9 个函数）。故移除该启发式，仅按目录名排除；少量随仓库签入的 vendored crate
    会被纳入，但 SimHash 的 IDF 降权、>5 仓库通用串过滤、基线通道与 LLM common_pattern 判定
    已专门用于消化「广泛共享代码」，宁可多收也不漏检。
    """
    repo = Path(repo).resolve()
    out: list[DiscoveredFile] = []

    for dirpath, dirnames, filenames in os.walk(repo):
        # 排除指定目录（原地修改 dirnames 以阻止 os.walk 下降），大小写不敏感
        dirnames[:] = sorted(d for d in dirnames if not _is_excluded_dir(d))

        for fn in sorted(filenames):
            lang = lang_of(fn)
            if lang is None:
                continue
            p = Path(dirpath) / fn
            rel_path = p.relative_to(repo)
            # 防御单文件测试命名（如 src/test_sched.rs），目录级排除之外再检查一次。
            if is_test_or_benchmark_path(rel_path):
                continue
            out.append(
                DiscoveredFile(
                    path=p,
                    rel_path=str(rel_path),
                    lang=lang,
                )
            )
    return out
