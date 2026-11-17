import json
from pathlib import Path
import subprocess
import sys
import time

import pytest
import yaml

from aiperf_hcu.config import ConfigError, load, tokenizer_identity
from aiperf_hcu.io import hashes, verify, write_json
from aiperf_hcu.results import compare, markdown, read_upstream
from aiperf_hcu.runner import command, doctor, execute
from aiperf_hcu.telemetry import parse
from mock_service import config, server


@pytest.mark.parametrize("change", [
    {"requests": 0}, {"concurrency": True}, {"timeout_s": float("inf")},
    {"repeats": -1}, {"arrival_pattern": "poisson"}, {"shell": "echo bad"},
])
def test_invalid_workload(tmp_path, change):
    c = config(tmp_path, "http://localhost:1")
    c["workload"].update(change)
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(c))
    with pytest.raises(ConfigError):
        load(p)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://key:password@example.com", "http://localhost?key=secret", "http://localhost/v1"])
def test_unsafe_url(tmp_path, url):
    c = config(tmp_path, "http://localhost:1")
    c["service"]["url"] = url
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(c))
    with pytest.raises(ConfigError):
        load(p)


def test_duplicate_yaml(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("schema_version: 1\nschema_version: 1\n")
    with pytest.raises(ConfigError):
        load(p)


def test_missing_tokenizer(tmp_path):
    with pytest.raises(ConfigError):
        tokenizer_identity(tmp_path)


def test_doctor_does_not_generate(tmp_path, monkeypatch):
    monkeypatch.setattr("aiperf_hcu.runner.engine_identity", lambda: {"version": "synthetic"})
    with server() as (url, state):
        c = config(tmp_path, url)
        assert doctor(c)["ok"]
        assert state["requests"] == []


def test_command_no_secret_or_shell(tmp_path, monkeypatch):
    c = config(tmp_path, "http://localhost:1")
    c["service"]["api_key_env"] = "SYNTHETIC_KEY"
    monkeypatch.setenv("SYNTHETIC_KEY", "not-a-real-secret")
    args = command(c, tmp_path / "raw")
    assert "not-a-real-secret" not in " ".join(args)
    assert args[-1] == "${AIPERF_HCU_REQUEST_KEY}"
    assert "--no-gpu-telemetry" in args and "--no-server-metrics" in args


def tconfig():
    return {"devices": [{"node": "node", "pcieBus_number": "0000:01:00.0", "service_device": "0"}], "max_age_s": 5}


@pytest.mark.parametrize("value,status", [(100, "observed"), (-1, "invalid"), ("NaN", "invalid"), ("+Inf", "invalid")])
def test_telemetry_values(value, status):
    text = f'hcu_power_usage{{node="node",pcieBus_number="0000:01:00.0"}} {value}\n'
    r = parse(text, tconfig(), now=100)[0]
    assert r["status"] == status
    assert r["freshness"] == "unknown"


def test_stale_and_wrong_device():
    text = 'hcu_power_usage{node="node",pcieBus_number="0000:01:00.0"} 100 1000\n'
    assert parse(text, tconfig(), now=100)[0]["status"] == "stale"
    assert parse(text.replace('node="node"', 'node="other"'), tconfig(), now=100)[0]["status"] == "missing"


def test_ambiguous_mapping():
    text = 'hcu_power_usage{node="node",pcieBus_number="0000:01:00.0"} 100\n'
    assert parse(text*2, tconfig())[0]["status"] == "ambiguous"


def test_units_and_no_missing_zero():
    rows = parse('hcu_usedmemory_bytes{node="node",pcieBus_number="0000:01:00.0"} 1048576\n', tconfig())
    assert rows[2]["value"] == 1048576 and rows[2]["unit"] == "bytes"
    assert rows[0]["value"] is None


def test_log_redaction_and_exit(tmp_path):
    log = tmp_path / "log"
    state, code = execute([sys.executable, "-c", "print('synthetic-secret'); raise SystemExit(3)"], 10, log, "synthetic-secret")
    assert (state, code) == ("failed", 3)
    assert "synthetic-secret" not in log.read_text()


def test_timeout(tmp_path):
    state, _ = execute([sys.executable, "-c", "import time; time.sleep(10)"], .2, tmp_path / "log", "")
    assert state == "timeout"


def test_interrupt_terminates_process(tmp_path, monkeypatch):
    original = subprocess.Popen
    def spawn(*args, **kwargs):
        proc = original(*args, **kwargs)
        wait = proc.wait
        first = True
        def interrupted(*a, **kw):
            nonlocal first
            if first:
                first = False
                raise KeyboardInterrupt
            return wait(*a, **kw)
        proc.wait = interrupted
        return proc
    monkeypatch.setattr(subprocess, "Popen", spawn)
    state, code = execute([sys.executable, "-c", "import time; time.sleep(30)"], 5, tmp_path / "log", "")
    assert state == "interrupted" and code is not None


def test_collector_failure_and_freshness(tmp_path):
    from aiperf_hcu.telemetry import Collector, summarize
    cfg = {**tconfig(), "url": "http://127.0.0.1:1/metrics", "interval_s": .1, "enabled": True}
    c = Collector(cfg, tmp_path / "telemetry.jsonl")
    assert c.sample()[0]["status"] == "collection_failed"
    c.start()
    time.sleep(.15)
    c.stop()
    assert summarize(c.path, True)["status"] == "incomplete"


def test_nonstreaming_command(tmp_path):
    c = config(tmp_path, "http://localhost:1", streaming=False)
    assert "--streaming" not in command(c, tmp_path / "raw")
    assert "--warmup-request-count" not in command(c, tmp_path / "raw")


def test_server_token_count_explicit(tmp_path):
    c = config(tmp_path, "http://localhost:1")
    assert "--use-server-token-count" not in command(c, tmp_path / "raw")
    c["service"]["token_count_source"] = "server"
    assert "--use-server-token-count" in command(c, tmp_path / "raw")
    c["service"]["token_count_source"] = "guessed"
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(c))
    with pytest.raises(ConfigError):
        load(p)


def test_upstream_missing_or_wrong_count(tmp_path):
    assert read_upstream(tmp_path, 3)["status"] == "incomplete"


def test_zero_error_inference_and_length(tmp_path):
    p = tmp_path / "profile_export_aiperf.json"
    data = {"aiperf_version": "0.13.0", "is_complete": True,
            "request_count": {"unit": "requests", "avg": 3},
            "output_sequence_length": {"unit": "tokens", "min": 8, "max": 8}}
    write_json(p, data)
    assert read_upstream(tmp_path, 3, 8)["status"] == "complete"
    assert read_upstream(tmp_path, 3, 9)["status"] == "incomplete"
    data["is_complete"] = False
    write_json(p, data)
    assert read_upstream(tmp_path, 3)["status"] == "incomplete"


def test_warmup_export_is_not_profiling(tmp_path):
    p = tmp_path / "profile_export_aiperf.json"
    data = {"aiperf_version": "0.13.0", "is_complete": True,
            "request_count": {"unit": "requests", "avg": 3}}
    (tmp_path / "warmup").mkdir()
    write_json(tmp_path / "warmup/profile_export_aiperf.json", {**data, "request_count": {"unit": "requests", "avg": 2}})
    assert read_upstream(tmp_path, 3)["status"] == "incomplete"
    write_json(tmp_path / "profile_export_aiperf.json", data)
    assert read_upstream(tmp_path, 3)["status"] == "complete"
    data["is_complete"] = True
    data["error_summary"] = [{"message": "some requests failed"}]
    write_json(p, data)
    assert read_upstream(tmp_path, 3)["status"] == "incomplete"
    write_json(tmp_path / "profile_export_aiperf.json", {"aiperf_version": "0.13.0", "is_complete": True,
        "request_count": {"unit": "requests", "avg": 2}, "error_request_count": {"unit": "requests", "avg": 0}})
    assert read_upstream(tmp_path, 3)["status"] == "incomplete"


def test_hash_tamper_and_traversal(tmp_path):
    write_json(tmp_path / "summary.json", {"a": 1})
    write_json(tmp_path / "manifest.json", {"artifacts": hashes(tmp_path)})
    verify(tmp_path)
    write_json(tmp_path / "summary.json", {"a": 2})
    with pytest.raises(ValueError):
        verify(tmp_path)
    write_json(tmp_path / "manifest.json", {"artifacts": {"../outside": "x"}})
    with pytest.raises(ValueError):
        verify(tmp_path)


def test_comparison_mismatch(tmp_path):
    for n in (1, 2):
        root = tmp_path / str(n)
        root.mkdir()
        write_json(root / "summary.json", {"runs": [{"execution": "completed", "upstream": {"status": "complete", "metrics": {}}}]})
        write_json(root / "manifest.json", {"comparison_identity": {"model": str(n)}, "artifacts": hashes(root)})
    result = compare(tmp_path / "1", tmp_path / "2")
    assert not result["comparable"] and result["differences"][0]["field"] == "model"


def test_collector_writer_failure_is_reported(tmp_path, monkeypatch):
    from aiperf_hcu.telemetry import Collector
    collector = Collector({"enabled": True}, tmp_path / "telemetry.jsonl")
    def broken():
        raise OSError("synthetic disk error")
    monkeypatch.setattr(collector, "_collect", broken)
    collector.start()
    with pytest.raises(RuntimeError, match="telemetry_writer_failed"):
        collector.stop()


@pytest.mark.parametrize("failure", ["stop", "start"])
def test_telemetry_failure_preserves_artifacts(tmp_path, monkeypatch, failure):
    import aiperf_hcu.runner as runner
    c = config(tmp_path, "http://127.0.0.1:1")
    monkeypatch.setattr(runner, "engine_identity", lambda: {"version": "synthetic"})
    monkeypatch.setattr(runner, "execute", lambda *args: ("completed", 0))
    def broken(self):
        raise OSError("synthetic writer error")
    monkeypatch.setattr(runner.Collector, failure, broken)
    root = tmp_path / "preserved"
    result = runner.run(c, root)
    assert result["status"] == "incomplete"
    assert result["runs"][0]["telemetry"]["status"] == "incomplete"
    assert result["experiment_source"] == "synthetic"
    assert "synthetic" in (root / "report.md").read_text(encoding="utf-8")
    verify(root)


def test_partial_repeat_is_not_comparable(tmp_path):
    environment = {k: "synthetic" for k in ("model_revision", "engine_commit", "image", "dtk", "driver", "card_model", "quantization", "parallelism", "cache_policy")}
    for n in (1, 2):
        root = tmp_path / str(n)
        root.mkdir()
        write_json(root / "summary.json", {"status": "incomplete", "runs": [{"execution": "completed", "upstream": {"status": "complete", "metrics": {}}}]})
        write_json(root / "manifest.json", {"comparison_identity": {"environment": environment}, "artifacts": hashes(root)})
    assert not compare(tmp_path / "1", tmp_path / "2")["comparable"]
