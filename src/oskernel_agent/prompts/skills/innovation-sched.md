name: innovation-sched
description: 进程/调度创新性分析（调度算法/上下文切换/SMP·负载均衡/同步原语 的「教学基线→工程实践→前沿」与 1–5 创新度锚点；仅分析「进程管理/调度」子系统时加载）
applies_to: subsys
<!-- body -->
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【技能：进程管理 / 调度（sched）子系统创新性分析】

**触发条件：** 仅当本子系统是「进程管理 / 调度」时加载。用本卡梯度在**子系统总览**里写一节
「创新性分析」（不写进模块详细）。判定**只能引用本卡内容**；无对应机制或与教学基线一致 →
定 1–2 分并写「与标准做法一致 / 无法对标前沿，存疑」，**禁止编造算法名/论文**。带 file:line。

━ 调度算法 ━
- 基线(1分)：单就绪队列 FIFO / Round-Robin / 时间片轮转（`TaskManager` 套 `VecDeque`）。rCore/xv6 默认。
- 工程(2–3分)：Stride / 优先级 / MLFQ 等经典改良；CFS 式 vruntime + 红黑树（有 nice→权重、min_vruntime）。
- 前沿(4–5分)：EEVDF（lag/eligibility + virtual deadline，出处：Linux 6.6、Peter Zijlstra；算法 EEVDF 1995）；
  可插拔/可编程调度 sched_ext/SCX（Linux 6.12）；用户态可编程调度 ghOSt（SOSP'21）。

━ 上下文切换 / 任务结构 ━
- 基线(1分)：手写 `__switch` 保存被调用者保存寄存器，TCB 存 ra/sp/s0-s11。
- 工程(2–3分)：内核/用户栈分离、惰性 FPU 保存、信号栈、线程组。
- 前沿(4–5分)：用户态线程/协程调度、async 内核任务（Rust async no_std）、有栈/无栈协程混合。

━ SMP / 负载均衡 ━
- 基线(1分)：单核，或多核共用一把大锁 + 一个全局就绪队列。
- 工程(3分)：per-CPU runqueue + 周期/空闲负载均衡、调度域、IPI 唤醒。
- 前沿(4–5分)：异构能耗感知调度(EAS/big.LITTLE)、NUMA 感知、work-stealing 队列。

━ 同步原语 ━
- 基线(1分)：关中断自旋锁、忙等 mutex。
- 工程(2–3分)：睡眠锁/条件变量、信号量、RwLock、优先级继承防优先级反转。
- 前沿(4–5分)：RCU、无锁/lock-free 队列、futex 式用户态快路径。

【创新度判定提示】整段取最高对位项。看信号：vruntime/虚拟截止期、红黑树/有序结构、per-CPU 队列、
负载均衡、可插拔策略接口、async/协程、RCU/无锁。仅「换 deque 为 list」不加分。
