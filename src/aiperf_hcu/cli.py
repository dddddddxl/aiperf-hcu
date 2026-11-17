import argparse
import json
from pathlib import Path
import sys
import signal

from .config import load
from .results import compare, report
from .runner import doctor, run


def main():
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description="海光 AIPerf 适配；不负责启动服务或分配卡")
    sub = parser.add_subparsers(dest="action", required=True)
    d = sub.add_parser("doctor")
    d.add_argument("--config", required=True)
    d.add_argument("--output", help="可选：只检查拟写入目录，不创建")
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--output", required=True)
    p = sub.add_parser("report")
    p.add_argument("--input", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--baseline", required=True)
    c.add_argument("--candidate", required=True)
    args = parser.parse_args()
    try:
        if args.action == "report":
            print(report(args.input))
            return 0
        if args.action == "compare":
            result = compare(args.baseline, args.candidate)
            code = 0 if result["comparable"] else 2
        elif args.action == "doctor":
            result = doctor(load(args.config))
            if args.output:
                p = Path(args.output).resolve()
                import os
                parent = p.parent
                while not parent.exists():
                    parent = parent.parent
                ok = not p.exists() and os.access(parent, os.W_OK)
                result["checks"].append({"name": "output", "ok": ok})
                result["ok"] = result["ok"] and ok
            code = 0 if result["ok"] else 2
        else:
            result = run(load(args.config), args.output)
            code = 0 if result["status"] == "completed" else 2
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return code
    except Exception as e:
        # Do not echo urllib exceptions, URLs or credential-bearing server bodies.
        print(json.dumps({"status": "error", "error_type": type(e).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
