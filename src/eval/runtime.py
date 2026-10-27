"""Local-only worker configuration, subprocess isolation and response grounding."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[3]


def default_python(tool: str) -> Path:
    configured = os.environ.get(f"DATASETAGENT_{tool.upper()}_PYTHON")
    if configured:
        return Path(configured).expanduser().resolve()
    candidates = ["ab-data-validator", "AbFlow"] if tool == "anarci" else ["AbFlow"]
    return next((ASSETS / "envs" / name / "bin/python" for name in candidates
                 if (ASSETS / "envs" / name / "bin/python").is_file()), Path(sys.executable))


def default_model() -> Path | None:
    if os.environ.get("DATASETAGENT_ESM2_MODEL"):
        return Path(os.environ["DATASETAGENT_ESM2_MODEL"]).expanduser().resolve()
    roots = [ASSETS / "hf_cache/transformers", Path(os.environ.get("HF_HUB_CACHE", str(Path.home() / ".cache/huggingface/hub")))]
    for root in roots:
        cache = root / "models--facebook--esm2_t33_650M_UR50D"
        ref = cache / "refs/main"
        if ref.is_file():
            model = cache / "snapshots" / ref.read_text().strip()
            if (model / "config.json").is_file():
                return model
    return None


@dataclass
class EvaluationConfig:
    anarci_python: Path
    esm2_python: Path
    esm2_model: Path | None
    anarci_enabled: bool = True
    esm2_enabled: bool = True
    device: str = "cuda"
    batch_size: int = 32
    timeout: int = 1800
    cpus: int = 1

    @classmethod
    def from_env(cls):
        enabled = lambda tool: os.environ.get(f"DATASETAGENT_{tool}_ENABLED", "auto").lower() not in {"0", "false", "no", "off"}
        return cls(default_python("anarci"), default_python("esm2"), default_model(),
            anarci_enabled=enabled("ANARCI"), esm2_enabled=enabled("ESM2"),
            device=os.environ.get("DATASETAGENT_ESM2_DEVICE", "cuda"),
            batch_size=int(os.environ.get("DATASETAGENT_ESM2_BATCH_SIZE", "32")),
            timeout=int(os.environ.get("DATASETAGENT_ESM2_TIMEOUT_SECONDS", "1800")),
            cpus=int(os.environ.get("DATASETAGENT_ANARCI_CPUS", "1")))


def run_worker(tool: str, items: list[dict], config: EvaluationConfig, output_dir: Path) -> tuple[dict, dict]:
    from .anarci import hmmer_directory

    output_dir.mkdir(parents=True, exist_ok=True)
    python = config.anarci_python if tool == "anarci" else config.esm2_python
    enabled = config.anarci_enabled if tool == "anarci" else config.esm2_enabled
    meta = {"tool": tool, "python": str(python), "status": "not_run", "elapsed_seconds": 0}
    if not enabled or not items:
        return {}, {**meta, "reason": "disabled" if not enabled else "no_sequences"}
    if not python.is_file() or (tool == "esm2" and (config.esm2_model is None or not (config.esm2_model / "config.json").is_file())):
        return {}, {**meta, "status": "unavailable", "reason": "Local evaluation Python or model is unavailable"}
    if config.batch_size < 1 or config.timeout < 1 or config.cpus < 1:
        raise ValueError("Evaluation batch size, timeout and CPUs must be positive")
    command = [str(python), "-m", "eval." + tool]
    meta["worker_sha256"] = hashlib.sha256(Path(__file__).with_name(tool + ".py").read_bytes()).hexdigest()
    if tool == "anarci":
        directory = hmmer_directory()
        if directory:
            command += ["--hmmer-dir", directory]
        command += ["--cpus", str(config.cpus)]
    else:
        command += ["--model", str(config.esm2_model), "--device", config.device,
                    "--batch-size", str(config.batch_size), "--sequence-input"]
        meta.update(model_path=str(config.esm2_model), model_revision=config.esm2_model.name, device=config.device,
                    config_sha256=hashlib.sha256((config.esm2_model / "config.json").read_bytes()).hexdigest())
    started = time.monotonic()
    request = json.dumps(items, ensure_ascii=False)
    (output_dir / f"{tool}_input.json").write_text(request + "\n")
    print(f"sequence-eval: {tool} sequences={len(items)}", flush=True)
    try:
        completed = subprocess.run(command, input=request, text=True, capture_output=True,
            timeout=config.timeout, check=True, env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]) + os.pathsep + os.environ.get("PYTHONPATH", "")})
        (output_dir / f"{tool}_stderr.log").write_text(completed.stderr)
        response = json.loads(completed.stdout)
        if not isinstance(response, list) or len(response) != len(items):
            raise ValueError("Evaluation worker changed the input count")
        originals = {item["id"]: item for item in items}
        answers = {}
        for item in response:
            key = item["id"]
            if key not in originals or key in answers:
                raise ValueError("Evaluation worker returned an unknown or duplicate sequence")
            sha = hashlib.sha256(originals[key]["sequence"].encode()).hexdigest()
            if item["sequence_sha256"] != sha or not isinstance(item.get("review"), dict):
                raise ValueError("Evaluation worker result does not match the evaluated sequence")
            review = item["review"]
            allowed = {"matched", "conflict", "unclassified", "partial", "multiple_domains", "invalid"} if tool == "anarci" else {"passed", "error", "manual_review"}
            if review.get("status") not in allowed:
                raise ValueError("Evaluation worker returned an unsupported status")
            # Reject NaN/Infinity rather than producing invalid JSON or optimistic decisions.
            json.dumps(review, allow_nan=False)
            answers[key] = review
        meta.update(status="completed")
        return answers, meta
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            (output_dir / f"{tool}_stderr.log").write_text(exc.stderr or "")
            reason = (exc.stderr or str(exc))[-1800:]
        else:
            reason = str(exc)[-1800:]
        meta.update(status="unavailable", reason=f"{type(exc).__name__}: {reason}")
        return {}, meta
    finally:
        meta["elapsed_seconds"] = round(time.monotonic() - started, 3)
