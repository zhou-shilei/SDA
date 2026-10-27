"""页面方向校正的兼容入口；核心实现已迁入 document.pdf2doc。"""

from __future__ import annotations

import json
from pathlib import Path

# 保留旧的导入路径，供现有裁图模块及外部调用使用。
from document.pdf2doc import (
    MANIFEST,
    ORIENTATION_VERSION as VERSION,
    WORKING_PDF,
    OrientationDetector,
    file_digest,
    find_tesseract,
    prepare_pdf,
    rotation_matrix,
    tesseract_env,
    validate_preparation,
)


def processing_pdf(root: Path) -> Path:
    """Legacy workspaces keep their geometry; new workspaces use the working PDF."""
    source, output, manifest = root / "source.pdf", root / WORKING_PDF, root / MANIFEST
    if manifest.exists():
        validate_preparation(source, output, manifest)
        # Never combine old geometry with a newly prepared PDF.
        for path, field in [(root / "regions.json", "document"), (root / "pages/pages.json", "source_pdf")]:
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            name = payload.get(field, {})
            if field == "document":
                name = name.get("path", "")
            if not isinstance(name, str) or Path(name).name != output.name:
                raise ValueError("Existing page geometry does not belong to the prepared PDF")
        state_path = root / "agent_run/agent_state.json"
        if state_path.is_file():
            config = json.loads(state_path.read_text(encoding="utf-8")).get("config", {})
            if Path(config.get("source_pdf") or "").name != output.name:
                raise ValueError("Existing agent evidence does not belong to the prepared PDF")
        return output
    return source


def has_legacy_artifacts(root: Path) -> bool:
    return any((root / name).exists() for name in (
        "regions.json", "tables/table_crops.json", "pages/pages.json", "agent_run/agent_state.json",
        "agent_run/ocr_results.json",
    ))
