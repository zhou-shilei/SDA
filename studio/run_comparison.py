"""Submit a comparison without restarting the HTTP server or its active tasks."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.sda_data import Workspace
from studio.server import StudioServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source", required=True)
    parser.add_argument("--engine", choices=("dataagent1", "dataagent2"), required=True)
    args = parser.parse_args()
    server = StudioServer(("127.0.0.1", 0), Workspace(args.root))
    try:
        patent = server.compare(args.source, engine=args.engine)
        print(json.dumps({"document_id": patent["document_id"], "extraction_engine": patent["extraction_engine"],
                          "log": patent["log"]}, ensure_ascii=False), flush=True)
        server.workers.shutdown(wait=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
