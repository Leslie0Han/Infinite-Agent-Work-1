import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def redirect_streams(log_path: str = "", err_path: str = ""):
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = open(log_path, "a", encoding="utf-8", buffering=1)
    if err_path:
        Path(err_path).parent.mkdir(parents=True, exist_ok=True)
        sys.stderr = open(err_path, "a", encoding="utf-8", buffering=1)


def parse_args():
    parser = argparse.ArgumentParser(description="Run Infinite Agent Work server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--log", default="")
    parser.add_argument("--err-log", default="")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    redirect_streams(args.log, args.err_log)
    import uvicorn
    import main

    uvicorn.run(main.app, host=args.host, port=args.port)
