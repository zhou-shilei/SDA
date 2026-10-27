"""Adapt SDA artifacts to the original DataAgent Studio browser contract."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from document.table_continuation import parse_grid
from units.cache_policy import cache_enabled
from studio.processing_metrics import processing_metrics


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else copy.deepcopy(default)


def slot_value(slot):
    return slot.get("value") if isinstance(slot, dict) and slot.get("status") == "present" else None


def compact(value):
    return re.sub(r"\s+", "", str(value or ""))


def experiment_keywords(experiment, observation, groups):
    """Display only supported species and assay systems; endpoints have their own column."""
    supplied = [*experiment.get("KEYWORDS", []), *experiment.get("TAGS", []),
                *observation.get("keywords", [])]
    output = []
    for category, group, field in (("species", "species", "SPECIES"), ("system", "systems", "SYSTEM")):
        raw = experiment.get(field) or []
        values = re.split(r"[,，;；、|]", raw) if isinstance(raw, str) else list(raw)
        if not values:
            values = [item.get("key", "") if isinstance(item, dict) else item
                      for item in supplied if not isinstance(item, dict) or item.get("category") == category]
        for definition in groups.get(group, []):
            keys = [definition["key"], definition.get("label_zh"), definition.get("label_en")]
            if any(str(value).strip().casefold() in {str(k).casefold() for k in keys if k}
                   or any(re.fullmatch(pattern, str(value).strip(), re.I)
                          for pattern in definition.get("patterns", [])) for value in values if value):
                output.append({"key": definition["key"], "category": category,
                    "label_zh": "human" if definition["key"] == "human" else definition.get("label_zh", definition["key"]),
                    "label_en": definition.get("label_en", definition["key"])})
    return output


def attach_table_text_comparison(record, ocr):
    """Render addressed OCR comparisons without changing stored sequence values."""
    index = {}
    for table in ocr:
        identity = table.get("source_id") or table.get("table_id")
        for ri, row in enumerate(table.get("rows", [])):
            for ci, cell in enumerate(row):
                index[(identity, ri, ci)] = cell
                for ref in (cell.get("ref"), cell.get("source_ref")):
                    if ref:
                        index[ref] = cell
    found = {}
    def visit(value, chain=None):
        if isinstance(value, list):
            for item in value:
                visit(item, chain)
        elif isinstance(value, dict):
            declared = str(value.get("chain", "")).upper()
            chain = declared if declared in {"VH", "VL"} else chain
            chain = {"variable_heavy": "VH", "variable_light": "VL"}.get(value.get("type") or value.get("kind"), chain)
            if chain:
                cell = None
                for field in ("ref", "source_ref", "source_id", "evidence_ref"):
                    ref = value.get(field)
                    if isinstance(ref, str) and ref in index:
                        cell = index[ref]
                        break
                if cell is None:
                    cell = index.get((value.get("table_id"), value.get("row_index"), value.get("col_index")))
                if cell and cell.get("sequence_ocr"):
                    key = (chain, cell.get("ref") or cell.get("source_ref") or str((value.get("table_id"), value.get("row_index"), value.get("col_index"))))
                    found[key] = cell["sequence_ocr"].get("page_comparison", {"status": "uncompared"})
            for key, item in value.items():
                if key not in {"sequence_source_conflicts", "sequence_ocr"}:
                    visit(item, key.upper() if key.upper() in {"VH", "VL"} else chain)
    visit(record)
    result = copy.deepcopy(record)
    for (chain, ref), comparison in found.items():
        if comparison["status"] == "mismatch":
            conflict = {"chain": chain, "table_ref": ref,
                        "text_ref": (comparison.get("page_refs") or [""])[0],
                        "table_quote": comparison["table_text"], "text_quote": comparison["page_text"],
                        "message": "表文不一致；采用表格序列复识别结果，正文差异仅作提示"}
            if conflict not in result.setdefault("sequence_source_conflicts", []):
                result["sequence_source_conflicts"].append(conflict)
    if found:
        result["sequence_table_text_status"] = ("mismatch" if result.get("sequence_source_conflicts") else
            "matched" if all(c["status"] == "matched" for c in found.values()) else "uncompared")
    return result


def review_payload(bundle):
    """Send fields used by the review UI; complete artifacts remain separately addressable."""
    citation_fields = {"ref", "source_ref", "source_id", "line_id", "table_id", "page", "pdf_page",
                       "quote", "source_type", "bbox", "bbox_crop", "bbox_pdf", "bbox_source",
                       "coordinate_space", "precision", "location_status", "value_ref", "cell_refs"}
    records = []
    for record in bundle["records"]:
        record = attach_table_text_comparison(record, bundle.get("ocr", []))
        fields = {name: {"status": slot.get("status"),
                         "provenance": [{k: v for k, v in source.items() if k in citation_fields}
                                        for source in slot.get("provenance", [])]}
                  for name, slot in record.get("sda_fields", {}).items()}
        records.append({**record, "sda_fields": fields})
    return {"records": records, "template": {"agent": bundle.get("template", {}).get("agent", {})},
            **{key: bundle[key] for key in ("contexts", "candidates", "classifications", "ocr", "runtime", "pages")}}


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.state = self.root / ".studio"
        self.state.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.cache = {}
        self.pdf_cache = {}
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS folders (
                    dataset_id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS publications (
                    dataset_id TEXT NOT NULL, document_id TEXT NOT NULL,
                    record_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(dataset_id, document_id, record_id));
                CREATE TABLE IF NOT EXISTS reviews (
                    document_id TEXT NOT NULL, record_id TEXT NOT NULL,
                    reviewer_name TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(document_id, record_id, reviewer_name));
            """)
            db.execute("INSERT OR IGNORE INTO folders VALUES (?, ?, ?)", ("SDA", "SDA", now()))

    def connection(self):
        return sqlite3.connect(self.state / "dataset.sqlite", timeout=20)

    def local_path(self, value, base=None):
        if not value:
            return None
        path = Path(value)
        path = (path if path.is_absolute() else (base or self.root) / path).resolve()
        return path if path.is_relative_to(self.root) and path.is_file() else None

    def jobs(self):
        return read_json(self.state / "jobs.json", {})

    def update_job(self, document_id, **changes):
        with self.lock:
            jobs = self.jobs()
            jobs[document_id] = {**jobs.get(document_id, {}), **changes, "updated_at": now()}
            temporary = self.state / "jobs.json.tmp"
            temporary.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.state / "jobs.json")

    def builtin_locations(self):
        """Original project artifacts remain protected after a Studio reanalysis."""
        documents = {}
        for path in sorted((self.root / "results").glob("*/dataset.json")):
            if not path.resolve().is_relative_to(self.root):
                continue
            data = read_json(path, {})
            identity = data.get("document_id")
            if identity and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}", identity):
                documents[identity] = (path.parent, self.root / "data" / identity)
        return documents

    def is_builtin(self, identity):
        return identity in self.builtin_locations() or self.jobs().get(identity, {}).get("builtin_source") is True

    def locations(self):
        builtin = self.builtin_locations()
        documents = dict(builtin)
        patents_root = (self.state / "patents").resolve()
        if not patents_root.is_relative_to(self.root):
            return documents
        for identity, job in self.jobs().items():
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}", identity):
                continue
            parent = (patents_root / identity).resolve()
            if not parent.is_relative_to(patents_root) or parent != patents_root / identity:
                continue
            result = Path(job.get("result_dir") or parent / "results" / identity)
            inputs = Path(job.get("input_dir") or parent / "tables")
            result = (result if result.is_absolute() else parent / result).resolve()
            inputs = (inputs if inputs.is_absolute() else parent / inputs).resolve()
            # Original built-in inputs are also reusable, without allowing arbitrary project paths.
            original_inputs = builtin.get(identity, (None, None))[1]
            builtin_inputs = (original_inputs is not None and inputs == original_inputs.resolve()
                              and inputs.is_relative_to(self.root))
            if result.is_relative_to(parent) and (inputs.is_relative_to(parent) or builtin_inputs):
                documents[identity] = (result, inputs)
        return documents

    def structured_files(self, document_id, result_dir):
        """仅暴露统一流程的固定产物；兼容历史运行和复用 OCR 的新运行。"""
        job = self.jobs().get(document_id, {})
        report = read_json(result_dir / "run_summary.json", {})
        directory = job.get("structured_dir") or report.get("structured_dir")
        if not directory:
            return {}
        directory = Path(directory).resolve()
        if not directory.is_relative_to(self.root):
            return {}
        names = {"json": "document.json", "markdown": "document.md", "text": "document.txt", "summary": "summary.json"}
        return {key: path for key, name in names.items() if (path := self.local_path(directory / name))}

    def bundle(self, document_id):
        with self.lock:
            location = self.locations().get(document_id)
            if location is None:
                raise KeyError("专利不存在")
            result_dir, input_dir = location
            if not cache_enabled():
                return self._build(document_id, result_dir, input_dir)
            paths = list(result_dir.glob("*.json")) + list(input_dir.glob("*.json"))
            paths += list(input_dir.parent.glob("source.pdf")) + list(input_dir.glob("*.pdf"))
            paths += list((self.state / "patents" / document_id).glob("source.pdf"))
            paths += list(self.structured_files(document_id, result_dir).values())
            signature = (str(result_dir), str(input_dir),
                         tuple(sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)))
            if self.cache.get(document_id, (None,))[0] != signature:
                self.cache[document_id] = (signature, self._build(document_id, result_dir, input_dir))
            return self.cache[document_id][1]

    def page_bundle(self, document_id):
        location = self.locations().get(document_id)
        if location is None:
            raise KeyError("专利不存在")
        _, input_dir = location
        evidence_id = self.jobs().get(document_id, {}).get("evidence_document_id") or document_id
        manifest = {}
        for path in sorted(input_dir.glob("*_manifest.json")):
            candidate = read_json(path, {})
            if (candidate.get("document_id") or candidate.get("patent_id")) == evidence_id:
                manifest = candidate
                break
        pdf = self.source_pdf(document_id, input_dir, manifest)
        pages, _ = self.pdf_metadata(pdf, "")
        return {"pdf": pdf, "pages": pages}

    def source_pdf(self, identity, input_dir, manifest):
        return (self.local_path(manifest.get("processing_pdf"), input_dir)
                or self.local_path(input_dir / "prepared.pdf")
                or self.local_path(input_dir / "source.pdf")
                or self.local_path(input_dir.parent / "source.pdf")
                or self.local_path(self.state / "patents" / identity / "source.pdf"))

    def _build(self, identity, result_dir, input_dir):
        dataset = read_json(result_dir / "dataset.json", {})
        template = read_json(result_dir / "template.completed.json", {})
        metadata = {}
        manifest = {}
        evidence_id = self.jobs().get(identity, {}).get("evidence_document_id") or identity
        for path in sorted(input_dir.glob("*_manifest.json")):
            candidate = read_json(path, {})
            if (candidate.get("document_id") or candidate.get("patent_id")) == evidence_id:
                manifest = candidate
                for row in candidate.get("images", []):
                    metadata[Path(row["image"]).name] = row
        pdf = self.source_pdf(identity, input_dir, manifest)
        base = f"api/patents/{quote(identity, safe='')}/"
        tables, candidates, contexts, classifications, ocr, crops = {}, [], [], [], [], {}
        for path in sorted(result_dir.glob("*.json")):
            table = read_json(path, {})
            if not isinstance(table, dict) or not table.get("table_id") or "table_text" not in table:
                continue
            table_id = str(table["table_id"])
            meta = {**metadata.get(Path(table.get("image_path") or table_id).name, {}), **table.get("layout", {})}
            image = self.local_path(table.get("image_path")) or self.local_path(input_dir / Path(table_id).name)
            crop_key = str(len(crops))
            if image:
                crops[crop_key] = image
            image_url = base + f"crops/{crop_key}.png" if image else ""
            page_match = re.search(r"page-(\d+)", table_id)
            page = meta.get("page") or (int(page_match[1]) if page_match else None)
            bbox = meta.get("bbox_padded") or meta.get("bbox")
            grid = parse_grid(table.get("table_text", ""), table_id)
            rows = grid["rows"] if grid else [[{"text": line}] for line in table.get("table_text", "").splitlines()]
            # Cell addresses are preserved. No cell rectangles are inferred from HTML.
            structure = table.get("table_evidence") or table.get("ocr_evidence") or {}
            boxes = {c.get("ref"): c for c in structure.get("cells", [])
                     if c.get("bbox") or c.get("bbox_crop") or c.get("bbox_pdf")}
            rows = [[{**cell, **boxes.get(cell.get("ref"), {})} for cell in row] for row in rows]
            tables[table_id] = {**table, "rows": rows, "page": page}
            candidates.append({"table_id": table_id, "page": page, "bbox": bbox,
                               "image_path": image_url, "crop_confidence": meta.get("confidence")})
            context = meta.get("table_context") or {}
            contexts.append({**context, "table_id": table_id, "page": page,
                             "context_image": image_url, "context_status": "available" if context else "missing"})
            classification = table.get("classification") or {}
            classifications.append({"table_id": table_id, "primary_label": classification.get("category"),
                                    "classifier": {"name": "ArchivedClassification" if classification else "DocumentExtraction"},
                                    "reasons": [classification["reason"]] if classification.get("reason") else [],
                                    "extraction_status": "failed" if table.get("error") else "complete"})
            ocr.append({"source_id": table_id, "page": page, "rows": rows,
                        "image": image_url, "backend": structure.get("backend") or "SDA table OCR",
                        "width": meta.get("width_px") or structure.get("width_px"),
                        "height": meta.get("height_px") or structure.get("height_px"),
                        "bbox_padded": meta.get("bbox_padded") or structure.get("bbox_padded"),
                        "crop_to_pdf": meta.get("crop_to_pdf") or structure.get("crop_to_pdf")})
        records = []
        experiments = {e.get("exp_id", e.get("id")): e for e in template.get("experiments", [])}
        from units.domain_knowledge import load_domain_knowledge
        vocabulary_groups = load_domain_knowledge()["groups"]
        entities = template.get("entities", [])
        original_names = {e["original_entity_id"]: e["name"] if isinstance(e.get("name"), str) else slot_value(e.get("name"))
                          for e in entities if e.get("original_entity_id") and e.get("name")}
        def display_question(text):
            for identity in sorted(original_names, key=len, reverse=True):
                text = text.replace(identity, str(original_names[identity]))
            return text
        if not entities:
            # A dataset without a completed template still remains browsable.
            for index, row in enumerate(dataset.get("records", [])):
                records.append({"record_id": f"{identity}:record:{index}", "document_id": identity,
                                "antibody_id": row.get("NAME"), "vh": row.get("VH"), "vl": row.get("VL"),
                                "ic50": row.get("VALUE"), "ic50_unit": row.get("UNIT"),
                                "assay_endpoint": row.get("METRIC"), "evidence": [],
                                "experiment_group": row.get("METRIC") or "", "link_status": "unmatched"})
        for entity in entities:
            if not entity.get("sequence_locations"):
                from dataagent.sequence_evidence import locate_sequence_provenance
                entity["sequence_locations"] = {field: locate_sequence_provenance(
                    slot_value(entity.get(field.upper(), {}).get("sequence", {})),
                    entity.get(field.upper(), {}).get("sequence", {}).get("provenance", []), chain=field,
                    seq_id=slot_value(entity.get(field.upper(), {}).get("seq_id", {}))) for field in ("vh", "vl")}
            for observation in entity.get("METRIC", []):
                experiment = experiments.get(observation.get("exp_id"), {})
                keywords = experiment_keywords(experiment, observation, vocabulary_groups)
                metric, value, unit = (slot_value(observation.get(k)) for k in ("metric", "value", "unit"))
                source = observation.get("source", {})
                record = {"record_id": f"{identity}:{observation['observation_id']}", "document_id": identity,
                          "entity_id": entity["entity_id"], "canonical_entity_id": entity["entity_id"],
                          "measurement_id": observation["observation_id"],
                          "antibody_id": source.get("measurement_name", entity.get("name")),
                          "canonical_antibody_id": entity.get("name"),
                          "vh": slot_value(entity.get("VH", {}).get("sequence")),
                          "vl": slot_value(entity.get("VL", {}).get("sequence")),
                          "ic50": value, "ic50_unit": unit, "assay_endpoint": metric,
                          "experiment_group": metric or "",
                          "experiment_name": experiment.get("NAME") or observation.get("label"),
                          "assay_condition": slot_value(observation.get("conditions")),
                          "antigen_target": slot_value(observation.get("target")),
                          "assay_method": observation.get("method") or slot_value(observation.get("experiment_type")),
                          "experiment_id": observation.get("exp_id"),
                          "assay_keywords": keywords,
                          "species": [k["key"] for k in keywords if k["category"] == "species"],
                          "assay_system": next((k["key"] for k in keywords if k["category"] == "system"), None),
                          "entity_aliases": entity.get("aliases", []), "construct_type": entity.get("construct_type"),
                          "evidence": [], "sda_sources": [],
                          "sda_fields": {**{k: observation.get(k, {}) for k in
                                            ("metric", "value", "unit", "conditions", "target", "experiment_type")},
                                         "vh": entity.get("VH", {}).get("sequence", {}),
                                         "vl": entity.get("VL", {}).get("sequence", {})}}
                table_id = source.get("table_id")
                table = tables.get(table_id, {})
                row_index, col_index = locate_row(table.get("rows", []), entity.get("name"), value)
                source_ref = (observation.get("value", {}).get("provenance") or [source])[0]
                ref = source_ref.get("value_ref") or source_ref.get("ref")
                match = re.search(r":R(\d+)C(\d+)$", str(ref or ""))
                if match and table:
                    row_index, col_index = map(int, match.groups())
                if table_id:
                    if row_index is not None:
                        record["evidence"].append({"type": "assay_measurement", "table_id": table_id,
                                                   "row_index": row_index, "col_index": col_index,
                                                   "endpoint": metric, "value": value, "unit": unit})
                    else:
                        record["sda_sources"].append({"table_id": table_id, "page": table.get("page"),
                                                       "label": metric or "原表证据", "kind": "source"})
                if any(location.get("length") for location in entity.get("sequence_locations", {}).values()):
                    record["evidence"].append({"type": "sequence_index",
                        "construct_key": entity.get("original_entity_id", entity["entity_id"]),
                        "locations": copy.deepcopy(entity["sequence_locations"])})
                for chain in ("VH", "VL"):
                    slot = entity.get(chain, {}).get("sequence", {})
                    for provenance in flatten_provenance(slot.get("provenance", [])):
                        sid = provenance.get("table_id")
                        if sid and sid in tables:
                            ri, ci = locate_sequence(tables[sid]["rows"], slot_value(slot))
                            cited_ref = (provenance.get("ref") or provenance.get("source_ref")
                                         or provenance.get("source_id") or provenance.get("evidence_ref"))
                            cited_cell = re.search(r":R(\d+)C(\d+)$", str(cited_ref or ""))
                            if cited_cell:
                                ri, ci = map(int, cited_cell.groups())
                            record["sda_sources"].append({"kind": "variable_heavy" if chain == "VH" else "variable_light",
                                                           "table_id": sid, "page": tables[sid].get("page"),
                                                           "row_index": ri, "col_index": ci, "chain": chain,
                                                           "label": chain, "detail": provenance.get("quote", "")})
                        elif provenance.get("page") or provenance.get("pdf_page"):
                            page_number = provenance.get("page") or provenance.get("pdf_page")
                            record["sda_sources"].append({"kind": "variable_heavy" if chain == "VH" else "variable_light",
                                                           "table_id": f"text:p{page_number}:{provenance.get('line_id', '')}",
                                                           "page": page_number, "chain": chain, "label": chain,
                                                           "source_bbox": provenance.get("bbox_pdf") or provenance.get("bbox"),
                                                           "detail": provenance.get("quote", "")})
                record["link_status"] = "linked" if record["vh"] and record["vl"] else "unmatched"
                evaluation = entity.get("sequence_evaluation", {})
                for tool in ("agent", "anarci", "esm2"):
                    review = evaluation.get(tool + "_review")
                    if review:
                        record[tool + "_review"] = copy.deepcopy(review)
                        record[tool + "_review_status"] = review["status"]
                if evaluation:
                    record["construct_validation_status"] = evaluation.get("pair_status", "review")
                    if evaluation.get("overall_review_status") != "passed":
                        record["link_status"] = "review"
                record["sequence_source_conflicts"] = [copy.deepcopy(issue) for issue in template.get("issues", [])
                    if issue.get("kind") == "table_text_mismatch" and issue.get("entity_id") == entity["entity_id"]]
                record["sequence_image_reviews"] = [copy.deepcopy(issue) for issue in template.get("issues", [])
                    if issue.get("kind") == "sequence_image_review" and issue.get("entity_id") == entity["entity_id"]]
                record["sequence_ocr_failures"] = [copy.deepcopy(issue) for issue in template.get("issues", [])
                    if issue.get("kind") == "table_sequence_ocr_failed" and issue.get("entity_id") == entity["entity_id"]]
                record["constant_regions"] = [copy.deepcopy(other["record"]) for other in entity.get("other_sequences", [])
                    if other.get("record", {}).get("region") in {"CH", "CL"}]
                qa = dataset.get("question_review", {})
                answers = {a["question_id"]: a for a in qa.get("answers", [])}
                decisions = {c["question_id"]: c for c in qa.get("changes", [])}
                native_id = entity.get("original_entity_id")
                record["question_answers"] = [{"question_id": q["id"], "question": display_question(q["question"]),
                    "answer": answers.get(q["id"], {}).get("answer", {}).get("answer", ""),
                    "status": decisions.get(q["id"], {}).get("status", "needs_review"),
                    "reason": decisions.get(q["id"], {}).get("reason", ""),
                    "chains": [t["chain"] for t in q.get("targets", []) if t["entity_id"] == native_id]}
                    for q in qa.get("questions", []) if native_id and any(
                        t["entity_id"] == native_id for t in q.get("targets", []))]
                # Presence of extracted values does not certify any independent review.
                record["record_verification"] = {"status": "manual_review", "messages": ["SDA 提取结果，尚未人工复核"]}
                records.append(record)
        pages, profile = self.pdf_metadata(pdf, base)
        return {"records": records, "contexts": contexts, "candidates": candidates,
                "classifications": classifications, "ocr": ocr, "template": template,
                "runtime": read_json(result_dir / "_timings.json", {}), "pages": pages,
                "dataset": dataset, "pdf": pdf, "pdf_form": profile, "crops": crops,
                "run": read_json(result_dir / "run_summary.json", {}),
                "structured_files": self.structured_files(identity, result_dir),
                "coverage": read_json(result_dir / "coverage.json", {}),
                "document_report": read_json(result_dir / "document_report.json", {}),
                "missing": read_json(result_dir / "missing_fields.json", {}),
                "tst": read_json(result_dir / "completion_report.json",
                                 read_json(result_dir / "tst_completion.json", {}))}

    def pdf_metadata(self, pdf, base):
        if not pdf:
            return {"page_count": 0, "pages": []}, {"kind": "unknown"}
        key = (str(pdf), pdf.stat().st_mtime_ns, pdf.stat().st_size)
        use_cache = cache_enabled()
        if not use_cache or key not in self.pdf_cache:
            import pymupdf
            pages, text_pages = [], 0
            with pymupdf.open(pdf) as document:
                for number, page in enumerate(document, 1):
                    width, height = page.rect.width, page.rect.height
                    text_pages += bool(page.get_text().strip())
                    pages.append({"page": number, "width": width, "height": height,
                                  "pixel_width": 1280, "pixel_height": round(height * 1280 / width),
                                  "image": f"pages/{number:04d}.png"})
            kind = "text" if text_pages == len(pages) else "scanned" if not text_pages else "mixed"
            result = ({"page_count": len(pages), "pages": pages}, {"kind": kind})
            if not use_cache:
                return result
            self.pdf_cache[key] = result
        return self.pdf_cache[key]

    def patents(self):
        patents, jobs = [], self.jobs()
        builtin = set(self.builtin_locations())
        with self.connection() as db:
            counts = dict(db.execute("SELECT document_id, COUNT(*) FROM publications GROUP BY document_id"))
        for identity, (result_dir, input_dir) in self.locations().items():
            # The registry needs counts and file metadata, not every record's
            # provenance, table grid and sequence location. Building all those
            # bundles here can exceed the browser's registry timeout.
            dataset = read_json(result_dir / "dataset.json", {})
            entities = dataset.get("entities")
            if entities is None and (result_dir / "template.completed.json").is_file():
                entities = read_json(result_dir / "template.completed.json", {}).get("entities")
            record_count = (sum(len(e.get("METRIC", [])) for e in entities) if entities
                            else len(dataset.get("records", [])))
            manifest = {}
            evidence_id = jobs.get(identity, {}).get("evidence_document_id") or identity
            for path in sorted(input_dir.glob("*_manifest.json")):
                candidate = read_json(path, {})
                if (candidate.get("document_id") or candidate.get("patent_id")) == evidence_id:
                    manifest = candidate
                    break
            pdf = self.source_pdf(identity, input_dir, manifest)
            pages, profile = self.pdf_metadata(pdf, "")
            bundle = {"dataset": dataset, "pdf": pdf, "pages": pages, "pdf_form": profile,
                      "structured_files": self.structured_files(identity, result_dir),
                      "run": read_json(result_dir / "run_summary.json", {}),
                      "runtime": read_json(result_dir / "_timings.json", {})}
            dataset = bundle["dataset"]
            job = jobs.get(identity, {})
            status = job.get("status") or ("failed" if dataset.get("status") == "failed" else "completed")
            base = f"api/patents/{quote(identity, safe='')}/"
            artifacts = {key: base + key + ".json" for key in
                         ("records", "contexts", "candidates", "classifications", "ocr", "template", "runtime", "pages")}
            artifacts["review"] = base + "review.json"
            structured = bundle.get("structured_files", {})
            for key, name in (("json", "structured.json"), ("markdown", "structured.md"), ("text", "structured.txt")):
                if key in structured:
                    artifacts["structured_" + key] = base + name
            count = counts.get(identity, 0)
            has_ocr = False
            for path in sorted(result_dir.glob("*.json"), key=lambda p: (not p.name.startswith("table-"), p.name)):
                table = read_json(path, {})
                if (isinstance(table, dict) and table.get("table_id")
                        and isinstance(table.get("table_text"), str) and table["table_text"].strip()):
                    has_ocr = True
                    break
            if not has_ocr:
                checkpoint = read_json(result_dir / "dataagent_checkpoint.json", {}) or read_json(result_dir / "dataagent2_checkpoint.json", {})
                table_source = checkpoint.get("inputs", {}).get("table_ocr")
                if table_source:
                    path = Path(table_source).resolve()
                    has_ocr = path.is_relative_to(self.root) and (path.is_file() or path.is_dir() and any(path.glob("*.json")))
                if not has_ocr and job.get("structured_dir"):
                    has_ocr = self.local_path(Path(job["structured_dir"]) / "table_ocr/tables.jsonl") is not None
            protected_source = identity in builtin or job.get("builtin_source") is True
            log = self.local_path(job.get("log"))
            patent_root = (self.state / "patents" / identity).resolve()
            log_available = bool(log and log.is_relative_to(patent_root))
            pipeline_available = bundle["pdf"] is not None
            patents.append({**job, "document_id": identity, "filename": job.get("filename") or f"{identity}.pdf",
                            "extraction_engine": job.get("extraction_engine") or bundle["run"].get("extraction_engine")
                                or ("dataagent2" if dataset.get("extraction_mode") == "dataagent2" else "dataagent1"),
                            "record_count": record_count, "page_count": bundle["pages"]["page_count"],
                            "pdf_form": bundle["pdf_form"], "status": status,
                            "semantic_status": dataset.get("status"), "stage": job.get("stage", "completed"),
                            "progress": job.get("progress", 100), "builtin": protected_source,
                            "protected_source": protected_source,
                            "pipeline_available": pipeline_available,
                            "comparison_available": pipeline_available and "json" in structured
                                and read_json(structured["json"].parent / "page_ocr/summary.json", {}).get("full_document_ocr_complete") is True,
                            "retry_available": pipeline_available and status not in {"queued", "processing"},
                            "analyze_available": has_ocr,
                            "log_available": log_available,
                            "log_url": base + "log" if log_available else None,
                            "review_required": dataset.get("status") != "complete",
                            "quality_warning_count": dataset.get("summary", {}).get("missing_required", 0),
                            "reparse_available": False, "collection_review_available": False,
                            "dataset_published": bool(count), "published_record_count": count,
                            "patent_text": self.patent_text(identity), "artifacts": artifacts})
            patents[-1]["processing_metrics"] = processing_metrics(
                self.root, identity, result_dir, job, bundle["run"], bundle["runtime"])
        return patents

    def patent_text(self, identity):
        location = self.locations().get(identity)
        directories = [location[1]] if location else []
        job = self.jobs().get(identity, {})
        parent = (self.state / "patents" / identity).resolve()
        patents_root = (self.state / "patents").resolve()
        if parent.is_relative_to(patents_root) and parent == patents_root / identity:
            for key in ("text_context_dir", "data_dir"):
                if job.get(key):
                    path = Path(job[key])
                    path = (path if path.is_absolute() else parent / path).resolve()
                    if path.is_relative_to(parent):
                        directories.append(path)
            directories.append(parent)
        if self.is_builtin(identity):
            directories.append(self.root / "data" / identity)
        local_result = None
        for directory in dict.fromkeys(directories):
            for name in ("patent_context.json", "patent_text.zh.txt", "patent_text.txt", "pdf_text.json"):
                path = self.local_path(directory / name)
                if not path:
                    continue
                try:
                    payload = read_json(path, {}) if name.endswith(".json") else None
                    if payload is not None:
                        if not isinstance(payload, dict) or payload.get("document_id") not in (None, identity):
                            continue
                        rows = [*payload.get("paragraphs", []), *payload.get("description", []),
                                *payload.get("claims", []), *payload.get("pages", [])]
                        text = " ".join(str(row.get("text") or "") for row in rows if isinstance(row, dict))
                        text += str(payload.get("text") or payload.get("abstract") or "")
                    else:
                        text = path.read_text(encoding="utf-8")
                    if not text.strip():
                        continue
                except (OSError, ValueError, TypeError):
                    continue
                result = {"status": "found", "checked_at": now(), "source": "local",
                          "source_file": str(path.relative_to(self.root)), "characters": len(text.strip())}
                # A local PDF's reference URL is not evidence of archived web prose.
                url = payload.get("source_url") if payload and name == "patent_context.json" else None
                if isinstance(url, str) and urlsplit(url).scheme in {"http", "https"} and urlsplit(url).netloc:
                    result.update(source="web", url=url.split("#", 1)[0] + "#description",
                                  publication_number=identity)
                    return result
                local_result = local_result or result
        return local_result or {"status": "not_found", "checked_at": now(), "source": "local"}

    def folders(self):
        with self.connection() as db:
            return [{"dataset_id": r[0], "name": r[1], "created_at": r[2]}
                    for r in db.execute("SELECT * FROM folders ORDER BY created_at, name")]

    def create_folder(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or re.search(r"[/\\\x00-\x1f]", name):
            raise ValueError("文件夹名称无效")
        folder = {"dataset_id": hashlib.sha256(name.strip().casefold().encode()).hexdigest()[:16],
                  "name": name.strip(), "created_at": now()}
        with self.connection() as db:
            try:
                db.execute("INSERT INTO folders VALUES (:dataset_id, :name, :created_at)", folder)
            except sqlite3.IntegrityError as exc:
                raise ValueError("文件夹名称已存在") from exc
        return folder

    def published(self, dataset_id=None):
        with self.connection() as db:
            rows = db.execute("SELECT dataset_id, document_id, payload FROM publications" +
                              (" WHERE dataset_id=?" if dataset_id else "") + " ORDER BY rowid",
                              (dataset_id,) if dataset_id else ()).fetchall()
        return [{**json.loads(payload), "_dataset_id": folder, "_patent_id": document}
                for folder, document, payload in rows]

    def publish(self, identity, record_ids, dataset_id):
        with self.lock:
            if not isinstance(record_ids, list) or not record_ids or any(not isinstance(x, str) for x in record_ids):
                raise ValueError("请选择记录")
            if self.jobs().get(identity, {}).get("status") in {"queued", "processing"}:
                raise ValueError("处理中的专利暂不能发布")
            records = {r["record_id"]: r for r in self.bundle(identity)["records"]}
            if any(key not in records for key in record_ids):
                raise ValueError("记录已变更，请刷新")
            with self.connection() as db:
                if not db.execute("SELECT 1 FROM folders WHERE dataset_id=?", (dataset_id,)).fetchone():
                    raise ValueError("目标文件夹不存在")
                previous = db.execute("SELECT COUNT(*) FROM publications WHERE dataset_id=? AND document_id=?", (dataset_id, identity)).fetchone()[0]
                db.execute("DELETE FROM publications WHERE dataset_id=? AND document_id=?", (dataset_id, identity))
                db.executemany("INSERT INTO publications VALUES (?, ?, ?, ?)",
                               [(dataset_id, identity, key, json.dumps(records[key], ensure_ascii=False)) for key in dict.fromkeys(record_ids)])
            return {"previous_record_count": previous, "published_record_count": len(set(record_ids)),
                    "patent": next(p for p in self.patents() if p["document_id"] == identity)}

    def reviews(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM reviews ORDER BY rowid")]

    def save_review(self, entry):
        identity = entry.get("document_id")
        if entry.get("record_id") not in {r["record_id"] for r in self.bundle(identity)["records"]}:
            raise ValueError("记录不存在")
        name = entry.get("reviewer_name")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("请填写审核人")
        stored = {k: entry.get(k, "") for k in ("document_id", "record_id", "reviewer_name", "review_action")}
        stored["reviewed_at"] = now()
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?)",
                       (identity, stored["record_id"], name, json.dumps(stored, ensure_ascii=False)))
        return stored


def locate_row(rows, name, value):
    """Only exact OCR text matches authorize a physical row address."""
    candidates = [(i, row) for i, row in enumerate(rows)
                  if name and any(compact(cell.get("text")) == compact(name) for cell in row)]
    if value is not None:
        exact = [(i, row) for i, row in candidates if any(compact(c.get("text")) == compact(str(value)) for c in row)]
        candidates = exact or candidates
    if len(candidates) != 1:
        return None, None
    i, row = candidates[0]
    columns = [j for j, cell in enumerate(row) if value is not None and compact(cell.get("text")) == compact(str(value))]
    return i, columns[0] if len(columns) == 1 else None


def locate_sequence(rows, sequence):
    matches = [(i, j) for i, row in enumerate(rows) for j, cell in enumerate(row)
               if sequence and compact(cell.get("text")) == compact(sequence)]
    return matches[0] if len(matches) == 1 else (None, None)


def flatten_provenance(items):
    for item in items:
        yield item
        yield from flatten_provenance(item.get("mapping_provenance", []))
        extra = item.get("extraction_evidence", [])
        if isinstance(extra, list):
            yield from flatten_provenance(x for x in extra if isinstance(x, dict))
        elif isinstance(extra, dict):
            for values in extra.values():
                if isinstance(values, list):
                    yield from flatten_provenance(x for x in values if isinstance(x, dict))
