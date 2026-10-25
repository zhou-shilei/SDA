"""Local, document-bound PDF evidence. Only selected snippets leave this store."""

import copy
import hashlib
import importlib.metadata
import json
from functools import lru_cache
from pathlib import Path
import re


VERSION = 1
SEQ_ID = re.compile(r"SEQ\s*ID\s*(?:NO\s*)?[.:：]?\s*(\d+)", re.I)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def compact(text):
    return re.sub(r"\s+", "", text)


def named(text, name):
    # Never interpret m8-variant as m8, or 1.34.4-鼠源 as 1.34.4.
    pattern = r"\s*".join(re.escape(c) for c in compact(name or ""))
    return bool(re.search(r"(?<![A-Za-z0-9_.-])" + pattern +
                          r"(?![A-Za-z0-9_.-])", text, re.I)) if name else False


def discover_pdf(directory, document_id):
    """Use a same-document manifest; validate original and processing PDF hashes."""
    directory = Path(directory)
    candidates = []
    for path in sorted(directory.glob("*_manifest.json")):
        manifest = json.loads(path.read_text())
        if (manifest.get("document_id") or manifest.get("patent_id")) != document_id:
            continue
        for key, hash_key in (("source_pdf", "source_pdf_sha256"),
                              ("processing_pdf", "processing_pdf_sha256")):
            if not manifest.get(key):
                continue
            pdf = Path(manifest[key])
            if not pdf.is_absolute():
                pdf = path.parent / pdf
            if pdf.is_file() and manifest.get(hash_key) and hashlib.sha256(
                    pdf.read_bytes()).hexdigest() != manifest[hash_key]:
                raise ValueError(f"PDF hash mismatch: {pdf}")
        value = manifest.get("processing_pdf") or manifest.get("source_pdf")
        if value:
            pdf = Path(value)
            candidates.append((pdf if pdf.is_absolute() else path.parent / pdf).resolve())
    candidates = list(dict.fromkeys(p for p in candidates if p.is_file()))
    if len(candidates) > 1:
        raise ValueError("Multiple different PDFs for the same document")
    if candidates:
        return candidates[0]
    pdf = directory / "source.pdf"
    return pdf.resolve() if pdf.is_file() else None


def _quality(lines):
    text = "".join(line["text"] for line in lines)
    return bool(len(text.strip()) >= 30 and text.count("\ufffd") <= len(text) * .02)


@lru_cache(maxsize=2)
def _page_engine(device, threads):
    from paddleocr import PaddleOCR
    return PaddleOCR(lang="ch", device=device, enable_mkldnn=False, cpu_threads=threads,
                     use_doc_orientation_classify=False, use_doc_unwarping=False,
                     use_textline_orientation=False)


class PageEvidenceStore:
    def __init__(self, document_id, data_dir, output_dir, *, pages_jsonl=None,
                 device="cpu", cpu_threads=4, max_side=2600, max_ocr_pages=8, ocr=None):
        import pymupdf
        self.document_id = document_id
        self.directory = Path(output_dir) / "evidence"
        self.pdf = discover_pdf(data_dir, document_id)
        self.pdf_sha256 = hashlib.sha256(self.pdf.read_bytes()).hexdigest() if self.pdf else None
        self.device, self.cpu_threads, self.max_side = device, cpu_threads, max_side
        self.max_ocr_pages, self.ocr = max_ocr_pages, ocr
        self.ocr_pages = 0
        self.issues = []
        self.pages = {}
        self.import_sha256 = None
        try:
            ocr_version = importlib.metadata.version("paddleocr")
        except importlib.metadata.PackageNotFoundError:
            ocr_version = None
        self.key = {"version": VERSION, "document_id": document_id,
                    "pdf_sha256": self.pdf_sha256, "max_side": max_side,
                    "device": device, "cpu_threads": cpu_threads,
                    "paddleocr": ocr_version, "pymupdf": pymupdf.VersionBind}
        self.cache_id = digest(self.key)
        index = self.directory / "index.json"
        if index.is_file():
            cached = json.loads(index.read_text())
            if cached.get("key") == self.key:
                self.pages = {p["page_number"]: p for p in cached["pages"]}
        if not self.pages and self.pdf:
            with pymupdf.open(self.pdf) as pdf:
                for number, page in enumerate(pdf, 1):
                    lines = []
                    for block in page.get_text("dict", sort=True)["blocks"]:
                        for line in block.get("lines", []):
                            text = "".join(s["text"] for s in line["spans"])
                            if text.strip():
                                box = list(line["bbox"])
                                if page.rotation:
                                    box = list(pymupdf.Rect(box) * page.rotation_matrix)
                                lines.append({"text": text, "bbox": box})
                    self.pages[number] = self._page(number, lines, "pdf_text", page.rect)
            save(index, {"key": self.key, "pages": list(self.pages.values())})
        if pages_jsonl:
            self._import_pages(Path(pages_jsonl))

    def _page(self, number, lines, source, rect=None):
        rows = [{**copy.deepcopy(line), "source_line_id": line.get("line_id"),
                 "line_id": f"pdf:p{number:04d}-l{i:04d}"}
                for i, line in enumerate(lines, 1) if line.get("text", "").strip()]
        return {"document_id": self.document_id, "page_number": number,
                "pdf_sha256": self.pdf_sha256, "source": source, "status": "success",
                "coordinate_space": "pdf_points", "lines": rows, "quality_ok": _quality(rows),
                "width": float(rect.width) if rect else None,
                "height": float(rect.height) if rect else None}

    def _import_pages(self, path):
        self.import_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        seen = set()
        for raw in path.read_text().splitlines():
            if not raw.strip():
                continue
            page = json.loads(raw)
            if page.get("document_id") != self.document_id:
                raise ValueError("OCR pages belong to another document")
            number = page.get("page_number")
            if isinstance(number, bool) or not isinstance(number, int) or number < 1 or number in seen:
                raise ValueError("Invalid or duplicate OCR page number")
            seen.add(number)
            if self.pdf_sha256 and page.get("pdf_sha256") not in {None, self.pdf_sha256}:
                raise ValueError("OCR PDF hash mismatch")
            if page.get("status") not in {None, "success", "complete"}:
                self.issues.append({"kind": "page_ocr_failed", "page": number})
                continue
            refs = [line.get("line_id") for line in page.get("lines", [])]
            if any(not isinstance(r, str) for r in refs) or len(set(refs)) != len(refs):
                raise ValueError("Invalid or duplicate OCR line IDs")
            entry = self._page(number, page.get("lines", []), "supplied_ocr")
            entry["input_sha256"] = self.import_sha256
            entry["coordinate_space"] = page.get("coordinate_space", "pdf_points")
            entry["source_file"] = str(path.resolve())
            for key in ("backend", "blocks", "line_granularity", "width", "height", "coordinate_frame"):
                if key in page:
                    entry[key] = copy.deepcopy(page[key])
            self.pages[number] = entry
            save(self.directory / "pages" / f"page-{number:04d}.json", entry)

    def read_page_evidence(self, number, *, force_ocr=False):
        """Only selected pages get high-resolution OCR; cached failures are retried."""
        entry = self.pages.get(number)
        if entry and (entry["source"] == "supplied_ocr" or
                      (entry["quality_ok"] and not force_ocr)):
            save(self.directory / "pages" / f"page-{number:04d}.json", entry)
            return copy.deepcopy(entry)
        if not self.pdf:
            return copy.deepcopy(entry) if entry else None
        path = self.directory / "pages" / f"page-{number:04d}.ocr.json"
        if path.is_file():
            cached = json.loads(path.read_text())
            if cached.get("cache_id") == self.cache_id and cached.get("status") == "success":
                self.pages[number] = cached
                save(self.directory / "pages" / f"page-{number:04d}.json", cached)
                return copy.deepcopy(cached)
        if self.ocr_pages >= self.max_ocr_pages:
            self.issues.append({"kind": "page_ocr_budget_deferred", "page": number})
            return copy.deepcopy(entry) if entry else None
        import pymupdf
        self.ocr_pages += 1
        print(f"{self.document_id} 本地页面 OCR：第 {number} 页，"
              f"本次 {self.ocr_pages}/{self.max_ocr_pages}", flush=True)
        try:
            with pymupdf.open(self.pdf) as pdf:
                page = pdf[number - 1]
                scale = self.max_side / max(page.rect.width, page.rect.height)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                image = self.directory / "images" / f"page-{number:04d}.png"
                image.parent.mkdir(parents=True, exist_ok=True)
                pix.save(image)
                if self.ocr:
                    lines = self.ocr(image, number)
                else:
                    result = next(iter(_page_engine(self.device, self.cpu_threads).predict(str(image))))
                    boxes = result.get("rec_boxes")
                    if boxes is None:
                        boxes = [[min(p[0] for p in poly), min(p[1] for p in poly),
                                  max(p[0] for p in poly), max(p[1] for p in poly)]
                                 for poly in result["rec_polys"]]
                    sx, sy = page.rect.width / pix.width, page.rect.height / pix.height
                    lines = [{"text": str(text), "bbox": [float(b[0])*sx, float(b[1])*sy,
                              float(b[2])*sx, float(b[3])*sy]} for text, b in zip(result["rec_texts"], boxes)]
                entry = self._page(number, lines, "local_ocr", page.rect)
        except Exception as exc:
            self.issues.append({"kind": "page_ocr_failed", "page": number,
                                "error": f"{type(exc).__name__}: {exc}"})
            save(path, {"cache_id": self.cache_id, "status": "failed", "issues": self.issues[-1:]})
            return copy.deepcopy(entry) if entry else None
        entry["cache_id"] = self.cache_id
        save(path, entry)
        save(self.directory / "pages" / f"page-{number:04d}.json", entry)
        self.pages[number] = entry
        return copy.deepcopy(entry)

    def search_evidence(self, *, names=(), seq_ids=(), keywords=(), max_pages=8):
        ranked = []
        for number, page in self.pages.items():
            text = "\n".join(line["text"] for line in page["lines"])
            ids = {int(m[1]) for m in SEQ_ID.finditer(text)}
            name_hits = sum(named(text, name) for name in names)
            score = name_hits * 10 + len(ids.intersection(seq_ids)) * 20
            score += sum(compact(k).casefold() in compact(text).casefold() for k in keywords)
            # A local named variable-chain heading outranks claims that merely
            # enumerate names and many unrelated SEQ IDs.
            for i in range(len(page["lines"])):
                window = "\n".join(r["text"] for r in page["lines"][i:i + 6])
                if any(named(window, name) for name in names) and re.search(
                        r"可\s*变\s*区|variable", window, re.I):
                    score += 40
                    break
            if score and (not names or name_hits):
                ranked.append((score + int(bool(ids)) * 5, number))
        return [number for _, number in sorted(ranked, key=lambda x: (-x[0], x[1]))[:max_pages]]

    def fingerprint(self):
        return digest({"key": self.key, "import": self.import_sha256,
                       "pages": {n: digest(p) for n, p in self.pages.items()}})
