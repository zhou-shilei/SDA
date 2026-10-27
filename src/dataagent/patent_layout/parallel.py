"""Bounded inference processes with per-runtime Python/native-library isolation.

PDF rendering stays in the coordinator. Threads only supervise subprocess I/O;
each process owns one model and its assigned images. Raw detections are fused
once, in backend/page order, using the same geometry as serial inference.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .engine import PatentRegionLocator, _sha256, fuse_page
from .render import RenderedPage, render_pdf
from .schema import RawDetection

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / '.studio/layout_runtime.json'
WORKER_EVENT = 'layout-worker-page '


def report_progress(completed: int, total: int, detail: str) -> None:
    print(f'layout-page {completed}/{total}: {detail}', flush=True)
    # Older running Studio servers only understand the original filename
    # pattern. Emit the completed COUNT so out-of-order pages cannot jump ahead.
    print(f'layout-progress page-{completed:05d}.png completed={completed}/{total}', flush=True)


def isolated_env(python: str, threads: int, *, paddle: bool = False) -> dict[str, str]:
    """Do not import another environment's Python packages or C++/CUDA libraries."""
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    prefix = Path(python).resolve().parent.parent
    libraries = [prefix / 'lib', Path('/usr/local/nvidia/lib'),
                 Path('/usr/local/nvidia/lib64'), Path('/usr/local/cuda/lib64')]
    env['LD_LIBRARY_PATH'] = os.pathsep.join(str(p) for p in libraries if p.is_dir())
    for key in ['OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                'NUMEXPR_NUM_THREADS', 'PADDLE_PDX_CPU_NUM_THREADS']:
        env[key] = str(threads)
    if paddle:
        # Paddle's predictor controls its own CPU threads; nested BLAS/OMP
        # pools otherwise multiply that limit for every page process.
        for key in ['OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS']:
            env[key] = '1'
    env['PYTHONUNBUFFERED'] = '1'
    return env


def load_runtime() -> dict[str, Any]:
    config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.is_file() else {}
    for key in ['execution', 'torch_python', 'torch_device', 'pp_device',
                'cpu_workers', 'max_workers', 'threads_per_worker']:
        value = os.environ.get('DATASETAGENT_LAYOUT_' + key.upper())
        if value is not None:
            config[key] = int(value) if key in {'cpu_workers', 'max_workers', 'threads_per_worker'} else value
    return config


def run_worker(job_path: Path) -> int:
    """Internal process entry point; JSONL carries unrounded source detections."""
    job = json.loads(job_path.read_text())
    name = job['backend']
    locator = PatentRegionLocator(backends=[name], model_dirs=job['model_dirs'],
                                  thresholds=job['thresholds'], device=job['device'], pp_engine=job['pp_engine'],
                                  cpu_threads=job['threads'])
    backend = locator.backends[0]
    try:
        import cv2
        cv2.setNumThreads(job['threads'])
    except ImportError:
        pass
    if name != 'pp_doclayoutv3':
        import torch
        torch.set_num_threads(job['threads'])
    with Path(job['results']).open('w') as output:
        for page in job['pages']:
            started = time.perf_counter()
            row = {'page': page['number'], 'backend': name, 'ok': False, 'detections': []}
            try:
                predictions = backend.predict(Path(page['image_path']))
                row.update(ok=True, detections=[{**asdict(item), 'bbox_px': [float(v) for v in item.bbox_px],
                                                'score': float(item.score)} for item in predictions])
            except Exception as exc:
                row.update(error_type=type(exc).__name__, error=str(exc))
            row['seconds'] = round(time.perf_counter() - started, 6)
            output.write(json.dumps(row) + '\n')
            output.flush()
            print(WORKER_EVENT + json.dumps({'backend': name, 'page': row['page'], 'ok': row['ok']}), flush=True)
    return 0


def plan_jobs(locator: PatentRegionLocator, pages: list[RenderedPage], directory: Path,
              torch_python: str | None, torch_device: str, pp_device: str,
              cpu_workers: int, threads: int) -> list[dict[str, Any]]:
    jobs = []
    for name in locator.backend_names:
        device = pp_device if name == 'pp_doclayoutv3' else torch_device
        python = (torch_python or sys.executable) if name != 'pp_doclayoutv3' else sys.executable
        if not Path(python).is_file():
            raise ValueError(f'Layout interpreter is missing: {python}')
        # GPU weights are loaded once per backend; CPU pages use bounded shards.
        shards = min(cpu_workers if device == 'cpu' else 1, len(pages))
        for index in range(shards):
            identity = f'{name}-{index + 1}'
            jobs.append({'id': identity, 'backend': name, 'python': python, 'device': device,
                         'threads': threads, 'pp_engine': locator.pp_engine,
                         'model_dirs': {k: str(v) for k, v in locator.model_dirs.items()},
                         'thresholds': locator.thresholds,
                         'pages': [{**asdict(p), 'image_path': str(p.image_path)} for p in pages[index::shards]],
                         'results': str(directory / (identity + '.jsonl')),
                         'log': str(directory / (identity + '.log'))})
    return jobs


def supervise(job: dict[str, Any], job_path: Path, events: queue.Queue) -> dict[str, Any]:
    started = time.perf_counter()
    command = [job['python'], '-m', 'dataagent.layout_tools', '_layout-worker', '--job', str(job_path)]
    env = isolated_env(job['python'], job['threads'], paddle=job['backend'] == 'pp_doclayoutv3')
    env['PYTHONPATH'] = str(ROOT / 'src')
    with Path(job['log']).open('w') as log:
        process = subprocess.Popen(command, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                if line.startswith(WORKER_EVENT):
                    event = json.loads(line[len(WORKER_EVENT):])
                    events.put((event['backend'], int(event['page'])))
            code = process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    return {'id': job['id'], 'backend': job['backend'], 'python': job['python'],
            'device': job['device'], 'pages': [p['number'] for p in job['pages']],
            'exit_code': code, 'elapsed_seconds': round(time.perf_counter() - started, 3),
            'log': job['log']}


def merge_results(locator: PatentRegionLocator, pages: list[RenderedPage], jobs: list[dict[str, Any]],
                  runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    errors = []
    for job, run in zip(jobs, runs):
        path = Path(job['results'])
        expected = {p['number'] for p in job['pages']}
        if path.exists():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                key = (row['backend'], row['page'])
                if row['backend'] != job['backend'] or row['page'] not in expected or key in rows:
                    raise ValueError('Unexpected or duplicate page/backend in layout worker output')
                rows[key] = row
        for number in expected:
            key = (job['backend'], number)
            if key not in rows:
                rows[key] = {'page': number, 'backend': job['backend'], 'ok': False,
                             'error_type': 'WorkerFailure',
                             'error': f"{job['id']} exited {run['exit_code']} without a page result; see {job['log']}"}
    results = []
    for page in sorted(pages, key=lambda p: p.number):
        detections = []
        successful = 0
        for name in locator.backend_names:
            row = rows[(name, page.number)]
            if row['ok']:
                successful += 1
                predictions = [RawDetection(**d) for d in row['detections']]
                if any(d.backend != name for d in predictions):
                    raise ValueError('Layout detection provenance does not match its worker')
                detections.extend(predictions)
            else:
                error = {k: row[k] for k in ['page', 'backend', 'error_type', 'error']}
                errors.append(error)
                if not locator.allow_missing_backends:
                    raise RuntimeError(f"{name} failed on PDF page {page.number}: {row['error']}")
        if not successful:
            raise RuntimeError(f'No layout backend succeeded on PDF page {page.number}; see layout worker logs')
        result, invalid = fuse_page(page, detections, locator.merge_iou)
        results.append(result)
        errors.extend(invalid)
    models = []
    for model in locator.backend_status:
        relevant = [r for (name, _), r in rows.items() if name == model['backend']]
        failed = [r for r in relevant if not r['ok']]
        models.append({**model, 'status': 'error' if len(failed) == len(relevant) else 'partial' if failed else 'ok',
                       'failed_pages': [r['page'] for r in failed],
                       'inference_seconds': round(sum(r.get('seconds', 0) for r in relevant), 3)})
    return results, errors, models


def locate_parallel(locator: PatentRegionLocator, pdf_path: Path, *, output_path: Path,
                    selected_pages: list[int] | None = None, torch_python: str | None = None,
                    torch_device: str = 'cpu', pp_device: str = 'cpu', cpu_workers: int = 2,
                    max_workers: int = 4, threads_per_worker: int = 4) -> dict[str, Any]:
    if not locator.backend_names or min(cpu_workers, max_workers, threads_per_worker) < 1:
        raise ValueError('Parallel layout needs at least one backend and positive worker/thread limits')
    if len(set(locator.backend_names)) != len(locator.backend_names):
        raise ValueError('Parallel layout backends must be unique')
    pdf_path = pdf_path.resolve()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    # Keep worker logs beside the final artifact for diagnosis and timings.
    work = Path(tempfile.mkdtemp(prefix=output_path.stem + '-workers-', dir=output_path.parent))
    with tempfile.TemporaryDirectory(prefix='patent-layout-images-') as images:
        render_started = time.perf_counter()
        pages = list(render_pdf(pdf_path, images, dpi=locator.dpi, pages=selected_pages))
        if not pages or (selected_pages is not None and {p.number for p in pages} != set(selected_pages)):
            raise ValueError('Requested PDF pages are empty or outside the document')
        render_seconds = time.perf_counter() - render_started
        jobs = plan_jobs(locator, pages, work, torch_python, torch_device, pp_device, cpu_workers, threads_per_worker)
        events: queue.Queue = queue.Queue()
        seen: dict[int, set[str]] = {p.number: set() for p in pages}
        completed = set()
        print(f'layout-runtime parallel processes={min(max_workers, len(jobs))} '
              f'torch={torch_device} paddle={pp_device} cpu_page_workers={cpu_workers}', flush=True)

        def progress():
            while True:
                try:
                    name, number = events.get_nowait()
                except queue.Empty:
                    return
                if number not in seen or name not in locator.backend_names:
                    raise ValueError('Unexpected layout progress event')
                seen[number].add(name)
                if len(seen[number]) == len(locator.backend_names) and number not in completed:
                    completed.add(number)
                    report_progress(len(completed), len(pages), f'source-page-{number:05d}')

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = []
            for job in jobs:
                path = work / (job['id'] + '.json')
                path.write_text(json.dumps(job))
                futures.append(pool.submit(supervise, job, path, events))
            while not all(f.done() for f in futures):
                progress()
                time.sleep(0.05)
            progress()
            runs = [f.result() for f in futures]
        results, errors, models = merge_results(locator, pages, jobs, runs)
        if len(completed) < len(pages):
            report_progress(len(pages), len(pages), 'merged with optional-backend failures')
        payload = {'schema_version': 'patent-layout-regions/v1',
                   'document': {'path': str(pdf_path), 'filename': pdf_path.name, 'sha256': _sha256(pdf_path)},
                   'settings': {'dpi': locator.dpi, 'merge_iou': locator.merge_iou,
                                'backends': locator.backend_names, 'coordinate_space': 'pdf_points', 'bbox_format': 'xyxy'},
                   'models': models, 'errors': errors, 'pages': results,
                   'performance': {'execution': 'parallel', 'elapsed_seconds': round(time.perf_counter() - started, 3),
                                   'render_seconds': round(render_seconds, 3), 'max_workers': max_workers,
                                   'cpu_workers': cpu_workers, 'threads_per_worker': threads_per_worker, 'workers': runs}}
        staged = output_path.with_suffix(output_path.suffix + '.tmp')
        staged.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        staged.replace(output_path)
        return payload
