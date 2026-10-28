"""Run only image-style recovery on a saved DataAgent2 input, without a reread."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataagent.llm_runtime import CodexProvider, json_write
from document.sequence_styles import review_prepared

parser = argparse.ArgumentParser()
parser.add_argument("seed", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
prepared = json.loads((args.seed / "sda_input.json").read_text())
config = json.loads((args.seed / "agent_state.json").read_text())["config"]
provider = CodexProvider(cache_dir=args.output / "requests", model=config["llm_model"],
    effort=config["llm_effort"], timeout=config["llm_timeout"], audit_path=args.output / "llm_calls.jsonl")
report = review_prepared(prepared, provider, args.output)
json_write(args.output / "sda_input.json", prepared)
print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
