"""Actual pinned engine integration, CPU-only and loopback only."""
import json
import os
from pathlib import Path

import pytest

from aiperf_hcu.runner import command, execute, run
from aiperf_hcu.results import read_upstream, report
from mock_service import config, server


@pytest.mark.upstream
@pytest.mark.parametrize("streaming", [True, False])
def test_real_engine(tmp_path, streaming):
    with server() as (url, state):
        c = config(tmp_path, url, streaming)
        root = tmp_path / "adapted"
        result = run(c, root)
        assert result["status"] == "completed", (result, (root / "run-001/process.log").read_text(encoding="utf-8")[-6000:])
        assert len(state["requests"]) == 3
        assert result["telemetry_complete"]
        assert "TTFT" in report(root)
        with pytest.raises(FileExistsError):
            run(c, root)
        # Direct invocation uses the exact command, bypassing the adapter lifecycle.
        raw = tmp_path / "direct"
        direct, _ = execute(command(c, raw), 90, tmp_path / "direct.log", "")
        assert direct == "completed"
        original = read_upstream(raw, 3)
        assert original["status"] == "complete"
        for tag in ("request_count", "input_sequence_length", "output_sequence_length"):
            assert original["metrics"][tag]["avg"] == result["runs"][0]["upstream"]["metrics"][tag]["avg"]


@pytest.mark.upstream
def test_real_engine_auth_and_failure(tmp_path, monkeypatch):
    secret = "synthetic-local-auth-only"
    monkeypatch.setenv("LOCAL_TEST_KEY", secret)
    with server(mode="error", key=secret) as (url, state):
        c = config(tmp_path, url)
        c["service"]["api_key_env"] = "LOCAL_TEST_KEY"
        c["workload"]["repeats"] = 3
        root = tmp_path / "failure"
        result = run(c, root)
        assert state["requests"], "Authentication environment substitution failed"
        assert result["status"] == "incomplete" and len(result["runs"]) == 1
        assert all(secret.encode() not in p.read_bytes() for p in root.rglob("*") if p.is_file())


@pytest.mark.upstream
def test_authenticated_rate_and_warmup(tmp_path, monkeypatch):
    secret = "synthetic-success-key"
    monkeypatch.setenv("LOCAL_TEST_KEY", secret)
    with server(key=secret) as (url, state):
        c = config(tmp_path, url)
        c["service"]["api_key_env"] = "LOCAL_TEST_KEY"
        c["workload"].update(warmup=2, request_rate=10, arrival_pattern="poisson")
        root = tmp_path / "authenticated"
        result = run(c, root)
        assert result["status"] == "completed", (result, (root / "run-001/process.log").read_text(encoding="utf-8")[-6000:])
        assert len(state["requests"]) == 5
        raw = json.loads((root / "run-001/raw/profile_export_aiperf.json").read_text(encoding="utf-8"))
        phases = raw["input_config"]["phases"]
        assert [(p["kind"], p["requests"], p["type"], p["rate"]) for p in phases] == [("warmup", 2, "poisson", 10), ("profiling", 3, "poisson", 10)]
        assert result["runs"][0]["upstream"]["metrics"]["request_count"]["avg"] == 3
        assert all(secret.encode() not in p.read_bytes() for p in root.rglob("*") if p.is_file())


@pytest.mark.upstream
def test_real_stream_disconnect_is_not_complete(tmp_path):
    with server(mode="disconnect") as (url, state):
        c = config(tmp_path, url)
        result = run(c, tmp_path / "disconnect")
        assert state["requests"]
        assert result["status"] == "incomplete"
