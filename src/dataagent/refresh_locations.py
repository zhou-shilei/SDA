"""Refresh derived sequence geometry in existing results, without rerunning agents.

Usage: python -m dataagent.refresh_locations /path/to/results/DOCUMENT [...]
Only location fields change. Original files are backed up before atomic writes.
"""

import argparse
import copy
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from document.cell_geometry import geometry_fields
from .main_pipeline import _sources_and_coverage, read, save_json
from .sequence_evidence import attach_sequence_locations, locate_sequence_provenance


def refresh_result_locations(result_dir, *, backup_dir=None):
    result_dir = Path(result_dir).resolve()
    paths = [result_dir / name for name in ("dataset.json", "template.completed.json", "template.json", "document_sources.json")]
    paths += [p for p in result_dir.glob("*.json") if p not in paths
              and isinstance(read(p, {}), dict) and "table_text" in read(p, {})]
    snapshots = {path: path.read_bytes() for path in paths if path.is_file()}
    dataset = json.loads(snapshots[result_dir / "dataset.json"])
    template = json.loads(snapshots[result_dir / "template.completed.json"])
    native_dir = Path(dataset.get("dataagent_directory", ""))
    native_paths = [native_dir / "agent_state.json", native_dir / "sda_input.json"]
    native_snapshots = {p: p.read_bytes() for p in native_paths if dataset.get("dataagent_directory") and p.is_file()}
    resolved, prepared, sources = {}, {}, {}
    if len(native_snapshots) == 2:
        state, prepared = (json.loads(native_snapshots[p]) for p in native_paths)
        sources, _, _ = _sources_and_coverage(state, prepared)
        resolved = {r["construct_key"]: r for r in state.get("resolved_sequences", [])}
        attach_sequence_locations(resolved, [], prepared["tables"], state.get("assay_method_pages", []),
                                  prepared.get("candidates", []), sources=sources)
    locations, values = {}, {}
    for entity in template["entities"]:
        native = resolved.get(entity.get("original_entity_id"), {})
        per_chain = {}
        for field in ("vh", "vl"):
            chain = entity.get(field.upper(), {})
            slot = chain.get("sequence", {})
            value = slot.get("value") if slot.get("status") == "present" else None
            if value and value == native.get(field):
                per_chain[field] = copy.deepcopy(native["sequence_locations"][field])
            else:
                per_chain[field] = locate_sequence_provenance(value, slot.get("provenance", []), chain=field,
                                                             seq_id=chain.get("seq_id", {}).get("value"))
        entity["sequence_locations"] = per_chain
        locations[entity["entity_id"]] = per_chain
        values[entity["entity_id"]] = {field: entity.get(field.upper(), {}).get("sequence", {}).get("value")
                                         for field in ("vh", "vl")}
    for entity in dataset.get("entities", []):
        if entity["entity_id"] in locations:
            entity["sequence_locations"] = {field: copy.deepcopy(location)
                for field, location in locations[entity["entity_id"]].items()
                if entity.get(field.upper(), {}).get("sequence") == values[entity["entity_id"]][field]}
    updates = {result_dir / "dataset.json": dataset, result_dir / "template.completed.json": template}
    if result_dir / "template.json" in snapshots:
        base = json.loads(snapshots[result_dir / "template.json"])
        for entity in base.get("entities", []):
            if entity["entity_id"] in locations:
                entity["sequence_locations"] = {field: copy.deepcopy(location)
                    for field, location in locations[entity["entity_id"]].items()
                    if entity.get(field.upper(), {}).get("sequence", {}).get("value") == values[entity["entity_id"]][field]}
        updates[result_dir / "template.json"] = base
    source_path = result_dir / "document_sources.json"
    if source_path in snapshots and sources:
        current_sources = json.loads(snapshots[source_path])
        for ref, source in current_sources.items():
            if ref in sources and source.get("text") == sources[ref].get("text"):
                source.update(geometry_fields(sources[ref]))
        updates[source_path] = current_sources
    tables = {t["source_id"]: t for t in prepared.get("tables", [])}
    for path, contents in snapshots.items():
        if path in updates:
            continue
        value = json.loads(contents)
        table = tables.get(value.get("table_id"))
        if table and value.get("table_text") == table["sda_source"]["text"]:
            evidence = table["sda_source"]["evidence"]
            value.setdefault("table_evidence", {}).update({key: copy.deepcopy(evidence[key]) for key in
                ("cells", "grid", "cell_locations", "bbox_padded", "crop_to_pdf", "width_px", "height_px") if key in evidence})
            updates[path] = value
    # Refuse to overwrite results or agent inputs changed by another running job.
    for path, contents in {**snapshots, **native_snapshots}.items():
        if path.read_bytes() != contents:
            raise RuntimeError(f"Results changed during location refresh: {path}; retry on the latest result.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = Path(backup_dir) if backup_dir else result_dir / "location_backups" / stamp
    backup.mkdir(parents=True, exist_ok=False)
    for path in updates:
        (backup / path.name).write_bytes(snapshots[path])
    for path, value in updates.items():
        save_json(path, value)
    nonempty = [location for pair in locations.values() for location in pair.values() if location["length"]]
    report = {"document_id": dataset["document_id"], "result_dir": str(result_dir), "backup_dir": str(backup),
              "sequences": len(nonempty), "statuses": dict(Counter(l["status"] for l in nonempty)),
              "precision": dict(Counter(s["precision"] for l in nonempty for s in l["segments"])),
              "scope": "Derived geometry only; existing sequence values, measurements and decisions are preserved.",
              "original_sha256": {str(path): hashlib.sha256(snapshots[path]).hexdigest() for path in updates}}
    save_json(result_dir / "sequence_locations.json", {"summary": report, "entities": locations})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_dirs", type=Path, nargs="+")
    args = parser.parse_args(argv)
    for directory in args.result_dirs:
        report = refresh_result_locations(directory)
        print(json.dumps({key: report[key] for key in
                          ("document_id", "sequences", "statuses", "precision", "backup_dir")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
