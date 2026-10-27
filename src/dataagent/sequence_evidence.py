"""Build, retrieve, and locate literal sequence evidence in patent documents."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import deque
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .llm_io import build_evidence, cell_ref, contains_entity_name, table_id
from document.cell_geometry import geometry_fields, transform_box, valid_box

# Document evidence graph

def source_digest(source: dict) -> str:
    return hashlib.sha256(str(source.get("text", "")).encode()).hexdigest()


def build_document_structure(document_id: str, tables: list[dict], *, blocks: list[dict] | None = None,
                             contexts: list[dict] | None = None, methods: list[dict] | None = None,
                             passages: list[dict] | None = None, classifications: list[dict] | None = None) -> dict:
    evidence = build_evidence(tables, contexts, methods, passages)
    sources = {ref: source for ref, source in evidence.items()
               if source.get("kind") not in {"table", "image"} and source.get("text", "").strip()}
    for block in blocks or []:
        if block.get("text", "").strip():
            ref = "block:" + str(block["block_id"])
            sources[ref] = {**block, "kind": "document_block", "source_ref": block["block_id"]}
    root = f"document:{document_id}"
    nodes: dict[str, dict] = {root: {"id": root, "kind": "document", "document_id": document_id}}
    edges: list[dict] = []

    def edge(parent: str, child: str, relation: str = "contains", **attributes: Any) -> None:
        edges.append({"source": parent, "target": child, "relation": relation, **attributes})

    def page_parent(page: Any) -> str:
        if page is None:
            return root
        key = f"page:{page}"
        if key not in nodes:
            nodes[key] = {"id": key, "kind": "page", "page": page}
            edge(root, key)
        return key

    decisions = {str(item["table_id"]): item for item in classifications or []}
    for table in tables:
        identity = table_id(table)
        rows = table.get("rows", [])
        nodes[identity] = {"id": identity, "kind": "table", "page": table.get("page"),
                           "image": table.get("image"), "row_count": len(rows),
                           "column_count": max(map(len, rows), default=0),
                           "caption": table.get("caption", ""),
                           "agent_annotation": decisions.get(identity, {}),
                           "annotation_is_source_fact": False}
        edge(page_parent(table.get("page")), identity)
        image_id = identity + ":image"
        nodes[image_id] = {"id": image_id, "kind": "image", "image": table.get("image"), "page": table.get("page")}
        edge(identity, image_id, "source_image")
        for ri, row in enumerate(rows):
            row_id = f"{identity}:row:{ri}"
            nodes[row_id] = {"id": row_id, "kind": "row", "table_id": identity, "row_index": ri}
            edge(identity, row_id)
            if ri:
                edge(f"{identity}:row:{ri - 1}", row_id, "next_row")
            for ci, cell in enumerate(row):
                ref = cell_ref(identity, ri, ci)
                nodes[ref] = {"id": ref, "kind": "cell", "table_id": identity, "row_index": ri,
                              "col_index": ci, "page": table.get("page"), "bbox": cell.get("bbox", []),
                              "confidence": cell.get("confidence"), "source_ref": cell.get("source_ref", ref),
                              "text": str(cell.get("text", "")), **geometry_fields(cell)}
                edge(row_id, ref)
    for ref, source in sources.items():
        if ref in nodes:
            continue
        nodes[ref] = {"id": ref, **source}
        owner = str(source.get("table_id", ""))
        edge(owner if owner in nodes else page_parent(source.get("page")), ref,
             "context" if owner in nodes else "contains")
    for context in contexts or []:
        child, parent = str(context.get("table_id")), str(context.get("continuation_of"))
        if child in nodes and parent in nodes:
            edge(parent, child, "possible_continuation", confidence=context.get("continuation_confidence"),
                 requires_agent_confirmation=True)
    return {"schema_version": 1, "document_id": document_id, "coordinate_note": "Cell bbox/bbox_crop use crop pixels; bbox_pdf uses working PDF points; bbox_source uses original source coordinates. Legacy cells may omit explicit frames. Document blocks use PDF coordinates.",
            "semantics": "Layout and literal source evidence only. Agent annotations are hypotheses.",
            "nodes": list(nodes.values()), "edges": edges, "sources": sources,
            "source_digest": hashlib.sha256(json.dumps(sources, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}


def structure_mermaid(graph: dict) -> str:
    """A compact navigable overview; the JSON retains every row/cell and text."""
    nodes = [node for node in graph["nodes"] if node["kind"] in {"document", "page", "table"}]
    ids = {node["id"]: f"n{i}" for i, node in enumerate(nodes)}
    lines = ["flowchart LR"]
    for node in nodes:
        if node["kind"] == "table":
            label = f"{node['id']} | {node['row_count']} rows × {node['column_count']} columns"
        elif node["kind"] == "page":
            label = f"PDF page {node['page']}"
        else:
            label = graph["document_id"]
        label = label.replace('"', "'").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f'  {ids[node["id"]]}["{label}"]')
    for edge in graph["edges"]:
        if edge["source"] in ids and edge["target"] in ids:
            relation = "续表候选，Agent 核实" if edge["relation"] == "possible_continuation" else "包含"
            lines.append(f'  {ids[edge["source"]]} -->|{relation}| {ids[edge["target"]]}')
    return "\n".join(lines) + "\n"


def write_structure_artifacts(graph: dict, root: Path) -> dict[str, str]:
    from .llm_runtime import json_write

    root.mkdir(parents=True, exist_ok=True)
    json_write(root / "document_structure.json", graph)
    (root / "document_structure.mmd").write_text(structure_mermaid(graph), encoding="utf-8")
    (root / "document_structure.md").write_text(
        "# 文档数据结构与序列证据\n\n"
        f"文档：`{graph['document_id']}`；{len(graph['nodes'])} 个节点，{len(graph['sources'])} 个完整文本来源。\n\n"
        "```mermaid\nflowchart LR\n"
        '  D["专利文档"] --> P["PDF 页"]\n'
        '  P --> T["原始 OCR 表格"]\n  T --> R["行 / 列关系"]\n  R --> C["单元格：完整原文、坐标、引用 ID"]\n'
        '  P --> B["正文 / 实施例 / 序列附录"]\n'
        '  B --> E["命名、SEQ 索引与来源证据"]\n  C --> A["SequenceExtractionAgent"]\n  E --> A\n'
        '  A --> V["VH / VL 判断 + 实际序列 + 引文 + 理由"]\n'
        '  V --> G["原文逐字核验与生物学复核"]\n```\n\n'
        "[完整结构与原文](document_structure.json) · [逐页关系图](document_structure.mmd) · "
        "[Agent 抽取与来源片段](sequence_extraction.json)\n\n"
        "表格分类、SEQ 链接与续表关系作为待核实线索提供。链型、实体归属和可变区边界由 Agent 判断；"
        "程序核验字符来源，不按模式自动补序列。缺失、冲突或证据不足保留待审。\n", encoding="utf-8")
    return {"document_structure": str(root / "document_structure.json"),
            "document_structure_diagram": str(root / "document_structure.mmd"),
            "document_structure_report": str(root / "document_structure.md")}


# Bounded evidence retrieval

def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def prompt_source(source: dict) -> dict:
    # Text is never shortened. Coordinates and OCR diagnostics remain in the
    # source graph and are used again when grounding the returned fragments.
    result = {key: source[key] for key in
              ("text", "kind", "page", "table_id", "row_index", "col_index", "section_id") if key in source}
    if source.get("sequence_ocr"):
        result["sequence_ocr_status"] = source["sequence_ocr"]["status"]
    if source.get("sequence_style"):
        result["sequence_style"] = {k: source["sequence_style"][k] for k in
                                    ("method", "spans", "domain", "annotation_quote")}
    if source.get("text_styles"):
        result["text_styles"] = source["text_styles"]
    return result


class EvidenceAccess:
    def __init__(self, graph: dict):
        self.sources = graph["sources"]
        self.nodes = {node["id"]: node for node in graph["nodes"]}
        self.children = {}
        for edge in graph["edges"]:
            if edge["relation"] in {"contains", "context"}:
                self.children.setdefault(edge["source"], []).append(edge["target"])
        self.index = [{key: node[key] for key in
                       ("id", "kind", "page", "row_count", "column_count") if key in node}
                      for node in graph["nodes"] if node["kind"] in {"page", "table"}]
        self.continuations = [{key: edge[key] for key in ("source", "target", "relation")}
                              for edge in graph["edges"] if edge["relation"] == "possible_continuation"]
        self.source_hashes = {ref: fingerprint(source) for ref, source in self.sources.items()}

    def descendants(self, ref: str) -> list[str]:
        pending, found, seen = deque([ref]), [], set()
        while pending:
            item = pending.popleft()
            if item in seen:
                continue
            seen.add(item)
            if item in self.sources:
                found.append(item)
            pending.extend(self.children.get(item, []))
        return found

    def matches(self, query: str) -> list[str]:
        return [ref for ref, source in self.sources.items()
                if query.casefold() in source.get("text", "").casefold()]

    def seed_refs(self, entities: list[dict], links: list[dict]) -> list[str]:
        names = sorted({name for e in entities for name in
                        [e.get("canonical_name", ""), *e.get("raw_names", [])] if name.strip()})
        ids = {link["seq_id"] for link in links if link.get("seq_id")}
        sequence_pattern = (re.compile(r"SEQ\s*ID(?:\s*NO\.?)?\s*[:#]?\s*(?:"
                            + "|".join(map(str, sorted(ids))) + r")(?!\d)", re.I) if ids else None)
        refs = [ref for item in [*entities, *links] for ref in item.get("evidence_refs", [])]
        # Literal matches select evidence for reading, never chain identity or
        # residues. Keep all matching refs in the dependency key, even omitted ones.
        refs += [ref for ref, source in self.sources.items()
                 if any(contains_entity_name(source.get("text", ""), name) for name in names)
                 or (sequence_pattern and sequence_pattern.search(source.get("text", "")))]
        for node in self.nodes.values():
            if node.get("kind") != "row" or not ids:
                continue
            row_sources = [self.sources[r] for r in self.descendants(node["id"])]
            texts = [s.get("text", "").strip() for s in row_sources]
            owner = self.nodes.get(node.get("table_id"), {})
            sequence_hint = "sequence_definition" in owner.get("agent_annotation", {}).get("labels", [])
            if any(text == str(number) for text in texts for number in ids) and (
                    sequence_hint or any(re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY\s]{20,}", text) for text in texts)):
                refs.append(node["id"])
        return list(dict.fromkeys(refs))

    def prefetch(self, refs: list[str], budget: int) -> tuple[dict, list[str]]:
        loaded, omitted, used = {}, [], 0
        expanded = []
        for ref in refs:
            source = self.sources.get(ref, self.nodes.get(ref, {}))
            expanded.append(ref)
            table, row = source.get("table_id"), source.get("row_index")
            if table is not None and row is not None:
                # The matching row, neighbors and leading headers provide
                # ownership/layout evidence without loading every table body.
                expanded.extend(f"{table}:row:{r}" for r in dict.fromkeys([row, 0, 1, row - 1, row + 1]) if r >= 0)
        for ref in dict.fromkeys(r for item in expanded for r in self.descendants(item)):
            size = len(ref) + len(json.dumps(prompt_source(self.sources[ref]), ensure_ascii=False))
            if used + size > budget:
                omitted.append(ref)
                continue
            loaded[ref] = self.sources[ref]
            used += size
        return loaded, omitted

    def dependencies(self, loaded: dict, requests: list[str], queries: list[str]) -> dict:
        return {"sources": {ref: self.source_hashes[ref] for ref in sorted(loaded)},
                "requests": {ref: self.descendants(ref) for ref in sorted(set(requests))},
                "queries": {query: {ref: self.source_hashes[ref] for ref in self.matches(query)}
                            for query in sorted(set(queries))}}

    def read_window(self, requests: list[str], previous: dict, budget: int) -> tuple[dict, list[str], list[str]]:
        """Admit explicit reads before retained context; never shorten evidence text."""
        # Exact sources and rows take priority over broad page/table reads.
        ordered = sorted(dict.fromkeys(requests), key=lambda ref: (
            0 if ref in self.sources else 1 if self.nodes.get(ref, {}).get("kind") == "row" else 2))
        requested = []
        for ref in ordered:
            # Cells/prose contain coherent evidence. Token alternatives remain
            # accessible, but should not displace whole rows during broad reads.
            requested.extend(sorted(self.descendants(ref), key=lambda child:
                "token" in self.sources[child].get("kind", "")))
        requested = list(dict.fromkeys(requested))
        requested_set = set(requested)
        loaded, omitted, used = {}, [], 0
        for ref in dict.fromkeys([*requested, *previous]):
            source = self.sources[ref]
            size = len(ref) + len(json.dumps(prompt_source(source), ensure_ascii=False))
            if used + size > budget:
                if ref in requested_set:
                    omitted.append(ref)
                continue
            loaded[ref] = source
            used += size
        return loaded, omitted, [ref for ref in previous if ref not in loaded]

    def unchanged(self, saved: dict) -> bool:
        if not isinstance(saved, dict) or set(saved) != {"sources", "requests", "queries"}:
            return False
        refs = saved["sources"]
        if any(ref not in self.sources for ref in refs):
            return False
        return saved == self.dependencies({ref: self.sources[ref] for ref in refs},
                                          list(saved["requests"]), list(saved["queries"]))


# Literal SEQ ID references

PARAGRAPH_START = re.compile(r"^\s*(?:\[\d{3,6}\]|E\s*\d+\s*[.．]|\d+\s*[.．]\s)")
SEQ_MARKER = re.compile(r"SEQ\s*ID\s*(?:N[O0]s?\.?)?\s*[:：]?\s*\d+", re.I)


def collect_sequence_reference_passages(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join PDF text lines within a paragraph, preserving their source boxes.

    Native text and detailed page OCR retain their original provenance.
    Search-only coarse OCR must not be passed as sequence evidence.
    """
    passages: list[dict[str, Any]] = []
    group: list[dict[str, Any]] = []

    def finish() -> None:
        if not group:
            return
        text = "\n".join(str(b["text"]) for b in group)
        if not SEQ_MARKER.search(text):
            return
        # A long unnumbered page is retained as overlapping source windows,
        # so a distant paragraph is never silently clipped out of the input.
        windows = [(0, len(group))]
        if len(text) > 5000:
            hits = [i for i, b in enumerate(group) if re.search(r"SEQ\s*ID", b["text"], re.I)]
            windows = sorted({(max(0, i - 3), min(len(group), i + 5)) for i in hits})
        for start, end in windows:
            items = group[start:end]
            value = "\n".join(str(b["text"]) for b in items)
            if not SEQ_MARKER.search(value):
                continue
            first = items[0]["block_id"]
            last = items[-1]["block_id"]
            sources = {b.get("source", "pdf_text") for b in items}
            passages.append({"ref": f"sequence:{first}:{last}", "page": items[0]["page"],
                             "text": value, "source": next(iter(sources)) if len(sources) == 1 else "mixed", "kind": "sequence_passage",
                             "block_ids": [b["block_id"] for b in items],
                             "lines": [{"text": b["text"], "bbox": b.get("bbox", [])} for b in items]})

    for block in blocks:
        text = str(block.get("text", "")).strip()
        if not text or not block.get("block_id") or not block.get("page"):
            continue
        if re.match(r"^(?:WO\s*\d|PCT[/／]|\d{1,4}$)", text, re.I):
            continue
        if group and (block["page"] != group[-1]["page"] or PARAGRAPH_START.match(text)
                      or (len(block.get("bbox", [])) == 4 and len(group[-1].get("bbox", [])) == 4
                          and block["bbox"][1] - group[-1]["bbox"][3] > 28)):
            finish()
            group = []
        group.append(block)
    finish()
    return passages


# Sequence source locations

def compact(text: Any) -> str:
    return re.sub(r"\s+", "", html.unescape(re.sub(r"<br\s*/?>", "", str(text or ""), flags=re.I))).upper()


def union_box(boxes: list[list[float]]) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def locate_sequence_provenance(sequence: str, provenance: list, *, chain: str, seq_id=None) -> dict:
    """Locate legacy sequences only inside their existing, addressed citations."""
    result = {"status": "missing" if not sequence else "unlocated", "chain": chain.upper(),
              "seq_id": seq_id, "length": len(sequence or ""), "segments": [], "covered_residues": 0}
    if not sequence:
        return result
    sources, seen = [], set()
    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            ref = value.get("evidence_ref") or value.get("source_id") or value.get("source_ref") or value.get("ref")
            text = str(value.get("text", value.get("quote", "")))
            literal = re.sub(r"\s+", "", text)
            if ref and ref not in seen and re.fullmatch(r"[A-Z]{10,}", literal):
                sources.append((str(ref), value, literal))
                seen.add(ref)
            for key in ("extraction_evidence", "evidence", "provenance"):
                if key in value:
                    collect(value[key])
    collect(provenance)
    stream = "".join(text for _, _, text in sources)
    start = stream.find(sequence)
    if start < 0 or stream.find(sequence, start + 1) >= 0:
        result["reason"] = "sequence_not_unique_in_cited_text"
        return result
    cursor = 0
    for ref, source, text in sources:
        low, high = max(start, cursor), min(start + len(sequence), cursor + len(text))
        cursor += len(text)
        if low >= high:
            continue
        box = source.get("bbox_pdf") or (source.get("bbox") if source.get("coordinate_space") == "pdf_points"
                or (source.get("coordinate_space") is None and (ref.startswith("ocr:") or source.get("kind") == "text")) else [])
        if valid_box(box) and source.get("page", source.get("pdf_page")):
            a, b = low - start, high - start
            result["segments"].append({"source_ref": ref, "table_id": source.get("table_id") or "source:" + ref,
                "page": source.get("page", source.get("pdf_page")), "bbox": box, "coordinate_space": "pdf_points",
                "sequence_start": a, "sequence_end": b, "text": sequence[a:b],
                "precision": source.get("precision") or "ocr_line"})
    result["covered_residues"] = sum(s["sequence_end"] - s["sequence_start"] for s in result["segments"])
    result["status"] = "located" if result["covered_residues"] == len(sequence) else "partial" if result["covered_residues"] else "unlocated"
    return result


class SequenceLocator:
    def __init__(self, tables: list[dict], methods: list[dict], candidates: list[dict], source_pdf: str | Path | None = None,
                 *, sources: dict | None = None):
        self.tables = {str(t.get("source_id") or t.get("image")): t for t in tables}
        self.methods = {f"method:p{p['page']}": p for p in methods if p.get("page")}
        self.candidates = {str(c["table_id"]): c for c in candidates if c.get("table_id")}
        self.cells: dict[tuple, dict] = {}
        self.page_sources: dict[str, tuple[str, dict]] = {}
        self.native: dict[int, list[dict]] = {}
        self.alignments: dict[str, dict[int, int]] = {}
        self.sources = sources or {}
        self.pdf = None
        if source_pdf and Path(source_pdf).is_file():
            try:
                import pymupdf
            except ImportError:  # PyMuPDF versions before the module rename.
                import fitz as pymupdf
            self.pdf = pymupdf.open(source_pdf)

    def close(self) -> None:
        if self.pdf is not None:
            self.pdf.close()

    def _source(self, identity: str) -> tuple[str, dict]:
        if identity in self.page_sources:
            return self.page_sources[identity]
        table, method = self.tables.get(identity), self.methods.get(identity)
        if table:
            rows = table.get("rows", [])
            page = table.get("page")
            source = "ocr"
        elif method:
            rows = [[{"text": text, **(method.get("lines", [])[i] if i < len(method.get("lines", [])) else {})}]
                    for i, text in enumerate(method.get("text", "").splitlines())]
            page, source = method.get("page"), method.get("source")
        else:
            return "", {}
        text, offsets = [], {}
        offset = 0
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row):
                key = (identity, ri, ci)
                value = compact(cell.get("text"))
                self.cells[key] = {"text": value, "page": page, "bbox": cell.get("bbox", []),
                                   "source": source, "offset": offset, **geometry_fields(cell)}
                for index in range(len(value)):
                    offsets[(ri, ci, index)] = offset + index
                text.append(value)
                offset += len(value)
        self.page_sources[identity] = ("".join(text), offsets)
        return self.page_sources[identity]

    def _native_chars(self, page: int) -> list[dict]:
        if page not in self.native:
            chars = []
            if self.pdf is not None and 1 <= page <= len(self.pdf):
                for bi, block in enumerate(self.pdf[page - 1].get_text("rawdict").get("blocks", [])):
                    for li, line in enumerate(block.get("lines", [])):
                        for span in line.get("spans", []):
                            for char in span.get("chars", []):
                                # Whitespace has no residue. Preserve the actual glyph's
                                # rectangle, not interpolated paragraph/line heights.
                                value = char["c"].upper()
                                if len(value) == 1 and not value.isspace():
                                    chars.append({"text": value, "bbox": list(char["bbox"]), "line": (bi, li)})
            self.native[page] = chars
        return self.native[page]

    def _alignment(self, identity: str, page: int) -> dict[int, int]:
        if identity not in self.alignments:
            source, _ = self._source(identity)
            native = "".join(c["text"] for c in self._native_chars(page))
            matched = {}
            if source == native:
                matched = {i: i for i in range(len(source))}
            elif native:
                for block in SequenceMatcher(None, source, native, autojunk=False).get_matching_blocks():
                    # Tiny repeated words/SEQ numbers are not a geometric anchor.
                    if block.size >= 12:
                        matched.update((block.a + i, block.b + i) for i in range(block.size))
            self.alignments[identity] = matched
        return self.alignments[identity]

    def _fallback_box(self, key: tuple) -> tuple[list[float], str]:
        identity, _, _ = key
        cell, table = self.cells[key], self.tables.get(identity, {})
        if valid_box(cell.get("bbox_pdf")):
            return list(cell["bbox_pdf"]), cell.get("precision", "ocr_cell")
        box = cell["bbox"]
        if len(box) != 4:
            return [], "unlocated"
        if identity in self.methods:
            # Legacy native-method lines were interpolated over a paragraph;
            # those are NOT reliable sequence rectangles.
            if cell["source"] == "pdf_text":
                return [], "unlocated"
            if cell.get("coordinate_space") == "pdf_points" or self.methods[identity].get("coordinate_space") == "pdf_points":
                return list(box), "ocr_line"
            method = self.methods[identity]
            width, height = method.get("width"), method.get("height")
            pw, ph = method.get("pdf_width"), method.get("pdf_height")
            if not all((width, height, pw, ph)):
                return [], "unlocated"
            return [box[0] * pw / width, box[1] * ph / height,
                    box[2] * pw / width, box[3] * ph / height], "ocr_cell"
        candidate = self.candidates.get(identity)
        if table.get("crop_to_pdf"):
            mapped = transform_box(box, table["crop_to_pdf"])
            return (mapped, cell.get("precision", "ocr_cell")) if mapped else ([], "unlocated")
        width, height = table.get("width"), table.get("height")
        if not width or not height:
            return [], "unlocated"
        if candidate and len(candidate.get("bbox", [])) == 4:
            x1, y1, x2, y2 = table.get("bbox_padded") or candidate["bbox"]
        elif table.get("mode") == "sequence_page" or identity.startswith("sequence-page-"):
            if self.pdf is None or not cell.get("page"):
                return [], "unlocated"
            rect = self.pdf[int(cell["page"]) - 1].rect
            x1, y1, x2, y2 = rect
        else:
            return [], "unlocated"
        return [x1 + box[0] / width * (x2 - x1), y1 + box[1] / height * (y2 - y1),
                x1 + box[2] / width * (x2 - x1), y1 + box[3] / height * (y2 - y1)], "ocr_cell"

    def locate(self, resolved: dict, field: str) -> dict:
        chain, sequence = field.upper(), str(resolved.get(field) or "")
        result = {"status": "missing" if not sequence else "unlocated", "chain": chain,
                  "seq_id": resolved.get(f"{field}_seq_id"), "length": len(sequence),
                  "source_scope": resolved.get("sequence_resolution", {}).get(field, {}).get("source_scope"),
                  "segments": []}
        if not sequence:
            return result
        agent_segments = resolved.get("sequence_resolution", {}).get(field, {}).get("segments")
        if agent_segments:
            # Use the agent's exact cell/substrings, including its chosen full-
            # chain boundary. Do not search for a similar sequence elsewhere.
            selected = []
            result["source_fragments"] = agent_segments
            for segment in agent_segments:
                identity, ri, ci = segment.get("table_id"), segment.get("row_index"), segment.get("col_index")
                source = self.sources.get(segment["source_ref"])
                if source and segment.get("source_sha256") and source_digest(source) != segment["source_sha256"]:
                    result["reason"] = "agent_fragment_source_changed"
                    return result
                if identity in self.tables and ri is not None and ci is not None:
                    self._source(identity)
                    key = (identity, ri, ci)
                    if key not in self.cells:
                        result["reason"] = "agent_fragment_cell_missing"
                        return result
                    original = str(self.tables[identity]["rows"][ri][ci].get("text", ""))
                    raw_text = str(source.get("text", "")) if source else original
                    if source:
                        self.cells[key].update(geometry_fields(source), text=compact(raw_text),
                                               reviewed_source=compact(original) != compact(raw_text))
                elif source:
                    identity, ri, ci = "source:" + segment["source_ref"], 0, 0
                    key = (identity, ri, ci)
                    raw_text = str(source.get("text", ""))
                    box = source.get("bbox_pdf") or (source.get("bbox") if source.get("coordinate_space") == "pdf_points" else [])
                    self.cells[key] = {"text": compact(raw_text), "page": source.get("page"), "bbox": [],
                                       "offset": 0, "source": "page_ocr", **geometry_fields(source)}
                    if valid_box(box):
                        self.cells[key].update(bbox_pdf=box, precision=source.get("precision", "ocr_line"))
                else:
                    result["reason"] = "agent_fragment_source_missing"
                    return result
                start = len(compact(raw_text[:segment["char_start"]]))
                if self.cells[key]["text"][start:start + len(segment["text"])] != segment["text"]:
                    result["reason"] = "agent_fragment_coordinate_alignment_failed"
                    return result
                self.cells[key]["source_ref"] = segment["source_ref"]
                selected.extend((key, start + index) for index in range(len(segment["text"])))
            if "".join(self.cells[key]["text"][index] for key, index in selected) != sequence:
                result["reason"] = "exported_sequence_differs_from_fragments"
                return result
            return self._locate_selected(result, sequence, selected)
        characters, origins, seen = [], [], set()
        for row in resolved.get("sequence_rows", []):
            if row.get("chain") != chain or row.get("seq_id") != result["seq_id"]:
                continue
            identity, ri = str(row["table_id"]), row["row_index"]
            if (identity, ri) in seen:
                continue
            seen.add((identity, ri))
            self._source(identity)
            for key, cell in self.cells.items():
                if key[:2] != (identity, ri):
                    continue
                characters.append(cell["text"])
                origins.extend((key, i) for i in range(len(cell["text"])))
        stream = "".join(characters)
        start = stream.find(sequence)
        if start < 0:
            result["reason"] = "exported_residues_not_exactly_in_declared_source"
            return result
        if stream.find(sequence, start + 1) >= 0:
            result["reason"] = "multiple_residue_matches_in_declared_source"
            return result
        selected = origins[start:start + len(sequence)]
        return self._locate_selected(result, sequence, selected)

    def _locate_selected(self, result: dict, sequence: str, selected: list) -> dict:
        # Group by source cell first. A partly unmatched cell remains a
        # labelled OCR-cell fallback instead of fabricated character boxes.
        groups = []
        for offset, (key, index) in enumerate(selected):
            if not groups or groups[-1][0] != key:
                groups.append((key, []))
            groups[-1][1].append((offset, index))
        for key, positions in groups:
            identity, ri, ci = key
            cell = self.cells[key]
            page = int(cell.get("page") or 0)
            if not page:
                continue
            alignment = {} if cell.get("reviewed_source") else self._alignment(identity, page)
            native = [] if cell.get("reviewed_source") else self._native_chars(page)
            mapped = [alignment.get(cell["offset"] + index) for _, index in positions]
            expected_box, _ = self._fallback_box(key)
            if not all(index is not None for index in mapped):
                # PDF reading order sometimes differs from OCR row order.
                # An entire, unique residue fragment is also a safe anchor.
                fragment = sequence[positions[0][0]:positions[-1][0] + 1]
                native_text = "".join(char["text"] for char in native)
                hit = native_text.find(fragment)
                if len(fragment) >= 20 and hit >= 0 and native_text.find(fragment, hit + 1) < 0:
                    mapped = list(range(hit, hit + len(fragment)))
            if expected_box and all(index is not None for index in mapped):
                matched_box = union_box([native[index]["bbox"] for index in mapped])
                cx, cy = (matched_box[0] + matched_box[2]) / 2, (matched_box[1] + matched_box[3]) / 2
                if not (expected_box[0] - 8 <= cx <= expected_box[2] + 8
                        and expected_box[1] - 8 <= cy <= expected_box[3] + 8):
                    mapped = [None] * len(positions)
            base = {"table_id": identity, "row_index": ri, "col_index": ci, "page": page,
                    "source_ref": cell.get("source_ref", f"{identity}:R{ri}C{ci}"), "coordinate_space": "pdf_points"}
            lines = cell.get("ocr_lines", [])
            if lines and "".join(compact(line["text"]) for line in lines) == cell["text"]:
                cursor = 0
                for line in lines:
                    end = cursor + len(compact(line["text"]))
                    covered = [offset for offset, index in positions if cursor <= index < end]
                    if covered and valid_box(line.get("bbox_pdf")):
                        a, b = covered[0], covered[-1] + 1
                        result["segments"].append({**base, "bbox": line["bbox_pdf"], "sequence_start": a,
                                                   "sequence_end": b, "text": sequence[a:b], "precision": "ocr_line"})
                    cursor = end
                continue
            if all(index is not None for index in mapped):
                line_groups = []
                for (offset, _), index in zip(positions, mapped):
                    glyph = native[index]
                    if not line_groups or line_groups[-1][0] != glyph["line"]:
                        line_groups.append((glyph["line"], []))
                    line_groups[-1][1].append((offset, glyph["bbox"]))
                for _, glyphs in line_groups:
                    a, b = glyphs[0][0], glyphs[-1][0] + 1
                    result["segments"].append({**base, "bbox": union_box([box for _, box in glyphs]),
                                               "sequence_start": a, "sequence_end": b, "text": sequence[a:b],
                                               "precision": "pdf_characters"})
            else:
                box, precision = self._fallback_box(key)
                if box:
                    a, b = positions[0][0], positions[-1][0] + 1
                    result["segments"].append({**base, "bbox": box, "sequence_start": a, "sequence_end": b,
                                               "text": sequence[a:b], "precision": precision,
                                               "geometry_alignment": cell.get("geometry_alignment"),
                                               "observed_ocr_lines": cell.get("ocr_lines", [])})
        covered = sum(s["sequence_end"] - s["sequence_start"] for s in result["segments"])
        result["covered_residues"] = covered
        result["status"] = "located" if covered == len(sequence) else "partial" if covered else "unlocated"
        if not covered:
            result["reason"] = "no_ocr_geometry_for_cited_fragments"
        return result


def attach_sequence_locations(resolved: dict[str, dict], records: list[dict], tables: list[dict],
                              methods: list[dict], candidates: list[dict], source_pdf: str | Path | None = None,
                              *, sources: dict | None = None) -> None:
    """Add derived provenance in place; never change record values or raw sources."""
    locator = SequenceLocator(tables, methods, candidates, source_pdf, sources=sources)
    try:
        for item in resolved.values():
            item["sequence_locations"] = {field: locator.locate(item, field) for field in ("vh", "vl")}
        for record in records:
            for evidence in record.get("evidence", []):
                if evidence.get("type") != "sequence_index":
                    continue
                item = resolved.get(evidence.get("construct_key"), {})
                # Reference-imported/manual values may differ from the resolver;
                # do not attach another sequence's residue coordinates to them.
                evidence["locations"] = {field: location for field, location in item.get("sequence_locations", {}).items()
                                         if record.get(field, "") == item.get(field, "")}
    finally:
        locator.close()
