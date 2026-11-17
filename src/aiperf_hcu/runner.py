import importlib.metadata as metadata
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time

import psutil

from . import AIPERF_COMMIT, AIPERF_VERSION, EXPORTER_COMMIT, __version__
from .config import ConfigError, digest, identity, token, tokenizer_identity
from .io import get, hashes, write_json
from .results import markdown, read_upstream
from .telemetry import Collector, summarize


def engine_identity():
    dist = metadata.distribution("aiperf")
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    commit = direct.get("vcs_info", {}).get("commit_id")
    if dist.version != AIPERF_VERSION or commit != AIPERF_COMMIT:
        raise ConfigError("AIPerf 版本/源码提交不符，请使用 uv sync --locked")
    return {"version": dist.version, "commit": commit}


def doctor(c):
    checks = []
    for name, fn in [
        ("engine", engine_identity),
        ("tokenizer", lambda: tokenizer_identity(c["service"]["tokenizer"])),
        ("models", lambda: json.loads(get(c["service"]["url"] + "/v1/models", token(c)))),
    ]:
        try:
            value = fn()
            if name == "models":
                ids = [m["id"] for m in value.get("data", [])]
                if c["service"]["model"] not in ids:
                    raise ConfigError("model_not_listed")
                value = {"selected_model_present": True}
            checks.append({"name": name, "ok": True, "details": value})
        except Exception as e:
            checks.append({"name": name, "ok": False, "error_type": type(e).__name__})
    if c["telemetry"]["enabled"]:
        rows = Collector(c["telemetry"], Path("unused")).sample()
        checks.append({"name": "telemetry", "ok": bool(rows) and all(r["status"] == "observed" for r in rows),
                       "freshness_verified": all(r.get("freshness") == "verified" for r in rows)})
    return {"checks": checks, "ok": all(x["ok"] for x in checks), "generation_requests": 0}


def command(c, output):
    s, w = c["service"], c["workload"]
    args = [sys.executable, "-m", "aiperf", "profile", "--model", s["model"], "--url", s["url"],
            "--endpoint-type", "chat", "--tokenizer", s["tokenizer"],
            "--request-count", str(w["requests"]),
            "--concurrency", str(w["concurrency"]), "--synthetic-input-tokens-mean", str(w["input_tokens"]),
            "--synthetic-input-tokens-stddev", str(w["input_stddev"]),
            "--output-tokens-mean", str(w["output_tokens"]), "--output-tokens-stddev", "0",
            "--random-seed", str(w["seed"]), "--artifact-dir", str(output),
            "--no-gpu-telemetry", "--no-server-metrics", "--ui", "none", "--export-level", "records"]
    if s["streaming"]:
        args.append("--streaming")
    if w["warmup"]:
        args += ["--warmup-request-count", str(w["warmup"])]
    if w.get("request_rate"):
        args += ["--request-rate", str(w["request_rate"]), "--arrival-pattern", w["arrival_pattern"]]
    if w["force_output_length"]:
        args += ["--extra-inputs", f"min_tokens:{w['output_tokens']}", "--extra-inputs", "ignore_eos:true"]
    if s.get("api_key_env"):
        # Expanded by upstream config resolver; no secret in process arguments.
        args += ["--api-key", "${AIPERF_HCU_REQUEST_KEY}"]
    return args


def terminate_tree(proc):
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for child in children:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    if proc.poll() is None:
        proc.terminate()
    _, alive = psutil.wait_procs(children, timeout=3)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def execute(args, timeout, log, secret):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AIPERF_")}
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false", PYTHONIOENCODING="utf-8")
    # v0.13.0 offline resolver requires a HF cache repo, not a local directory.
    # Stage verified files in a run-owned cache; never mutate the user's HF cache.
    args = list(args)
    if "--tokenizer" in args:
        index = args.index("--tokenizer") + 1
        source = Path(args[index])
        files = tokenizer_identity(source)
        revision = digest(files)[:40]
        repo_name = "hcu-local/" + revision
        cache = log.parent / "tokenizer-cache"
        repo = cache / ("models--" + repo_name.replace("/", "--"))
        snapshot = repo / "snapshots" / revision
        snapshot.mkdir(parents=True, exist_ok=False)
        for name in files:
            shutil.copyfile(source / name, snapshot / name)
        (repo / "refs").mkdir()
        (repo / "refs" / "main").write_text(revision, encoding="utf-8")
        env["HF_HUB_CACHE"] = str(cache.resolve())
        args[index] = repo_name
    write_json(log.parent / (log.stem + ".actual-command.json"), args)
    if secret:
        env["AIPERF_HCU_REQUEST_KEY"] = secret
        if "--api-key" in args:
            index = args.index("--api-key")
            del args[index:index+2]
            # v0.13 expands environment references in YAML, not raw CLI values.
            config_file = log.parent / "upstream-auth.json"
            write_json(config_file, {"benchmark": {"model": args[args.index("--model")+1],
                       "endpoint": {"api_key": "${AIPERF_HCU_REQUEST_KEY}"}}})
            args += ["--config", str(config_file.resolve())]
        write_json(log.parent / (log.stem + ".actual-command.json"), args)
    with log.open("w", encoding="utf-8") as f:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=env,
                                start_new_session=os.name != "nt")
        def pump():
            for line in proc.stdout:
                f.write(line.replace(secret, "[REDACTED]") if secret else line)
                f.flush()
        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        state = "completed"
        try:
            code = proc.wait(timeout=timeout)
            if code:
                state = "failed"
        except subprocess.TimeoutExpired:
            state = "timeout"
            terminate_tree(proc)
        except KeyboardInterrupt:
            state = "interrupted"
            terminate_tree(proc)
        finally:
            thread.join(timeout=5)
            if thread.is_alive():
                terminate_tree(proc)
                raise RuntimeError("log_shutdown_timeout")
        return state, proc.returncode


def run(c, root):
    engine = engine_identity()
    exp_identity = identity(c)
    secret = token(c)
    root = Path(root).resolve()
    # Refuse even an empty pre-existing directory, preserving first failures.
    root.mkdir(parents=True, exist_ok=False)
    started = time.time()
    manifest = {"schema_version": 1, "adapter_version": __version__, "engine": engine,
                "exporter_reference_commit": EXPORTER_COMMIT, "config": c,
                "config_sha256": digest(c), "comparison_identity": exp_identity,
                "started_at": started, "status": "running", "artifacts": {}}
    write_json(root / "manifest.json", manifest)
    runs = []
    for repeat in range(1, c["workload"]["repeats"] + 1):
        folder = root / f"run-{repeat:03}"
        folder.mkdir()
        args = command(c, folder / "raw")
        write_json(folder / "command.json", args)
        collector = Collector(c["telemetry"], folder / "telemetry.jsonl")
        phase_start = time.time()
        state, code, error_type = "failed", None, None
        try:
            collector.start()
            state, code = execute(args, c["workload"]["timeout_s"], folder / "process.log", secret)
        except KeyboardInterrupt:
            state = "interrupted"
        except Exception as e:
            error_type = type(e).__name__
        finally:
            collector.stop()
        upstream = read_upstream(folder / "raw", c["workload"]["requests"], c["workload"]["output_tokens"] if c["workload"]["force_output_length"] else None)
        telemetry = summarize(folder / "telemetry.jsonl", c["telemetry"]["enabled"])
        if secret and any(secret.encode() in p.read_bytes() for p in folder.rglob("*") if p.is_file()):
            state, error_type = "failed", "credential_detected_in_private_artifacts"
        row = {"repeat": repeat, "execution": state, "returncode": code, "error_type": error_type,
               "seconds": time.time()-phase_start, "upstream": upstream, "telemetry": telemetry}
        runs.append(row)
        # Persist checkpoints after each repeat; never continue through errors/OOM.
        write_json(root / "summary.json", {"status": "running", "runs": runs})
        if state != "completed" or upstream["status"] != "complete":
            break
    execution_complete = len(runs) == c["workload"]["repeats"] and all(r["execution"] == "completed" and r["upstream"]["status"] == "complete" for r in runs)
    summary = {"schema_version": 1, "status": "completed" if execution_complete else "incomplete", "runs": runs,
               "telemetry_complete": all(r["telemetry"]["status"] == "complete" for r in runs),
               "hardware_validation": "not_certified", "total_seconds": time.time()-started}
    write_json(root / "summary.json", summary)
    (root / "report.md").write_text(markdown(summary), encoding="utf-8")
    manifest.update(status=summary["status"], finished_at=time.time(), artifacts=hashes(root))
    write_json(root / "manifest.json", manifest)
    return summary
