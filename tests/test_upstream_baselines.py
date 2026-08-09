"""上游基线 vendored / ABI 受限代码 识别测试（oskernel_agent.comparison.report.upstream_baselines）。

对应评审实测：ArceOS 上游整库 vendored 被当跨队抄袭（70% confirmed 误报）、
metadata_to_kstat 等 ABI 受限实现被计借鉴。验证降级不误伤自研 asynctask。
"""

from __future__ import annotations

from oskernel_agent.comparison.report import upstream_baselines as UB
from oskernel_agent.comparison.report import semantic_compare as SC


def _q(fp, fn, lang="rust", start=10, module="arch"):
    return {"repo_id": "2026/new", "file_path": fp, "func_name": fn,
            "start_line": start, "end_line": start + 9, "module_tag": module,
            "lang": lang, "raw_code": "fn f(){}"}


def _pair(qf, cf, tier="confirmed", score=0.96):
    return {"tier": tier, "final_score": score, "query_func": qf, "candidate_func": cf, "evidence": {}}


# ── upstream framework path（候选改模块名 / 版本差异时的系统化兜底） ──────────────

def test_framework_path_catches_renamed_candidate():
    # query 在 arceos/modules/axfs/ 下（ArceOS 框架固有模块），候选队改名 axfs-ng → 路径全等漏判，
    # 但框架路径判据按上游模块段名（axfs）命中。用兜底模块集（不依赖已 ingest 的 baseline 仓库）。
    assert UB.is_upstream_framework_path("arceos/modules/axfs/src/disk.rs") is True
    assert UB.is_upstream_framework_path("arceos/modules/axhal/src/x.rs") is True
    # 队伍自研模块（asynctask/trampoline 不在上游固有模块集）→ 不命中，保留为真实信号
    assert UB.is_upstream_framework_path("arceos/modules/asynctask/src/task.rs") is False
    assert UB.is_upstream_framework_path("arceos/modules/trampoline/src/x.rs") is False
    # 不在 upstream_root 下的队伍自研代码 → 不命中
    assert UB.is_upstream_framework_path("core/src/task/stat.rs") is False
    assert UB.is_upstream_framework_path("api/src/vfs/tmp.rs") is False


def test_framework_modules_are_isolated_by_corresponding_baseline(tmp_path):
    arceos = tmp_path / "baseline_arceos" / "modules" / "axfs"
    virtio = tmp_path / "baseline_virtio_drivers" / "crates" / "transport"
    arceos.mkdir(parents=True)
    virtio.mkdir(parents=True)
    glob_path = str(tmp_path / "baseline_*")

    assert UB.is_upstream_framework_path(
        "arceos/modules/axfs/src/disk.rs", baseline_glob=glob_path,
    ) is True
    assert UB.is_upstream_framework_path(
        "arceos/modules/transport/src/queue.rs", baseline_glob=glob_path,
    ) is False


def test_framework_layout_uses_longest_root_without_version_cross_contamination(tmp_path):
    (tmp_path / "baseline_rcore_v3" / "crates" / "v3only").mkdir(parents=True)
    (tmp_path / "baseline_rcore_v1" / "crates" / "v1only").mkdir(parents=True)
    glob_path = str(tmp_path / "baseline_*")
    roots = ("rcore", "rcore-v3")

    assert UB.is_upstream_framework_path(
        "rcore-v3/crates/v3only/src/lib.rs", roots=roots,
        baseline_glob=glob_path,
    ) is True
    assert UB.is_upstream_framework_path(
        "rcore/crates/v3only/src/lib.rs", roots=roots,
        baseline_glob=glob_path,
    ) is False
    assert UB.is_upstream_framework_path(
        "rcore/crates/v1only/src/lib.rs", roots=roots,
        baseline_glob=glob_path,
    ) is True


def test_framework_root_normalization_is_consistent_for_case_and_separators(tmp_path):
    (tmp_path / "baseline_ArceOS" / "modules" / "mixed").mkdir(parents=True)
    (tmp_path / "baseline_xv6_riscv" / "src" / "kernel").mkdir(parents=True)
    glob_path = str(tmp_path / "baseline_*")

    assert UB.is_upstream_framework_path(
        "ARCEOS/modules/mixed/src/lib.rs", roots=("ArceOS",),
        baseline_glob=glob_path,
    ) is True
    assert UB.is_upstream_framework_path(
        "xv6-riscv/src/kernel/trap.c", roots=("xv6_riscv",),
        baseline_glob=glob_path,
    ) is True


def test_tag_upstream_baselines_catches_framework_path():
    # 候选改名 axfs-ng（双侧路径不等）；框架路径还必须有独立代码证据才可判 upstream。
    s = _pair(_q("arceos/modules/axfs/src/disk.rs", "new"),
              _q("2025/team/arceos/modules/axfs-ng/src/disk.rs", "new"))
    s["evidence"] = {
        "line_similarity": 0.9,
        "exact_match_lines": 8,
        "function_identity_relation": "exact_counterpart",
    }
    UB.tag_upstream_baselines([s])
    assert s.get("upstream_vendored")  # 被标为上游基线
    # 只有路径提示、没有 pair 代码证据时不能把任意候选归入共同上游。
    noise = _pair(_q("arceos/modules/axfs/src/disk.rs", "new"),
                  _q("2025/team/unrelated/net.rs", "parse"), score=0.38)
    UB.tag_upstream_baselines([noise])
    assert not noise.get("upstream_vendored")
    # 自研 asynctask 不被框架路径误标
    s2 = _pair(_q("arceos/modules/asynctask/src/task.rs", "from"),
               _q("2024/x/crates/taskctx/src/task.rs", "from"))
    UB.tag_upstream_baselines([s2])
    assert not s2.get("upstream_vendored")


# ── upstream vendored ────────────────────────────────────────────────────────

def test_upstream_vendored_same_rel_path():
    # 双方都在 arceos/ 下、相对路径相同 → vendored 上游
    s = _pair(_q("arceos/modules/axhal/x.rs", "f"),
              _q("2025/other/arceos/modules/axhal/x.rs", "g"))
    assert UB.is_upstream_vendored_pair(s) == "arceos"


def test_upstream_vendored_preserves_original_module():
    # 队伍自研 asynctask 模块：候选在其他队不存在同相对路径 → 不命中（保留为真实借鉴信号）
    s = _pair(_q("arceos/modules/asynctask/src/task.rs", "from"),
              _q("2024/x/crates/taskctx/src/task.rs", "from"))
    assert UB.is_upstream_vendored_pair(s) is None


def test_upstream_vendored_different_rel_path_not_hit():
    # 双方都在 arceos/ 下但相对路径不同 → 不判 vendored（可能是跨模块借鉴，留人工）
    s = _pair(_q("arceos/modules/axhal/a.rs", "f"),
              _q("2025/o/arceos/modules/axruntime/b.rs", "g"))
    assert UB.is_upstream_vendored_pair(s) is None


def test_upstream_vendored_requires_same_recognized_root():
    s = _pair(_q("arceos/modules/common/x.rs", "f"),
              _q("2025/o/rcore/modules/common/x.rs", "g"))
    assert UB.is_upstream_vendored_pair(s) is None


def test_upstream_vendored_backslash_paths():
    # 真实数据用反斜杠
    s = _pair(_q("arceos\\modules\\axhal\\x.rs", "f"),
              _q("2025\\o\\arceos\\modules\\axhal\\x.rs", "g"))
    assert UB.is_upstream_vendored_pair(s) == "arceos"


# ── ABI constrained ───────────────────────────────────────────────────────────

def test_abi_name_pattern_kstat():
    s = _pair(_q("api/src/file/fs.rs", "metadata_to_kstat"),
              _q("2025/o/api/src/file/fs.rs", "metadata_to_kstat"))
    assert UB.is_abi_constrained(s) is True


def test_abi_name_pattern_sys_shim():
    thin = "pub fn sys_shmctl(id: usize) -> isize { convert_shmctl(id as i32) }"
    s = _pair({**_q("api/src/mm.rs", "sys_shmctl"), "raw_code": thin},
              _q("2025/o/api/src/mm.rs", "sys_shmctl"))
    assert UB.is_abi_constrained(s) is True


def test_abi_generic_sys_name_does_not_exclude_complex_implementation():
    code = """pub fn sys_fchdir(fd: usize) -> isize {
        let file = match current_task().fd_table().get_file(fd) {
            Some(file) => file,
            None => return -9,
        };
        if !file.can_lookup() { return -20; }
        for group in current_task().groups() {
            if group.can_execute(&file) { current_task().set_pwd(file.path()); return 0; }
        }
        -13
    }"""
    s = _pair({**_q("api/src/syscall/fs.rs", "sys_fchdir"), "raw_code": code},
              _q("2025/o/api/src/syscall/fs.rs", "sys_fchdir"))
    assert UB.is_abi_constrained(s) is False


def test_multiline_user_syscall_forwarder_is_abi_constrained():
    code = """pub fn sys_sendto(
        sockfd: usize,
        buf: *const u8,
        len: usize,
        flags: u32,
        dest_addr: *const u8,
        addrlen: u32,
    ) -> isize {
        syscall(
            SYSCALL_SENDTO,
            [
                sockfd as isize,
                buf as isize,
                len as isize,
                flags as isize,
                dest_addr as isize,
                addrlen as isize,
            ],
        )
    }"""
    s = _pair({**_q("user/src/syscall/socket.rs", "sys_sendto"), "raw_code": code},
              _q("2025/o/user/src/syscall.rs", "sys_mmap"))
    assert UB.is_abi_constrained(s) is True


def test_long_syscall_with_real_statements_is_not_thin_adapter():
    statements = "\n".join(f"let value_{i} = transform({i});" for i in range(8))
    code = f"""pub fn sys_custom(arg: usize) -> isize {{
        {statements}
        commit_state(arg);
        0
    }}"""
    s = _pair({**_q("os/src/syscall/custom.rs", "sys_custom"), "raw_code": code},
              _q("2025/o/os/src/syscall/custom.rs", "sys_custom"))
    assert UB.is_abi_constrained(s) is False


def test_abi_path_glob_ctypes():
    # 路径命中 ctypes/ → ABI shim（即使函数名不匹配模式）
    s = _pair(_q("os/ctypes/fs.rs", "convert"),
              _q("2025/o/os/ctypes/fs.rs", "convert"))
    assert UB.is_abi_constrained(s) is True


def test_abi_does_not_hit_real_logic():
    # 真实 VFS 逻辑 / trait 样板：不命中 ABI 模式 → 保留在报告里
    for fn, fp in [("read_dir", "os/src/fs/dir.rs"), ("from", "os/src/lib.rs"),
                   ("default", "os/src/cfg.rs"), ("requeue", "os/src/sched.rs")]:
        s = _pair(_q(fp, fn), _q(f"2025/o/{fp}", fn))
        assert UB.is_abi_constrained(s) is False, f"{fn} 不应被判 ABI 受限"


# ── 标注 + 排除集成 ──────────────────────────────────────────────────────────

def test_tag_and_exclude_integration():
    suspects = [
        # vendored 上游
        _pair(_q("arceos/modules/axhal/x.rs", "f"),
              _q("2025/o/arceos/modules/axhal/x.rs", "g")),
        # ABI 受限
        _pair(_q("api/src/file/fs.rs", "metadata_to_kstat"),
              _q("2025/o/api/src/file/fs.rs", "metadata_to_kstat")),
        # 真实借鉴（应保留）
        _pair(_q("arceos/modules/asynctask/src/task.rs", "from"),
              _q("2024/x/crates/taskctx/src/task.rs", "from")),
    ]
    counts = UB.tag_upstream_baselines(suspects)
    assert counts == {"upstream_vendored": 1, "abi_constrained": 1}
    assert UB.is_upstream_vendored_pair(suspects[0])  # 已打标
    assert suspects[0]["upstream_vendored"] == "arceos"
    assert suspects[1]["abi_constrained"] is True
    # 排除谓词：前两个剔除，第三个保留
    assert SC._is_excluded_pair(suspects[0]) and SC._is_excluded_pair(suspects[1])
    assert not SC._is_excluded_pair(suspects[2])
    # 主借鉴清单只保留真实借鉴
    groups = SC.collect_file_pairs(suspects)
    assert len(groups) == 1 and groups[0]["query_func"] == "from"
    # 幂等
    assert UB.tag_upstream_baselines(suspects) == counts


def test_tag_skips_library_and_false_positive_pairs():
    # 已标库复用 / 误报的对不重复打 upstream 标
    s = _pair(_q("arceos/modules/axhal/x.rs", "f"),
              _q("2025/o/arceos/modules/axhal/x.rs", "g"))
    s["reuse_library"] = "lwext4"
    assert UB.tag_upstream_baselines([s]) == {"upstream_vendored": 0, "abi_constrained": 0}
    s2 = _pair(_q("arceos/modules/axhal/x.rs", "f"),
               _q("2025/o/arceos/modules/axhal/x.rs", "g"))
    s2["false_positive"] = "cross_arch"
    assert UB.tag_upstream_baselines([s2]) == {"upstream_vendored": 0, "abi_constrained": 0}


# ── 清单数据 ─────────────────────────────────────────────────────────────────

def test_upstream_baseline_stats_grouping():
    suspects = [
        _pair(_q("arceos/modules/axhal/x.rs", "f"),
              _q("2025/o/arceos/modules/axhal/x.rs", "g")),
        _pair(_q("api/src/file/fs.rs", "metadata_to_kstat"),
              _q("2025/o/api/src/file/fs.rs", "metadata_to_kstat")),
    ]
    UB.tag_upstream_baselines(suspects)
    stats = UB.upstream_baseline_stats(suspects)
    assert len(stats) == 2
    reasons = {x["name"]: x["reason"] for x in stats}
    assert reasons["f"] == "upstream_vendored"
    assert reasons["metadata_to_kstat"] == "abi_constrained"
    # upstream_vendored 排前
    assert stats[0]["reason"] == "upstream_vendored"


def test_abi_stats_never_uses_unrelated_first_candidate_as_source():
    q = _q("api/src/file/fs.rs", "metadata_to_kstat")
    noise = _pair(q, _q("thirdparty/syn/src/expr.rs", "parse_expr"), score=0.38)
    valid = _pair(q, _q("2025/o/api/src/file/fs.rs", "metadata_to_kstat"), score=0.82)
    valid["evidence"] = {
        "line_similarity": 0.76,
        "exact_match_lines": 9,
        "function_identity_score": 1.0,
        "function_identity_relation": "exact_counterpart",
    }
    UB.tag_upstream_baselines([noise, valid])
    stats = UB.upstream_baseline_stats([noise, valid])
    assert len(stats) == 1
    assert stats[0]["source"]["func"] == "metadata_to_kstat"
    assert stats[0]["source"]["sim"] == 0.76


def test_abi_stats_has_no_source_when_all_candidates_are_retrieval_noise():
    s = _pair(
        _q("api/src/file/fs.rs", "metadata_to_kstat"),
        _q("thirdparty/smoltcp/src/socket.rs", "poll"),
        score=0.38,
    )
    UB.tag_upstream_baselines([s])
    stats = UB.upstream_baseline_stats([s])
    assert len(stats) == 1
    assert stats[0]["source"] is None


def test_abi_stats_does_not_show_different_syscall_wrapper_as_source():
    code = """pub fn sys_sendto(a: usize, b: usize, c: usize, d: usize, e: usize, f: usize) -> isize {
        syscall(SYSCALL_SENDTO, [a as isize, b as isize, c as isize,
                                d as isize, e as isize, f as isize])
    }"""
    s = _pair(
        {**_q("user/src/syscall/socket.rs", "sys_sendto"), "raw_code": code},
        _q("2025/o/user/src/syscall.rs", "sys_mmap"),
        score=0.91,
    )
    s["evidence"] = {
        "line_similarity": 0.75,
        "exact_match_lines": 15,
        "function_identity_score": 0.82,
        "function_identity_relation": "compatible_renamed",
    }
    UB.tag_upstream_baselines([s])
    stats = UB.upstream_baseline_stats([s])
    assert len(stats) == 1
    assert stats[0]["source"] is None


def test_single_standard_constant_does_not_exempt_complex_logic():
    code = """fn mount_and_recover(dev: Device) -> Result<Fs> {
        if dev.magic() != 0xef53 { return Err(BadFs); }
        for block in dev.journal_blocks() {
            match block.state() {
                Dirty => replay(block)?,
                Clean => verify(block)?,
            }
        }
        rebuild_free_space(&dev)?;
        Ok(Fs::new(dev))
    }"""
    s = _pair({**_q("core/src/fs.rs", "mount_and_recover"), "raw_code": code},
              _q("2025/o/fs.rs", "mount_and_recover"))
    assert UB.is_abi_constrained(s) is False


def test_unrelated_error_and_permission_numbers_are_not_signal_mapping():
    code = """fn check_access(mode: usize) -> isize {
        const ERRORS: [isize; 4] = [1, 3, 6, 9];
        if mode & 13 == 0 { return -20; }
        for error in ERRORS { audit(error); }
        -13
    }"""
    s = _pair({**_q("os/src/syscall/fs.rs", "check_access"), "raw_code": code},
              _q("2025/o/fs.rs", "check_access"))
    assert UB.is_abi_constrained(s) is False


def test_unimplemented_syscall_stub_is_not_mistaken_for_adapter():
    code = "fn sys_splice(_fd: usize, _len: usize) -> isize { -38 }"
    s = _pair({**_q("api/src/syscall/fs.rs", "sys_splice"), "raw_code": code},
              _q("2025/o/fs.rs", "sys_splice"))
    assert UB.is_abi_constrained(s) is False


# ── 硬编码标准常数（POSIX 信号号 / 文件系统魔数） ──────────────────────────────

def test_abi_standard_constants_posix_signals():
    # 评审点名 check_pending_timer_signal：POSIX 定时器信号 14/26/27 规范映射
    code = ("match self.timer_type { TimerType::REAL => Some(14), "
            "TimerType::VIRTUAL => Some(26), TimerType::PROF => Some(27), _ => None }")
    s = _pair(_q("arceos/modules/asynctask/src/stat.rs", "check_pending_timer_signal", code=code)
              if False else {**_q("api/src/x.rs", "check_pending_timer_signal"), "raw_code": code},
              _q("2025/o/x.rs", "check_pending_timer_signal"))
    assert UB.is_abi_constrained(s) is True


def test_abi_standard_constants_fs_magic():
    s = {"tier":"confirmed","final_score":1.0,
         "query_func": {**_q("core/src/fs.rs","stat"), "raw_code":"FsStat { fs_type: 0xef53, .. }"},
         "candidate_func": _q("2025/o/fs.rs","stat"), "evidence":{}}
    assert UB.is_abi_constrained(s) is True


def test_abi_standard_constants_no_false_positive():
    # 普通函数含个别数字 → 不命中
    s = {"tier":"confirmed","final_score":1.0,
         "query_func": {**_q("core/src/vfs/file.rs","set_len"),
                        "raw_code":"fn set_len(&self,len:u64){ data.resize(len as usize,0); }"},
         "candidate_func": _q("2025/o/file.rs","set_len"), "evidence":{}}
    assert UB.is_abi_constrained(s) is False
