# AIPerf HCU

面向海光推理服务的独立 AIPerf 适配层。固定上游版本，不修改压测调度或指标算法。

开发状态：私有首版；真实 HCU 验证等待资源，模拟结果不能代表硬件性能。

计划提供 `doctor`、`run`、`report`、`compare`，旁路采集 hcu-exporter 指标。
不部署模型、不重启服务、不自动占卡，不修改现有 hcu-bench。
