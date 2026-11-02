"""Raw PDF + dataset requirements -> an independent Codex extraction."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New result directory")
    parser.add_argument("--requirements", type=Path, default=Path(__file__).resolve().parents[2] / "prompt/codex_baseline.txt")
    args = parser.parse_args()
    pdf = args.pdf.resolve(strict=True)
    prompt = args.requirements.read_text(encoding="utf-8")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    work = Path(tempfile.mkdtemp(prefix="sda-simple-baseline-"))
    shutil.copyfile(pdf, work / "input.pdf")
    (output / "prompt.txt").write_text(prompt, encoding="utf-8")
    command = [
        "codex", "--ask-for-approval", "never", "exec", "--model", "gpt-6.1-sol",
        "-c", 'model_reasoning_effort="xhigh"', "--sandbox", "danger-full-access",
        "--ephemeral", "--json", "--disable", "multi_agent", "--disable", "skill_search",
        "-c", 'web_search="disabled"', "--skip-git-repo-check", "--cd", str(work),
        "--output-last-message", str(output / "last_message.txt"), "--ignore-user-config", "-",
    ]
    manifest = {
        "source_pdf": str(pdf), "source_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "working_directory": str(work), "command": command,
        "started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
        "inputs": ["input.pdf", "prompt.txt"],
    }
    write_json(output / "manifest.json", manifest)
    print(f"Results: {output}", flush=True)
    started = time.monotonic()
    usages = []
    with (output / "events.jsonl").open("w", encoding="utf-8", buffering=1) as events, (output / "stderr.log").open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
        manifest["pid"] = process.pid
        write_json(output / "manifest.json", manifest)
        process.stdin.write(prompt)
        process.stdin.close()
        for line in process.stdout:
            events.write(line)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "turn.completed":
                usages.append(event.get("usage") or {})
        code = process.wait()
    dataset = work / "dataset.json"
    validation_error = None
    if dataset.exists():
        shutil.copyfile(dataset, output / "dataset.json")
        try:
            data = json.loads(dataset.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data or not any(data.values()):
                validation_error = "Dataset is not a nonempty JSON object"
        except (ValueError, UnicodeError) as error:
            validation_error = str(error)
    else:
        validation_error = "Codex did not write dataset.json"
    manifest.update(
        status="completed" if code == 0 and validation_error is None else "failed",
        exit_code=code, elapsed_seconds=round(time.monotonic() - started, 2),
        finished_at_utc=datetime.now(timezone.utc).isoformat(),
        validation_error=validation_error, usage=usages,
    )
    write_json(output / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    return 0 if manifest["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
