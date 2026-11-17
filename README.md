# AIPerf HCU

面向海光推理服务的独立 AIPerf 适配层。固定上游版本，不修改压测调度或指标算法。

开发状态：私有首版；真实 HCU 验证等待资源，模拟结果不能代表硬件性能。当前范围为单节点物理卡上的文本 Chat Completions 服务。

提供 `doctor`、`run`、`report`、`compare`，旁路采集 hcu-exporter 指标。
不部署模型、不重启服务、不自动占卡，不修改现有 hcu-bench。

首版软件与干净安装验收见 [验证结果](docs/VALIDATION-RESULTS.md)，机器可读示例见 [模拟摘要](docs/mock-example.json)。示例不代表硬件性能。

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

## 使用

复制 `configs/sglang-smoke.yaml` 为不跟踪的 `*.local.yaml`，填写服务、tokenizer、环境及物理设备映射。密钥仅设置配置引用的环境变量，不写入文件。

```bash
uv run --no-sync aiperf-hcu doctor --config configs/bench.local.yaml --output artifacts/first
uv run --no-sync aiperf-hcu run --config configs/bench.local.yaml --output artifacts/first
uv run --no-sync aiperf-hcu report --input artifacts/first
uv run --no-sync aiperf-hcu compare --baseline artifacts/first --candidate artifacts/second
```

- `doctor` 读取 `/v1/models` 和可选 `/metrics`，不发送生成请求；不创建输出目录。健康检查不代表性能或遥测新鲜度验收。
- `run` 串行重复单个配置，输出目录必须不存在；失败、中断、超时、计数或强制输出长度不符合要求时停止后续轮次。600 秒默认值是显式故障上限。
- 不自动下载 tokenizer；校验本地文件哈希，复制到本轮专属离线缓存再由原版 AIPerf 加载。上游 v0.13.0 的离线解析器不直接接受本地路径，此兼容处理不改其核心代码。
- 原始请求计划和真正执行命令分别保存；鉴权使用上游 YAML 环境变量引用，不把密钥放在命令参数。原始工件可能含业务输入/响应，整个输出目录视为私有数据。
- `report`/`compare` 校验工件哈希后只向 stdout 输出，不改写首次运行。配置身份不匹配或关键环境信息未知时不计算性能提升百分比。
- `run` 退出 0 仅表示压测执行与计数/长度校验完成；不代表遥测完整、性能放行或硬件认证。其他失败退出 2；telemetry_complete 单独显示。

## 输出

每次运行保存 `manifest.json`、`summary.json`、中文 `report.md`；各 `run-NNN/` 保存 `raw/` 原版工件、`telemetry.jsonl`、脱敏进程日志、命令和本地 tokenizer 缓存。每轮结束写检查点；不覆盖旧目录。

TTFT 与 TTFO 分列；非流式不补造首 token/逐 token 时间。保留上游分位数，不把多轮 p99 做平均后冒充整体 p99。
上下文长度目标与 chat 模板后的实际输入长度可能不同，输出以原始测量为准。

`service.token_count_source` 默认 `client`（兼容原版行为）；可显式设为 `server`，转发上游原生 `--use-server-token-count`，采用服务端 usage 计算长度/吞吐。缺少 usage 不回退或填零。报告同时列出 usage 指标；计数口径纳入比较身份。Qwen3 推理输出的返回文本分词可能与服务端生成 token 数不同，不能把其中一种口径重命名成另一种。

## 海光遥测

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

首版还未完成真实 Qwen3-4B/SGLang/HCU 验证、EvalScope 实机交叉对照及遥测/客户端干扰试验。没有“已经验证”的 vLLM 或服务启动承诺；资源由用户后续提供。
后续公开或迁入 HYGON-AI 需再次检查私有路径、凭据、数据集与依赖许可，见 [第三方声明](THIRD_PARTY_NOTICES.md)。
