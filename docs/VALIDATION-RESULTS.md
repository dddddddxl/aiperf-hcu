# 首版软件验收记录

本文保留首次离线验收时的状态。后续获得用户授权后的实机结果见 [2026-11-17 实机验证](HCU-VALIDATION-20261117.md)；不能将下面的历史“尚未开始”当成当前状态。

日期：2026-11-17。结论：**软件离线验证通过；真实 HCU 验证尚未开始。**

用户明确实机资源后续提供，本轮没有登录历史节点、占卡、启动推理服务或请求付费模型接口。

## 版本与范围

- 代码验证提交：`93ba2e4722cf299d3b0def310f73c6bc6e96785e`。
- 私有独立仓库：`dddddddxl/aiperf-hcu`，开发分支 `codex/hcu-adaptation-v1`；未合并 main、未公开、未迁入组织。
- AIPerf v0.13.0：`794f8bb75f8582f22e412d7e650fc71ca2a3d21a`。
- hcu-exporter 参考：`30cfbc1031957737731725ceddb663bc508d7c96`，指标定义核对其 hcu-dcgm v3.0.2 依赖。
- Python 3.11.18，uv 0.13.8，依赖固定在 `uv.lock`。

## 已交付

| 项目 | 结果 |
|---|---|
| doctor/run/report/compare | 已实现；doctor 不发送生成负载 |
| 配置与鉴权 | 严格 YAML 字段、环境变量引用、命令参数不含密钥 |
| 上游引擎 | 固定源码身份；未修改调度、分词统计和延迟算法 |
| 本地 tokenizer | 哈希校验、运行专属离线缓存，不下载权重或修改用户缓存 |
| 遥测 | 只读旁路；按 node + PCI BDF 映射功耗/温度/显存/利用率 |
| 工件 | 原始结果、逐轮日志与遥测、manifest/hash、中文报告、机器摘要 |
| 异常 | 超时/中断终止所属子进程；保留不完整结果，拒绝覆盖旧目录 |
| 比较 | 条件差异与未知身份阻止提升率计算，不设置自动放行阈值 |
| 测试矩阵 | 生成冒烟、固定并发、Poisson 和干扰对照配置；不自动执行 |

## Linux CPU 和安装验证

[完整 CI 记录](https://github.com/dddddddxl/aiperf-hcu/actions/runs/44020320941)。

- 锁定依赖的普通安装：全部 **40 项**测试通过，零跳过。
- 构建 wheel 和源码包：通过。
- 新建独立 `.clean` 环境，从锁文件导出依赖并安装构建出的 wheel：通过。
- 干净安装中验证固定引擎身份、CLI，再跑全部测试：**40 passed in 38.65s**，零跳过。
- 测试父进程和 AIPerf 子进程禁止非 loopback 网络连接；安装阶段仍需访问包源。
- 使用 GitHub 托管 Linux CPU runner，不占 HCU runner。没有定时硬件压测。
- Windows Python 3.11.18 本地全量复验：**40 passed in 258.40s**，零跳过；本地证据 `artifacts/f10.xml`。平台启动耗时不同，不作为吞吐性能比较。
- 后续成功 CI 提供 `aiperf-hcu-cpu-validated` 下载包（保留 7 天）：wheel、源码包、依赖锁和测试摘要；不上传完整运行日志/业务输入。

35 项配置/错误处理/遥测/工件测试，5 项真实 AIPerf 子进程集成测试。集成测试涵盖流式、非流式、原版直接调用对照、鉴权下正常与失败响应、预热与 Poisson、流式断连。超时与中断用受控子进程验证，不伪称硬件故障实测。

原版对照复用同一参数构建器与离线 tokenizer 桥接，绕过适配 run 生命周期：验证成功请求数、实际输入/输出长度相同。它不是独立实现的第二个压测工具，也不能替代后续 EvalScope 交叉验证。墙钟延迟不要求跨次相等。

## 调试发现及处理

1. `--warmup-request-count 0` 上游不接受：零预热时不传该参数。
2. 上游离线 resolver 不直接接受本地 tokenizer 目录：复制已校验文件到每次运行独立的 HF 格式缓存，避免联网与用户缓存变更。
3. CLI API key 字符串不展开环境变量：使用上游 YAML 环境引用；校验实际鉴权成功且工件不包含测试密钥。
4. 上游无错误时省略 ERROR_ONLY 计数：仅在明确完成、计划请求全部成功、无错误摘要时推定零错误，并显式注明；不改原始文件。
5. 带预热时存在总报告和阶段报告：只读固定路径的总报告，不能混入 warmup 请求。
6. Windows 缓存路径过长：去除路径中重复哈希，保留运行隔离及内容版本。
7. 遥测写入/停止异常：显式降级并保留报告和 manifest，不静默判完整。

首次失败 CI 和本地失败工件保留，没有改写成通过记录。本地 `artifacts/` 不提交，避免泄露路径和未来业务输入。

## 可复现命令

```bash
uv sync --locked --python 3.11 --no-editable
uv run --no-sync pytest -q
uv build
```

wheel 需要安装锁定的 Git 版 AIPerf 依赖，而不是任意同版本 PyPI 包：

```bash
uv venv .clean --python 3.11
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file dist/dependencies.txt
uv pip sync --python .clean/bin/python dist/dependencies.txt
uv pip install --python .clean/bin/python --no-deps dist/*.whl
.clean/bin/aiperf-hcu --help
```

Windows 对应使用 `.clean/Scripts/python.exe`、`.clean/Scripts/aiperf-hcu.exe`。

## 仍待完成，不能视为首版完整实机验收

- Qwen3-4B BF16、海光版 SGLang、单张物理卡的实际兼容性及启动说明。
- 10 请求冒烟、固定负载三档、Poisson 三档及各点重复实验。
- 固定 EvalScope 版本后的相同请求交叉核对。
- 开关遥测、增加客户端 CPU 配额的测量干扰检查。
- 核验实际 exporter 版本、设备归属、采集健康与时间新鲜度。参考 exporter 未保证输出源时间戳；没有证据时仍为未知，不能仅靠 HTTP 200 或零值宣称完整。
- vLLM、其他型号/量化/镜像均未实测，不做通用兼容承诺。
- 公开前还需检查依赖许可、私有地址/路径和业务数据，并确认维护人与支持矩阵。

拿到资源窗口后按 [验证协议](VALIDATION-PROTOCOL.md) 执行；不沿用历史节点授权，不扩大资源范围。
