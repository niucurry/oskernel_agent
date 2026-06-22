# 启动汇编样例（用于 normalize 测试）。
    .section .text.entry
    .globl _start
_start:
    la sp, boot_stack_top      # set up stack
    li t0, 0x80200000
    mv t1, t0
    call rust_main
    j park

park:
    wfi
    j park
