# Qwen3-4B / SGLang / BW1100 单卡启动验证

以下配置来自 2026-11-17 的专用容器实测，不是所有海光环境的通用启动保证。参考
[海光 inference-cookbook 的 Qwen3 文档](https://github.com/HYGON-AI/inference-cookbook-das/blob/main/docs/model-deployment/sglang/qwen3.md)
中 Qwen3-4B、BW1100 单卡、SGLang 0.5.18 的配置。

## 已核对环境

- 镜像：`sha256:4560bce63036ebe8fe19f2590b0ffeb90e2f1336671d84fd0984a2ad72298e21`。
- SGLang：`0.5.18+dtk2604.torch2110.2611111158.ga98b1b`；镜像标注 DTK 26.04.1。
- Torch 2.11，服务端 Python 3.10；客户端独立 Python 3.11.18。
- BW1100 单张物理卡，驱动 `6.4.2-V2.1.0`；Qwen3-4B BF16、TP1。
- 权重和完整引擎源码 commit 尚未核实。镜像摘要、已安装版本和 tokenizer 哈希已保存，但不冒充完整源码版本。

## 隔离与启动要求

先核实获准物理卡的 PCI BDF 与 DRM 节点，只映射该卡的 render/card 节点以及运行所需的 `/dev/kfd`、`/dev/mkfd`，不直接映射整个 `/dev/dri`。必须在实际容器中确认 `torch.cuda.device_count() == 1` 且设备名称正确。单独设置可见卡环境变量不能替代设备隔离。

本次容器只看见一张物理卡，因此使用 `HIP_VISIBLE_DEVICES=0`、`CUDA_VISIBLE_DEVICES=0`；不能再把物理卡号填入 `ROCR_VISIBLE_DEVICES`。首次这样设置时实际设备初始化失败，失败记录已保留。

服务端限制为 8 CPU、32 GiB 内存、8 GiB shm；客户端 4 CPU、8 GiB 内存，CPU 集互不重叠。模型目录和 `/opt/hyhal` 只读挂载，结果写入专用工作目录。服务只监听 loopback，不暴露公共推理端口。

使用 cookbook 的 HCU 优化环境变量，另按本次容器隔离设置 `SGLANG_SET_CPU_AFFINITY=0`、`OMP_NUM_THREADS=4` 和独立缓存目录。没有执行修改主机全局状态的 `sysctl` 命令。完整实际脚本与容器配置保存在私有验证工件，不含口令。

模型启动的核心参数如下（`/model` 为只读挂载位置，端口需先确认空闲）：

```bash
python3 -m sglang.launch_server \
  --model-path /model --served-model-name Qwen3-4B \
  --host 127.0.0.1 --port 18460 --tp-size 1 --dtype bfloat16 \
  --context-length 4096 --max-running-requests 8 --max-total-tokens 16384 \
  --page-size 64 --mem-fraction-static 0.3 --disable-radix-cache \
  --attention-backend fa3 --disable-cuda-graph --disable-overlap-schedule \
  --skip-server-warmup --tool-call-parser qwen3_coder --reasoning-parser qwen3
```

这段只列核心参数，不能省略 cookbook 中与实际版本配套的环境设置。首次缺少 `--page-size 64` 时，gfx936 FlashAttention 因 KV block 大小要求拒绝启动。

相对 cookbook，本次降低 KV 预算和最大并发、禁用 radix cache，以约束共享节点资源并统一跨工具缓存条件；保留 graph/overlap 关闭设置。没有据此宣称模型的最优吞吐。内置 Qwen3 架构不额外启用 `--trust-remote-code`。

## 健康检查与 token 口径

先检查 `/v1/models`，再执行 10 请求流式冒烟。正式负载使用 `token_count_source: server`，对应上游原生 `--use-server-token-count`，不修改引擎分词或时延算法。

本次合成输入目标 128 token 经 chat 模板后，服务端 usage 为 136；输出强制 128。客户端对返回推理文本重新分词曾得到 127，不把它直接冒充服务端生成数。两种口径单独保留，首次不满足长度检查的结果没有覆盖。

Qwen3 思考内容在 128 token 上限内可能没有产生非推理答案；有 TTFT 不代表有 TTFO。缺失 TTFO 保持未测量，不填零，也不据此判断模型回答质量。

## 运行保护

启动前检查卡上分配，启动和守护由同一进程管理；运行中按物理 BDF 检查每个进程显存。一旦其他任务在该卡分配显存，或无法可靠查询，停止本次自有服务和客户端，不停止 CI 或他人容器。显存守护仅用于资源保护，不替代 hcu-exporter 的四项性能遥测。

正式每轮 16 次预热加 128 次单并发请求需要超过 600 秒；经用户批准，本次配置使用 900 秒故障上限。提前完成立即结束，超时保留工件并停止后续点。配置生成器继承 `workload.timeout_s`，不会再写死 600 秒。

## 与 EvalScope 交叉检查的指标边界

固定 AIPerf v0.13.0 和 EvalScope v1.13.0，不凭指标名称直接认定两者完全等价。

| 项目 | AIPerf 当前源码 | EvalScope 当前源码 |
|---|---|---|
| TTFT | 请求开始到首个有内容的响应 | 请求开始到首个有输出的流式事件，包含 reasoning 字段 |
| 请求延迟终点 | 最后一个有内容的响应，不含纯 usage 事件 | 最后一个解析的 JSON 事件，可含 usage |
| AIPerf ITL 对应关系 | 解码窗口 / 后续输出 token 数；本次无逐块 usage，分母为输出长度减 1 | 更接近 TPOT；原生 ITL 是输出事件的块间间隔统计，不能直接当成同一口径 |
| 输出计数 | 本次显式选 server usage | 本次使用响应 usage；需核对逐请求计数 |

实现依据：
[AIPerf ITL](https://github.com/ai-dynamo/aiperf/blob/794f8bb75f8582f22e412d7e650fc71ca2a3d21a/src/aiperf/metrics/types/inter_token_latency_metric.py)、
[AIPerf 请求延迟](https://github.com/ai-dynamo/aiperf/blob/794f8bb75f8582f22e412d7e650fc71ca2a3d21a/src/aiperf/metrics/types/request_latency_metric.py)、
[EvalScope 统计](https://github.com/modelscope/evalscope/blob/v1.13.0/evalscope/perf/utils/benchmark_util.py)、
[EvalScope 流式接收](https://github.com/modelscope/evalscope/blob/v1.13.0/evalscope/perf/plugin/api/default_api.py)。本次同时核对了已安装固定版本源码。

跨工具复用 AIPerf `inputs.json` 的完整请求对象，不重新生成另一组随机文本。EvalScope 使用独立环境、原生 `line_by_line` 数据集；请求构造预检只允许额外显式指定 `temperature: 0.6`，与本地模型的 `generation_config.json` 默认值一致。不修改任何上游工具源码。缺少服务端 usage、请求失败或输出约束不满足时不能以进程退出 0 宣称通过。
