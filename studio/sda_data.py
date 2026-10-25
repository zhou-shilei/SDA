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
from table_continuation import parse_grid


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else copy.deepcopy(default)


def slot_value(slot):
    return slot.get("value") if isinstance(slot, dict) and slot.get("status") == "present" else None


def compact(value):
    return re.sub(r"\s+", "", str(value or ""))


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

    def bundle(self, document_id):
        with self.lock:
            location = self.locations().get(document_id)
            if location is None:
                raise KeyError("专利不存在")
            result_dir, input_dir = location
            paths = list(result_dir.glob("*.json")) + list(input_dir.glob("*.json"))
            paths += list(input_dir.parent.glob("source.pdf")) + list(input_dir.glob("*.pdf"))
            paths += list((self.state / "patents" / document_id).glob("source.pdf"))
            signature = tuple(sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths))
            if self.cache.get(document_id, (None,))[0] != signature:
                self.cache[document_id] = (signature, self._build(document_id, result_dir, input_dir))
            return self.cache[document_id][1]

    def _build(self, identity, result_dir, input_dir):
        dataset = read_json(result_dir / "dataset.json", {})
        template = read_json(result_dir / "template.completed.json", {})
        metadata = {}
        manifest = {}
        for path in sorted(input_dir.glob("*_manifest.json")):
            candidate = read_json(path, {})
            if (candidate.get("document_id") or candidate.get("patent_id")) == identity:
                manifest = candidate
                for row in candidate.get("images", []):
                    metadata[Path(row["image"]).name] = row
        pdf = (self.local_path(manifest.get("processing_pdf"), input_dir)
               or self.local_path(input_dir / "prepared.pdf")
               or self.local_path(input_dir / "source.pdf")
               or self.local_path(input_dir.parent / "source.pdf")
               or self.local_path(self.state / "patents" / identity / "source.pdf"))
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
            boxes = {c.get("ref"): c for c in structure.get("cells", []) if c.get("bbox")}
            rows = [[{**cell, **boxes.get(cell.get("ref"), {})} for cell in row] for row in rows]
            tables[table_id] = {**table, "rows": rows, "page": page}
            candidates.append({"table_id": table_id, "page": page, "bbox": bbox,
                               "image_path": image_url, "crop_confidence": meta.get("confidence")})
            context = meta.get("table_context") or {}
            contexts.append({**context, "table_id": table_id, "page": page,
                             "context_image": image_url, "context_status": "available" if context else "missing"})
            classification = table.get("classification") or {}
            classifications.append({"table_id": table_id, "primary_label": classification.get("category"),
                                    "classifier": {"name": "TableClassificationAgent"},
                                    "reasons": [classification["reason"]] if classification.get("reason") else [],
                                    "extraction_status": "failed" if table.get("error") else "complete"})
            ocr.append({"source_id": table_id, "page": page, "rows": rows,
                        "image": image_url, "backend": structure.get("backend") or "SDA table OCR",
                        "width": meta.get("width_px"), "height": meta.get("height_px")})
        records = []
        experiments = {e.get("exp_id", e.get("id")): e for e in template.get("experiments", [])}
        from tst_completion import load_domain_knowledge
        vocabulary = {row["key"]: row for rows in load_domain_knowledge()["groups"].values() for row in rows}
        entities = template.get("entities", [])
        if not entities:
            # A dataset without a completed template still remains browsable.
            for index, row in enumerate(dataset.get("records", [])):
                records.append({"record_id": f"{identity}:record:{index}", "document_id": identity,
                                "antibody_id": row.get("NAME"), "vh": row.get("VH"), "vl": row.get("VL"),
                                "ic50": row.get("VALUE"), "ic50_unit": row.get("UNIT"),
                                "assay_endpoint": row.get("METRIC"), "evidence": [],
                                "experiment_group": row.get("METRIC") or "", "link_status": "unmatched"})
        for entity in entities:
            for observation in entity.get("METRIC", []):
                experiment = experiments.get(observation.get("exp_id"), {})
                keywords = list(dict.fromkeys(experiment.get("KEYWORDS", []) + experiment.get("TAGS", []) + observation.get("keywords", [])))
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
                          "experiment_group": experiment.get("NAME") or " · ".join(str(x) for x in (metric, observation.get("label")) if x),
                          "assay_condition": slot_value(observation.get("conditions")),
                          "antigen_target": slot_value(observation.get("target")),
                          "assay_method": observation.get("method") or slot_value(observation.get("experiment_type")),
                          "experiment_id": observation.get("exp_id"),
                          "assay_keywords": [{"key": k, "label_zh": vocabulary[k].get("label_zh", k),
                                              "label_en": vocabulary[k].get("label_en", k), "category": "method"}
                                             if k in vocabulary else k for k in keywords],
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
                for chain in ("VH", "VL"):
                    slot = entity.get(chain, {}).get("sequence", {})
                    for provenance in flatten_provenance(slot.get("provenance", [])):
                        sid = provenance.get("table_id")
                        if sid and sid in tables:
                            ri, ci = locate_sequence(tables[sid]["rows"], slot_value(slot))
                            record["sda_sources"].append({"kind": "variable_heavy" if chain == "VH" else "variable_light",
                                                           "table_id": sid, "page": tables[sid].get("page"),
                                                           "row_index": ri, "col_index": ci, "chain": chain,
                                                           "label": chain, "detail": provenance.get("quote", "")})
                        elif provenance.get("page") or provenance.get("pdf_page"):
                            page_number = provenance.get("page") or provenance.get("pdf_page")
                            record["sda_sources"].append({"kind": "variable_heavy" if chain == "VH" else "variable_light",
                                                           "table_id": f"text:p{page_number}:{provenance.get('line_id', '')}",
                                                           "page": page_number, "chain": chain, "label": chain,
                                                           "source_bbox": provenance.get("bbox"),
                                                           "detail": provenance.get("quote", "")})
                record["link_status"] = "linked" if record["vh"] and record["vl"] else "unmatched"
                # Presence of extracted values does not certify any independent review.
                record["record_verification"] = {"status": "manual_review", "messages": ["SDA 提取结果，尚未人工复核"]}
                records.append(record)
        pages, profile = self.pdf_metadata(pdf, base)
        return {"records": records, "contexts": contexts, "candidates": candidates,
                "classifications": classifications, "ocr": ocr, "template": template,
                "runtime": read_json(result_dir / "_timings.json", {}), "pages": pages,
                "dataset": dataset, "pdf": pdf, "pdf_form": profile, "crops": crops,
                "run": read_json(result_dir / "run_summary.json", {}),
                "coverage": read_json(result_dir / "coverage.json", {}),
                "document_report": read_json(result_dir / "document_report.json", {}),
                "missing": read_json(result_dir / "missing_fields.json", {}),
                "tst": read_json(result_dir / "tst_completion.json", {})}

    def pdf_metadata(self, pdf, base):
        if not pdf:
            return {"page_count": 0, "pages": []}, {"kind": "unknown"}
        key = (str(pdf), pdf.stat().st_mtime_ns, pdf.stat().st_size)
        if key not in self.pdf_cache:
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
            self.pdf_cache[key] = ({"page_count": len(pages), "pages": pages}, {"kind": kind})
        return self.pdf_cache[key]

    def patents(self):
        patents, jobs = [], self.jobs()
        builtin = set(self.builtin_locations())
        with self.connection() as db:
            counts = dict(db.execute("SELECT document_id, COUNT(*) FROM publications GROUP BY document_id"))
        for identity, (result_dir, _) in self.locations().items():
            bundle = self.bundle(identity)
            dataset = bundle["dataset"]
            job = jobs.get(identity, {})
            status = job.get("status") or ("failed" if dataset.get("status") == "failed" else "completed")
            base = f"api/patents/{quote(identity, safe='')}/"
            artifacts = {key: base + key + ".json" for key in
                         ("records", "contexts", "candidates", "classifications", "ocr", "template", "runtime", "pages")}
            count = counts.get(identity, 0)
            has_ocr = False
            for path in result_dir.glob("*.json"):
                table = read_json(path, {})
                if (isinstance(table, dict) and table.get("table_id")
                        and isinstance(table.get("table_text"), str) and table["table_text"].strip()):
                    has_ocr = True
                    break
            protected_source = identity in builtin or job.get("builtin_source") is True
            log = self.local_path(job.get("log"))
            patent_root = (self.state / "patents" / identity).resolve()
            log_available = bool(log and log.is_relative_to(patent_root))
            pipeline_available = bundle["pdf"] is not None
            patents.append({**job, "document_id": identity, "filename": job.get("filename") or f"{identity}.pdf",
                            "record_count": len(bundle["records"]), "page_count": bundle["pages"]["page_count"],
                            "pdf_form": bundle["pdf_form"], "status": status,
                            "semantic_status": dataset.get("status"), "stage": job.get("stage", "completed"),
                            "progress": job.get("progress", 100), "builtin": protected_source,
                            "protected_source": protected_source,
                            "pipeline_available": pipeline_available,
                            "retry_available": pipeline_available and status not in {"queued", "processing"},
                            "analyze_available": has_ocr,
                            "log_available": log_available,
                            "log_url": base + "log" if log_available else None,
                            "review_required": dataset.get("status") != "complete",
                            "quality_warning_count": dataset.get("summary", {}).get("missing_required", 0),
                            "reparse_available": False, "collection_review_available": False,
                            "dataset_published": bool(count), "published_record_count": count,
                            "patent_text": self.patent_text(identity), "artifacts": artifacts})
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
