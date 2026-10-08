# 新的实际验证：执行身份与文件格式的兼容性

本阶段继续研究真实 OS 评审，不涉及已停止的行为图、条件 JSON 或描述压缩。此前 304 个运行单元和全部负结果保持。新增 88 个实际运行单元，实验对象模型调用 0 次；编译和启动诊断单独记录。

最有实际依据的新增结果是：固定版本 StarryX 对同一个 ELF 的加载行为随 `.sh` 后缀改变。它支持继续研究**有明确适用条件的身份变换如何帮助评委复核源码风险**，但目前没有超越普通规格检查／变形测试的新算法证据，不能据此宣称顶刊方法已经成立。

## 独立 OS 准备的真实结果

先冻结两个公开作者仓库、下载体积、编译次数、超时和磁盘下限，再准备环境。作者公开版本与正式参赛提交没有建立认证关系。

| 对象 | 固定提交 | 已实际完成 | 当前状态 |
|---|---|---|---|
| Chronix | `413b2c0f774c3e88f426ae7a1024cabcdc1a821c` | 原用户程序和内核均编译；两次自建双磁盘启动 | 首次没有找到 initproc；第二次进入原 initproc，随后装载 BusyBox 的环境不满足该加载器要求。用完启动诊断预算，记 unknown，效果单元 0 |
| NoAxiom | `77c4a15c75a7af9b31fa723b7095f3e53f9950e9` | 原子模块 `2a187c3fd09bb8a149e5a4aa63e2d1f6c5cfd3aa`、精确日期 Rust 组件、两个 locked vendor 操作 | 内核 vendor 221,844,068 字节，超过冻结的 209,715,200 字节上限；没有执行编译或效果单元，记 unknown |

Chronix 的 339 个原文件、NoAxiom 的 420 个原文件和用户子模块的 49 个文件校验均未变化。两项 unknown 不能作为队伍缺陷、方法负效果、独立确认样本或“方向彻底不可行”的证据。[准备总账](../research/contest_corpus_inventory/analysis.json)保留在选择分母中。

为继续工作，完整归档并逐文件校验了这轮新建的 Cargo 编译中间件，保留原源码、顶层 ELF/bin、用户程序和所有原日志。两个归档分别验证 1,139 和 1,556 个文件后才释放中间件目录，归档本身仍在私有缓存。首次 gzip 校验因随机反向定位性能问题在删除前中断，之后改成单次流式校验；没有把重新校验所花的几秒当完整准备成本。历史退休归档没有改动。

## 已执行的方向 B 原型

生产系统扫描的是复核线索，不能从名称分支直接推断违规。本轮首先阅读固定版本的 [StarryX 加载器](https://github.com/2zuqisong/StarryX/blob/3a3971c9047fbcea4ecbf75b22206e5bfbd971ec/xcore/src/mm/init.rs)，发现 `load_file` 在读取文件内容前将 `.sh` 路径转交 BusyBox。

随后在新目录冻结 [协议](../research/identity_conformance/protocol.json)：四个先前已经暴露的官方程序，每个使用原名、普通不透明名、`.elf`、`.sh` 四种名称，各运行两次；32 个新鲜 QEMU 客体和 32 个 Linux 原生运行。这是源码引导的探索，选中的分支、文件后缀和旧程序均已公开，不是盲测或随机普遍性样本。

实验没有改内核、原始 C 函数体、系统调用结果或评分脚本。每种架构内的四份程序副本 SHA256、大小和执行权限一致；原 `main(void)` 不使用程序名。静态 C 启动器直接调用 `execve`，避免把 shell 自己的格式回退当内核行为。客体预先安装原有 BusyBox 到加载器所用的 `/musl/busybox`，并使用快照磁盘。PID 比较的是每次运行的身份关系，不要求不同进程的 PID 数字相同。

ELF 的身份由头部格式定义；Linux `execve` 对可执行二进制和 shebang 程序的处理提供了明确的对照范围。[ELF 官方格式](https://gabi.xinuos.com/elf/02-eheader.html)、[execve 手册](https://man7.org/linux/man-pages/man2/execve.2.html)。这些材料与本轮直接执行对照支持这里的变换适用性，不能推出任意路径、动态链接器、权限或资源变换都保持语义。Open Group 的 exec 页面本轮访问失败，没有把未读页面作为已核查依据。

| 原始程序 | Linux 四种名称 | StarryX 原名／普通名／`.elf` | StarryX `.sh` |
|---|---|---|---|
| getpid | 全部完成、满分；外部 PID 一致 | 完成、满分；同轮参考一致 | ELF 被当作文本；原测试未运行、得分 0、命令退出 2 |
| getppid | 全部完成、满分；外部 PPid 一致 | 完成、满分；同轮参考一致 | 同上 |
| uname | 全部完成、满分 | 完成、满分；同轮参考一致 | 同上 |
| dup | 全部完成、满分 | 完成、满分；描述符新分配及最低空闲项一致 | 同上 |

两次重复一致，64 个运行均完成。它们验证的是**一个自然加载器机制**，不能写成四个独立缺陷、32 个缺陷或真实比赛分数变化。这里没有能力移除或成功形态的人工错误注入。

初版分析器漏识别了日志提示符 `# `、原程序 `ppid :` 格式和 END 标记，保守地产生全部 unknown。初版分析、源代码和哈希均保留。另存的 [解析修正](../research/identity_conformance/measurement-parser-v2.json)只修正上述文本读取，没有改实验、阈值、正常控制要求、原判定或假设，也没有重跑单元；[第二版分析](../research/identity_conformance/analysis-v2.json)将它与预先代码区分。修正不是新的确认实验。

## 对适用条件的主动反证

发现结果后单独冻结 [格式对照协议](../research/identity_controls/protocol.json)，执行 24 个单元：ELF、合法 shebang 脚本、无头纯文本，原名／`.sh`，Linux／StarryX，各两次。这是发现后的机制诊断，不是新的独立发现。

- ELF 原名在两环境执行；`.sh` 在 Linux 执行，在 StarryX 未执行，退出 2。
- shebang 脚本在两环境、两种名称均执行成功，排除了此环境一般无法运行 shell 脚本的解释。
- 无头纯文本原名在两环境直接 exec 失败，errno 8；`.sh` 在 Linux 仍失败，而 StarryX 将其作为文本执行。它不满足“已识别的可执行 ELF 应继续执行”的正向适用条件，保留为 inapplicable，不能用它证明任意改名都应成功。

[格式对照分析](../research/identity_controls/analysis.json)包含所有单元。没有从源码复核问题推断作者作弊意图，也没有向作者发送报告或改写其内核。

## 强基线和当前取舍

普通 ELF／exec 规格检查和合法后缀变形测试得到相同判断。源码引导在本例定位了一个值得执行的条件，但还没有证明同预算下比通用变形测试、普通兼容性测例或人工复核发现更多问题。不会用弱随机字符串基线把 `.sh` 的命中包装成算法优势。

[MR-Scout 的原始材料及官方实现](https://mr-scout.github.io/)已研究从已有测例构造变形关系；[Juxta 原始论文](https://compsec.snu.ac.kr/papers/min-juxta.pdf)和[官方实现](https://github.com/sslab-gatech/juxta)已研究多实现语义偏差。此次读取了论文、实现说明和检查器种类，没有完整运行 Juxta；它的旧 LLVM／Linux 环境及 C 文件系统对象不能被说成已经在本轮 Rust OS 上公平复现。因此：

1. 保留“规格约束的身份／格式复核”作为实际系统方法及可复现实证工具。
2. 淘汰“仅改执行名称便构成新检测算法”的主张；当前强基线同判定。
3. 保留经验研究问题，但当前独立可运行 OS 数量、独立自然机制、选择偏差和同预算比较仍未满足 TSE／TOSEM 研究的充分证据要求。研究目标继续 active，不能标成已找到顶刊方法，也不能把两项环境 unknown 当彻底失败。

## 生产系统与成本

原扫描器在该文件没有复核线索。现在补充有限的 `.sh` 后缀＋解释器加载调用线索，附带“核对 ELF 内容识别优先级、直接执行复核、不证明测试特化或违规”的说明；单纯后缀日志和内容选解释器不命中新规则。[修改前](../research/identity_conformance/production-scanner-before.json)、[修改后](../research/identity_conformance/production-scanner-after.json)结果均保留。没有把模式命中提升为已证实违规。

`tests/test_identity_signals.py`、`tests/test_runtime_evidence.py` 和 `tests/test_finals_description.py` 共 **101 passed**。这只是工程回归；已有运行判定一致性工具和所有用户原修复保留。

88 个新增单元耗执行墙钟 23.058182 秒、子进程 CPU 29.706555 秒；另有 128 次原始 public judge 进程，墙钟 3.036382 秒、CPU 2.954881 秒。源码取得、工具链、依赖准备、编译、失败启动和归档不包含在这 23 秒中，分别在准备账本记录。与先前阶段合计 **392 个实际运行单元**，含全部失败和 unknown，不是独立研究样本数，也不是正式比赛样本数。

[新阶段完整审计](../research/identity_conformance/final-audit.json)及[证据页](../research/identity_conformance/evidence.html)连回原始文件。
