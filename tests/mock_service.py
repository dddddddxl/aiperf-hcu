"""Synthetic deterministic OpenAI server; no model weights or GPU."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time


def make_tokenizer(path):
    from tokenizers import Tokenizer, models, pre_tokenizers
    path = Path(path)
    path.mkdir()
    tok = Tokenizer(models.WordLevel({"[UNK]": 0, "word": 1, "[EOS]": 2}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(path / "tokenizer.json"))
    (path / "tokenizer_config.json").write_text(json.dumps({"tokenizer_class": "PreTrainedTokenizerFast", "unk_token": "[UNK]", "eos_token": "[EOS]", "model_max_length": 8192}), encoding="utf-8")
    return path


@contextmanager
def server(mode="ok", key=None):
    state = {"requests": [], "gets": [], "mode": mode}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args):
            pass
        def send(self, body, code=200):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            state["gets"].append(self.path)
            if self.path == "/metrics":
                labels = 'node="mock-node",pcieBus_number="0000:01:00.0",device_id="synthetic"'
                text = "\n".join(f"{k}{{{labels}}} {v}" for k, v in {
                    "hcu_power_usage": 100, "hcu_temp": 45, "hcu_usedmemory_bytes": 1024,
                    "hcu_utilizationrate": 50, "test_sample_timestamp_seconds": time.time()}.items())
                data = text.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send({"object": "list", "data": [{"id": "mock-model", "object": "model"}]})
        def do_POST(self):
            if key and self.headers.get("Authorization") != "Bearer " + key:
                self.send({"error": "unauthorized"}, 401)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(request)
            if state["mode"] == "error":
                self.send({"error": {"message": "synthetic failure"}}, 500)
                return
            if state["mode"] == "timeout":
                time.sleep(3)
                self.close_connection = True
                return
            n = request.get("max_completion_tokens", request.get("max_tokens", 8))
            usage = {"prompt_tokens": 8, "completion_tokens": n, "total_tokens": 8+n}
            if not request.get("stream"):
                self.send({"id": "synthetic", "model": "mock-model", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "word "*n}, "finish_reason": "length"}], "usage": usage})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for i in range(n):
                    chunk = {"id": "synthetic", "model": "mock-model", "object": "chat.completion.chunk",
                             "choices": [{"index": 0, "delta": {"content": "word "}, "finish_reason": None}]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.flush()
                    if state["mode"] == "disconnect":
                        self.close_connection = True
                        return
                    time.sleep(.005)
                self.wfile.write(("data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}], "usage": usage}) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", state
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join()


def config(tmp_path, url, streaming=True):
    import yaml
    tokenizer = make_tokenizer(tmp_path / "tokenizer")
    c = {"schema_version": 1, "service": {"url": url, "model": "mock-model", "tokenizer": str(tokenizer), "streaming": streaming},
         "workload": {"requests": 3, "warmup": 0, "input_tokens": 8, "output_tokens": 8, "concurrency": 1, "timeout_s": 90},
         "telemetry": {"enabled": True, "url": url + "/metrics", "interval_s": .1, "timestamp_metric": "test_sample_timestamp_seconds",
                       "devices": [{"node": "mock-node", "pcieBus_number": "0000:01:00.0", "service_device": "0"}]},
         "environment": {"engine": "mock", "card_count": 1, "source": "synthetic"}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(c), encoding="utf-8")
    from aiperf_hcu.config import load
    return load(path)
