"""Prepare document-bound local prose for uploaded PDFs without model calls."""

import hashlib
import json
from pathlib import Path
import shutil


def prepare_document_input(directory, document_id, pdf, *, existing=None, extract_text=True):
    directory = Path(directory)
    pdf = Path(pdf).resolve() if pdf is not None else None
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "source.pdf"
    if pdf is not None and target.resolve() != pdf:
        shutil.copyfile(pdf, target)
    if existing:
        existing = Path(existing)
        for name in ("patent_context.json", "patent_text.zh.txt", "patent_text.txt", "sequences.json"):
            original = existing / name
            if original.is_file() and not (directory / name).exists():
                if name.endswith(".json"):
                    payload = json.loads(original.read_text(encoding="utf-8"))
                    if payload.get("document_id") != document_id:
                        raise ValueError(f"资料文档号与 {document_id} 不一致：{original}")
                shutil.copyfile(original, directory / name)
        if (existing / "wipo").is_dir() and not (directory / "wipo").exists():
            shutil.copytree(existing / "wipo", directory / "wipo")
    if not extract_text:
        return
    # Existing native/web prose is more complete than text from PDF image placeholders.
    if any((directory / name).is_file() for name in ("patent_context.json", "patent_text.zh.txt", "patent_text.txt")):
        return
    if pdf is None:
        return
    import pymupdf
    paragraphs, text_pages = [], []
    with pymupdf.open(pdf) as document:
        page_count = len(document)
        for number, page in enumerate(document, 1):
            blocks = [block for block in page.get_text("blocks", sort=True)
                      if len(block) > 6 and block[6] == 0 and str(block[4]).strip()]
            if blocks:
                text_pages.append(number)
            for index, block in enumerate(blocks, 1):
                paragraphs.append({"id": f"pdf-p{number:04d}-b{index:04d}",
                                   "text": block[4].strip(), "pdf_page": number,
                                   "bbox": [round(v, 3) for v in block[:4]],
                                   "source_type": "pdf_text", "section": None, "example": None})
    payload = {"document_id": document_id, "source_type": "pdf_text", "source_pdf": str(pdf),
               "source_pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
               "paragraphs": paragraphs,
               "coverage": {"page_count": page_count, "text_pages": text_pages,
                            "missing_text_pages": [n for n in range(1, page_count + 1) if n not in text_pages],
                            "method": "native PDF text layer; blank/scanned pages are not claimed as extracted"}}
    # Empty text must not break load_document; page evidence can OCR scanned pages later.
    name = "patent_context.json" if paragraphs else "pdf_text.json"
    (directory / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
