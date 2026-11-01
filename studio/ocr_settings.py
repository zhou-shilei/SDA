"""Choose bounded Studio table parallelism without importing an OCR/GPU model."""

import re
import subprocess


# Refined against the complete 29-table 49-Sructure workload on an A100 80GB.
# The benchmark report records the selected cap and measured memory separately.
TABLE_WORKER_LIMIT = 6
TABLE_WORKER_MEMORY_MIB = 12000
OCR_MEMORY_RESERVE_MIB = 8 * 1024


def table_worker_settings(device, backend, requested=None, *, page_workers=1):
    if type(page_workers) is not int or page_workers < 1:
        raise ValueError("page_workers 必须为正整数")
    if requested is not None:
        if type(requested) is not int or requested < 1:
            raise ValueError("table_workers 必须为正整数；省略时按显存自动选择")
        return {"workers": requested, "policy": "explicit", "device": device, "backend": backend}
    reserve = OCR_MEMORY_RESERVE_MIB * page_workers
    result = {"workers": 1, "policy": "auto", "device": device, "backend": backend,
              "worker_limit": TABLE_WORKER_LIMIT, "memory_per_worker_mib": TABLE_WORKER_MEMORY_MIB,
              "memory_reserve_mib": reserve, "page_workers": page_workers}
    match = re.fullmatch(r"gpu:(\d+)", device)
    if backend != "vl" or not match:
        return {**result, "reason": "CPU or non-VL backend uses one worker by default"}
    try:
        proc = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits",
                               "--id=" + match[1]], capture_output=True, text=True, timeout=3, check=True)
        free = int(proc.stdout.strip())
        if free < 0:
            raise ValueError("Invalid free GPU memory")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {**result, "reason": "GPU memory unavailable; using one worker"}
    workers = max(1, min(TABLE_WORKER_LIMIT, (free - reserve) // TABLE_WORKER_MEMORY_MIB))
    return {**result, "workers": workers, "available_gpu_memory_mib": free,
            "reason": "Measured concurrency cap, bounded by available memory with room for parallel page OCR"}
