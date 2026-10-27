"""ANARCI/HMMER worker: chain typing and IMGT domain/region numbering."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

AA = set("ACDEFGHIKLMNPQRSTVWY")
IMGT_REGIONS = {"FR1": (1, 26), "CDR1": (27, 38), "FR2": (39, 55),
                "CDR2": (56, 65), "FR3": (66, 104), "CDR3": (105, 117), "FR4": (118, 128)}


def hmmer_directory() -> str | None:
    configured = os.environ.get("DATASETAGENT_HMMER_DIR")
    for directory in [configured, str(Path(sys.executable).parent),
                      str(Path(__file__).resolve().parents[3] / "bin")]:
        if directory and (Path(directory) / "hmmscan").is_file():
            return directory
    executable = shutil.which("hmmscan")
    return str(Path(executable).parent) if executable else None


def pending(message: str) -> dict:
    return {"available": False, "status": "unavailable", "chain_type": None,
            "scheme": "imgt", "domains": [], "message": message}


def runtime_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("anarci") is not None and hmmer_directory() is not None


def domain_details(numbered, detail: dict, sequence: str) -> dict:
    numbering, start, end = numbered
    entries = [{"position": int(pos[0]), "insertion": str(pos[1]).strip(), "residue": residue}
               for pos, residue in numbering]
    regions = {name: "".join(e["residue"] for e in entries if low <= e["position"] <= high
                              and e["residue"] != "-") for name, (low, high) in IMGT_REGIONS.items()}
    return {"chain_type": detail.get("chain_type"), "hmm_species": detail.get("species"),
        "bitscore": detail.get("bitscore"), "evalue": detail.get("evalue"),
        "sequence_start": int(start), "sequence_end": int(end) + 1,
        "numbered_residues": sum(e["residue"] != "-" for e in entries),
        "coverage": sum(e["residue"] != "-" for e in entries) / len(sequence),
        "numbering": entries, "regions": regions}


def review_sequences(items: list[dict], *, hmmerpath: str | None = None, ncpu: int = 1) -> list[dict]:
    from anarci import anarci

    unique = {}
    for item in items:
        sequence = item["sequence"]
        if sequence and set(sequence) <= AA:
            unique.setdefault(sequence, "sequence-" + str(len(unique)))
    sequences = [(identity, seq) for seq, identity in unique.items()]
    results = {}
    if sequences:
        with contextlib.redirect_stdout(sys.stderr):
            numbered, details, _ = anarci(sequences, scheme="imgt", output=False,
                hmmerpath=hmmerpath or hmmer_directory() or "", ncpu=ncpu)
        if len(numbered) != len(sequences) or len(details) != len(sequences):
            raise ValueError("ANARCI returned a different sequence count")
        for (identity, sequence), domains, annotations in zip(sequences, numbered, details):
            results[sequence] = [domain_details(domain, annotation, sequence)
                                 for domain, annotation in zip(domains or [], annotations or [])]
    output = []
    for item in items:
        sequence, declared = item["sequence"], item["chain"]
        domains = results.get(sequence, [])
        chain_type = domains[0]["chain_type"] if len(domains) == 1 else None
        predicted = "VH" if chain_type == "H" else "VL" if chain_type in {"K", "L"} else None
        if not sequence or set(sequence) - AA:
            status, message = "invalid", "Sequence is missing or contains invalid amino-acid characters."
        elif len(domains) > 1:
            status, message = "multiple_domains", "More than one domain was detected in a variable-region field."
        elif not domains:
            status, message = "unclassified", "ANARCI could not number an antigen-receptor domain."
        elif predicted != declared:
            status, message = "conflict", f"ANARCI chain type {chain_type} conflicts with declared {declared}."
        elif domains[0]["coverage"] < 0.9:
            status, message = "partial", "ANARCI numbered less than 90% of this variable-region field."
        else:
            status, message = "matched", f"ANARCI classified and numbered this sequence as {declared}."
        output.append({"id": item["id"], "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
            "review": {"available": True, "status": status, "chain_type": chain_type,
                "scheme": "imgt", "domains": domains, "message": message}})
    return output


def evaluate_chain_in_process(sequence: str, declared_chain: str) -> dict:
    """Lightweight pre-repair check; final evaluation can use another Python environment."""
    if not runtime_available():
        return pending("ANARCI/HMMER is unavailable in the extraction Python; final evaluation uses the configured worker.")
    try:
        return review_sequences([{"id": "query", "sequence": sequence, "chain": declared_chain}])[0]["review"]
    except Exception as exc:
        return {**pending(f"ANARCI execution failed: {type(exc).__name__}: {exc}"), "runtime_error": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hmmer-dir")
    parser.add_argument("--cpus", type=int, default=1)
    args = parser.parse_args()
    items = json.load(sys.stdin)
    if not isinstance(items, list) or args.cpus < 1:
        raise ValueError("ANARCI requires a JSON list and positive CPU count")
    json.dump(review_sequences(items, hmmerpath=args.hmmer_dir, ncpu=args.cpus), sys.stdout, allow_nan=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
