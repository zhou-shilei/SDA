"""Compatibility entrypoint; sequence evaluation now lives in src/eval."""
from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.esm2 import (classify_chain, main, pair_review, review_pairs,
                       review_sequences, review_thresholds, score_sequences)

if __name__ == "__main__":
    raise SystemExit(main())
