"""Generate local-only validation configs; never start a service or run pressure."""
import argparse
from copy import deepcopy
from pathlib import Path
import yaml

parser = argparse.ArgumentParser(description="生成待人工安排资源窗口的验证配置，不执行压测")
parser.add_argument("--config", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
from aiperf_hcu.config import load
base = load(args.config)
root = Path(args.output)
root.mkdir(parents=True, exist_ok=False)
cases = [("smoke", {"requests": 10, "warmup": 0, "concurrency": 1, "repeats": 1})]
cases += [(f"fixed-c{c}", {"concurrency": c}) for c in (1, 4, 8)]
cases += [(f"poisson-r{r}", {"input_tokens": 512, "input_stddev": 128, "concurrency": 8, "request_rate": r, "arrival_pattern": "poisson", "warmup": 0}) for r in (1, 2, 4)]
cases += [("fixed-c8-no-telemetry", {"concurrency": 8}), ("fixed-c8-more-client-cpu", {"concurrency": 8})]
for name, updates in cases:
    c = deepcopy(base)
    c["workload"] = {"requests": 128, "warmup": 16, "repeats": 3, "input_tokens": 128, "input_stddev": 0,
                     "output_tokens": 128, "concurrency": 1, "seed": 42, "timeout_s": 600, "force_output_length": True,
                     "arrival_pattern": "constant", **updates}
    if name.endswith("no-telemetry"):
        c["telemetry"]["enabled"] = False
    (root / f"{name}.local.yaml").write_text(yaml.safe_dump(c, allow_unicode=True), encoding="utf-8")
print(f"已生成 {len(cases)} 个配置；没有启动任务。more-client-cpu 需另行调整客户端配额并记录。")
