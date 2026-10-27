"""PDF page rendering with a single, explicit coordinate convention."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


@dataclass
class RenderedPage:
    number: int
    image_path: Path
    width_px: int
    height_px: int
    width_pt: float
    height_pt: float


def render_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    dpi: int = 150,
    pages: Iterable[int] | None = None,
) -> Iterator[RenderedPage]:
    """Render selected 1-based PDF pages to PNG files.

    PyMuPDF is used because it preserves the native PDF page rectangle.  The
    detector sees pixels; output conversion back to PDF points happens in the
    engine after prediction.
    """

    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        try:
            import fitz  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "PDF rendering requires PyMuPDF. Install DataAgent/vendor/requirements-pdf.txt"
            ) from exc

    pdf_path = Path(pdf_path).expanduser().resolve()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    requested = set(pages) if pages is not None else None
    scale = float(dpi) / 72.0
    document = fitz.open(pdf_path)
    try:
        for index in range(len(document)):
            number = index + 1
            if requested is not None and number not in requested:
                continue
            page = document.load_page(index)
            rectangle = page.rect
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image_path = output_dir / f"page-{number:05d}.png"
            pixmap.save(str(image_path))
            yield RenderedPage(
                number=number,
                image_path=image_path,
                width_px=int(pixmap.width),
                height_px=int(pixmap.height),
                width_pt=float(rectangle.width),
                height_pt=float(rectangle.height),
            )
    finally:
        document.close()
