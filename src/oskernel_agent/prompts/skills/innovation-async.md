name: innovation-async
description: 异步运行时创新性分析（no_std async executor·Future·waker/协程模型/中断驱动唤醒·async 驱动 的基线→前沿与 1–5 锚点；仅分析「异步运行时/协程」子系统时加载）
applies_to: subsys
<!-- body -->
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【技能：异步运行时（async）子系统创新性分析】

**触发条件：** 仅当本子系统是「异步运行时 / 协程执行器」时加载（与 `innovation-sched` 的线程
调度互补）。用本卡梯度在**子系统总览**写「创新性分析」。判定**只能引用本卡内容**；无对应机制
或与基线一致 → 1–2 分写「存疑」，**禁止编造**。带 file:line。

━ 执行器 / Future ━
- 基线(1分)：无异步，纯同步阻塞。
- 工程(2–3分)：no_std async executor、Future 轮询、waker/任务队列、`async fn` 内核任务。
- 前沿(4–5分)：embassy 风格中断驱动唤醒(无忙等)、优先级 + 公平的 async 调度集成。

━ 协程模型 ━
- 基线(1分)：无。
- 工程(2–3分)：无栈协程(状态机)、有栈协程切换。
- 前沿(4–5分)：有栈/无栈混合、async 与线程统一调度、用户态 async 运行时。

━ async 设备 / IO ━
- 基线(1分)：轮询 IO。
- 工程(2–3分)：async 驱动(await 中断完成)、async 块/网络 IO。
- 前沿(4–5分)：io_uring 式提交/完成队列驱动的 async IO。

【创新度判定提示】整段取最高对位项。看信号：executor/waker、中断驱动唤醒、无栈/有栈协程、
async 驱动、与调度器集成。仅「包了个 block_on」不加分。
