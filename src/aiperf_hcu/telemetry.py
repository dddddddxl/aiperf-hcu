"""Read-only physical HCU exporter adapter; never fabricates missing values."""
import json
import math
import threading
import time
import statistics

from prometheus_client.parser import text_string_to_metric_families

from .io import get

# Pinned exporter: low-cost physical HCU metrics. No NVIDIA field aliases.
METRICS = {
    "hcu_power_usage": ("power_w", "W"),
    "hcu_temp": ("temperature_c", "C"),
    "hcu_usedmemory_bytes": ("memory_used_bytes", "bytes"),
    "hcu_utilizationrate": ("utilization_percent", "%"),
}


def parse(text, config, now=None):
    now = time.time() if now is None else now
    samples = [s for f in text_string_to_metric_families(text) for s in f.samples]
    records = []
    for device in config["devices"]:
        matching = [s for s in samples if all(s.labels.get(k) == device[k] for k in ("node", "pcieBus_number"))]
        for name, (key, unit) in METRICS.items():
            found = [s for s in matching if s.name == name]
            row = {"collected_at": now, "device": device, "metric": key, "source_metric": name, "unit": unit,
                   "value": None, "status": "missing", "freshness": "unknown", "source_timestamp": None}
            if len(found) > 1:
                row["status"] = "ambiguous"
            elif found:
                s = found[0]
                value = float(s.value)
                ts = float(s.timestamp) if s.timestamp is not None else None
                if ts is None and config.get("timestamp_metric"):
                    stamps = [x for x in matching if x.name == config["timestamp_metric"]]
                    if len(stamps) == 1:
                        ts = float(stamps[0].value)
                row["source_timestamp"] = ts if ts is not None and math.isfinite(ts) else None
                if not math.isfinite(value) or value < 0 or key == "utilization_percent" and value > 100:
                    row["status"] = "invalid"
                elif ts is not None and (not math.isfinite(ts) or now - ts > config["max_age_s"] or ts > now + 1):
                    row.update(status="stale", freshness="stale")
                else:
                    row.update(value=value, status="observed", freshness="verified" if ts is not None else "unknown")
            records.append(row)
    return records


class Collector:
    def __init__(self, config, path):
        self.config, self.path = config, path
        self.stop_event = threading.Event()
        self.thread = None
        self.error_type = None

    def sample(self):
        try:
            return parse(get(self.config["url"], timeout=3), self.config)
        except Exception as e:
            # HTTP exception strings can contain private hosts; retain type only.
            return [{"collected_at": time.time(), "status": "collection_failed", "error_type": type(e).__name__}]

    def _work(self):
        try:
            self._collect()
        except Exception as e:
            self.error_type = type(e).__name__

    def _collect(self):
        with self.path.open("a", encoding="utf-8") as f:
            while not self.stop_event.is_set():
                started = time.monotonic()
                for row in self.sample():
                    f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                f.flush()
                self.stop_event.wait(max(0, self.config["interval_s"] - (time.monotonic() - started)))

    def start(self):
        self.path.touch(exist_ok=False)
        if self.config["enabled"]:
            self.thread = threading.Thread(target=self._work, daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError("telemetry_shutdown_timeout")
        if self.error_type:
            raise RuntimeError("telemetry_writer_failed:" + self.error_type)


def summarize(path, enabled):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    observed = [r for r in rows if r["status"] == "observed"]
    problems = [r for r in rows if r["status"] != "observed"]
    unknown = sum(r.get("freshness") == "unknown" for r in observed)
    times = sorted(set(r["collected_at"] for r in rows))
    groups = {}
    for r in observed:
        key = (r["device"]["node"], r["device"]["pcieBus_number"], r["metric"])
        groups.setdefault(key, []).append(r)
    statistics_rows = []
    for _, series in sorted(groups.items()):
        values = [r["value"] for r in series]
        statistics_rows.append({"device": series[0]["device"], "metric": series[0]["metric"], "unit": series[0]["unit"],
                                "count": len(values), "min": min(values), "max": max(values), "mean": statistics.mean(values)})
    return {"enabled": enabled, "samples": len(rows), "observed": len(observed), "problems": len(problems),
            "freshness_unknown": unknown,
            "status": "disabled" if not enabled else "complete" if observed and not problems and not unknown else "incomplete",
            "max_scrape_gap_s": max((b-a for a, b in zip(times, times[1:])), default=None),
            "statistics": statistics_rows,
            "limitation": "HTTP 可达不证明底层采集新鲜；exporter 未提供样本时间戳时保留未知。exporter 返回 0 也不能证明驱动查询无异常。"}
