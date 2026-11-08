import argparse
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from brush_backend.app import create_app

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description="启动毛笔书写工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1") and len(os.environ.get("BRUSH_API_TOKEN", "")) < 16:
        parser.error("局域网模式请先设置至少 16 位的 BRUSH_API_TOKEN；本机 CSV 演示无需修改 host。")
    print(f"毛笔工作台：http://{args.host}:{args.port}    接口文档：/docs")
    uvicorn.run(create_app(args.data_dir), host=args.host, port=args.port)
