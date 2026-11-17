import json
import math
from pathlib import Path
import statistics

from . import AIPERF_VERSION
from .io import verify, write_json

METRICS = {
    "time_to_first_token": "首个 token 时间 TTFT",
    "time_to_first_output_token": "首个非推理输出时间 TTFO",
    "inter_token_latency": "token 间延迟 ITL",
    "request_latency": "完整响应耗时",
    "output_token_throughput": "输出 token 吞吐",
    "request_throughput": "请求吞吐",
    "input_sequence_length": "输入长度（所选计数口径）",
    "output_sequence_length": "输出长度（所选计数口径）",
    "usage_prompt_tokens": "服务端 usage 输入长度",
    "usage_completion_tokens": "服务端 usage 输出长度",
    "request_count": "成功请求数",
    "error_request_count": "失败请求数",
}


def read_upstream(directory, expected_requests, expected_output=None):
    # v0.13 additionally exports per-phase files (including warmup). Only the
    # canonical root aggregate describes the requested profiling result.
    canonical = Path(directory) / "profile_export_aiperf.json"
    files = [canonical] if canonical.is_file() else []
    if len(files) != 1:
        return {"status": "incomplete", "reason": "missing_or_ambiguous_upstream_export", "metrics": {}}
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"), parse_constant=lambda _: None)
        metrics = {k: v for k, v in data.items() if isinstance(v, dict) and "unit" in v}
        for v in metrics.values():
            for key, value in v.items():
                if isinstance(value, float) and not math.isfinite(value):
                    v[key] = None
        count = metrics.get("request_count", {}).get("avg")
        errors = metrics.get("error_request_count", {}).get("avg")
        complete = data.get("is_complete") is True and not data.get("was_cancelled") and data.get("aiperf_version") == AIPERF_VERSION
        # v0.13 ERROR_ONLY counters are absent on zero errors. Infer zero ONLY
        # with explicit completion, all planned successful requests and no error summary.
        zero_inferred = errors is None and complete and count == expected_requests and not data.get("error_summary")
        count_ok = count == expected_requests and (errors == 0 or zero_inferred) and not data.get("error_summary")
        length = metrics.get("output_sequence_length", {})
        length_ok = expected_output is None or length.get("min") == expected_output == length.get("max")
        return {"status": "complete" if complete and count_ok and length_ok else "incomplete", "metrics": metrics,
                "token_count_source": "server" if data.get("input_config", {}).get("endpoint", {}).get("use_server_token_count") else "client",
                "upstream_complete": complete, "count_verified": count_ok,
                "zero_errors_inferred_from_complete_count": zero_inferred, "output_length_verified": length_ok if expected_output else None,
                "reason": None if complete and count_ok and length_ok else "upstream_incomplete_or_count_or_length_unverified",
                "schema_version": data.get("schema_version"), "aiperf_version": data.get("aiperf_version")}
    except (ValueError, TypeError, AttributeError):
        return {"status": "incomplete", "reason": "invalid_upstream_export", "metrics": {}}


def markdown(summary):
    lines = ["# 海光推理性能评测报告", "", f"执行状态：{summary['status']}",
             "", f"实验来源：{summary.get('experiment_source', 'unknown')}（synthetic 表示模拟服务，不是 HCU 性能实测）",
             "", "这是性能测量记录，不是模型精度或自动放行结论。硬件兼容性仅限实际记录的环境。",
             "", "| 轮次 | 执行 | 指标完整性 | 遥测 |", "|---|---|---|---|"]
    for r in summary["runs"]:
        lines.append(f"| {r['repeat']} | {r['execution']} | {r['upstream']['status']} | {r['telemetry']['status']} |")
        lines.extend(["", f"## 第 {r['repeat']} 轮", "", "| 指标 | 单位 | 均值 | p50 | p95 | p99 |", "|---|---|---|---|---|---|"])
        for key, label in METRICS.items():
            m = r["upstream"]["metrics"].get(key, {})
            vals = [m.get(k) for k in ("unit", "avg", "p50", "p95", "p99")]
            lines.append("| " + " | ".join([label] + [str(v) if v is not None else "未测量" for v in vals]) + " |")
        lines.extend(["", f"Token 计数口径：{r['upstream'].get('token_count_source', 'unknown')}；client 为返回文本分词/原始提示词，server 为服务端 usage。两者可能因 chat 模板、特殊 token、推理解析而不同，不混用吞吐口径。"])
        lines.extend(["", f"遥测：{r['telemetry']['observed']} 个有效观测，{r['telemetry']['problems']} 个异常/缺失；新鲜度未知 {r['telemetry']['freshness_unknown']}。", ""])
        lines.extend(["| 设备（node / PCI） | 指标 | 单位 | 最小 | 平均 | 最大 |", "|---|---|---|---|---|---|"])
        for stat in r["telemetry"].get("statistics", []):
            device = stat["device"]
            fields = [f"{device['node']} / {device['pcieBus_number']}", stat["metric"], stat["unit"], stat["min"], stat["mean"], stat["max"]]
            lines.append("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in fields) + " |")
        if r["upstream"].get("zero_errors_inferred_from_complete_count"):
            lines.append("上游省略 ERROR_ONLY 计数：根据明确完成状态、全部计划请求成功及无错误摘要推定失败数为 0（原始字段未改写）。")
    lines += ["## 限制", "", "- 上游原始工件未改写，保存在各轮 raw 目录；日志单独脱敏。",
              "- 遥测跨整个子进程生命周期，包含启动和预热，不宣称等同于正式请求阶段。",
              "- 未提供源时间戳时，不把本地抓取时间当成设备采样时间。",
              "- 没有运行硬件测试的环境，只能报告模拟验证。", ""]
    return "\n".join(lines)


def report(root):
    root = Path(root)
    verify(root)
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    return markdown(summary)


def compare(baseline, candidate):
    a, b = verify(baseline), verify(candidate)
    sa = json.loads((Path(baseline) / "summary.json").read_text(encoding="utf-8"))
    sb = json.loads((Path(candidate) / "summary.json").read_text(encoding="utf-8"))
    differences = []
    def walk(x, y, prefix=""):
        if isinstance(x, dict) and isinstance(y, dict):
            for key in sorted(x.keys() | y.keys()):
                walk(x.get(key), y.get(key), f"{prefix}.{key}".strip("."))
        elif x != y:
            differences.append({"field": prefix, "baseline": x, "candidate": y})
    walk(a["comparison_identity"], b["comparison_identity"])
    required = ("model_revision", "engine_commit", "image", "dtk", "driver", "card_model", "quantization", "parallelism", "cache_policy")
    missing_identity = {side: [key for key in required if m["comparison_identity"].get("environment", {}).get(key) in (None, "", "unknown")]
                        for side, m in (("baseline", a), ("candidate", b))}
    valid = all(s.get("status") == "completed" and s["runs"] and all(r["execution"] == "completed" and r["upstream"]["status"] == "complete" for r in s["runs"]) for s in (sa, sb))
    valid = valid and not any(missing_identity.values())
    stats = {}
    for key in METRICS:
        values = [[r["upstream"]["metrics"].get(key, {}).get("avg") for r in s["runs"]] for s in (sa, sb)]
        if all(v and all(isinstance(n, (int, float)) and math.isfinite(n) for n in v) for v in values):
            ma, mb = [statistics.mean(v) for v in values]
            stats[key] = {"baseline_mean": ma, "candidate_mean": mb,
                          "baseline_std": statistics.stdev(values[0]) if len(values[0]) > 1 else None,
                          "candidate_std": statistics.stdev(values[1]) if len(values[1]) > 1 else None,
                          "change_percent": (mb-ma)/ma*100 if valid and not differences and ma else None}
    return {"comparable": valid and not differences, "differences": differences, "missing_identity": missing_identity, "metrics": stats,
            "automatic_gate": False, "limitation": "不合并各轮分位数；轮次不足时标准差未测量；未校准性能放行阈值。"}
