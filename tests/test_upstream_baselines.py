"""上游基线 vendored / ABI 受限代码 识别测试（src.report.upstream_baselines）。

对应评审实测：ArceOS 上游整库 vendored 被当跨队抄袭（70% confirmed 误报）、
metadata_to_kstat 等 ABI 受限实现被计借鉴。验证降级不误伤自研 asynctask。
"""

from __future__ import annotations

from src.report import upstream_baselines as UB
from src.report import semantic_compare as SC


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
    segs = frozenset(UB._DEFAULT_FRAMEWORK_SEGS)
    assert UB.is_upstream_framework_path("arceos/modules/axfs/src/disk.rs") is True
    assert UB.is_upstream_framework_path("arceos/modules/axhal/src/x.rs") is True
    # 队伍自研模块（asynctask/trampoline 不在上游固有模块集）→ 不命中，保留为真实信号
    assert UB.is_upstream_framework_path("arceos/modules/asynctask/src/task.rs") is False
    assert UB.is_upstream_framework_path("arceos/modules/trampoline/src/x.rs") is False
    # 不在 upstream_root 下的队伍自研代码 → 不命中
    assert UB.is_upstream_framework_path("core/src/task/stat.rs") is False
    assert UB.is_upstream_framework_path("api/src/vfs/tmp.rs") is False


def test_tag_upstream_baselines_catches_framework_path():
    # 候选改名 axfs-ng（双侧路径不等），但 query 在 arceos/modules/axfs/ → 框架路径判 upstream
    s = _pair(_q("arceos/modules/axfs/src/disk.rs", "new"),
              _q("2025/team/arceos/modules/axfs-ng/src/disk.rs", "new"))
    UB.tag_upstream_baselines([s])
    assert s.get("upstream_vendored")  # 被标为上游基线
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
    s = _pair(_q("api/src/mm.rs", "sys_shmctl"),
              _q("2025/o/api/src/mm.rs", "sys_shmctl"))
    assert UB.is_abi_constrained(s) is True


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
