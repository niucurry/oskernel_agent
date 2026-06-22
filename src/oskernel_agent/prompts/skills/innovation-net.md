name: innovation-net
description: 网络协议栈创新性分析（协议栈 ARP·IP·TCP·UDP/socket 层·epoll/零拷贝·卸载·用户态栈 的基线→前沿与 1–5 锚点；仅分析「网络」子系统时加载）
applies_to: subsys
<!-- body -->
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【技能：网络协议栈（net）子系统创新性分析】

**触发条件：** 仅当本子系统是「网络协议栈」时加载。用本卡梯度在**子系统总览**写「创新性
分析」。判定**只能引用本卡内容**；无对应机制或与基线一致 → 1–2 分写「存疑」，**禁止编造**。带 file:line。

━ 协议栈 ━
- 基线(1分)：无网络，或仅回环。
- 工程(2–3分)：集成 smoltcp，或自实现 ARP/IP/ICMP/UDP/TCP(状态机)、分片重组、重传。
- 前沿(4–5分)：拥塞控制算法(CUBIC/BBR 思路)、TSO/GRO 与校验和卸载、零拷贝收发。

━ socket 层 ━
- 基线(1分)：无 socket 抽象，直连协议函数。
- 工程(2–3分)：BSD socket API、bind/listen/accept、poll/epoll 多路复用、与 VFS 统一为 fd。
- 前沿(4–5分)：io_uring 网络、XDP/eBPF 快路径、用户态协议栈(类 DPDK)。

━ 驱动对接 ━
- 网卡收发与中断处理的创新另见 `innovation-driver`（DMA ring / NAPI / 多队列）。

【创新度判定提示】整段取最高对位项。看信号：自实现 TCP 状态机/重传、拥塞控制、socket+epoll、
零拷贝/卸载、用户态栈。仅「调了缓冲区大小」不加分。
