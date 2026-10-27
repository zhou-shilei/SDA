"""Export separately labelled computational domains with exact OCR fragments."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re

from .ledger import AA, residues
from .provider import digest, write_json


def slice_fragments(fragments, sources, start, end):
    """Map residue offsets back to original character offsets without editing residues."""
    if not 0 <= start < end:
        raise ValueError("Invalid domain interval")
    output, cursor = [], 0
    for fragment in fragments:
        source = sources[str(fragment["s"])] if str(fragment["s"]) in sources else sources[fragment["s"]]
        quote = source["text"][fragment["start"]:fragment["end"]]
        if quote != fragment["quote"] or digest(source["text"]) != fragment["source_sha256"]:
            raise ValueError("Source text or fragment identity changed")
        positions = []
        for match in re.finditer(r"<br\s*/?>|\s+|.", quote, flags=re.S | re.I):
            token = match.group()
            if token.isspace() or re.fullmatch(r"<br\s*/?>", token, re.I):
                continue
            if token not in AA:
                raise ValueError("Cannot derive a domain from invalid literal residues")
            positions.append((match.start(), match.end()))
        if residues(quote) != fragment["sequence"] or len(positions) != len(fragment["sequence"]):
            raise ValueError("Full-chain fragment residue mismatch")
        low, high = max(0, start - cursor), min(len(positions), end - cursor)
        if low < high:
            a, b = positions[low][0], positions[high - 1][1]
            output.append({**fragment, "start": fragment["start"] + a,
                "end": fragment["start"] + b, "quote": quote[a:b], "sequence": residues(quote[a:b])})
        cursor += len(positions)
    if end > cursor or sum(len(f["sequence"]) for f in output) != end - start:
        raise ValueError("Domain extends beyond supplied literal full-chain fragments")
    return output


def augment(dataset_path, domains_path, sources_path, output):
    dataset_path, output = Path(dataset_path), Path(output)
    if output.resolve() in (dataset_path.resolve(), dataset_path.with_name("dataset.json").resolve()):
        raise ValueError("Computational domains must use a separate output file")
    dataset = json.loads(dataset_path.read_text())
    domains = json.loads(Path(domains_path).read_text())
    sources = json.loads(Path(sources_path).read_text())
    input_sha = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    if input_sha != domains["input_sha256"] or dataset["document_id"] != domains["document"]:
        raise ValueError("Domain analysis belongs to a different dataset snapshot")
    result = copy.deepcopy(dataset)
    entities = {e["id"]: e for e in result["entities"]}
    added = []
    for candidate in domains["candidates"]:
        item, chain = candidate["item"], candidate["chain"]
        if chain not in ("VH", "VL") or not candidate["numbering_matches_literal"]:
            raise ValueError("Candidate lacks a verified numbered literal domain")
        entity = entities[item]
        if entity["sequences"][chain]["status"] == "supported":
            raise ValueError("Computational candidate would overwrite an explicit domain")
        full_chain = "H_CHAIN" if chain == "VH" else "L_CHAIN"
        expected_types = {"H"} if chain == "VH" else {"K", "L"}
        full = entity["sequences"][full_chain]
        start, end = candidate["start"], candidate["end"]
        if candidate["chain_type"] not in expected_types or full["status"] != "supported" or full["seq_id"] != candidate["full_seq_id"]:
            raise ValueError("Computational domain conflicts with full-chain ownership/type")
        if full["value"][start:end] != candidate["sequence"]:
            raise ValueError("Candidate changes original full-chain residues")
        fragments = slice_fragments(full["fragments"], sources, start, end)
        if "".join(f["sequence"] for f in fragments) != candidate["sequence"]:
            raise ValueError("Domain fragments do not reconstruct the literal candidate")
        entity["sequences"][chain] = {"status": "computed_domain", "value": candidate["sequence"],
            "seq_id": full["seq_id"], "seq_id_scope": "full_chain",
            "definition_refs": full["definition_refs"], "mapping_refs": full["mapping_refs"],
            "fragments": fragments, "boundary_method": "ANARCI/IMGT",
            "full_chain_residue_start": start, "full_chain_residue_end": end,
            "bitscore": candidate["bitscore"], "evalue": candidate["evalue"],
            "validation": "Literal residues checked; computational boundary, not explicit patent boundary"}
        added.append({"item": item, "chain": chain})
    for entity in result["entities"]:
        entity["missing_explicit_domains"] = entity["missing"]
        entity["missing_including_computed"] = [c for c in ("VH", "VL") if not entity["sequences"][c]["value"]]
    explicit = dataset["summary"]["variable_chains"]
    result["computational_augmentation"] = {"model_tokens": 0, "input_sha256": input_sha,
        "explicit_variable_slots": explicit, "computed_variable_slots": len(added),
        "populated_variable_slots_including_computed": explicit + len(added),
        "complete_pairs_including_computed": sum(not e["missing_including_computed"] for e in result["entities"]),
        "note": "Explicit extraction counters remain unchanged. Computational domains are separately labelled; no OCR correction or independent ownership validation.",
        "added": added}
    write_json(output, result)
    return result["computational_augmentation"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("domains", type=Path)
    parser.add_argument("sources", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(augment(args.dataset, args.domains, args.sources, args.output), ensure_ascii=False))
