"""Optional, explicit record references for a complete ActionAgent dataset.

The model still specifies every output record and every semantic decision. The
host copies only those exact selected records; it makes no matching decisions.
"""
import copy

from .action_agent import SCHEMA_ACTION, DATASET_SCHEMA, target_at
from .contracts import S, obj
from .ledger import ground_sequence


SCHEMA_ACTION_RECORD_REFS = copy.deepcopy(SCHEMA_ACTION)
for _kind, _spec in SCHEMA_ACTION_RECORD_REFS["properties"]["dataset"]["properties"].items():
    if _kind == "document_id":
        continue
    _choices = [_spec["items"], obj({"seed_path": S}),
                obj({"seed_collection": {"type": "string", "enum": ["all"]},
                     "exclude": {"type": "array", "items": {"type": "integer", "minimum": 0}}})]
    if _kind != "entities":
        _choices.append(obj({"action_id": {"type": ["string", "integer"]}}))
    _spec["items"] = {"anyOf": _choices}
for _kind in ("action_decisions", "conflicts"):
    SCHEMA_ACTION_RECORD_REFS["properties"][_kind]["items"]["properties"]["action_ids"]["items"] = {
        "anyOf": [{"type": ["string", "integer"]},
                  obj({"start": {"type": "integer", "minimum": 0},
                       "stop": {"type": "integer", "minimum": 1}})]}


OUTPUT_ENCODING = """For unchanged records, use the exact record-reference forms allowed by the
output schema instead of copying their full fields. A dataset array element may be
{"seed_path":"/initial_dataset/observations/0"} or {"action_id":"c1:observations:0"}.
Each single-record reference occupies one output array position. The host copies that exact
record and runs all normal completeness and evidence checks; it performs no semantic
merge. Every complete output array must be explicitly specified. Changed/new records
must use the full record form. Entities may reuse initial seed records; new entities
need explicit IDs and all entity fields. Reader sequence references reuse their exact
parts; the host materializes the literal value from those addressed parts. References
must stay in the same fact kind. action_decisions targets address the expanded arrays,
whose order and indices are exactly the order you return. All actions still need one
decision; all initial facts still need preservation or explicit seed_changes.
Prefer the zero-based _position integer for action_id and action_decisions.action_ids;
it is an exact alias for that Reader action, not a source ID. Source refs stay source
IDs. Compatible actions with the same outcome may share one concise decision group;
include every action reference and all of their output targets, without repeating
the same rationale per occurrence. This changes representation only.
Prefer {"seed_collection":"all","exclude":[]} to preserve an entire unchanged
initial collection. It expands in original order; exclude contains only explicit
zero-based initial-record indices to replace/reject, with the normal seed_changes
accounting. This avoids enumerating or inventing seed paths. Only valid seed paths
are allowed by the schema. Collection targets /dataset/links (or /dataset/links/*)
select every actual record in that output collection; the host expands them into
record pointers. All targets address the expanded output arrays.
In action_decisions.action_ids and conflicts.action_ids, a contiguous run of action
positions may instead use {"start":0,"stop":100}, meaning positions 0 through 99.
stop is exclusive. This is only an exact positional abbreviation, never a semantic
default: use a range only when every included action has the stated disposition.
Mixed ranges, individual positions and original IDs are allowed. No overlap,
omission or out-of-bounds position is allowed; the host expands every occurrence
and still validates complete, exactly-once accounting and all retained evidence.
"""


def expand_record_refs(response, payload, data):
    result = copy.deepcopy(response)
    actions = {a["id"]: a for a in payload["reader_actions"]}
    def identity(value):
        if type(value) is int:
            if value < 0 or value >= len(payload["reader_actions"]):
                raise ValueError("Action position is outside the supplied Reader actions")
            return payload["reader_actions"][value]["id"]
        if not isinstance(value, str) or value not in actions:
            raise ValueError("Unknown Reader action reference")
        return value

    def identities(values):
        expanded = []
        for value in values:
            if isinstance(value, dict):
                if (set(value) != {"start", "stop"} or type(value["start"]) is not int
                        or type(value["stop"]) is not int
                        or not 0 <= value["start"] < value["stop"] <= len(payload["reader_actions"])):
                    raise ValueError("Action range must be nonempty and inside the supplied Reader actions")
                expanded.extend(identity(i) for i in range(value["start"], value["stop"]))
            else:
                expanded.append(identity(value))
        return expanded
    for kind, records in result["dataset"].items():
        if kind == "document_id":
            continue
        fields = DATASET_SCHEMA["properties"][kind]["items"]["properties"]
        expanded = []
        for record in records:
            if set(record) == {"seed_path"}:
                path = record["seed_path"]
                if not path.startswith(f"/initial_dataset/{kind}/"):
                    raise ValueError("Seed reference must stay in its dataset kind")
                expanded.append(copy.deepcopy(target_at(payload["initial_dataset"], path, "/initial_dataset/")))
            elif set(record) == {"seed_collection", "exclude"}:
                original = payload["initial_dataset"][kind]
                if record["seed_collection"] != "all" or any(type(i) is not int or i < 0 or i >= len(original) for i in record["exclude"]):
                    raise ValueError("Seed collection exclusion does not exist")
                excluded = set(record["exclude"])
                expanded.extend(copy.deepcopy(value) for i, value in enumerate(original) if i not in excluded)
            elif set(record) == {"action_id"}:
                action = actions.get(identity(record["action_id"]))
                if kind == "entities" or action is None or action["kind"] != kind:
                    raise ValueError("Reader reference must address a known action of the same fact kind")
                fact = copy.deepcopy(action["fact"])
                if kind == "sequences":
                    fact["value"], _ = ground_sequence(fact["parts"], data["sources"])
                if set(fact) != set(fields):
                    raise ValueError("Referenced Reader fact does not have the complete canonical record fields")
                expanded.append(fact)
            else:
                expanded.append(record)
        result["dataset"][kind] = expanded
    for decision in result["action_decisions"]:
        decision["action_ids"] = identities(decision["action_ids"])
    for decision in [*result["action_decisions"], *result["seed_changes"]]:
        targets = []
        for path in decision["targets"]:
            collection = path[:-2] if path.endswith("/*") else path
            if collection.startswith("/dataset/") and collection.count("/") == 2:
                kind = collection.split("/")[2]
                records = result["dataset"].get(kind)
                if not isinstance(records, list):
                    raise ValueError("Collection target does not address a dataset collection")
                targets.extend(f"/dataset/{kind}/{i}" for i in range(len(records)))
            else:
                targets.append(path)
        decision["targets"] = list(dict.fromkeys(targets))
    for conflict in result["conflicts"]:
        conflict["action_ids"] = identities(conflict["action_ids"])
    return result


def record_reference_schema(payload):
    """Constrain reference syntax to this request's real seed addresses."""
    schema = copy.deepcopy(SCHEMA_ACTION_RECORD_REFS)
    raw_actions = payload["reader_actions"]
    count = len(raw_actions) if isinstance(raw_actions, list) else sum(len(t["rows"]) for t in raw_actions.values())
    bounds = {"minimum": 0, "maximum": max(0, count - 1)}
    for kind, spec in schema["properties"]["dataset"]["properties"].items():
        if kind == "document_id":
            continue
        size = len(payload["initial_dataset"][kind])
        choices = []
        for choice in spec["items"]["anyOf"]:
            props = choice.get("properties", {})
            if "seed_path" in props:
                if not size:
                    continue
                props["seed_path"]["enum"] = [f"/initial_dataset/{kind}/{i}" for i in range(size)]
            if "action_id" in props:
                props["action_id"].update(bounds)
            if "seed_collection" in props and size:
                props["exclude"]["items"]["maximum"] = size - 1
            choices.append(choice)
        spec["items"]["anyOf"] = choices
    for kind in ("action_decisions", "conflicts"):
        choices = schema["properties"][kind]["items"]["properties"]["action_ids"]["items"]["anyOf"]
        choices[0].update(bounds)
        choices[1]["properties"]["start"].update(bounds)
        choices[1]["properties"]["stop"]["maximum"] = count
    return schema
