"""Resolve OCR executables and supervise isolated table OCR workers."""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any, Iterator

from .patent_layout.parallel import isolated_env

# Executable discovery

def find_tesseract() -> str | None:
    configured = os.environ.get("DATASETAGENT_TESSERACT")
    if configured:
        executable = shutil.which(configured)
        if not executable:
            raise RuntimeError(f"Configured Tesseract executable is unavailable: {configured}")
        return str(Path(executable).resolve())
    installed = shutil.which("tesseract")
    if installed:
        return str(Path(installed).resolve())
    # Shared project environments can provide the executable independently of
    # the web server's Python. Never add another environment's site-packages.
    environments = Path(__file__).resolve().parents[3] / "envs"
    return next((str(path.resolve()) for path in sorted(environments.glob("*/bin/tesseract"))
                 if path.is_file() and os.access(path, os.X_OK)), None)


def tesseract_env(executable: str, lang: str) -> dict[str, str]:
    env = os.environ.copy()
    prefix = Path(executable).parent.parent
    data = env.get("DATASETAGENT_TESSDATA") or env.get("TESSDATA_PREFIX")
    local_data = prefix / "share" / "tessdata"
    if not data and local_data.is_dir():
        data = str(local_data)
    if data:
        for language in lang.split("+"):
            if not (Path(data) / f"{language}.traineddata").is_file():
                raise RuntimeError(f"Tesseract language data missing: {language}.traineddata in {data}")
        # Conda packages may retain a prefix from a former mount point.
        env["TESSDATA_PREFIX"] = data
    libraries = prefix / "lib"
    if libraries.is_dir():
        env["LD_LIBRARY_PATH"] = os.pathsep.join([str(libraries), env.get("LD_LIBRARY_PATH", "")]).rstrip(os.pathsep)
    return env


# Isolated table OCR workers

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / '.studio/ocr_runtime.json'
EVENT = 'ocr-worker-result '


def load_runtime() -> dict[str, Any]:
    config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.is_file() else {}
    for key in ['workers', 'threads_per_worker', 'device', 'table_device', 'python', 'rec_batch_size', 'cpu_fallback_workers']:
        override = os.environ.get('DATASETAGENT_OCR_' + key.upper())
        if override is not None:
            config[key] = int(override) if key in {'workers', 'threads_per_worker', 'rec_batch_size', 'cpu_fallback_workers'} else override
    return config


def shard_tasks(tasks: list[dict[str, Any]], workers: int) -> list[list[dict[str, Any]]]:
    """Balance expensive large images without changing their source ordinal."""
    if workers < 1:
        raise ValueError('OCR workers must be positive')
    shards: list[list[dict[str, Any]]] = [[] for _ in range(min(workers, len(tasks)))]
    costs = [0] * len(shards)
    for task in sorted(tasks, key=lambda t: Path(t['image_path']).stat().st_size, reverse=True):
        index = min(range(len(shards)), key=costs.__getitem__)
        shards[index].append(task)
        costs[index] += Path(task['image_path']).stat().st_size
    return shards


def run_worker(path: Path) -> int:
    import cv2

    from .table_ocr import extract_one, make_backend

    job = json.loads(path.read_text())
    cv2.setNumThreads(job['threads'])
    backend = make_backend(job['backend'], job['lang'], job['device'], job['max_side'], job.get('rec_batch_size'))
    for task in job['tasks']:
        started = time.perf_counter()
        result = extract_one(Path(task['image_path']), job['metadata'], backend, task['number'])
        destination = path.parent / f"{job['id']}-table-{task['number']:06d}.json"
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(json.dumps({'table_id': task['table_id'], 'result': result.as_dict(),
                                          'seconds': round(time.perf_counter() - started, 6)}, ensure_ascii=False))
        temporary.replace(destination)
        print(EVENT + json.dumps({'table_id': task['table_id'], 'path': str(destination)}), flush=True)
    return 0


def supervise(job: dict[str, Any], path: Path, events: queue.Queue,
              active: list[subprocess.Popen], lock: threading.Lock, cancelled: threading.Event) -> dict[str, Any]:
    started = time.perf_counter()
    python = job.get('python') or sys.executable
    env = isolated_env(python, job['threads'], paddle=True)
    env['DATASETAGENT_OCR_CPU_THREADS'] = str(job['threads'])
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    with path.with_suffix('.log').open('w') as log:
        process = subprocess.Popen([python, '-m', 'dataagent.ocr_runtime', '_worker', '--job', str(path)],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        with lock:
            active.append(process)
            if cancelled.is_set() and process.poll() is None:
                process.terminate()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                if line.startswith(EVENT):
                    events.put(json.loads(line[len(EVENT):]))
            code = process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            with lock:
                active.remove(process)
    return {'id': job['id'], 'pid': process.pid, 'python': python, 'exit_code': code, 'tables': len(job['tasks']),
            'elapsed_seconds': round(time.perf_counter() - started, 3), 'log': str(path.with_suffix('.log'))}


def _extract_parallel(tasks: list[dict[str, Any]], metadata: dict[str, Any], output_dir: Path, *,
                     workers: int, threads: int, backend: str, lang: str, device: str | None,
                     max_side: int, python: str | None = None, rec_batch_size: int | None = None) -> Iterator[tuple[str, Any]]:
    from .table_ocr import TableResult

    if not tasks:
        return
    if min(workers, threads) < 1 or (rec_batch_size is not None and rec_batch_size < 1):
        raise ValueError('OCR process, thread and batch limits must be positive')
    python = str(Path(python or sys.executable).absolute())
    if not Path(python).is_file() or not os.access(python, os.X_OK):
        raise ValueError(f'OCR interpreter is missing or not executable: {python}')
    started = time.perf_counter()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='ocr-workers-', dir=output_dir))
    events: queue.Queue = queue.Queue()
    active: list[subprocess.Popen] = []
    lock = threading.Lock()
    cancelled = threading.Event()
    expected = {t['table_id']: t for t in tasks}
    if len(expected) != len(tasks):
        raise ValueError('OCR table identifiers must be unique')
    completed = set()
    task_seconds = {}
    runs = []
    pool = ThreadPoolExecutor(max_workers=min(workers, len(tasks)))
    futures = []
    print(f'ocr-runtime processes={min(workers, len(tasks))} threads_per_worker={threads} device={device or "auto"} rec_batch_size={rec_batch_size}', flush=True)
    try:
        for index, shard in enumerate(shard_tasks(tasks, workers)):
            job = {'id': f'ocr-{index + 1}', 'threads': threads, 'tasks': shard, 'metadata': metadata,
                   'backend': backend, 'lang': lang, 'device': device, 'max_side': max_side,
                   'python': python, 'rec_batch_size': rec_batch_size}
            path = work / f'ocr-{index + 1}.json'
            path.write_text(json.dumps(job))
            futures.append(pool.submit(supervise, job, path, events, active, lock, cancelled))
        while not all(f.done() for f in futures) or not events.empty():
            try:
                event = events.get(timeout=0.1)
            except queue.Empty:
                continue
            identity = event['table_id']
            path = Path(event['path']).resolve()
            if identity not in expected or identity in completed or not path.is_relative_to(work):
                raise ValueError('Unexpected OCR worker result')
            row = json.loads(path.read_text())
            if row['table_id'] != identity or row['result']['image'] != Path(expected[identity]['image_path']).name:
                raise ValueError('OCR worker result does not match its source image')
            source_id = metadata.get(row['result']['image'], {}).get('id')
            if source_id is not None and row['result'].get('source_id') != source_id:
                raise ValueError('OCR worker result has the wrong source identifier')
            result = TableResult.from_dict(row['result'])
            completed.add(identity)
            task_seconds[identity] = row['seconds']
            yield identity, result
        runs = [future.result() for future in futures]
        failures = [r for r in runs if r['exit_code'] != 0]
        if failures or completed != set(expected):
            raise RuntimeError(f'OCR workers failed or left tables incomplete; completed {len(completed)}/{len(expected)}; see {work}')
    finally:
        # On checkpoint/write/cancellation errors, stop inference before joining
        # the supervisor threads; never leave background model processes behind.
        cancelled.set()
        pool.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + 5
        terminated = set()
        while any(not future.done() for future in futures):
            with lock:
                processes = list(active)
            for process in processes:
                if process.poll() is None:
                    if time.monotonic() >= deadline:
                        process.kill()
                    elif process.pid not in terminated:
                        process.terminate()
                        terminated.add(process.pid)
            time.sleep(0.05)
        pool.shutdown(wait=True, cancel_futures=True)
        report = {'execution': 'parallel', 'workers': min(workers, len(tasks)),
                  'threads_per_worker': threads, 'device': device, 'python': python,
                  'rec_batch_size': rec_batch_size, 'completed_tables': len(completed),
                  'total_tables': len(tasks), 'elapsed_seconds': round(time.perf_counter() - started, 3),
                  'table_seconds': task_seconds, 'processes': runs, 'diagnostics': str(work)}
        path = output_dir / 'ocr_performance.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(path)


def needs_cpu_fallback(result: Any) -> bool:
    """Keep legacy recognition for blank or predominantly rotated evidence.

    The CPU path is a compatibility fallback, not an orientation correction.
    GPU/CPU rounding and recognition padding can change these difficult crops.
    """
    if not result.items:
        return True
    words = [item for item in result.items if len(item.text.strip()) >= 3]
    vertical = sum(item.height > 1.5 * max(1.0, item.x2 - item.x1) for item in words)
    return vertical >= 3 and vertical >= len(words) / 2


def extract_parallel(tasks: list[dict[str, Any]], metadata: dict[str, Any], output_dir: Path, *,
                     workers: int, threads: int, backend: str, lang: str, device: str | None,
                     max_side: int, python: str | None = None, rec_batch_size: int | None = None,
                     cpu_fallback_workers: int = 0) -> Iterator[tuple[str, Any]]:
    if cpu_fallback_workers < 0:
        raise ValueError('CPU fallback worker count must be non-negative')
    options = dict(workers=workers, threads=threads, backend=backend, lang=lang,
                   device=device, max_side=max_side, python=python, rec_batch_size=rec_batch_size)
    if not cpu_fallback_workers or not device or not device.startswith(('gpu', 'cuda')) or not tasks:
        yield from _extract_parallel(tasks, metadata, output_dir, **options)
        return
    started = time.perf_counter()
    by_id = {task['table_id']: task for task in tasks}
    fallback = []
    completed = set()
    cpu_dir = output_dir / 'ocr_cpu_fallback'
    path = output_dir / 'ocr_performance.json'
    cpu_path = cpu_dir / 'ocr_performance.json'
    def stamp(file: Path):
        if not file.is_file():
            return None
        stat = file.stat()
        return stat.st_ino, stat.st_mtime_ns, stat.st_size
    previous_gpu, previous_cpu = stamp(path), stamp(cpu_path)
    status = 'failed'
    cpu_started = False
    try:
        with closing(_extract_parallel(tasks, metadata, output_dir, **options)) as stream:
            for identity, result in stream:
                if needs_cpu_fallback(result):
                    fallback.append(by_id[identity])
                else:
                    completed.add(identity)
                    yield identity, result
        if fallback:
            cpu_started = True
            print(f'ocr-runtime cpu-fallback tables={len(fallback)} workers={cpu_fallback_workers} reason=rotated-or-empty', flush=True)
            # Use the coordinator's original CPU environment, not the GPU venv.
            with closing(_extract_parallel(fallback, metadata, cpu_dir, workers=cpu_fallback_workers,
                    threads=1, backend=backend, lang=lang, device='cpu', max_side=max_side,
                    python=sys.executable, rec_batch_size=rec_batch_size)) as stream:
                for identity, result in stream:
                    completed.add(identity)
                    yield identity, result
        status = 'completed'
    finally:
        if path.is_file() and stamp(path) != previous_gpu:
            report = json.loads(path.read_text())
            cpu = json.loads(cpu_path.read_text()) if cpu_started and cpu_path.is_file() and stamp(cpu_path) != previous_cpu else {}
            report['gpu_table_seconds'] = dict(report['table_seconds'])
            for identity, seconds in cpu.get('table_seconds', {}).items():
                report['table_seconds'][identity] = report['table_seconds'].get(identity, 0) + seconds
            report.update(execution='gpu_with_cpu_fallback', status=status,
                          gpu_elapsed_seconds=report['elapsed_seconds'],
                          elapsed_seconds=round(time.perf_counter() - started, 3),
                          completed_tables=len(completed), cpu_fallback_tables=len(fallback),
                          cpu_fallback_ids=[t['table_id'] for t in fallback], cpu_fallback=cpu)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            temporary.replace(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['_worker'])
    parser.add_argument('--job', required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(run_worker(args.job))
