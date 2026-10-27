"""Read allowlisted antibody terminology for extraction and review display."""

import copy
import json
from functools import lru_cache
from pathlib import Path

@lru_cache(maxsize=1)
def load_domain_knowledge():
    """Read only reusable terminology, never units, profiles or length rules."""
    path = Path(__file__).resolve().parents[2] / "schemas" / "antibody_knowledge.json"
    if not path.is_file():
        return {"template_id": None, "groups": {}, "scope_patterns": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    groups = {}
    groups["constructs"] = raw.get("antibody_construct_knowledge", {}).get("types", [])
    for key in ("methods", "systems", "species"):
        groups[key] = raw.get("assay", {}).get(key, [])
    # The allowlist deliberately excludes assay.default_unit, document_profiles,
    # variable_length and OCR scan heuristics from the migrated source.
    fields = {"key", "label_zh", "label_en", "patterns", "endpoint", "system", "minimum_score", "summary_zh"}
    return {"template_id": raw.get("template_id"), "version": raw.get("version"),
            "groups": {group: [{k: copy.deepcopy(v) for k, v in row.items() if k in fields}
                               for row in rows] for group, rows in groups.items()},
            "scope_patterns": raw.get("sequence_definition", {}).get("scope_patterns", {})}
