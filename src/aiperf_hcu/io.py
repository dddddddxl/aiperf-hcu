import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def get(url, secret="", timeout=5):
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    with build_opener(NoRedirect).open(Request(url, headers=headers), timeout=timeout) as response:
        data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("HTTP response exceeds 8 MiB")
        return data.decode("utf-8")


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob("*")) if p.is_file() and p.name != "manifest.json"}


def verify(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["artifacts"].items():
        p = (root / name).resolve()
        if not p.is_relative_to(root) or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != expected:
            raise ValueError("工件路径或哈希校验失败")
    return manifest
