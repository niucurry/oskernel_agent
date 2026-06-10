name: rust-crate-analysis
description: Rust 工作区的 crate 角色识别与架构合理性分析（仅当项目为 Rust / 存在 Cargo workspace 时需要）
applies_to: subsys, verdict
<!-- body -->
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【技能：Rust 工作区 crate 架构分析】

**触发条件：** 仅当项目主要语言为 Rust、或仓库根存在 `Cargo.toml` 工作区
（initialize_analysis 返回的 Layer 2 / facts 中标明 has_cargo 或列出了 crate 角色）时使用。

━━ crate 角色识别 ━━

Rust 内核项目通常按 Cargo workspace 的 members 拆成多个 crate，每个 crate 承担一类
职责。初始化返回的 Layer 2「Crate 架构」段已给出 crate → 角色映射，请直接采用；
若需自行判断，按 crate 名（小写、`-`→`_`）参照下列常见角色：

- `os` / `kernel` / `kern` → 内核主体
- `user` → 用户态程序集；`userlib` → 用户态库
- `easy_fs` / `fs` → 文件系统实现；`easy_fs_fuse` → 文件系统宿主工具（FUSE）；`fatfs` → FAT 文件系统
- `drivers` / `driver` → 设备驱动；`virtio` → VirtIO 驱动
- `buddy` → Buddy 分配器；`allocator` → 内存分配器
- `trap` / `interrupt` / `irq` → 中断 / 异常处理
- `ipc` → 进程间通信；`pipe` → 管道（IPC）；`signal` → 信号机制（IPC）
- `sync` / `lock` → 同步原语；`spinlock` → 自旋锁；`mutex` → 互斥锁
- `smp` → 多核支持；`cpu` → CPU/Hart 管理；`hart` → Hart 管理（RISC-V 多核）
- `boot` / `startup` → 启动初始化 / 启动序列

无法对应到上表的 crate，归为「子模块（<crate 名>）」。

━━ 在分析中如何使用 ━━

- SUBSYS：识别模块拆分时，优先以 crate 边界为参照——一个 crate 往往就是一个清晰模块；
  跨 crate 的依赖（如 kernel 依赖 easy_fs）要在总览的「总体架构」里点明，并附 path:line。
- VERDICT：评「架构合理性」维度时，把 crate 划分的清晰度、职责单一性、依赖方向是否合理
  作为关键证据；workspace 边界清晰、无循环依赖通常是加分项，单 crate 巨石或职责混杂是减分项。
  reason 必须能溯源到具体 crate 名与 path:line。
