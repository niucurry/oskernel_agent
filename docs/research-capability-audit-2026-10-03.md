# 真实内核与比赛 syscall 判定：已执行的分层审计

这是持续研究的阶段记录，接续 [重新选题](research-reselection-2026-10-03.md)。旧三条描述研究主线仍停止投入。当前保留的研究问题是：**评审所见的通过判定，究竟支持哪一层系统能力主张？** 已取得可信的局部因果证据和工程工具，完成公开比赛相关项目的 guest 验证；没有新增算法优势或正式比赛成绩影响的证据。

## 实际开发与执行

已为实验私下准备 QEMU、GDB、ARM 工具链及原始 OS 源码；未安装系统包、使用 sudo 或修改用户全局工具链。三种源码为 NuttX 12.9.0、RT-Thread 5.2.1、uCore x86 lab8 answer，完整 commit、文件哈希、编译失败与准备日志保留在 [capability_confirmation](../research/capability_confirmation/)。NuttX 使用完整 sim 内核；RT-Thread 和 uCore 在 QEMU 中运行。原测试主体不改动。NuttX 仅增加选取 ostest 组件的薄入口，其完成判定是本地适配，不能称为正式比赛成绩。uCore 与 xv6/JOS 有来源关系，不能算完全独立 OS 家族。

主协议冻结 3 OS × 8 API/工作负载 × 正常/阻断 × 2 次，共 **96 次**；单次限时 45 秒、200 操作跟踪点、8 CPU 小时、0 模型调用。外部 GDB 在精确入口跳过 API 主体，设置约定错误/NULL，并验证返回寄存器与调用方 PC。它只撤回选定工作负载内的 API 操作，不证明整个内核能力已移除。

结果见 [逐对分析](../research/capability_confirmation/analysis.json)，重复执行不当作独立程序样本：

| OS | 对 API 撤回敏感 | 撤回后仍通过 | 未知/无有效正常控制 | 总计 |
|---|---:|---:|---:|---:|
| NuttX | 7 | 1 | 0 | 8 |
| RT-Thread | 3 | 2 | 3 | 8 |
| uCore | 0 | 0 | 8 | 8 |

96 次累计运行墙钟 714.58 秒、子进程 CPU 268.22 秒；并行批次的墙钟相加不等于用户等待时间，下载/编译另计。11 对未知均留在 24 对分母中。uCore 包含初始私有 QEMU 固件启动失败、原正常程序的内存清理断言失败、无原判分谓词及未调用 read；RT-Thread 包含初始驱动适配错误、控制台注册缺失和正常 timer 跟踪超过预算。没有补跑后抹掉这些失败。

必须披露协议偏离：准备及运行中进行了不止一次环境/驱动适配，包括 GDB 静态函数地址转换、QEMU 固件、RT-Thread 控制台配置；[v2](../research/capability_confirmation/protocol-v2.json)、[v3](../research/capability_confirmation/protocol-v3.json)和旧脚本保存。它不满足前一记录提出的“每 OS 最多一次环境适配”，不能作为无偏离的盲确认实验。没有为结果改测试主体、原判定、选定 API 或错误值。

## 三种幸存干预的解释不同

**RT-Thread 线程测试：失败被汇总遗忘。** 原日志先打印 6 条断言失败，随后给整体 PASSED。原 `utest_unit_run` 每次清零 `failed_num`，整体只读取最后一个单元留下的计数。核对依据是 [固定版本官方实现](https://github.com/RT-Thread/rt-thread/blob/97893c004c65760c638fd7eb571a08fc987a55e5/components/utilities/utest/utest.c)，并非模型评判。

进行了独立冻结的 **8 次因果对照**：只在实验缓存的框架添加独立累计失败计数，保留每单元的公开计数，测试和内核操作主体均不动。线程正常对照 2/2 仍通过；相同 NULL 干预 2/2 从通过变为失败。堆测试的早退仍通过。该对照定位了汇总机制，并没有修复所有测试充分性问题。[补丁与原始 ELF](../research/verdict_binding/)、[原始对照](../research/capability_confirmation/causal-runs.jsonl)保留；未向上游发送消息或 PR。

**RT-Thread 堆测试：主体没有执行。**  backing allocation 为 NULL 时，原 `memheap_test` 直接 return，没有失败断言。追加 **8 次效果观察**同时记录下游 API：正常两次均到达 memheap alloc/realloc，阻断两次均未到达，但仍整体通过。这证明该运行没有覆盖这部分工作负载，不证明整个 OS 缺少堆能力。[效果协议和记录](../research/verdict_binding/effect-protocol.json)。

**NuttX 信号量：这一个输入的初始化效果可被已有零状态替代。** 正常和阻断两次观察都显示：请求初始化值为 0，32 字节 sem_t 初始化前后均为全零，之后实际到达 wait/post。不能把这个样本写成“无信号量功能也通过”。另用 **4 次 errno 控制**在内部 nxsem_init 返回 -38，让原公共 wrapper 执行错误处理；观察到 public sem_init 返回 -1、errno=38，组件仍完成。该控制还触及工作负载内三个附带的内部初始化，不能描述成只改了一个对象。它排除了“公共 -1 没设置 errno”的单一解释，但不是可自然发生的 valid-input 错误或全能力移除。[协议](../research/verdict_binding/errno-control/protocol.json)。

这 20 次追加属于事后机制诊断，不能合并成原 96 次的独立留出。没有将同一对象字节相等提升成一般语义等价证明。

## 官方基础测例中的成功值语义

新的 [contest_syscall_audit](../research/contest_syscall_audit/)取得官方 pre-2025 基础源码，以及固定版本 autotest 的完整 glibc/musl judge。原 getpid、getppid、uname、dup 四个 C 测试主体逐字节复制。通过薄宏与宿主 libc/断言适配，在 Linux 上执行；这不是原 RISC-V/LoongArch ELF，也不是参赛 guest。

协议比较正常、返回错误、返回看似成功但独立错误的结果，各重复两次，先后 **24 + 24 = 48 次**。v1 的 getpid/uname 动态符号断点未触达，12 单元留为未知。v2 使用原 ELF 中实际调用位置与指令字节校验；修改的是干预位置适配，原测试、判定、常量、参考与门槛不变，另冻结版本，不能冒称新的盲测。

v2 的 24 个单元全部取得可核对的操作/干预见证，正常 8/8 与独立 Linux 参考一致。两份官方 judge 结果在这四个测试上相同，但不算两个独立 OS：

| 原测例 | 正常分 | 错误返回分 | 成功形态但错误的结果分 | 独立见证 |
|---|---:|---:|---:|---|
| getpid | 3/3 | 0/3 | 3/3 | 返回 42 与真实 inferior PID 不同 |
| getppid | 2/2 | 1/2 | 2/2 | 返回 42 与外部 /proc PPid 不同 |
| dup | 2/2 | 0/2 | 2/2 | 返回 42，但 /proc fd 表没有此描述符 |
| uname | 2/2 | 0/2 | 1/2 | 声称成功却未填充输出对象，与宿主 uname 不同 |

每格两次一致。getppid 的错误返回仍得部分分，不可写成全拒绝或全通过。uname 的空结果被一项谓词识别，不能为了四项全胜而补写假字符串。原定义要求 getppid 返回调用者的父进程 ID，dup 必须产生可用的复制描述符；参考依据是 [POSIX getppid](https://pubs.opengroup.org/onlinepubs/009696699/functions/getppid.html)、[POSIX dup](https://pubs.opengroup.org/onlinepubs/9799919799/functions/dup.html)和独立运行见证。这里使用错误返回来模拟接口不可用；getpid/getppid 在 POSIX 中本应始终成功，故不是主张这种错误是合法 POSIX 行为。

**有限证据支持：只检查“接口返回错误时测试会否失败”不足以证明测试检查了成功结果的含义。** 三个测例的原 judge 对错误返回有响应，却给语义错误的成功结果满分。[所有分数、失败与成本](../research/contest_syscall_audit/analysis.json)保留。48 次运行墙钟合计 8.00 秒、CPU 8.08 秒，另有 96 个 judge 进程，不能漏计；同一函数两次和两份 judge 不增加独立样本数。

## 方法选择与论文边界

保留的验证流程是：先建立稳定正常控制，再验证干预确实发生；分别测错误返回与成功值/效果错误；用外部身份、对象或操作见证区分不敏感、主体跳过、输入特例和无效实验；最后核对断言到汇总的判定传播。它适合继续作为**OS 评测有效性的实证方法**，已比只做错误返回试验多发现三个有限语义缺口。

这不是新故障注入或新 mutation 算法。Linux 已有 [error-return injection](https://docs.kernel.org/fault-injection/fault-injection.html)；[RCU 变异测试经验研究](https://stairs.ics.uci.edu/papers/2017/Applying_Mutation_Analysis_on_Kernel_Test_Suites_An_Experience_Report.pdf)已定位测试 harness 缺口；[Falsification-Driven Verification and Testing](https://agroce.github.io/asej18.pdf)还讨论修改 harness 来审查验证强度；[Necessist 官方实现](https://github.com/trailofbits/necessist)直接寻找测试中的无效动作。成功值变异加精确参考也是应采用的强基线，当前没有证明超越它。因此停止“普通 API 撤回 + trace 是新增算法”的主张，不将工程修复包装成 TSE/TOSEM 方法创新。

相较 A 构建覆盖、B 测试特化与 C 上游残余，D 目前具有最多实际运行和原判定证据，独立参考成本也最低。论文拟贡献需转为**新的跨评测层测量结果、适用条件、覆盖成本与反例分类**。TSE/TOSEM 研究仍缺实际参赛内核、更多独立 workload/版本、强 mutation/fault-injection 工具及等预算比较；若主张评委时间或准确率改善，还需对应评委研究。当前不声称正式比赛受这些样本影响，不给队伍贴标签或承诺录用。

Moncake、Chronix 原 GitLab 地址无法在当前无凭据读取 HEAD，原始失败记录保留；StarryX 作者公开仓库固定为 3a3971c9047fbcea4ecbf75b22206e5bfbd971ec，已完成下面的 guest 验证。公开作者项目不能冒称经赛事认证的最终提交版本。

## 已保留的系统改进

增加 `python -m oskernel_agent.finals audit-run --log ... --output ...`，可导出 HTML/JSON；核对 RT-Thread 明确的最后一轮 started/finished 边界。失败断言不能被同一轮后续 PASSED 覆盖；之前独立失败轮次也不会污染后续完整轮次；未识别、未完成、零测例或计数不全留为 unknown。退出码分别为 0（判定一致并报告通过）、1（失败/矛盾）、2（未知/输入错误），并禁止覆盖输入日志。

库函数 `analyze_log(kind="run"/"test")`使用这项有边界的核对，原构建日志的先失败后成功逻辑保留。它不执行仓库，不验证日志来源或 API 正确性，尚未自动接入作品描述流程。[实际线程矛盾核对页](../research/verdict_binding/thread-audit.html)。

针对新入口、矛盾传播、轮次隔离、未知及原描述相关路径，初次 96 项工程测试通过；补充非法计数、重复结果和硬链接保护后，最终 **98 项通过**。这只验证软件回归，不能计入论文方法效果。已有生产修复、两项用户原有文档删除和历史负结果均保留。

## StarryX 原内核的实际 guest 验证

[contest_guest_validation](../research/contest_guest_validation/)使用原作者项目、私有 nightly-2025-01-18、RISC-V musl 交叉工具链及 QEMU 8.2.2。完整 6,680 个原源码文件哈希保持一致，包括 Cargo.lock；没有修改内核、原 C 测例或原判分函数。编译添加调试信息，启动使用自建 64 MiB ext4、BusyBox 1.36.1 和四个静态 musl 程序，故不是官方比赛镜像或另一个 glibc 环境。

必须披露准备偏离：第一版两次构建均因路径/依赖环境失败而用尽预算，记录为 unknown_environment。随后另立探索性准备版本，两次上限内完成：从原 Cargo.lock 机械补齐遗漏的 vendor 映射，允许公开固定依赖解析，不改变版本；三次失败和成功日志均保留。第一次 boot-only 因 BusyBox standalone 使用 /proc/self/exe 而失败；关闭 standalone、使用原 /bin applet 链接后启动成功。原镜像也保留，未删除失败记录。没有把这些准备成功计为论文效果。

私有 rustup 安装命令返回 1：核心 toolchain 已装好，最后因私有 CARGO_HOME 没有 rustup manager 而报错。之后使用该 toolchain 的绝对 cargo/rustc 路径，并记录实际版本成功验证；不是把失败退出码改写成成功。第一份组合版本命令也包含 cmake 缺库的错误，补齐私有依赖后另有单独 cmake 检查。

四个原测试主体之外单独链接 main wrapper，先取得未干预 PID、PPid、uname，再用 GDB 精确核对 RV64 用户态调用指令，正常/错误/成功形态变异各两次，共 **24 次**。干预检查调用方 PC、ra、sp、返回值及用户态 privilege；全部有效。getpid 返回 42 与参考 PID=8 不同，getppid 返回 42 与参考 PPid=7 不同，dup 返回 42 但之后的未干预 fcntl 证明它不存在，均 2/2 仍满分；uname 未填对象时只得 1/2。正常 8/8 与相应参考一致。两份 judge 只对同一 musl 输出交叉核对，不增添独立环境。

这一阶段累计执行墙钟 13.77 秒、CPU 18.12 秒，另有 48 个 judge 进程。guest 身份和 uname 参考来自同一原实现的干预前调用，**不能发现原实现与参考共有的错误**；它们只核对本次已知变异，独立性弱于宿主 /proc。dup 这时仅检查存在性，尚不能证明最低空闲描述符、共享偏移等全部 POSIX 性质。[分析与全部限制](../research/contest_guest_validation/analysis.json)。

## 主动证伪审计器自身

[oracle_falsification](../research/oracle_falsification/)固定四种边界控制，在宿主和 guest 各 16 次，共 **32 次**。getpid/getppid 回放本轮正确身份，uname 回放本轮正确对象；dup 返回已存在的 stdout=1 而没有产生新描述符。所有正常与干预记录有效：

| 检查 | 宿主 | StarryX guest | 决定 |
|---|---:|---:|---|
| 只要调用被跳过就判能力缺失 | 错拒 6 次正确回放 | 错拒 6 次正确回放 | 淘汰充分性主张 |
| 只核对描述符存在/宿主 target | 错收 2 次 dup 别名 | 错收 2 次 dup 别名 | 淘汰充分性主张 |
| 标准接口参考；dup 加新描述符与最低空闲检查 | 16/16 与已知变异标签一致 | 16/16 一致 | 保留限定工程检查 |

这里的“新描述符”只用于 dup 的标准语义，不是已经淘汰的通用“任何操作都必须改变终态”门槛。PID 回放可正确且无状态变化；uname 回放可以提供这一个输入的正确输出。无法据此主张整个内核能力可被回放替代。描述符检查仍未覆盖一般共享偏移语义。官方 judge 对 dup=1 本身会扣分至 1/2，因此新检查**不全面优于原判定**。[边界结果](../research/oracle_falsification/analysis.json)。

## 新文件/目录工作负载未支持推广假设

[contest_effect_audit](../research/contest_effect_audit/)固定 getcwd、open、close、read、write、lseek、fstat、mkdir 八项清单，以正常/错误/成功形态错误各两次；预先要求至少 6 对有效正常控制、至少 2 个新的满分语义缺口。lseek 无原测例，六个计划单元不执行且保留未知，没有换成更易成功的函数。

v1 实际 42 次，只有 4 对有效正常控制：host printf 宏未同步刷新，导致 open/read/write 的 write 输出先于 START，原 judge 无法判正常通过。另立 [framing-v2](../research/contest_effect_audit/framing-v2/protocol.json)，只刷新 START/END 的宿主宏；原主体、fixture、API、值、参考、谓词和门槛完全不变。它是已暴露程序的适配复核，不能冒称新的盲确认。原 42 次全部保留，v2 另执行 42 次。

| 原测例 | 正常 | 错误返回 | 成功形态语义错误 | 已核对的实际错误 |
|---|---:|---:|---:|---|
| getcwd | 2/2 | 1/2 | 1/2 | 空对象与外部真实 cwd 不同 |
| open | 3/3 | 0/3 | 0/3 | fd 42 不存在；正常为新开的目标文件 |
| close | 2/2 | 0/2 | 2/2 | 声称关闭，但外部 fd 表仍存在 |
| read | 3/3 | 0/3 | 0/3 | 仍有 52 字节可读却返回 0、不填对象 |
| write | 2/2 | 0/2 | 0/2 | 返回请求长度但真实 stdout 文件无对应字节 |
| fstat | 3/3 | 0/3 | 2/3 | 零 stat 对象与外部文件 metadata 不同 |
| mkdir | 3/3 | 0/3 | 2/3 | 返回成功而目录没有创建 |
| lseek | 未知 | 未知 | 未知 | 原测例缺失 |

每格两次一致。v2 七对正常全部稳定且外部参考吻合，42 次操作/干预均有效，但只有 close 一个新的满分缺口，**预设推广假设被否定**。不会补挑常量、改谓词或换样本救回门槛。部分分不写成满分漏判；也不写成全拒绝。[两版分析](../research/contest_effect_audit/framing-v2/analysis.json)。

该观察支持测例间异质性，不支持“官方基本测例普遍对成功语义盲目”。加上最初四项，11 个有原主体的官方任务在当前受控变异中有 getpid、getppid、dup、close 四个满分缺口；这不是随机抽样或队伍自然错误率，不能以 4/11 推断赛事总体比例。

## 本轮方法决定与可用范围

**系统采用：有正常控制的接口语义审计，加同一测试轮次的判定一致性核对。** 它能给评委提供可复核的反例和未知状态，避免由“通过”“到达 API”或“不报错”直接推断功能正确。适用的最小流程是固定版本和场景、正常控制、错误与成功值/效果控制、接口专用参考、原判分及失败传播核对；没有可信参考时输出 unknown。机制验证自动运行，参考不由模型投票决定。

**论文继续保留的只是 OS 评测证据有效性的实证问题；新增通用算法主张停止。** 本轮没有优于“标准语义变异＋接口参考”强基线。调用撤回、终态变化、描述符存在，以及更广泛的满分盲点假设各有明确负结果。不能把它们再换名为新架构或用更多模型调参挽救。

这一流程已在原内核、实际 guest 和独立宿主参考上验证局部适用性，工程上可用；**尚不能判定为顶刊级研究方法**。若要主张新的算法优势，需提出有独立增益的新机制并重新冻结强基线比较；若走经验研究，需扩大独立比赛实现/版本和工作负载，不能重复计数现有运行或把适配复核叫盲测。任何减少评委时间或提高真实评审准确率的主张仍需要对应人工研究。本轮不承诺录用。

本轮累计 **304 个实际运行单元**：116 个 QEMU 内核/guest、40 个 NuttX sim、148 个宿主程序；另有两版各六个 lseek 计划单元未执行。编译、下载、boot-only 与 judge 进程单独计，不混入独立样本数。研究受测模型 API 调用为 0；这不表示本助手推理或所有工程工作零成本。所有准备偏离、未知、失败、原始日志和已停止主张保留。复核入口见 [研究运行说明](../research/capability_confirmation/README.md)与[最终总账](../research/capability_confirmation/final-audit.json)。
