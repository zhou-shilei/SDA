"""Optional local ANARCI feasibility check. Does not modify extraction results."""

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import sys


def probe(dataset_path, output_path, hmmer_dir):
    from anarci import anarci

    dataset_path = Path(dataset_path)
    dataset = json.loads(dataset_path.read_text())
    sequences = list(dict.fromkeys(d["value"] for d in dataset["sequences"]
                                  if d["region"] in ("H_CHAIN", "L_CHAIN")))
    annotations = {}
    if sequences:
        with contextlib.redirect_stdout(sys.stderr):
            numbered, details, _ = anarci([(str(i), s) for i, s in enumerate(sequences)],
                scheme="imgt", allow={"H", "K", "L"}, hmmerpath=str(hmmer_dir), ncpu=1)
        if len(numbered) != len(sequences) or len(details) != len(sequences):
            raise ValueError("ANARCI response count mismatch")
        for sequence, domains, info in zip(sequences, numbered, details):
            records = []
            for (positions, start, last), detail in zip(domains or [], info or []):
                literal = sequence[int(start):int(last) + 1]
                reconstructed = "".join(aa for _, aa in positions if aa != "-")
                records.append({"start": int(start), "end": int(last) + 1,
                    "chain_type": detail.get("chain_type"), "bitscore": detail.get("bitscore"),
                    "evalue": detail.get("evalue"), "sequence": literal,
                    "numbering_matches_literal": literal == reconstructed})
            annotations[sequence] = records
    candidates, unresolved = [], []
    for entity in dataset["entities"]:
        for chain, full_chain, types in [("VH", "H_CHAIN", {"H"}), ("VL", "L_CHAIN", {"K", "L"})]:
            if entity["sequences"][chain]["status"] == "supported":
                continue
            full = entity["sequences"][full_chain]
            value = full.get("value")
            domains = annotations.get(value, [])
            record = {"item": entity["id"], "name": entity["name"], "chain": chain,
                      "full_seq_id": full.get("seq_id"), "definition_refs": full.get("definition_refs", []),
                      "mapping_refs": full.get("mapping_refs", [])}
            if len(domains) == 1 and domains[0]["chain_type"] in types and domains[0]["numbering_matches_literal"]:
                domain = domains[0]
                if 65 <= len(domain["sequence"]) <= 180:
                    candidates.append({**record, **domain, "status": "computational_boundary_candidate"})
                    continue
            unresolved.append({**record, "reason": "no unambiguous matching domain in a supported full chain"})
    result = {"document": dataset["document_id"], "input_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "model_tokens": 0, "full_sequence_values_numbered": len(sequences),
        "existing_explicit_variable_slots": dataset["summary"]["variable_chains"],
        "additional_computational_candidate_slots": len(candidates),
        "candidates": candidates, "unresolved": unresolved,
        "note": "Separate feasibility analysis. HMM boundaries are computational predictions, not explicit patent boundaries. Does not repair characters, infer ownership or update dataset.json."}
    Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return {k: v for k, v in result.items() if k not in ("candidates", "unresolved")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--hmmer-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(probe(args.dataset, args.output, args.hmmer_dir), ensure_ascii=False))
