#!/usr/bin/env python3
"""Validate generated examples; re-evaluate TF32 threshold-boundary cases in FP32."""
import argparse
import json
import math
import sys
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
import benchmark as b

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,default=b.ROOT/'results/pilot_20261027')
    p.add_argument('--boundary',action='store_true')
    p.add_argument('--numerics',action='store_true')
    args=p.parse_args();out=args.out.resolve()
    if not out.is_relative_to(b.ROOT):raise ValueError('Output must stay in PPL')
    items=b.load_lines(out/'sequences.jsonl')
    clean={x['id']:x for x in items if x['kind']=='clean'}
    assert len({x['id'] for x in items})==len(items)
    splits={name:{x['sequence'] for x in clean.values() if x['split']==name} for name in ['test','calibration']}
    assert not splits['test']&splits['calibration']
    for x in items:
        seq=x['sequence'];old=clean[x['parent_id']]['sequence'];kind=x['kind']
        if kind in ['random_sub1','conservative_sub1','ocr_like_sub1','random_sub3'] or kind.startswith('anchor_'):
            assert len(seq)==len(old)
            n=3 if kind=='random_sub3' else 1
            assert sum(a!=v for a,v in zip(seq,old))==n==len(x['edits'])
            for e in x['edits']:
                assert old[e['position']-1]==e['old'] and seq[e['position']-1]==e['new']
                from eval.anarci import IMGT_REGIONS
                num=int(''.join(v for v in e['imgt'] if v.isdigit()))
                low,high=IMGT_REGIONS[x['region']];assert low<=num<=high
        if kind=='delete1':
            q=x['edits'][0]['position']-1;assert old[:q]+old[q+1:]==seq
        if kind=='insert1':
            q=x['edits'][0]['position']-1;assert old[:q]+x['edits'][0]['new']+old[q:]==seq
        if kind=='conservative_sub1':
            e=x['edits'][0];assert e['old'] not in 'CGP'
            assert any(e['old'] in g and e['new'] in g for g in b.GROUPS)
        if kind=='ocr_like_sub1':
            e=x['edits'][0];assert b.OCR[e['old']]==e['new']
    audit={'rows':len(items),'checks':'unique IDs, chain-disjoint splits, edit reconstruction, residue counts, IMGT regions, substitution groups'}
    if args.numerics:
        import torch
        from eval.esm2 import score_sequences
        from eval.runtime import default_model
        torch.set_num_threads(4);torch.backends.cuda.matmul.allow_tf32=False
        refs=b.load_lines(out/'esm_fp32_reference.jsonl')
        current={s['sha256']:s for s in b.load_lines(out/'esm_scores.jsonl')}
        err=max(abs(a-v) for ref in refs for a,v in zip(ref['log_probs'],current[ref['sha256']]['log_probs']))
        decisions=all((r['max_site_nll']>=math.log(1000))==(current[r['sha256']]['max_site_nll']>=math.log(1000)) for r in refs)
        original=score_sequences([r['sequence'] for r in refs[:2]],default_model(),device='cuda:0',batch_size=64)
        origerr=max(abs(v-p['log_probability']) for r in refs[:2] for v,p in zip(r['log_probs'],original[r['sequence']]['position_scores']))
        numeric={'tf32_vs_fp32_sequences':len(refs),'tf32_max_abs_site_logprob_diff':err,
                 'tf32_site1000_decisions_identical':decisions,'batched_vs_original_sequences':2,
                 'batched_vs_original_max_abs_site_logprob_diff':origerr}
        assert err<.02 and decisions and origerr<.001,numeric
        b.dump(out/'numerical_validation.json',numeric)
    if args.boundary:
        import torch
        from eval.esm2 import score_sequences
        from eval.runtime import default_model
        torch.set_num_threads(4);torch.backends.cuda.matmul.allow_tf32=False
        scores=b.load_lines(out/'esm_scores.jsonl')
        if {b.digest(x['sequence']) for x in items if set(x['sequence'])<=set(b.AA)}-{s['sha256'] for s in scores}:
            raise RuntimeError('Complete ESM scoring before boundary audit')
        candidates=[s for s in scores if any(abs(-v-math.log(1000))<.05 for v in s['log_probs'])]
        print('FP32 boundary candidates:',len(candidates),flush=True)
        refs=score_sequences([s['sequence'] for s in candidates],default_model(),device='cuda:0',batch_size=64)
        replacements=[];changes=0;maxdiff=0.
        for s in candidates:
            lp=[r['log_probability'] for r in refs[s['sequence']]['position_scores']]
            maxdiff=max(maxdiff,max(abs(a-v) for a,v in zip(lp,s['log_probs'])))
            changes+=sum((a<=-math.log(1000))!=(v<=-math.log(1000)) for a,v in zip(lp,s['log_probs']))
            replacements.append({**s,'log_probs':lp,'mean_nll':-sum(lp)/len(lp),'max_site_nll':-min(lp),
                                 'pseudo_perplexity':math.exp(-sum(lp)/len(lp)), 'precision_override':'FP32 original SDA implementation'})
        b.write_lines(out/'esm_fp32_boundary_overrides.jsonl',replacements)
        audit.update(boundary_margin_nll=.05,boundary_sequences=len(candidates),boundary_flagged_site_changes=changes,
                     boundary_max_abs_site_logprob_diff=maxdiff)
    b.dump(out/'audit.json',audit);print(audit)

if __name__=='__main__':main()
