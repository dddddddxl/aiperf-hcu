# AIPerf HCU

用同一批请求测两件事：**模型回答得有多快，以及回答时海光卡用了多少功耗和显存。** 例如，把并发从 1 调到 8，可以比较整体吞吐是否上升、用户等第一个 token 的时间是否变长；如果已部署海光监控，还能在同一份中文报告中查看目标卡的功耗、温度、显存和利用率。

**当前版本：** `0.1.0a2`，私有预览。实测组合为 **BW1100 单卡 / SGLang 0.5.18 / Qwen3-4B BF16 / 流式 Chat Completions**。这是压测客户端，不是推理引擎：开始前需要已有可用的模型服务。本工具不部署或重启模型、不分配卡，也不替换现有 hcu-bench。

| 分工 | 具体做什么 |
|---|---|
| [上游 AIPerf v0.13.0](https://github.com/ai-dynamo/aiperf/tree/v0.13.0) | 生成请求、控制并发和到达速率、记录响应、计算吞吐与延迟分位数。适配层锁定上游提交，不重写这些算法。 |
| 本仓库 | 提供统一配置和 `doctor / run / report / compare`，加载本地 tokenizer，校验请求数与 token 计数，保存可复验的原始结果和中文报告。 |
| [hcu-exporter](https://github.com/HYGON-AI/hcu-exporter)（可选） | 在推理节点暴露硬件指标。压测期间由本仓库的采集器读取，按节点与 PCI 地址选中实际跑模型的卡；不使用 NVIDIA GPU 遥测。 |

本入口当前支持文本聊天、固定或变长输入、并发及指定请求速率。不将上游的 Embedding、图像/音频、生产流量回放和实时终端界面列为海光入口已支持功能；vLLM、单机多卡和多节点推理也不在本版实测范围内。

若要了解适配改了哪些地方、为何同时保留 EvalScope，以及哪些结论还不能下，见[项目概览与工具对比](docs/PROJECT-OVERVIEW-ZH.md)。

## 已经验证了什么

| 验证 | 结果与边界 |
|---|---|
| [安装与软件回归](docs/HCU-VALIDATION-20261117.md) | Linux CPU CI 的普通安装和干净 wheel 安装各通过 46 项测试，零跳过。使用本地模拟服务，不把模拟结果当成 HCU 性能。另见[首版安装验收](docs/VALIDATION-RESULTS.md)。 |
| [单卡压测矩阵](docs/HCU-VALIDATION-20261117.md) | 2026-11-17：固定并发 1/4/8、变长输入 Poisson 目标 1/2/4 req/s，共 18 轮、2,304 个正式请求，全部通过计数与长度核对。计入 EvalScope 和客户端 CPU 配额对照后为 3,072 个；不是 3,072 个都由 AIPerf 发送。 |
| [性能与功耗联合测量](docs/HCU-TELEMETRY-AB-20261118.md) | 2026-11-18：并发 8，三对开关实验、768 个正式请求。关闭/开启采集时，输出吞吐分别为 212.068 / 212.077 token/s，开启组平均 TTFT 高 0.90 ms。三轮开启组的平均卡功耗为 293.54～296.32 W；这是整轮卡功耗，不是单个请求的能耗。 |

## 安装

需要 Python 3.11、Git 和 uv 0.13.8。客户端环境与 SGLang/vLLM 服务环境分开；不需要客户端安装 HCU 驱动或模型权重。

```bash
git clone https://github.com/dddddddxl/aiperf-hcu.git
cd aiperf-hcu
git switch codex/hcu-adaptation-v1
uv sync --locked --python 3.11 --no-editable
uv run --no-sync aiperf-hcu --help
```

使用 `--no-editable` 安装包以避免 Windows Python 3.11 在中文路径下的 editable `.pth` 编码问题。
首次安装需访问源码与包源，测试本身只访问本地模拟服务。`uv.lock` 固定依赖与上游 Git 提交；不要只执行无锁 `pip install aiperf-hcu`。
上游仍依赖 `nvidia-ml-py` Python 包，安装该包不等于必须存在 NVIDIA GPU；本项目显式关闭上游 GPU 遥测，由海光旁路采集替代。

## 第一次运行：先只测性能

以下为 Bash 示例。先准备一个已获准压测的文本推理服务，以及客户端可读取、与模型匹配的 tokenizer 目录。要复现已测组合，可参考 [SGLang 单卡启动说明](docs/SGLANG-QWEN3-BW1100.md)。不需要 exporter 就能完成这一步。

```bash
cp configs/sglang-smoke.yaml configs/bench.local.yaml
```

编辑复制出的文件，保留模板的 `workload` 和 `environment`，替换下面两个**完整配置段**，不要把同名段追加到文件末尾：

```yaml
service:
  url: http://127.0.0.1:30000    # 换成从客户端可达的服务根地址，不带 /v1
  model: Qwen3-4B                 # 必须与该服务 /v1/models 返回的 id 一致
  tokenizer: /models/Qwen3-4B     # 客户端上的本地目录，不是服务容器内的路径
  streaming: true
  token_count_source: server      # 实测 SGLang 支持流式 usage；其他服务需先确认
telemetry:
  enabled: false
```

如服务需要鉴权，在 `service` 中设置 `api_key_env` 为环境变量名，密钥只放在客户端环境中。按实际情况填写 `environment` 里的模型版本、引擎提交、镜像、DTK、驱动、卡型、并行度和缓存策略。不知道的字段保留 `unknown`，可以冒烟，但不能给出可信的性能提升百分比。

```bash
uv run --no-sync aiperf-hcu doctor --config configs/bench.local.yaml --output artifacts/first
uv run --no-sync aiperf-hcu run --config configs/bench.local.yaml --output artifacts/first
uv run --no-sync aiperf-hcu report --input artifacts/first
```

模板的冒烟负载是 **并发 1、10 个正式请求、输入/输出目标各 128 token**。先看 `doctor` 的 `ok`，通过后再执行 `run`。结果写入新目录 `artifacts/first`；重跑时换目录，不用删除首次结果。

### 进一步：改负载或比较两次运行

| 想回答的问题 | 如何调整本地配置 |
|---|---|
| 并发升高后，吞吐和延迟怎么变？ | 分别设 `workload.concurrency` 为 1、4、8，每个点用独立输出目录。比较各报告中的吞吐和延迟，不把不同并发的结果当成同条件性能回归。 |
| 输入长短不一、请求间隔不固定时会怎样？ | 在 `workload` 中设 `input_tokens: 512`、`input_stddev: 128`、`arrival_pattern: poisson`、`request_rate: 2`、`concurrency: 8`。2 req/s 是目标，不是已达到的速率；并发打满后实际派发可能更慢。 |
| 同一配置跑两次，结果波动多大？ | 设置 `repeats: 3` 可在一次运行内获得三轮结果。要比较两次独立实验，第二次运行指定 `--output artifacts/second`，再执行下方命令。 |

```bash
uv run --no-sync aiperf-hcu compare --baseline artifacts/first --candidate artifacts/second
```

`compare` 输出 JSON。先看 `comparable`；若为 `false`，看 `differences` 和 `missing_identity`，不要把均值差写成提升百分比。遥测设置不在自动比较条件中，比较硬件指标前需自行核对物理卡、采样周期和采集窗口。

- `doctor` 读取 `/v1/models` 和可选 `/metrics`，不发送生成请求；不创建输出目录。健康检查不代表性能或遥测新鲜度验收。
- `run` 串行重复单个配置，输出目录必须不存在；失败、中断、超时、计数或强制输出长度不符合要求时停止后续轮次。600 秒默认值是显式故障上限。
- 不自动下载 tokenizer；校验本地文件哈希，复制到本轮专属离线缓存再由原版 AIPerf 加载。上游 v0.13.0 的离线解析器不直接接受本地路径，此兼容处理不改其核心代码。
- 原始请求计划和真正执行命令分别保存；鉴权使用上游 YAML 环境变量引用，不把密钥放在命令参数。原始工件可能含业务输入/响应，整个输出目录视为私有数据。
- `report`/`compare` 校验工件哈希后只向 stdout 输出，不改写首次运行。配置身份不匹配或关键环境信息未知时不计算性能提升百分比。
- `run` 退出 0 仅表示压测执行与计数/长度校验完成；不代表遥测完整、性能放行或硬件认证。其他失败退出 2；telemetry_complete 单独显示。

## 拿到报告后看什么

每次运行保存 `manifest.json`、`summary.json`、中文 `report.md`；各 `run-NNN/` 保存 `raw/` 原版工件、`telemetry.jsonl`、脱敏进程日志、命令和本地 tokenizer 缓存。每轮结束写检查点；不覆盖旧目录。

| 先看什么 | 怎么读 |
|---|---|
| 这次测完了吗？ | 看 `summary.json` 的 `status` 和每轮的 `upstream.status`。如果是 `incomplete`，先查该轮的 `reason`（在 `upstream` 下）、`error_type` 和 `process.log`，不要拿不完整结果算提速。遥测是否完整另看 `telemetry_complete`。 |
| 用户要等多久？ | 看 TTFT（首个 token）、TTFO（首个非推理输出）和完整请求延迟。推理模型可能先输出推理内容，TTFT 小不等于用户很快就看到了正式答案。看 p95/p99 时同时看请求数：10 个请求用于冒烟，不用于给长尾定指标。 |
| 服务每秒做多少工作？ | 输出吞吐是所有请求合计生成的 token/s；请求吞吐是每秒完成多少个请求。比较前核对实际输入/输出长度和计数口径。 |

可先打开[真实单卡机器摘要](docs/hcu-validation-summary.json)看一份已验证的数据；[模拟摘要](docs/mock-example.json)只用于理解格式。

TTFT 与 TTFO 分列；非流式不补造首 token/逐 token 时间。保留上游分位数，不把多轮 p99 做平均后冒充整体 p99。
上下文长度目标与 chat 模板后的实际输入长度可能不同，输出以原始测量为准。

`service.token_count_source` 默认 `client`（兼容原版行为）；可显式设为 `server`，转发上游原生 `--use-server-token-count`，采用服务端 usage 计算长度/吞吐。缺少 usage 不回退或填零。报告同时列出 usage 指标；计数口径纳入比较身份。Qwen3 推理输出的返回文本分词可能与服务端生成 token 数不同，不能把其中一种口径重命名成另一种。

## 同一轮同时测性能与硬件：开启海光遥测

先在推理节点准备好 hcu-exporter，确认压测客户端能访问其 Prometheus `/metrics`。一个节点级实例可供多轮压测复用，本工具不会自动安装它。保留上述服务和负载配置，把 `telemetry` 段替换为：

```yaml
telemetry:
  enabled: true
  url: http://127.0.0.1:16080/metrics  # 示例：仅当客户端能从此地址访问 exporter 时有效
  interval_s: 1
  devices:
    - node: "REPLACE_WITH_EXPORTER_NODE_LABEL"
      pcieBus_number: "REPLACE_WITH_PHYSICAL_PCI_BDF"
      service_device: "0"
```

先核对服务使用的物理卡，再从 exporter 样本复制该卡的 `node` 与 `pcieBus_number` 标签；容器内的逻辑卡号 `0` 不是物理身份。重新执行 `doctor`，用新输出目录执行 `run`。一次运行就会生成带性能和硬件两组指标的 `report.md`；开启采集时，每轮的 `telemetry.jsonl` 可用于追查缺失样本。

固定参考 exporter `30cfbc1`：`hcu_power_usage`（W）、`hcu_temp`（C）、`hcu_usedmemory_bytes`（bytes）、`hcu_utilizationrate`（瞬时 CU %）。只按明确的 node + PCI BDF 物理卡映射采集；不支持 vHCU。

采样覆盖子进程生命周期，包含启动/预热，不冒充仅 profiling 区间。没有源时间戳时保留“新鲜度未知”；可用 `timestamp_metric` 指定同设备标签、以秒表示的源采样时间指标，但**固定参考 exporter 不保证提供该指标**，不得填一个本地时间伪装来源。模拟服务的时间戳只用于回归。

exporter 的某些底层错误可能产生 0 值，适配层不能从一个零值反推出设备正常。真实验收须核对采集端健康及日志。映射依据和实测流程见 [验证协议](docs/VALIDATION-PROTOCOL.md)。

## 测试、打包与限制

```bash
uv run --no-sync pytest -q
uv build
```

测试包含固定 AIPerf 的真实子进程，仅连接 loopback 模拟服务；不会执行目标模型。Linux CPU CI 不使用 self-hosted runner，不运行定期 HCU 压测。
干净安装使用 `uv.lock` 导出的上游依赖后再安装 wheel，CLI 会拒绝缺少固定源码身份的 AIPerf 安装。

单卡验证范围仅限上述模型和镜像。EvalScope 的请求一致性检查已通过；跨工具和 CPU 对照的细小延迟偏移仍待归因。真实 exporter 轻量遥测和并发 8 的开关对照已完成，但无逐样本源时间戳，也没有建立自动性能放行阈值；vLLM 和其他组合未经实测。
后续公开或迁入 HYGON-AI 需再次检查私有路径、凭据、数据集与依赖许可，见 [第三方声明](THIRD_PARTY_NOTICES.md)。
