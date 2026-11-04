"""Validate portable case assets and the web service's import dependencies."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.experience import ExperienceWorkspace

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument("--parsing", action="store_true", help="Also check local OCR and model-client prerequisites")
args = parser.parse_args()
workspace = ExperienceWorkspace(args.root)
result = workspace.validate_cases()
if args.parsing:
    modules = {name: importlib.util.find_spec(name) is not None for name in ("paddle", "paddlex", "paddleocr", "langgraph", "pydantic")}
    result["parsing_modules"] = modules
    result["model_client"] = shutil.which("codex") is not None
    root = Path.home() / ".paddlex/official_models"
    result["ocr_models"] = {name: (root / name).is_dir() for name in (
        "PP-DocLayoutV3", "PP-OCRv6_medium_det", "PP-OCRv6_medium_rec", "PaddleOCR-VL-1.6")}
    result["parsing_prerequisites_present"] = all(modules.values()) and result["model_client"] and all(result["ocr_models"].values())
print(json.dumps(result, ensure_ascii=False, indent=2))
if args.parsing and not result["parsing_prerequisites_present"]:
    sys.exit(1)
