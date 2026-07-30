"""第三方库路径识别必须区分依赖包目录与同名业务/架构目录。"""

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
