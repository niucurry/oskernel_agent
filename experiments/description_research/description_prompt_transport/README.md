# 描述提示传递缺陷：实际 OpenCode 对照

生产配置曾把自定义提示放在 `agent.system`；OpenCode 1.14.48 官方配置使用 `agent.prompt`，旧字段没有传递给模型。现已修正配置生成器，并核对全部描述会话提示。

`probe.py` 使用原生产 setup、命令和环境，分别启动原 OpenCode 两次，MCP 在本传输对照中关闭。两组唯一差别是 system/prompt 字段。后端仅为本机 loopback 固定回复，不读取研究私有配置，不进行模型推理。旧字段组 2 次本地 HTTP 请求，预期 7,908 字符系统提示未送达；新字段组 2 次，完整提示及 SHA 一致。两组用户标记均送达，无超时，退出码 0。远端请求 0；mock usage=0 不计真实 token。

完整配置、原始捕获、输出及协议保存。setup 和 llm_batch 相关回归共 19 项通过，XML 为 related-regression.xml。这些结果仅验证工程传输，不支持报告语义质量或论文效果。

`previous-run-amendment.json` 对第三阶段原始 10 份请求逐个哈希并检查：预期 SUBSYS 系统提示均未出现。第三阶段确实运行了当时生产代码，110,727 token 和交付失败仍有效；但不能称为预期完整提示模板的语义基线。旧原始请求、冻结快照和已归档文件没有改写。

离线复核：读取 results.json 和各组 captured-requests.json，对 expected-system.txt 逐字包含检查。probe.py 会再次启动本地模拟进程，不会访问远端模型；历史旧结果不覆写。官方文档与 schema 的取得记录见 official-sources.json。未来正常运行前需用修正后的 setup 重新生成配置；本工作区没有既有项目 opencode.json 需要迁移。
