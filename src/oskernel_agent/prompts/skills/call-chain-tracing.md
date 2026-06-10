name: call-chain-tracing
description: 跨函数调用链 / 控制流追踪（find_entry_symbol + expand_callees / get_subsystem_call_chain），仅当模块控制流复杂、需要还原执行路径时需要
applies_to: subsys
<!-- body -->
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【技能：调用链 / 控制流追踪】

**触发条件：** 仅当某模块的控制流跨多个函数、靠单次 read_file / find_symbol_definition
说不清执行路径时（典型：trap 分发、调度器上下文切换、文件读写主路径）。简单模块不必用。

━━ 推荐顺序（省预算）━━

1. `find_entry_symbol(name)` — 先轻量确认入口函数存在，拿到 file:line 与 kind，
   不读源码、不展开。**这一步能避免对幻觉函数名做昂贵展开。**
2. 确认存在后，再展开调用树（二选一）：
   - `expand_callees(name, max_depth=3)` — 展开该函数往下调了谁（callee 树，最大 5 层）；
   - `get_subsystem_call_chain(entry_function, max_depth=3)` — 从入口展开整条调用链。
   深度默认 3 足够说明主路径；除非必要不要开到 5（结果膨胀、耗预算）。

注意：调用链展开计入工具预算（subsys 总调用 ≤12 次），不要对每个模块都展开，
只对 1–2 个最关键的控制流路径用。

━━ 写进报告 ━━

在该模块的「实现要点」里用**文字 + file:line** 描述主路径，例如：
「trap 入口 `usertrap`（kernel/trap.c:42）按 scause 分发到 `syscall`
（kernel/syscall.c:18）→ 具体 `sys_*` handler」。
**不要画调用图 / 流程图**（禁止 Mermaid），用一句话顺序串起 file:line 即可。
未经工具确认的调用关系不要写。
