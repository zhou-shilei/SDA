"""Cached page search for scanned patents; coarse OCR is never sequence evidence.

Only the coordinator accesses PyMuPDF. Workers run independent, single-threaded
Tesseract processes on rendered PNG bytes. Cache entries are atomic and keyed by
PDF contents, resolution, language and OCR runtime, so interrupted runs resume.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .page_ocr import _native_lines, candidate_assay_method_pages
from .ocr_runtime import find_tesseract, tesseract_env
from units.cache_policy import cache_enabled

VERSION = 1
CACHE_ROOT = Path(__file__).resolve().parents[2] / ".studio" / "page_index_cache"
SEQ = re.compile(r"SEQ\s*[I1l]D\s*(?:N[O0]s?\.?)?\s*[:：]?\s*(\d+)", re.IGNORECASE)
EXAMPLE = re.compile(
    r"(?:^|\n)[ \t]*(?:\d{1,2}[ \t]+)?(?:example[ \t]*[:.]?[ \t]*(\d+)\b|实施例[ \t]*(\d+)|实验例[ \t]*(\d+)|EXAMPLES[ \t]*(?=\n|$))",
    re.IGNORECASE,
)
CHAIN = re.compile(r"\b(?:VH|VL|heavy|light|variable|CDR)\b|重链|轻链", re.IGNORECASE)
METHOD = re.compile(
    r"\b(?:IC\s*50|EC\s*50|KD|assay|ELISA|Biacore|KinExA|SPR|BLI)\b|实验方法",
    re.IGNORECASE,
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".page-",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime(lang: str) -> tuple[str, dict, dict]:
    executable = find_tesseract()
    if not executable:
        raise RuntimeError("Page index requires Tesseract")
    env = tesseract_env(executable, lang)
    env.update(OMP_THREAD_LIMIT="1", OMP_NUM_THREADS="1")
    probe = subprocess.run(
        [executable, "--list-langs"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    available = (probe.stdout + probe.stderr).splitlines()
    if any(language not in available for language in lang.split("+")):
        raise RuntimeError(f"Tesseract languages unavailable: {lang}")
    version = subprocess.run(
        [executable, "--version"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.splitlines()[0]
    data_path = env.get("TESSDATA_PREFIX")
    if not data_path:
        match = re.search(r'"([^"]+)"', probe.stdout + probe.stderr)
        data_path = match.group(1) if match else ""
    fingerprints = {}
    for path in [
        Path(executable),
        *(Path(data_path) / f"{item}.traineddata" for item in lang.split("+")),
    ]:
        if path.is_file():
            stat = path.stat()
            fingerprints[str(path.resolve())] = [stat.st_size, stat.st_mtime_ns]
    return executable, env, {"version": version, "files": fingerprints}


def _recognize(
    png: bytes,
    runtime: tuple,
    lang: str,
    timeout: int,
    width: int,
    height: int,
    pdf_width: float,
    pdf_height: float,
) -> list[dict]:
    executable, env, _ = runtime
    result = subprocess.run(
        [executable, "stdin", "stdout", "-l", lang, "--psm", "3", "tsv"],
        input=png,
        env=env,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-1000:])
    groups: dict[tuple, list] = {}
    for word in csv.DictReader(
        io.StringIO(result.stdout.decode("utf-8", errors="replace")),
        delimiter="\t",
        quoting=csv.QUOTE_NONE,
    ):
        if word.get("level") != "5" or not word.get("text", "").strip():
            continue
        key = tuple(word[field] for field in ("block_num", "par_num", "line_num"))
        groups.setdefault(key, []).append(word)
    lines = []
    for words in groups.values():
        x1 = min(int(w["left"]) for w in words)
        y1 = min(int(w["top"]) for w in words)
        x2 = max(int(w["left"]) + int(w["width"]) for w in words)
        y2 = max(int(w["top"]) + int(w["height"]) for w in words)
        lines.append(
            {
                "text": " ".join(w["text"] for w in words),
                "confidence": round(
                    sum(max(0, float(w["conf"])) for w in words) / len(words) / 100, 4
                ),
                "bbox": [
                    round(x1 * pdf_width / width, 2),
                    round(y1 * pdf_height / height, 2),
                    round(x2 * pdf_width / width, 2),
                    round(y2 * pdf_height / height, 2),
                ],
            }
        )
    return lines


def _page_record(
    number: int, lines: list[dict], width: float, height: float, source: str
) -> dict:
    text = "\n".join(line["text"] for line in lines)
    confidence = sum(
        line.get("confidence", 0) * len(line["text"]) for line in lines
    ) / max(1, sum(len(line["text"]) for line in lines))
    quality = (
        "ok"
        if sum(c.isalnum() for c in text) >= 80 and confidence >= 0.65
        else "uncertain"
    )
    return {
        "page": number,
        "source": source,
        "text": text,
        "lines": lines,
        "pdf_width": width,
        "pdf_height": height,
        "width": width,
        "height": height,
        "coordinate_space": "pdf_points",
        "quality": quality,
        "confidence": round(confidence, 4),
    }


def build_page_index(
    source_pdf: Path,
    output_dir: Path,
    *,
    workers: int = 6,
    max_side: int = 1600,
    lang: str = "en",
    cache_dir: Path | None = None,
    page_numbers: list[int] | None = None,
    stage: str = "index",
    timeout: int = 60,
) -> dict:
    """Read all pages for search, or a selected set at detail resolution."""
    import pymupdf

    if stage not in {"index", "detail"} or workers < 1 or max_side < 500 or timeout < 1:
        raise ValueError("Invalid page-index settings")
    started = time.perf_counter()
    use_cache = cache_enabled()
    source_pdf = Path(source_pdf).resolve()
    lang = {"en": "eng", "ch": "chi_sim", "zh": "chi_sim"}.get(lang, lang)
    runtime = None
    pdf_hash = _digest(source_pdf)
    output: dict[int, dict] = {}
    hits = 0
    rendered = 0
    render_seconds = 0.0
    with (
        pymupdf.open(source_pdf) as document,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        count = len(document)
        requested = (
            list(range(1, count + 1))
            if page_numbers is None
            else sorted(set(page_numbers))
        )
        if any(number < 1 or number > count for number in requested):
            raise ValueError("Requested page is outside PDF")
        pending = {}

        def collect() -> None:
            nonlocal pending
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                number, width, height, cache_path = pending.pop(future)
                try:
                    record = _page_record(
                        number, future.result(), width, height, "page_ocr"
                    )
                    record.update(pdf_sha256=pdf_hash, max_side=max_side, stage=stage)
                    if use_cache:
                        write_json(cache_path, record)
                except Exception as exc:  # noqa: BLE001 -- isolate a failed page and retry it next run
                    record = _page_record(number, [], width, height, "page_ocr")
                    record.update(quality="error", error=f"{type(exc).__name__}: {exc}")
                output[number] = record
                print(
                    f"page-{stage} {len(output)}/{len(requested)}: page {number} {record['quality']}",
                    flush=True,
                )

        for number in requested:
            page = document[number - 1]
            width, height = float(page.rect.width), float(page.rect.height)
            native = _page_record(
                number, _native_lines(page), width, height, "pdf_text"
            )
            if native["quality"] == "ok":
                output[number] = native
                continue
            if runtime is None:
                runtime = _runtime(lang)
            settings = {
                "version": VERSION,
                "max_side": max_side,
                "lang": lang,
                "stage": stage,
                "runtime": runtime[2],
            }
            key = hashlib.sha256(
                json.dumps(settings, sort_keys=True).encode()
            ).hexdigest()[:24]
            cache_path = (
                Path(
                    cache_dir
                    or os.environ.get("DATASETAGENT_PAGE_INDEX_CACHE_DIR", CACHE_ROOT)
                )
                / pdf_hash
                / key
                / f"page-{number:05d}.json"
            )
            try:
                if not use_cache:
                    raise FileNotFoundError("Page cache disabled")
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    cached["page"] == number
                    and cached["pdf_sha256"] == pdf_hash
                    and cached["quality"] in {"ok", "uncertain"}
                    and isinstance(cached["text"], str)
                    and isinstance(cached["lines"], list)
                    and all(
                        isinstance(line.get("text"), str)
                        and len(line.get("bbox", [])) == 4
                        for line in cached["lines"]
                    )
                ):
                    output[number] = cached
                    hits += 1
                    continue
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            if len(pending) >= workers:
                collect()
            render_start = time.perf_counter()
            pix = page.get_pixmap(
                matrix=pymupdf.Matrix(
                    max_side / max(width, height), max_side / max(width, height)
                ),
                colorspace=pymupdf.csGRAY,
                alpha=False,
            )
            png = pix.tobytes("png")
            render_seconds += time.perf_counter() - render_start
            rendered += 1
            future = pool.submit(
                _recognize,
                png,
                runtime,
                lang,
                timeout,
                pix.width,
                pix.height,
                width,
                height,
            )
            pending[future] = (number, width, height, cache_path)
        while pending:
            collect()
    pages = [output[number] for number in requested]
    manifest = {
        "version": VERSION,
        "pdf_sha256": pdf_hash,
        "page_count": count,
        "stage": stage,
        "max_side": max_side,
        "lang": lang,
        "pages": pages,
        "performance": {
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "render_seconds": round(render_seconds, 3),
            "workers": workers,
            "ocr_pages": rendered,
            "cache_hits": hits,
            "native_pages": sum(p["source"] == "pdf_text" for p in pages),
        },
        "uncertain_pages": [p["page"] for p in pages if p["quality"] != "ok"],
    }
    path = Path(output_dir) / f"page_{stage}.json"
    write_json(path, manifest)
    return {
        "status": "ready",
        "manifest": str(path.resolve()),
        **{k: v for k, v in manifest.items() if k != "pages"},
    }


def plan_page_reads(
    index: dict,
    tables: list[dict],
    *,
    limit: int = 32,
    contexts: list[dict] | None = None,
) -> dict:
    """Search locally; select method sections and SEQ/chain references anywhere.

    A confidently found Examples start can narrow the legacy method window.
    Unknown pages keep that window open. Budget overflow is explicit review work.
    """
    pages = {p["page"]: p for p in index["pages"]}
    count = index["page_count"]
    window = candidate_assay_method_pages(tables, count)
    # Classification can recognize an endpoint despite damaged header OCR.
    anchors = [
        t["page"]
        for t in tables
        if isinstance(t.get("page"), int)
        and any(
            r.get("kind") == "assay"
            for r in t.get("table_classification", {}).get("regions", [])
        )
    ]
    if anchors:
        window = sorted(
            set(window)
            | set(range(max(1, min(anchors) - 36), min(count, max(anchors)) + 1))
        )
    unknown = [number for number, page in pages.items() if page.get("quality") != "ok"]
    starts = []
    for number in window:
        for match in EXAMPLE.finditer(pages.get(number, {}).get("text", "")):
            value = next((g for g in match.groups() if g), "")
            if not value or value == "1":
                starts.append(number)
    narrowed = bool(starts and not set(window).intersection(unknown))
    methods = (
        list(range(min(starts), min(count, max(window) + 1) + 1))
        if narrowed
        else window
    )
    raw_text = "\n".join(
        str(c.get("text", ""))
        for t in tables
        for row in t.get("rows", [])
        for c in row
        if isinstance(c, dict)
    )
    seq_ids = set(SEQ.findall(raw_text))
    names = set()
    for table in tables:
        rows = table.get("rows", [])
        for region in table.get("table_classification", {}).get("regions", []):
            names.update(
                g["raw_name"].strip()
                for g in region.get("entity_groups", [])
                if g.get("raw_name")
            )
            column = region.get("entity_column")
            if isinstance(column, int) and column >= 0:
                for row in rows[
                    max(0, region.get("row_start", 0)) : region.get(
                        "row_end", len(rows) - 1
                    )
                    + 1
                ]:
                    if len(row) > column and isinstance(row[column], dict):
                        names.add(str(row[column].get("text", "")).strip())
    names = {
        name
        for name in names
        if 2 <= len(name) <= 80 and any(c.isalpha() for c in name)
    }
    name_patterns = [
        re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
        for name in sorted(names)
    ]
    table_numbers = set()
    for context in contexts or []:
        value = str(context.get("table_number") or "")
        match = re.fullmatch(r"(?:Table\s*)?(\d+[A-Za-z]?)", value, re.IGNORECASE)
        if match:
            table_numbers.add(match.group(1))
    citation_patterns = [
        re.compile(r"\bTable\s+" + re.escape(n) + r"\b", re.IGNORECASE)
        for n in sorted(table_numbers)
    ]
    reasons: dict[int, list[str]] = {}
    scores: dict[int, int] = {}

    def add(number: int, reason: str, score: int) -> None:
        if 1 <= number <= count:
            reasons.setdefault(number, [])
            if reason not in reasons[number]:
                reasons[number].append(reason)
            scores[number] = max(scores.get(number, 0), score)

    for number in methods:
        add(
            number, "assay_method_section" if narrowed else "assay_window_fallback", 100
        )
    for number, page in pages.items():
        text = page.get("text", "")
        found = set(SEQ.findall(text))
        entity_hit = any(pattern.search(text) for pattern in name_patterns)
        relevant = bool(
            (found and (CHAIN.search(text) or found.intersection(seq_ids)))
            or (entity_hit and CHAIN.search(text))
        )
        if relevant:
            add(number, "sequence_reference", 90 if found.intersection(seq_ids) else 80)
            for neighbor in (number - 1, number + 1):
                add(neighbor, "sequence_reference_neighbor", 40)
        # Numbered methods outside the legacy window also remain searchable.
        if (
            EXAMPLE.search(text)
            or any(pattern.search(text) for pattern in citation_patterns)
        ) and METHOD.search(text):
            add(number, "method_heading_or_table_reference", 120)
            for neighbor in (number - 1, number + 1):
                add(neighbor, "method_heading_neighbor", 105)
    for number in unknown:
        add(number, "uncertain_index", 70)
    ordered = sorted(reasons, key=lambda n: (-scores[n], n))
    selected = sorted(ordered[:limit])
    pending = sorted(set(ordered) - set(selected))
    method_hits = set(methods) | {
        n
        for n, rs in reasons.items()
        if any(r.startswith("method_heading") for r in rs)
    }
    return {
        "selected_pages": selected,
        "pending_pages": pending,
        "method_pages": sorted(method_hits.intersection(selected)),
        "legacy_method_pages": window,
        "method_window_narrowed": narrowed,
        "uncertain_index_pages": unknown,
        "query_seq_ids": sorted(seq_ids, key=int),
        "query_entity_names": sorted(names),
        "query_table_numbers": sorted(table_numbers),
        "reasons": {str(n): reasons[n] for n in sorted(reasons)},
        "limit": limit,
    }


def retrieve_page_evidence(
    source_pdf: Path,
    output_dir: Path,
    summary: dict,
    tables: list[dict],
    *,
    workers: int = 6,
    max_side: int = 2400,
    limit: int = 32,
    lang: str = "en",
    cache_dir: Path | None = None,
    contexts: list[dict] | None = None,
) -> dict:
    index = json.loads(Path(summary["manifest"]).read_text(encoding="utf-8"))
    if index["pdf_sha256"] != _digest(Path(source_pdf)):
        raise ValueError("Page index belongs to a different PDF")
    plan = plan_page_reads(index, tables, limit=limit, contexts=contexts)
    detail = build_page_index(
        source_pdf,
        output_dir,
        workers=workers,
        max_side=max_side,
        lang=lang,
        cache_dir=cache_dir,
        page_numbers=plan["selected_pages"],
        stage="detail",
    )
    pages = json.loads(Path(detail["manifest"]).read_text(encoding="utf-8"))["pages"]
    methods, blocks = [], []
    for page in pages:
        number = page["page"]
        if number in plan["method_pages"] and page["text"]:
            methods.append(
                {
                    **page,
                    "source_id": f"assay-method-page-{number:04d}",
                    "image": "",
                    "mode": "assay_method_page",
                }
            )
        for i, line in enumerate(page["lines"]):
            blocks.append(
                {
                    **line,
                    "page": number,
                    "block_id": f"page-detail-{number:04d}-line-{i:04d}",
                    "source": page["source"],
                    "coordinate_space": "pdf_points",
                }
            )
    unresolved = sorted(
        set(plan["uncertain_index_pages"])
        - {p["page"] for p in pages if p["quality"] == "ok"}
    )
    warnings = []
    for code, numbers in (
        ("page_read_budget", plan["pending_pages"]),
        ("page_index_uncertain", unresolved),
        ("page_detail_uncertain", detail["uncertain_pages"]),
    ):
        if numbers:
            warnings.append(
                {
                    "code": code,
                    "severity": "medium",
                    "table_ids": [],
                    "message": f"Page evidence requires review ({code}): {numbers}",
                    "pages": numbers,
                }
            )
    plan.update(
        status="needs_review" if warnings else "ready",
        warnings=warnings,
        detail=detail,
        unresolved_index_pages=unresolved,
    )
    path = Path(output_dir) / "page_retrieval.json"
    write_json(path, plan)
    return {
        "assay_method_pages": methods,
        "document_blocks": blocks,
        "page_retrieval": plan,
        "artifacts": {
            "page_index": summary["manifest"],
            "page_detail": detail["manifest"],
            "page_retrieval": str(path.resolve()),
        },
    }
