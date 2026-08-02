"""第三方库路径识别必须区分依赖包目录与同名业务/架构目录。"""

from pathlib import Path

from src.report import libraries as LIB


def test_ambiguous_riscv_segment_requires_dependency_context():
    assert LIB.match_library("os/src/arch/riscv/trap.rs") is None
    assert LIB.match_library("kernel/platform/riscv/interrupt.rs") is None
    assert LIB.match_library("crates/riscv/src/register.rs") == "riscv"
    assert LIB.match_library("riscv/src/register.rs") == "riscv"


def test_ambiguous_fatfs_segment_requires_dependency_context():
    assert LIB.match_library("os/src/fatfs/inode.rs") is None
    assert LIB.match_library("deps/fatfs/src/lib.rs") == "fatfs"
    assert LIB.match_library("os/src/rust-fatfs/lib.rs") == "fatfs"


def test_distinctive_library_and_vendor_layout_still_match():
    assert LIB.match_library("os/libs/smoltcp/src/socket.rs") == "smoltcp"
    assert LIB.match_library("user/vendor/private_helper/src/lib.rs") == "private_helper"


def test_manifest_and_import_evidence_activate_registered_adapter(tmp_path: Path):
    registry = tmp_path / "libraries.yaml"
    registry.write_text(
        """libraries:
  - name: widget
    segments: [widget_core]
    integration_segments: [widget_adapter]
    import_names: [widget_core]
vendor_dirs: [vendor]
""",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    renamed_package = repo / "deps" / "misspelled-folder"
    renamed_package.mkdir(parents=True)
    (renamed_package / "Cargo.toml").write_text(
        '[package]\nname = "widget_core"\nversion = "0.1.0"\n', encoding="utf-8")
    adapter = repo / "kernel" / "src" / "widget_adapter"
    adapter.mkdir(parents=True)
    (adapter / "inode.rs").write_text(
        "use widget_core::Device;\nfn open() {}\n", encoding="utf-8")
    (adapter / "mod.rs").write_text("mod inode;\n", encoding="utf-8")

    context = LIB.discover_library_context(repo, path=str(registry))

    # 包目录即使被任意改名，也由清单中的真实包名恢复；同一已验证适配目录整体继承归属。
    assert LIB.match_library(
        "deps/misspelled-folder/src/lib.rs", path=str(registry), context=context) == "widget"
    assert LIB.match_library(
        "kernel/src/widget_adapter/mod.rs", path=str(registry), context=context) == "widget"


def test_adapter_name_alone_never_marks_project_code_as_library(tmp_path: Path):
    registry = tmp_path / "libraries.yaml"
    registry.write_text(
        """libraries:
  - name: widget
    segments: [widget_core]
    integration_segments: [widget_adapter]
    import_names: [widget_core]
""",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    adapter = repo / "kernel" / "src" / "widget_adapter"
    adapter.mkdir(parents=True)
    (adapter / "inode.rs").write_text(
        "// widget_core is not a dependency\nfn original_fs() {}\n", encoding="utf-8")

    context = LIB.discover_library_context(repo, path=str(registry))

    assert context.integration_roots == ()
    assert LIB.match_library(
        "kernel/src/widget_adapter/inode.rs", path=str(registry), context=context) is None


def test_lwext4_adapter_requires_repo_evidence(tmp_path: Path):
    repo = tmp_path / "repo"
    package = repo / "crates" / "renamed-lwext4"
    package.mkdir(parents=True)
    (package / "Cargo.toml").write_text(
        '[package]\nname = "lwext4_rust"\nversion = "0.1.0"\n', encoding="utf-8")
    adapter = repo / "os" / "src" / "fs" / "ext4_lw"
    adapter.mkdir(parents=True)
    (adapter / "inode.rs").write_text(
        "use lwext4_rust::Ext4File;\n", encoding="utf-8")

    context = LIB.discover_library_context(repo)

    assert LIB.match_library("os/src/fs/ext4_lw/inode.rs", context=context) == "lwext4"
    assert LIB.match_library("os/src/fs/ext4/inode.rs", context=context) is None

    suspects = [{
        "query_func": {
            "file_path": "os/src/fs/ext4_lw/inode.rs", "start_line": 10,
        },
        "candidate_func": {
            "file_path": "os/src/fs/ext4_lw/inode.rs", "repo_id": "history/team-a",
        },
    }]
    recall = {"results": [
        {"query": {"file_path": "os/src/fs/ext4_lw/inode.rs", "start_line": 10}},
        {"query": {"file_path": "os/src/fs/ext4_lw/sb.rs", "start_line": 20}},
    ]}

    assert LIB.tag_library_reuse(suspects, context=context) == 1
    assert suspects[0]["reuse_library"] == "lwext4"
    assert LIB.reused_library_stats(suspects, recall, context=context) == [{
        "name": "lwext4", "func_count": 2, "pair_count": 1, "repo_count": 1,
    }]
