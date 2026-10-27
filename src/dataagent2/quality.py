"""Check literal provenance and expose incomplete sequence/measurement joins."""

from collections import Counter

from .ledger import residues
from .provider import digest


def audit_dataset(dataset, sources):
    entities = {e["id"]: e for e in dataset["entities"]}
    errors, chains = [], 0
    for entity in entities.values():
        for chain, slot in entity["sequences"].items():
            if slot["status"] != "supported":
                continue
            chains += 1
            pieces = []
            for fragment in slot.get("fragments", []):
                source = sources[fragment["s"]]
                quote = source["text"][fragment["start"]:fragment["end"]]
                if (quote != fragment["quote"] or digest(source["text"]) != fragment["source_sha256"]
                        or residues(quote) != fragment["sequence"]):
                    errors.append({"kind": "sequence_source_mismatch", "item": entity["id"], "chain": chain, "s": fragment["s"]})
                pieces.append(residues(quote))
            if "".join(pieces) != slot["value"]:
                errors.append({"kind": "sequence_reconstruction_mismatch", "item": entity["id"], "chain": chain})
    records = []
    for observation in dataset["observations"]:
        entity = entities[observation["item"]]
        value_supported = bool(observation["value"]) and observation["value"] in sources[observation["s"]]["text"]
        unit_supported = not observation.get("unit") or any(observation["unit"] in sources[r]["text"] for r in observation["refs"])
        if not value_supported or not unit_supported:
            errors.append({"kind": "measurement_source_mismatch", "observation": observation["id"],
                           "value_supported": value_supported, "unit_supported": unit_supported})
        missing = [c for c in ("VH", "VL") if entity["sequences"][c]["status"] != "supported"]
        records.append({"observation": observation["id"], "item": entity["id"], "name": entity["name"],
                        "experiment": observation.get("experiment"), "missing_variable_domains": missing,
                        "value_supported": value_supported, "unit_supported": unit_supported})
    return {"scope": "Literal provenance and join completeness; not independent validation against PDF pixels",
            "supported_chain_slots_checked": chains, "literal_errors": errors,
            "measurements": len(records),
            "measurements_with_complete_variable_pair": sum(not r["missing_variable_domains"] for r in records),
            "measured_entities_with_complete_variable_pair": len({r["item"] for r in records if not r["missing_variable_domains"]}),
            "failed_ocr_sources": [sid for sid, s in sources.items() if s.get("sequence_ocr", {}).get("status") == "failed"],
            "unresolved_issue_kinds": dict(Counter(i["kind"] for i in dataset["issues"])),
            "measurement_links": records}
