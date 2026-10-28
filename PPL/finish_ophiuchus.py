#!/usr/bin/env python3
"""Finish precision audits and reporting once already-running scorers finish."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.dont_write_bytecode = True
import benchmark as b
from ophiuchus import targets


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, default=b.ROOT / 'results/pilot_20261027')
    args = p.parse_args(); out = args.out.resolve()
    if not out.is_relative_to(b.ROOT):
        raise ValueError('All outputs must stay in PPL')
    items = b.load_lines(out / 'sequences.jsonl')
    env = dict(os.environ, TMPDIR=str(b.ROOT / 'tmp'), PYTHONDONTWRITEBYTECODE='1',
               MPLCONFIGDIR=str(b.ROOT / 'tmp/matplotlib'), HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    def status(stage, **extra):
        b.dump(out / 'ophiuchus_pipeline_status.json', {'stage': stage, 'updated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **extra})
        print(stage, extra, flush=True)
    def run(script, arguments, logname):
        command = [sys.executable, str(b.ROOT / script), '--out', str(out)] + arguments
        with (b.ROOT / 'logs' / logname).open('w') as log:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    for mode, pref, flags in [('single', 'ophiuchus_single', []), ('paired', 'ophiuchus_paired_fp16', ['--amp-fp16'])]:
        expected = {x['cache_key'] for x in targets(items, mode).values()}
        last = None
        while True:
            try:
                seen = {x['cache_key'] for x in b.load_lines(out / f'{pref}_scores.jsonl')}
            except json.JSONDecodeError:
                time.sleep(5)
                continue
            if len(seen) != last:
                status('waiting_' + mode, scored=len(seen), required=len(expected)); last = len(seen)
            if expected <= seen:
                break
            time.sleep(10)
        status('validating_' + mode)
        run('ophiuchus.py', ['validate', '--mode', mode, '--batch-size', '64'] + flags, f'{pref}_validate.log')
    status('reporting')
    run('compare_models.py', ['--paired-fp16'], 'model_comparison_report.log')
    run('check_comparison.py', [], 'model_comparison_verification.log')
    status('complete', report=str(b.ROOT / 'REPORT_ophiuchus_zh.md'))


if __name__ == '__main__':
    main()
