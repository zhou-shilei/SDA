"""Conservative comparison candidates; grouping never certifies comparability."""
import hashlib
import json


def canonical(value):
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        return sorted((canonical(v) for v in value), key=packed)
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items())}
    return value


def packed(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


DIMENSIONS = ("document_id", "experiment_id", "assay_protocol_id", "assay_endpoint",
              "ic50_unit", "antigen_target", "antigen_species", "antigen_construct",
              "assay_method", "assay_system", "assay_condition", "donor_id", "replicate_id",
              "batch_id", "construct_type", "assay_purpose", "assay_readout", "cell_model",
              "cell_host_species", "sample")


def assign_comparison_candidates(records):
    """Use explicit experiment identity plus every recorded assay dimension.

    Missing dimensions remain unknown. Do not merge experiments, patents, or
    records just because their endpoint/table matches. Only multi-molecule
    buckets receive G labels. The review signature covers membership and data.
    """
    buckets = {}
    for row in records:
        if row.get("comparison_group_id"):
            continue  # Preserve source-supplied groups and statuses.
        if not all(row.get(k) for k in ("document_id", "experiment_id", "assay_endpoint")):
            continue
        dimensions = {k: canonical(row.get(k)) for k in DIMENSIONS}
        dimensions["annotations"] = canonical([
            {k: tag.get(k) for k in ("category", "key", "target_id")}
            for tag in row.get("assay_keywords", []) if isinstance(tag, dict)])
        buckets.setdefault(packed(dimensions), []).append(row)
    reserved = {r.get("comparison_group_id") for r in records}
    number = 0
    for key, members in sorted(buckets.items()):
        molecules = {r.get("entity_id") or r.get("canonical_entity_id") or r.get("antibody_id") for r in members}
        if len(molecules - {None, ""}) < 2:
            continue
        number += 1
        while f"G{number}" in reserved:
            number += 1
        group = f"G{number}"
        contents = sorted((canonical({k: r.get(k) for k in (
            "record_id", "entity_id", "antibody_id", "ic50", "ic50_unit", "vh", "vl",
            "h_chain", "l_chain", "table_id")}) for r in members), key=packed)
        fingerprint = hashlib.sha256(packed(["candidate-v1", key, contents]).encode()).hexdigest()
        for row in members:
            row.update(comparison_group_id=group, comparison_status="candidate",
                       comparison_signature=fingerprint, comparison_group_size=len(members),
                       comparison_basis={k: v for k, v in json.loads(key).items() if v})
    return records


def apply_comparison_reviews(records, reviews, corrections=None):
    """Return snapshot copies with decisions matching the current group data."""
    effective = lambda row: {k: (corrections or {}).get(
        f"{row.get('document_id')}::{row.get('record_id')}::{k}", {}).get("value", row.get(k))
        for k in ("vh", "vl", "ic50", "ic50_unit")}
    invalid = set()
    for row in records:
        review = reviews.get(f"{row.get('document_id')}::{row.get('record_id')}::comparison_group", {})
        if (review.get("comparison_signature") == row.get("comparison_signature")
                and "comparison_values" in review and review["comparison_values"] != effective(row)):
            invalid.add(row.get("comparison_signature"))
    result = []
    for row in records:
        row = dict(row)
        review = reviews.get(f"{row.get('document_id')}::{row.get('record_id')}::comparison_group")
        current_values = effective(row)
        row.update(current_values)
        if (review and row.get("comparison_signature")
                and row["comparison_signature"] not in invalid
                and review.get("comparison_signature") == row["comparison_signature"]
                and ("comparison_values" not in review or review["comparison_values"] == current_values)
                and review.get("status") in {"confirmed", "rejected", "candidate"}):
            row.update(comparison_status=review["status"],
                       comparison_reviewer=review.get("reviewer_name"),
                       comparison_reviewed_at=review.get("updated_at"))
        result.append(row)
    return result
