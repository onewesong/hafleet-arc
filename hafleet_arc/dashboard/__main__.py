from __future__ import annotations

import argparse
from pathlib import Path

from .server import DashboardServer


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the HAFleet ARC run dashboard.")
    parser.add_argument("output_dir", help="HAFleet output directory to observe.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3200)
    args = parser.parse_args()
    server = DashboardServer(Path(args.output_dir), host=args.host, port=args.port)
    print(f"HAFleet dashboard: http://{args.host}:{args.port}", flush=True)
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
