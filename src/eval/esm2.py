"""Score antibody variable domains with ESM-2 masked pseudo-log-likelihood.

The module is also a small JSON-in/JSON-out subprocess so the lightweight
Agent runtime can delegate GPU inference to a compatible PyTorch environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any


AA = set("ACDEFGHIKLMNPQRSTVWY")


def site_threshold(chain: str) -> tuple[float | None, str]:
    if os.environ.get("DATASETAGENT_ESM2_SITE_SCREEN_ENABLED", "1").lower() in {"0", "false", "off", "no"}:
        return None, "disabled"
    value = os.environ.get(f"DATASETAGENT_ESM2_{chain}_SITE_MAX_PPL",
                           os.environ.get("DATASETAGENT_ESM2_SITE_MAX_PPL"))
    threshold = float(value) if value is not None else 1000.0
    if not math.isfinite(threshold) or threshold <= 1:
        raise ValueError("ESM-2 site perplexity threshold must be finite and greater than 1")
    return threshold, "configured" if value is not None else "default_screening"


def position_score(position: int, residue: str, log_probability: float) -> dict:
    if not math.isfinite(log_probability) or log_probability > 1e-6:
        raise ValueError("Residue log probabilities must be finite and nonpositive")
    return {"position": position, "residue": residue, "log_probability": log_probability,
        "probability": math.exp(log_probability),
        "pseudo_perplexity": math.exp(-log_probability) if -log_probability < 709 else None}


def review_thresholds() -> dict[str, dict[str, float] | None]:
    """Load independently configurable VH/VL decision thresholds."""

    thresholds = {}
    for chain in ("VH", "VL"):
        prefix = f"DATASETAGENT_ESM2_{chain}"
        passed, failed = os.environ.get(f"{prefix}_PASS_PLL"), os.environ.get(f"{prefix}_FAIL_PLL")
        if passed is None and failed is None:
            thresholds[chain] = None
            continue
        if passed is None or failed is None:
            raise ValueError(f"Both {chain} pass/fail thresholds must be supplied")
        passed, failed = float(passed), float(failed)
        if not math.isfinite(passed) or not math.isfinite(failed) or failed >= passed:
            raise ValueError(
                f"{chain} ESM-2 fail threshold must be lower than pass threshold"
            )
        thresholds[chain] = {"pass": passed, "fail": failed}
    return thresholds


def classify_chain(
    chain: str,
    sequence: str,
    metrics: dict[str, float] | None,
    thresholds: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """Turn one chain's score into the three review states used by the UI."""

    normalized = "".join(str(sequence).split()).upper()
    invalid = sorted(set(normalized) - AA)
    decision = thresholds[chain]
    if not normalized or invalid:
        return {
            "status": "error",
            "length": len(normalized),
            "invalid_characters": invalid,
            "thresholds": decision,
            "message": (
                f"{chain} is missing."
                if not normalized
                else f"{chain} contains invalid amino-acid characters: {', '.join(invalid)}."
            ),
        }
    if metrics is None:
        return {
            "status": "manual_review",
            "length": len(normalized),
            "thresholds": decision,
            "message": f"{chain} did not receive an ESM-2 score.",
        }

    mean_pll = float(metrics["mean_pll"])
    if not all(math.isfinite(float(metrics[k])) for k in ("mean_pll", "min_log_probability", "bottom_decile_mean")):
        raise ValueError("ESM-2 returned nonfinite log probabilities")
    minimum = float(metrics["min_log_probability"])
    if minimum > 1e-6 or mean_pll > 1e-6:
        raise ValueError("ESM-2 log probabilities must be nonpositive")
    raw_positions = metrics.get("position_scores", metrics.get("lowest_positions", []))
    positions, seen = [], set()
    for item in raw_positions:
        index = item["position"]
        if not isinstance(index, int) or index < 1 or index > len(normalized) or index in seen or item["residue"] != normalized[index - 1]:
            raise ValueError("ESM-2 position score does not match the evaluated residue")
        seen.add(index)
        positions.append(position_score(index, item["residue"], float(item["log_probability"])))
    complete = "position_scores" in metrics
    if complete and (len(positions) != len(normalized) or not math.isclose(min(p["log_probability"] for p in positions), minimum, abs_tol=1e-6)
                     or not math.isclose(sum(p["log_probability"] for p in positions) / len(positions), mean_pll, abs_tol=1e-6)):
        raise ValueError("ESM-2 position scores are incomplete or inconsistent with chain metrics")
    maximum, threshold_source = site_threshold(chain)
    flagged = [p for p in positions if maximum is not None and p["log_probability"] <= -math.log(maximum)]
    site_failure = maximum is not None and minimum <= -math.log(maximum)
    status = (
        "manual_review" if decision is None else "passed"
        if mean_pll >= decision["pass"]
        else "error"
        if mean_pll <= decision["fail"]
        else "manual_review"
    )
    message = {
        "passed": f"{chain} ESM-2 score meets the configured pass threshold.",
        "error": f"{chain} ESM-2 score is below the configured failure threshold.",
        "manual_review": f"{chain} ESM-2 score falls in the manual-review interval.",
    }[status]
    if decision is None:
        message = f"{chain} ESM-2 score computed; pass/fail thresholds have not been configured."
    if site_failure:
        status = "error"
        locations = ", ".join(f"{p['position']}{p['residue']}" for p in flagged)
        message = (f"{chain} high site pseudo-perplexity risk: {locations or 'location details unavailable'}; "
                   f"threshold >= {maximum:g} (residue probability <= {1 / maximum:g}). "
                   "This is a model risk flag; verify against the original source.")
    elif decision is None and maximum is not None:
        status = "passed"
        message = f"{chain} has no site reaching the configured pseudo-perplexity risk threshold {maximum:g}."
    return {
        "status": status,
        "metric_status": "scored",
        "length": len(normalized),
        "mean_pll": mean_pll,
        "pseudo_perplexity": math.exp(-mean_pll) if -mean_pll < 709 else None,
        "min_log_probability": float(metrics["min_log_probability"]),
        "bottom_decile_mean": float(metrics["bottom_decile_mean"]),
        "masked_position_count": metrics.get("masked_position_count", len(normalized)),
        "position_scores": sorted(positions, key=lambda p: p["position"]) if complete else [],
        "position_scores_complete": complete,
        "lowest_positions": sorted(positions, key=lambda p: p["log_probability"])[:10],
        "flagged_positions": sorted(flagged, key=lambda p: p["log_probability"]),
        "flagged_position_count": len(flagged) if complete else None,
        "max_site_pseudo_perplexity": math.exp(-minimum) if -minimum < 709 else None,
        "max_site_log_perplexity": -minimum,
        "site_screening": {"enabled": maximum is not None, "max_pseudo_perplexity": maximum,
            "min_probability": 1 / maximum if maximum is not None else None,
            "threshold_source": threshold_source, "risk_detected": site_failure,
            "interpretation": "model_risk_screen_not_confirmed_sequence_error"},
        "thresholds": decision,
        "message": message,
    }


def pair_review(
    vh: str,
    vl: str,
    scores: dict[str, dict[str, float]],
    thresholds: dict[str, dict[str, float]],
    model_name: str,
) -> dict[str, Any]:
    chains = {
        "VH": classify_chain("VH", vh, scores.get(vh), thresholds),
        "VL": classify_chain("VL", vl, scores.get(vl), thresholds),
    }
    statuses = {item["status"] for item in chains.values()}
    status = (
        "error"
        if "error" in statuses
        else "passed"
        if statuses == {"passed"}
        else "manual_review"
    )
    message = {
        "passed": "Both chains passed ESM-2 sequence-plausibility review.",
        "error": "At least one chain failed ESM-2 sequence-plausibility review.",
        "manual_review": "At least one chain has an inconclusive ESM-2 score.",
    }[status]
    return {
        "status": status,
        "model": model_name,
        "method": "masked_pseudo_log_likelihood",
        "chains": chains,
        "message": message,
    }


def score_sequences(
    sequences: list[str],
    model_path: Path,
    *,
    device: str,
    batch_size: int,
) -> dict[str, dict[str, float]]:
    """Compute exact one-position-at-a-time masked PLL for unique sequences."""

    import torch
    from transformers import AutoTokenizer, EsmForMaskedLM

    if device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable in the configured ESM-2 runtime")

    if batch_size < 1:
        raise ValueError("ESM-2 batch_size must be positive")
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    model = EsmForMaskedLM.from_pretrained(model_path, local_files_only=True)
    model = model.to(device).eval()
    special_ids = set(tokenizer.all_special_ids)
    mask_id = tokenizer.mask_token_id
    if mask_id is None:
        raise RuntimeError("ESM-2 tokenizer has no mask token")

    results: dict[str, dict[str, float]] = {}
    for sequence in dict.fromkeys(sequences):
        normalized = "".join(str(sequence).split()).upper()
        if not normalized or set(normalized) - AA:
            continue
        token_ids = tokenizer(
            normalized,
            add_special_tokens=True,
            return_tensors="pt",
        )["input_ids"][0]
        positions = [
            index
            for index, token_id in enumerate(token_ids.tolist())
            if token_id not in special_ids
        ]
        if len(positions) != len(normalized):
            raise ValueError("ESM-2 tokenizer did not preserve one token per amino acid")
        maximum = getattr(model.config, "max_position_embeddings", 1026)
        if len(token_ids) > maximum:
            raise ValueError("Sequence exceeds the model position limit")
        log_probabilities: list[float] = []
        with torch.inference_mode():
            for start in range(0, len(positions), batch_size):
                chunk = positions[start : start + batch_size]
                batch = token_ids.unsqueeze(0).repeat(len(chunk), 1)
                rows = torch.arange(len(chunk))
                columns = torch.tensor(chunk)
                truth = batch[rows, columns].clone().to(device)
                batch[rows, columns] = mask_id
                logits = model(input_ids=batch.to(device)).logits[
                    rows.to(device), columns.to(device)
                ]
                values = (
                    logits.float()
                    .log_softmax(dim=-1)
                    .gather(1, truth[:, None])
                    .squeeze(1)
                    .cpu()
                    .tolist()
                )
                log_probabilities.extend(float(value) for value in values)
        ordered = sorted(log_probabilities)
        bottom_count = max(1, len(ordered) // 10)
        results[normalized] = {
            "mean_pll": sum(ordered) / len(ordered),
            "min_log_probability": ordered[0],
            "bottom_decile_mean": sum(ordered[:bottom_count]) / bottom_count,
            "masked_position_count": len(positions),
            "position_scores": [position_score(i + 1, normalized[i], value)
                                for i, value in enumerate(log_probabilities)],
            "lowest_positions": [{"position": i + 1, "residue": normalized[i], "log_probability": value}
                for i, value in sorted(enumerate(log_probabilities), key=lambda x: x[1])[:10]],
        }
    return results


def review_sequences(items, model_path, *, device="cuda", batch_size=32):
    """Hash-grounded chain input used by the shared evaluation runtime."""
    thresholds = review_thresholds()
    scores = score_sequences([item["sequence"] for item in items], model_path,
                             device=device, batch_size=batch_size)
    model_config = json.loads((model_path / "config.json").read_text())
    model_name = model_config.get("_name_or_path") or model_path.parent.parent.name.removeprefix("models--").replace("--", "/")
    return [{"id": item["id"], "sequence_sha256": hashlib.sha256(item["sequence"].encode()).hexdigest(),
        "review": {**classify_chain(item["chain"], item["sequence"], scores.get(item["sequence"]), thresholds),
                   "model": model_name, "method": "masked_pseudo_log_likelihood"}}
        for item in items]


def review_pairs(
    pairs: list[dict[str, Any]],
    model_path: Path,
    *,
    device: str = "cuda",
    batch_size: int = 64,
) -> list[dict[str, Any]]:
    thresholds = review_thresholds()
    sequences = [
        "".join(str(pair.get(chain, "")).split()).upper()
        for pair in pairs
        for chain in ("vh", "vl")
    ]
    scores = score_sequences(
        sequences,
        model_path,
        device=device,
        batch_size=batch_size,
    )
    model_name = "esm2_t33_650M_UR50D"
    return [
        {
            "construct_key": str(pair.get("construct_key", "")),
            "review": pair_review(
                "".join(str(pair.get("vh", "")).split()).upper(),
                "".join(str(pair.get("vl", "")).split()).upper(),
                scores,
                thresholds,
                model_name,
            ),
        }
        for pair in pairs
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Score VH/VL pairs with local ESM-2")
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--sequence-input", action="store_true")
    parser.add_argument("--device", default=os.environ.get("DATASETAGENT_ESM2_DEVICE", "cuda"))
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("DATASETAGENT_ESM2_BATCH_SIZE", "64")),
    )
    args = parser.parse_args()
    payload = json.load(sys.stdin)
    if not isinstance(payload, list):
        raise ValueError("ESM-2 review input must be a JSON list")
    result = (review_sequences if args.sequence_input else review_pairs)(
        payload,
        args.model,
        device=args.device,
        batch_size=max(1, args.batch_size),
    )
    json.dump(result, sys.stdout, ensure_ascii=False, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
