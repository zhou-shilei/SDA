#!/usr/bin/env python3
"""Exact single-mask Ophiuchus-Ab scoring on the existing frozen benchmark.

Single-chain inputs give an ESM2 input-matched comparison. Paired inputs retain
the original, unedited partner; scores cover only the target chain. Neither
score is the diffusion ELBO or an autoregressive sequence likelihood.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
import benchmark as b


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def prefix(args):
    return f'ophiuchus_{args.mode}' + ('_fp16' if args.amp_fp16 else '')


def targets(items, mode):
    clean = {x['id']: x for x in items if x['kind'] == 'clean'}
    pairs = {(x['pair_id'], x['chain']): x for x in clean.values()}
    result = {}
    for x in items:
        if set(x['sequence']) - set(b.AA):
            continue  # Invalid O is not silently mapped to an unknown token.
        parent = clean[x['parent_id']]
        chain = parent['chain']
        partner = pairs[(parent['pair_id'], 'VL' if chain == 'VH' else 'VH')]
        # Use biological chain slots, even in the intentionally wrong-label control.
        t = {'sequence': x['sequence'], 'mode': mode}
        if mode == 'paired':
            t.update(partner_sequence=partner['sequence'], target_chain=chain)
        key = b.digest(json.dumps(t, sort_keys=True))
        result[x['id']] = {**t, 'cache_key': key}
    return result


def load_model(args):
    import torch
    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32 = args.tf32
    torch.backends.cudnn.allow_tf32 = args.tf32
    source = args.airgen / 'src/regress/encoders/ophiuchus.py'
    spec = importlib.util.spec_from_file_location('ppl_ophiuchus_loader', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    model = module.ESM2(num_layers=33, embed_dim=1280, attention_heads=20,
                        token_dropout=True, use_multimer=True)
    checkpoint = torch.load(str(args.checkpoint), map_location='cpu', mmap=True, weights_only=True)
    state = module.OphiuchusEncoder._normalize_state_dict(
        module.OphiuchusEncoder._extract_state_dict(checkpoint))
    incompatible = model.load_state_dict(state, strict=True)
    assert not incompatible.missing_keys and not incompatible.unexpected_keys
    model = model.to(args.device).eval()
    sources = [source] + sorted((args.airgen / 'src/byprot/models/lm/modules/mint').rglob('*.py'))
    metadata = {'checkpoint': str(args.checkpoint.resolve()),
                'checkpoint_sha256': file_hash(args.checkpoint),
                'source_hashes': {str(p.relative_to(args.airgen)): file_hash(p) for p in sources},
                'torch': torch.__version__, 'device': args.device,
                'cuda_device': torch.cuda.get_device_name() if args.device.startswith('cuda') else None,
                'parameters': sum(p.numel() for p in model.parameters()),
                'dtype': str(next(model.parameters()).dtype), 'allow_tf32': args.tf32,
                'autocast_fp16': args.amp_fp16,
                'strict_state_dict': True, 'vocabulary': list(model.alphabet.all_toks),
                'method': 'exact one target residue masked; normalize over full native vocabulary; target-chain PPPL',
                'paired_layout': 'CLS VH EOS PAD-to-150 CLS VL EOS PAD-to-128; chain IDs 0/1; no truncation',
                'script_sha256': file_hash(Path(__file__)),
                'sequence_manifest_sha256': file_hash(args.out / 'sequences.jsonl')}
    return model, metadata


def encode(model, group, device):
    import torch
    def chain_tokens(s):
        ids = [model.cls_idx] + model.alphabet.encode(s) + [model.eos_idx]
        assert len(ids) == len(s) + 2
        assert ''.join(model.alphabet.get_tok(i) for i in ids[1:-1]) == s
        return ids
    rows, chain_rows, columns = [], [], []
    paired = group[0]['mode'] == 'paired'
    assert all((x['mode'] == 'paired') == paired for x in group)
    width = 278 if paired else max(len(x['sequence']) + 2 for x in group)
    for x in group:
        if paired:
            h, l = (x['sequence'], x['partner_sequence']) if x['target_chain'] == 'VH' else (x['partner_sequence'], x['sequence'])
            hi, li = chain_tokens(h), chain_tokens(l)
            if len(hi) > 150 or len(li) > 128:
                raise ValueError('Paired layout would truncate residues; use larger explicit layout before scoring')
            row = hi + [model.padding_idx] * (150 - len(hi)) + li + [model.padding_idx] * (128 - len(li))
            chain_rows.append([0] * 150 + [1] * 128)
            start = 1 if x['target_chain'] == 'VH' else 151
        else:
            row = chain_tokens(x['sequence'])
            row += [model.padding_idx] * (width - len(row))
            chain_rows.append([0] * width)
            start = 1
        rows.append(row)
        columns.append(list(range(start, start + len(x['sequence']))))
    return (torch.tensor(rows, dtype=torch.long, device=device),
            torch.tensor(chain_rows, dtype=torch.long, device=device), columns)


def score_group(model, group, device, batch_size):
    import torch
    ids, chains, columns = encode(model, group, device)
    tasks = [(j, p) for j, cols in enumerate(columns) for p in cols]
    values = [[] for _ in group]
    with torch.inference_mode():
        for offset in range(0, len(tasks), batch_size):
            part = tasks[offset:offset + batch_size]
            seqids = torch.tensor([j for j, _ in part], device=device)
            cols = torch.tensor([p for _, p in part], device=device)
            rows = torch.arange(len(part), device=device)
            batch = ids[seqids].clone()
            truth = batch[rows, cols].clone()
            batch[rows, cols] = model.mask_idx
            assert (batch == model.mask_idx).sum(1).eq(1).all()
            logits = model(batch, chain_ids=chains[seqids])['logits'][rows, cols]
            lp = logits.float().log_softmax(-1).gather(1, truth[:, None]).squeeze(1).cpu().tolist()
            for (j, _), v in zip(part, lp):
                values[j].append(v)
    results = []
    for x, lp in zip(group, values):
        assert len(lp) == len(x['sequence']) and all(math.isfinite(v) and v <= 0 for v in lp)
        results.append({**x, 'log_probs': lp, 'mean_nll': -sum(lp) / len(lp),
                        'max_site_nll': -min(lp), 'pseudo_perplexity': math.exp(-sum(lp) / len(lp))})
    return results


def score(args):
    items = b.load_lines(args.out / 'sequences.jsonl')
    mapping = targets(items, args.mode)
    unique = {x['cache_key']: x for x in mapping.values()}
    cachefile = args.out / f'{prefix(args)}_scores.jsonl'
    previous = b.load_lines(cachefile)
    assert len({x['cache_key'] for x in previous}) == len(previous)
    done = {x['cache_key'] for x in previous}
    pending = [x for k, x in unique.items() if k not in done]
    if args.limit:
        pending = pending[:args.limit]
    model, meta = load_model(args)
    meta.update(mode=args.mode, batch_size=args.batch_size)
    metafile = args.out / f'{prefix(args)}_runtime.json'
    if metafile.exists():
        prior = json.loads(metafile.read_text())
        for key in ['checkpoint_sha256', 'source_hashes', 'dtype', 'allow_tf32', 'method', 'paired_layout', 'sequence_manifest_sha256']:
            if prior[key] != meta[key]:
                raise ValueError('Refusing incompatible score cache: ' + key)
    elif previous:
        raise ValueError('Score cache has no provenance')
    b.dump(metafile, meta)
    snapshot = args.out / f'{prefix(args)}_scoring_source.py'
    if not snapshot.exists():
        snapshot.write_text(Path(__file__).read_text())
    began = time.monotonic()
    masks = 0
    print(f'Ophiuchus {args.mode}: unique={len(unique)} pending={len(pending)} parameters={meta["parameters"]}', flush=True)
    with cachefile.open('a') as f:
        for start in range(0, len(pending), args.group_size):
            group = pending[start:start + args.group_size]
            import torch
            with torch.autocast(device_type='cuda', dtype=torch.float16, enabled=args.amp_fp16):
                records = score_group(model, group, args.device, args.batch_size)
            for record in records:
                f.write(json.dumps(record, allow_nan=False) + '\n')
                masks += len(record['sequence'])
            f.flush()
            seconds = time.monotonic() - began
            print(f'ophiuchus {args.mode} {min(start + args.group_size, len(pending))}/{len(pending)} masks={masks} elapsed={seconds:.1f}s masks/s={masks / seconds:.1f}', flush=True)
    b.dump(args.out / f'{prefix(args)}_last_run.json',
           {'new_sequences': len(pending), 'masked_positions': masks, 'seconds': time.monotonic() - began})


def validate(args):
    """Native batch invariance, pairing sensitivity, and FP32 threshold audit."""
    import torch
    import numpy as np
    items = b.load_lines(args.out / 'sequences.jsonl')
    mapping = targets(items, args.mode)
    scores = {x['cache_key']: x for x in b.load_lines(args.out / f'{prefix(args)}_scores.jsonl')}
    if set(x['cache_key'] for x in mapping.values()) - set(scores):
        raise RuntimeError('Finish scoring before validation')
    args.tf32 = False
    model, meta = load_model(args)
    original_meta = json.loads((args.out / f'{prefix(args)}_runtime.json').read_text())
    assert meta['checkpoint_sha256'] == original_meta['checkpoint_sha256']
    assert meta['source_hashes'] == original_meta['source_hashes']
    assert meta['sequence_manifest_sha256'] == original_meta['sequence_manifest_sha256']
    reference_file = args.out / f'{prefix(args)}_fp32_reference_cache.jsonl'
    reference_meta = args.out / f'{prefix(args)}_fp32_reference_runtime.json'
    reference_identity = {k: meta[k] for k in ['checkpoint_sha256', 'source_hashes', 'sequence_manifest_sha256', 'torch']}
    if reference_file.exists():
        assert json.loads(reference_meta.read_text()) == reference_identity
        reference_cache = {s['cache_key']: s for s in b.load_lines(reference_file)}
    else:
        # A completed prior audit is already an exact FP32 reference. Reuse it
        # when extending the set of numerical boundary checks.
        prior_validation = args.out / f'{prefix(args)}_validation.json'
        prior_overrides = args.out / f'{prefix(args)}_fp32_overrides.jsonl'
        reference_cache = {}
        if prior_validation.exists() and prior_overrides.exists():
            prior = json.loads(prior_validation.read_text())
            assert prior['checkpoint_sha256'] == meta['checkpoint_sha256']
            reference_cache = {s['cache_key']: s for s in b.load_lines(prior_overrides)}
        b.dump(reference_meta, reference_identity)
        b.write_lines(reference_file, list(reference_cache.values()))
    # Independent single-sequence vs cross-sequence padding/batching, both in FP32.
    probes = [mapping[x['id']] for x in items if x['kind'] == 'clean' and x['split'] == 'test'][:4]
    grouped = score_group(model, probes, args.device, args.batch_size)
    alone = [score_group(model, [x], args.device, args.batch_size)[0] for x in probes]
    batch_diff = max(abs(v - w) for a, c in zip(grouped, alone) for v, w in zip(a['log_probs'], c['log_probs']))
    assert batch_diff < 0.001, batch_diff
    # Reproduce the fixed threshold plus all clean-calibrated mean/max decisions.
    thresholds = {}
    for chain in ['VH', 'VL']:
        cal = [scores[mapping[x['id']]['cache_key']] for x in items
               if x['kind'] == 'clean' and x['split'] == 'calibration' and x['chain'] == chain]
        thresholds[chain] = {metric: float(np.quantile([x[metric] for x in cal], .95, method='higher'))
                             for metric in ['max_site_nll', 'mean_nll']}
    margin = 0.2 if args.amp_fp16 else 0.05
    mean_margin = 0.005
    candidates = {x['cache_key']: x for x in probes}
    # Recompute every clean control, so calibrated thresholds themselves use FP32.
    for x in items:
        if x['kind'] == 'clean':
            t = mapping[x['id']]
            candidates[t['cache_key']] = t
    clean_scores = {key: s for key, s in reference_cache.items() if key in candidates}
    values = list(candidates.values())
    values = [s for s in values if s['cache_key'] not in clean_scores]
    for offset in range(0, len(values), args.group_size):
        for s in score_group(model, values[offset:offset + args.group_size], args.device, args.batch_size):
            clean_scores[s['cache_key']] = s
            reference_cache[s['cache_key']] = s
        b.write_lines(reference_file, list(reference_cache.values()))
        print(f'FP32 controls {min(offset + args.group_size, len(values))}/{len(values)}', flush=True)
    for chain in ['VH', 'VL']:
        cal = [clean_scores[mapping[x['id']]['cache_key']] for x in items
               if x['kind'] == 'clean' and x['split'] == 'calibration' and x['chain'] == chain]
        thresholds[chain] = {metric: float(np.quantile([x[metric] for x in cal], .95, method='higher'))
                             for metric in ['max_site_nll', 'mean_nll']}
    for x in items:
        if x['id'] not in mapping:
            continue
        t = mapping[x['id']]; s = scores[t['cache_key']]
        near = any(abs(-v - math.log(1000)) < margin for v in s['log_probs'])
        # Localization can change at a non-maximum residue even when the chain
        # remains flagged at another site. Audit every site's calibrated cutoff.
        near |= any(abs(-v - thresholds[x['chain']]['max_site_nll']) < margin for v in s['log_probs'])
        near |= abs(s['mean_nll'] - thresholds[x['chain']]['mean_nll']) < mean_margin
        if near:
            candidates[t['cache_key']] = t
    replacements = dict(reference_cache)
    replacements.update(clean_scores)
    pending = [t for key, t in candidates.items() if key not in replacements]
    print(f'FP32 {args.mode}: controls={len(clean_scores)} boundary={len(pending)}', flush=True)
    for offset in range(0, len(pending), args.group_size):
        for s in score_group(model, pending[offset:offset + args.group_size], args.device, args.batch_size):
            replacements[s['cache_key']] = s
        b.write_lines(reference_file, list(replacements.values()))
        print(f'FP32 boundary {min(offset + args.group_size, len(pending))}/{len(pending)}', flush=True)
    max_diff = max(abs(v - w) for key, s in replacements.items() for v, w in zip(s['log_probs'], scores[key]['log_probs']))
    assert max_diff < margin, (max_diff, margin)
    max_mean_diff = max(abs(s['mean_nll'] - scores[key]['mean_nll']) for key, s in replacements.items())
    assert max_mean_diff < mean_margin, (max_mean_diff, mean_margin)
    site_changes = sum((v <= -math.log(1000)) != (w <= -math.log(1000)) for key, s in replacements.items()
                       for v, w in zip(s['log_probs'], scores[key]['log_probs']))
    b.write_lines(args.out / f'{prefix(args)}_fp32_overrides.jsonl', list(replacements.values()))
    validation = {'batch_vs_single_max_abs_logprob_diff': batch_diff,
                  'fp32_sequences': len(replacements), 'all_clean_controls_fp32': True,
                  'boundary_margin_nll': margin, 'mean_boundary_margin_nll': mean_margin,
                  'all_sites_checked_at_calibrated_cutoff': True,
                  'fast_inference': 'autocast_fp16' if args.amp_fp16 else 'fp32_tf32',
                  'tf32_vs_fp32_max_abs_logprob_diff': max_diff, 'fast_vs_fp32_max_abs_mean_nll_diff': max_mean_diff,
                  'site1000_decision_changes': site_changes, 'final_calibrated_thresholds': thresholds,
                  'checkpoint_sha256': meta['checkpoint_sha256']}
    if args.mode == 'paired':
        probe = dict(probes[0]); probe['partner_sequence'] = probes[2]['partner_sequence']
        changed = score_group(model, [probe], args.device, args.batch_size)[0]
        validation['partner_swap_max_abs_logprob_change'] = max(abs(v - w) for v, w in zip(changed['log_probs'], alone[0]['log_probs']))
        assert validation['partner_swap_max_abs_logprob_change'] > 1e-5
    b.dump(args.out / f'{prefix(args)}_validation.json', validation)
    print(json.dumps(validation, indent=2), flush=True)


def probe(args):
    """Early precision/layout check before the long scoring run has finished."""
    items = b.load_lines(args.out / 'sequences.jsonl')
    mapping = targets(items, args.mode)
    scores = {x['cache_key']: x for x in b.load_lines(args.out / f'{prefix(args)}_scores.jsonl')}
    chosen = [mapping[x['id']] for x in items if x['kind'] == 'clean'
              and x['id'] in mapping and mapping[x['id']]['cache_key'] in scores][:4]
    if len(chosen) < 4:
        raise RuntimeError('Score at least four original controls first')
    args.tf32 = False
    model, meta = load_model(args)
    together = score_group(model, chosen, args.device, args.batch_size)
    alone = [score_group(model, [x], args.device, args.batch_size)[0] for x in chosen]
    batched_diff = max(abs(a - c) for x, y in zip(together, alone) for a, c in zip(x['log_probs'], y['log_probs']))
    precision_diff = max(abs(a - c) for x in together for a, c in zip(x['log_probs'], scores[x['cache_key']]['log_probs']))
    assert batched_diff < .001
    report = {'sequences': len(chosen), 'batch_vs_single_max_abs_logprob_diff': batched_diff,
              'tf32_vs_fp32_max_abs_logprob_diff': precision_diff,
              'checkpoint_sha256': meta['checkpoint_sha256']}
    if args.mode == 'paired':
        changed = dict(chosen[0], partner_sequence=chosen[2]['partner_sequence'])
        replacement = score_group(model, [changed], args.device, args.batch_size)[0]
        report['partner_swap_max_abs_logprob_change'] = max(abs(a - c) for a, c in zip(replacement['log_probs'], alone[0]['log_probs']))
        assert report['partner_swap_max_abs_logprob_change'] > 1e-5
    b.dump(args.out / f'{prefix(args)}_probe.json', report)
    print(json.dumps(report, indent=2), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage', choices=['score', 'validate', 'probe'])
    p.add_argument('--out', type=Path, default=b.ROOT / 'results/pilot_20261027')
    p.add_argument('--airgen', type=Path, default=b.ROOT.parent.parent / 'AirGen-Dev')
    p.add_argument('--checkpoint', type=Path)
    p.add_argument('--mode', choices=['single', 'paired'], default='single')
    p.add_argument('--device', default='cuda:0')
    p.add_argument('--batch-size', type=int, default=32)
    p.add_argument('--group-size', type=int, default=8)
    p.add_argument('--limit', type=int)
    p.add_argument('--tf32', action='store_true')
    p.add_argument('--amp-fp16', action='store_true', help='Separate FP16-autocast cache; validate against native FP32')
    args = p.parse_args()
    args.out = args.out.resolve()
    if not args.out.is_relative_to(b.ROOT):
        raise ValueError('All outputs must stay inside PPL')
    args.checkpoint = args.checkpoint or args.airgen / 'byprot-checkpoints/Ophiuchus-Ab.ckpt'
    {'score': score, 'validate': validate, 'probe': probe}[args.stage](args)


if __name__ == '__main__':
    main()
