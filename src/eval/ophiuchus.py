"""Local Ophiuchus-Ab worker: exact single-mask, single-chain scoring.

Independent of the PPL benchmark. No downloads, partner inference or sequence edits.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from .esm2 import AA, classify_chain, position_score, review_thresholds


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_model(source, checkpoint, device):
    import torch

    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    loader = source / "src/regress/encoders/ophiuchus.py"
    spec = importlib.util.spec_from_file_location("sda_ophiuchus_loader", loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    model = module.ESM2(num_layers=33, embed_dim=1280, attention_heads=20,
                        token_dropout=True, use_multimer=True)
    state = torch.load(str(checkpoint), map_location="cpu", mmap=True, weights_only=True)
    state = module.OphiuchusEncoder._normalize_state_dict(module.OphiuchusEncoder._extract_state_dict(state))
    model.load_state_dict(state, strict=True)
    sources = [loader, *sorted((source / "src/byprot/models/lm/modules/mint").rglob("*.py"))]
    metadata = {"checkpoint_sha256": file_hash(checkpoint), "strict_state_dict": True,
                "source_hashes": {str(p.relative_to(source)): file_hash(p) for p in sources},
                "torch": torch.__version__, "precision": "fp32", "mode": "single"}
    return model.to(device).eval(), metadata


def score_sequence(model, sequence, device, batch_size):
    import torch

    tokens = [model.cls_idx, *model.alphabet.encode(sequence), model.eos_idx]
    if len(tokens) != len(sequence) + 2 or "".join(model.alphabet.get_tok(t) for t in tokens[1:-1]) != sequence:
        raise ValueError("Ophiuchus tokenizer changed the sequence")
    if len(tokens) > 1024:
        raise ValueError("Ophiuchus sequence exceeds the position limit")
    ids = torch.tensor(tokens, dtype=torch.long, device=device)
    values = []
    with torch.inference_mode():
        for start in range(1, len(sequence) + 1, batch_size):
            columns = torch.arange(start, min(start + batch_size, len(sequence) + 1), device=device)
            rows = torch.arange(len(columns), device=device)
            batch = ids.unsqueeze(0).repeat(len(columns), 1)
            truth = batch[rows, columns].clone()
            batch[rows, columns] = model.mask_idx
            logits = model(batch, chain_ids=torch.zeros_like(batch))["logits"][rows, columns]
            values.extend(logits.float().log_softmax(-1).gather(1, truth[:, None]).squeeze(1).cpu().tolist())
    ordered = sorted(values)
    bottom = max(1, len(values) // 10)
    return {"mean_pll": sum(values) / len(values), "min_log_probability": ordered[0],
            "bottom_decile_mean": sum(ordered[:bottom]) / bottom, "masked_position_count": len(values),
            "position_scores": [position_score(i + 1, residue, values[i]) for i, residue in enumerate(sequence)]}


def review_sequences(items, model_path, source, *, device="cuda", batch_size=32):
    if batch_size < 1:
        raise ValueError("Ophiuchus batch size must be positive")
    thresholds = review_thresholds("OPHIUCHUS")
    valid = list(dict.fromkeys(i["sequence"] for i in items if i["sequence"] and not set(i["sequence"]) - AA))
    scores, metadata = {}, {"mode": "single"}
    if valid:
        model, metadata = load_model(source, model_path, device)
        for index, sequence in enumerate(valid):
            scores[sequence] = score_sequence(model, sequence, device, batch_size)
            print(f"ophiuchus: {index + 1}/{len(valid)}", file=sys.stderr, flush=True)
    return [{"id": item["id"], "sequence_sha256": hashlib.sha256(item["sequence"].encode()).hexdigest(),
             "review": {**classify_chain(item["chain"], item["sequence"], scores.get(item["sequence"]),
                                          thresholds, tool="OPHIUCHUS"),
                        "model": "Ophiuchus-Ab", "method": "masked_pseudo_log_likelihood", **metadata}}
            for item in items]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--batch-size", default=32, type=int)
    args = parser.parse_args()
    items = json.load(sys.stdin)
    if not isinstance(items, list):
        raise ValueError("Ophiuchus input must be a JSON list")
    with contextlib.redirect_stdout(sys.stderr):
        results = review_sequences(items, args.model, args.source, device=args.device, batch_size=args.batch_size)
    json.dump(results, sys.stdout, ensure_ascii=False, allow_nan=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
