"""Evaluate saved final sequences without OCR, LLM calls or data publication."""
import argparse
import json
from pathlib import Path

from .pipeline import evaluate_sequences
from .runtime import EvaluationConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="resolved_sequences.json or native run directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--anarci-python", type=Path)
    parser.add_argument("--esm2-python", type=Path)
    parser.add_argument("--esm2-model", type=Path)
    parser.add_argument("--device")
    parser.add_argument("--batch-size", type=int)
    args = parser.parse_args()
    config = EvaluationConfig.from_env()
    for key in ("anarci_python", "esm2_python", "esm2_model", "device", "batch_size"):
        if getattr(args, key) is not None:
            setattr(config, key, getattr(args, key))
    source = args.input / "resolved_sequences.json" if args.input.is_dir() else args.input
    items = json.loads(source.read_text())
    if isinstance(items, dict):
        items = list(items.values())
    report = evaluate_sequences(items, args.output, config=config)
    print(json.dumps({"status": report["status"], **report["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
