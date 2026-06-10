name: syscall-coverage
description: syscall 实现覆盖率分析（list_implemented_syscalls + facts.syscall），仅分析「系统调用」子系统或评「完整性」维度时需要
applies_to: subsys, verdict
<!-- body -->
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【技能：syscall 覆盖率分析】

**触发条件：**
- SUBSYS：当前分析的子系统是「系统调用」时；
- VERDICT：评估「完整性」维度、或需要核实 facts.syscall 数据时。
其它情况无需本技能。

━━ 取数 ━━

- 调一次 `list_implemented_syscalls`（阶段一即可调，不重复调）：返回已实现 syscall
  列表、与标准 Linux 集合的比对、覆盖率。这是判断「实现了多少 / 缺哪些关键能力」
  的一手依据。
- VERDICT 通常无需再调，直接用 `facts.syscall`（已含 ref_* 与计数）；仅当 facts
  缺字段或需核实时才调工具。

━━ 解读与写法 ━━

- 覆盖率不是越高越好——教学内核实现 20–40 个核心 syscall 很正常。重点看**关键类别
  是否齐全**：进程（fork/exec/exit/wait）、内存（mmap/brk）、文件（open/read/write/close）、
  IPC（pipe/signal）、时间/调度（sleep/yield）。
- SUBSYS「系统调用」子系统：在实现要点里说明 syscall 分发机制（ecall→trap→dispatch
  的路径，附 file:line）、已实现 syscall 的分组、明显缺失项。**summary 保持中性事实**，
  缺失项归入 issues（带 file:line，如分发表所在位置）。
- VERDICT「完整性」维度：reason 引用真实 syscall 计数与缺失的关键类别，例如
  「实现 21 个 syscall，进程/内存/文件齐全，缺 network 与 mmap（≤200 字）」。
  禁止臆造数字，数字必须来自工具或 facts.syscall。
