"""Find Tesseract OSD without importing another Python environment."""

import os
from pathlib import Path
import shutil


def find_tesseract():
    configured = os.environ.get("SDA_TESSERACT")
    if configured:
        executable = shutil.which(configured)
        if not executable:
            raise RuntimeError(f"SDA_TESSERACT is unavailable: {configured}")
        return str(Path(executable).resolve())
    installed = shutil.which("tesseract")
    if installed:
        return str(Path(installed).resolve())
    environments = Path(__file__).resolve().parents[2] / "envs"
    return next((str(p.resolve()) for p in sorted(environments.glob("*/bin/tesseract"))
                 if p.is_file() and os.access(p, os.X_OK)), None)


def tesseract_env(executable, lang):
    env = os.environ.copy()
    prefix = Path(executable).parent.parent
    data = env.get("SDA_TESSDATA") or env.get("TESSDATA_PREFIX")
    if not data and (prefix / "share/tessdata").is_dir():
        data = str(prefix / "share/tessdata")
    if data:
        for language in lang.split("+"):
            if not (Path(data) / f"{language}.traineddata").is_file():
                raise RuntimeError(f"Missing Tesseract language data: {language}.traineddata in {data}")
        env["TESSDATA_PREFIX"] = data
    libraries = prefix / "lib"
    if libraries.is_dir():
        env["LD_LIBRARY_PATH"] = os.pathsep.join([str(libraries), env.get("LD_LIBRARY_PATH", "")]).rstrip(os.pathsep)
    return env
