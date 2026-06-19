name: innovation-mm
description: 内存管理创新性分析（物理帧分配/页表·地址空间/lazy·CoW/TLB·巨页 的「教学基线→工程实践→前沿」与 1–5 创新度锚点；仅分析「内存管理」子系统时加载）
applies_to: subsys
<!-- body -->
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【技能：内存管理（mm）子系统创新性分析】

**触发条件：** 仅当本子系统是「内存管理」时加载。用本卡的「基线→前沿」梯度，在**子系统
总览**里写一节「创新性分析」（不要写进各模块详细）。判定**只能引用本卡内容**；本卡没有
对应机制、或代码与「教学基线」一致时，定 1–2 分并写「与教学标准做法一致 / 无法对标前沿，
存疑」，**禁止凭记忆编造论文/出处**。每条结论带 file:line。

按下列基本实现逐项对位（看代码命中哪些信号）：

━ 物理帧分配器 ━
- 基线(1分)：栈式 `StackFrameAllocator`（current 指针 + recycled 栈）/ bitmap 位图；直接挂
  `buddy_system_allocator` crate。rCore/xv6 常规，不算创新。
- 工程(2–3分)：buddy + 迁移类型抗碎片、per-CPU page cache 降锁、slab/slub 对象缓存层；
  用 Rust 所有权给「帧不被重复释放」加静态保证。
- 前沿(4–5分)：folio/large folio 批量页管理（出处：Linux 5.16+ folio、Matthew Wilcox）；
  CXL 内存分层感知分配（近年 ASPLOS/OSDI）。

━ 页表与地址空间 ━
- 基线(1分)：SV39 三级页表手工 map/unmap，一个 `MemorySet`/`PageTable` 封装 walk；恒等映射内核。
- 工程(2–3分)：RAII 守护页表/帧生命周期、ASID 复用、内核地址空间共享、细粒度权限位管理。
- 前沿(4–5分)：每地址空间 RCU 保护的 VMA、free-page reporting、页表自映射优化等。

━ lazy 分配 / CoW / 按需调页 ━
- 基线(1分)：fork 即整段拷贝、无缺页惰性分配；mmap 立即分配。
- 工程(3分)：缺页驱动的 lazy alloc、Copy-on-Write fork（引用计数 + 写时复制）、demand paging、
  swap。属经典改良，有完整缺页路径 + 引用计数才给 3。
- 前沿(4–5分)：userfaultfd 式用户态缺页处理、大页 CoW、影子页表等。

━ TLB / 巨页 ━
- 基线(1分)：全量 `sfence.vma` 刷 TLB。
- 工程(2–3分)：按 ASID/地址精确 shootdown、多核 TLB 一致性（IPI）、透明大页(THP)。
- 前沿(4–5分)：TLB coalescing、range-based shootdown 优化等。

【创新度判定提示】整段定级取该子系统最高对位项；仅「换数据结构但语义等同教学版」不加分。
看信号：是否有引用计数、缺页处理路径、per-CPU/ASID、所有权封装、批量/大页、分层。
