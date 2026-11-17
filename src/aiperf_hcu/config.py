"""Closed configuration surface: no arbitrary command execution."""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml


class ConfigError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ConfigError("配置字段缺失或不受支持")


def number(value, low, high, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError("数值必须有限")
    if not low <= value <= high or integer and not isinstance(value, int):
        raise ConfigError("数值范围或整数类型错误")


def url(value):
    if not isinstance(value, str):
        raise ConfigError("URL 必须是字符串")
    p = urlsplit(value)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ConfigError("只支持无凭据、无查询参数的 HTTP(S) 地址")
    return value.rstrip("/")


def load(path):
    path = Path(path).resolve()
    # Duplicate YAML keys otherwise silently overwrite intended experiment identity.
    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        out = {}
        for k, v in node.value:
            key = loader.construct_object(k, deep=deep)
            if key in out:
                raise ConfigError("重复 YAML 字段")
            out[key] = loader.construct_object(v, deep=deep)
        return out

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    c = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    keys(c, ("schema_version", "service", "workload", "telemetry", "environment"), ("schema_version", "service", "workload", "environment"))
    if c["schema_version"] != 1:
        raise ConfigError("不支持的 schema_version")
    s = c["service"]
    keys(s, ("url", "model", "tokenizer", "api_key_env", "streaming", "token_count_source"), ("url", "model", "tokenizer"))
    s["url"] = url(s["url"])
    if urlsplit(s["url"]).path not in ("", "/"):
        raise ConfigError("service.url 使用服务根地址，不包含 /v1")
    if not isinstance(s["model"], str) or not s["model"].strip():
        raise ConfigError("model 必须非空")
    if not isinstance(s["tokenizer"], str):
        raise ConfigError("tokenizer 必须为本地路径")
    s["tokenizer"] = str((path.parent / s["tokenizer"]).resolve())
    s.setdefault("streaming", True)
    s.setdefault("token_count_source", "client")
    if s["token_count_source"] not in ("client", "server"):
        raise ConfigError("token_count_source 必须为 client 或 server")
    if not isinstance(s["streaming"], bool):
        raise ConfigError("streaming 必须是布尔值")
    if s.get("api_key_env") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s["api_key_env"]):
        raise ConfigError("api_key_env 必须为环境变量名")
    w = c["workload"]
    keys(w, ("requests", "warmup", "repeats", "input_tokens", "input_stddev", "output_tokens", "concurrency", "request_rate", "arrival_pattern", "seed", "timeout_s", "force_output_length"), ("requests", "input_tokens", "output_tokens", "concurrency"))
    for k, default in {"warmup": 0, "repeats": 1, "input_stddev": 0, "seed": 42, "timeout_s": 600, "force_output_length": True, "arrival_pattern": "constant"}.items():
        w.setdefault(k, default)
    for k in ("requests", "input_tokens", "output_tokens", "concurrency", "repeats"):
        number(w[k], 1, 1000000, True)
    for k in ("warmup", "input_stddev", "seed"):
        number(w[k], 0, 2147483647, True)
    number(w["timeout_s"], 1, 86400)
    if w.get("request_rate") is not None:
        number(w["request_rate"], .001, 1000000)
    if w["arrival_pattern"] not in ("constant", "poisson") or w["arrival_pattern"] == "poisson" and not w.get("request_rate"):
        raise ConfigError("Poisson 需要 request_rate")
    if not isinstance(w["force_output_length"], bool):
        raise ConfigError("force_output_length 必须为布尔值")
    t = c.setdefault("telemetry", {"enabled": False})
    keys(t, ("enabled", "url", "interval_s", "max_age_s", "devices", "timestamp_metric"), ("enabled",))
    if not isinstance(t["enabled"], bool):
        raise ConfigError("telemetry.enabled 必须为布尔值")
    t.setdefault("interval_s", 1)
    t.setdefault("max_age_s", 5)
    number(t["interval_s"], .1, 60)
    number(t["max_age_s"], .1, 300)
    if t["enabled"]:
        t["url"] = url(t.get("url"))
        if not isinstance(t.get("devices"), list) or not t["devices"]:
            raise ConfigError("必须指定物理设备映射")
        seen = set()
        for d in t["devices"]:
            keys(d, ("node", "pcieBus_number", "service_device"), ("node", "pcieBus_number", "service_device"))
            if any(not isinstance(v, str) or not v for v in d.values()):
                raise ConfigError("设备映射字段必须非空字符串")
            identity = (d["node"], d["pcieBus_number"])
            if identity in seen:
                raise ConfigError("物理设备映射重复")
            seen.add(identity)
    e = c["environment"]
    keys(e, ("model_revision", "engine", "engine_commit", "image", "dtk", "driver", "card_model", "card_count", "quantization", "parallelism", "cache_policy", "source"), ("engine", "card_count", "source"))
    number(e["card_count"], 1, 1024, True)
    if e["source"] not in ("user_declared", "synthetic"):
        raise ConfigError("environment.source 必须标识来源")
    if t["enabled"] and len(t["devices"]) != e["card_count"]:
        raise ConfigError("映射设备数与 card_count 不一致")
    return c


def token(c):
    name = c["service"].get("api_key_env")
    if name and not os.environ.get(name):
        raise ConfigError("指定的 API key 环境变量未设置")
    return os.environ.get(name, "") if name else ""


def identity(c):
    result = deepcopy(c)
    result["service"].pop("url", None)
    result["service"].pop("api_key_env", None)
    result["service"]["tokenizer"] = tokenizer_identity(c["service"]["tokenizer"])
    result.pop("telemetry", None)
    return result


def tokenizer_identity(directory):
    p = Path(directory)
    names = ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "tokenizer.model", "vocab.json", "merges.txt", "config.json")
    hashes = {}
    for n in names:
        file = p / n
        if file.is_file():
            if file.is_symlink() or file.stat().st_size > 128 * 1024 * 1024:
                raise ConfigError("tokenizer 文件不支持符号链接或超过 128 MiB")
            hashes[n] = hashlib.sha256(file.read_bytes()).hexdigest()
    if not hashes or not any(n in hashes for n in ("tokenizer.json", "tokenizer.model", "vocab.json")):
        raise ConfigError("缺少本地 tokenizer 文件；不会联网下载")
    return hashes
