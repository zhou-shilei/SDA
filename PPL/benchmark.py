#!/usr/bin/env python3
"""Reproducible region-wise extraction error benchmark; all outputs stay in PPL.

Uses the existing SDA ANARCI decision unchanged, and exact single-mask ESM2
pseudo-likelihood. Synthetic errors measure screening, not biological function.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import sys
import time
from collections import Counter, defaultdict

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'src'))
AA = 'ACDEFGHIKLMNPQRSTVWY'
REGIONS = ['FR1', 'CDR1', 'FR2', 'CDR2', 'FR3', 'CDR3', 'FR4']
GROUPS = ['AVLIM', 'FYW', 'ST', 'NQ', 'DE', 'KRH', 'CGP']
OCR = {'I':'L', 'L':'I', 'V':'Y', 'Y':'V', 'S':'G', 'G':'S',
       'D':'Q', 'Q':'D', 'E':'F', 'F':'E', 'N':'H', 'H':'N', 'M':'W', 'W':'M',
       'A':'R', 'R':'A', 'C':'G', 'T':'I', 'K':'R', 'P':'F'}

def digest(s):
    return hashlib.sha256(s.encode()).hexdigest()

def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')

def load_lines(path):
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]

def write_lines(path, items):
    with path.open('w') as f:
        for x in items: f.write(json.dumps(x, ensure_ascii=False, allow_nan=False)+'\n')

def csv_out(path, rows):
    if not rows: return
    with path.open('w', newline='') as f:
        w=csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

def positions(review, sequence):
    domain=review['domains'][0]
    p=domain['sequence_start']
    result={r:[] for r in REGIONS}
    from eval.anarci import IMGT_REGIONS
    for e in domain['numbering']:
        if e['residue']=='-': continue
        assert sequence[p]==e['residue'], (p, e, sequence)
        for r,(lo,hi) in IMGT_REGIONS.items():
            if lo<=e['position']<=hi: result[r].append((p,e['position'],e['insertion']))
        p+=1
    assert p==domain['sequence_end']
    return result

def prepare(args):
    from eval.anarci import review_sequences
    rng=random.Random(args.seed)
    # Reservoir of rows (natural multiplicity), no score-based filtering.
    sample=[]; total=eligible=0; species=Counter(); bad=Counter()
    with args.source.open(newline='') as f:
        for rowidx,row in enumerate(csv.DictReader(f), 1):
            total+=1; species[row.get('species','unknown')]+=1
            h=row.get('cleaned_h_sequence') or row['h_sequence']
            l=row.get('cleaned_l_sequence') or row['l_sequence']
            if not h or not l or set(h+l)-set(AA): bad['invalid_alphabet_or_empty']+=1;continue
            if not (80<=len(h)<=160 and 80<=len(l)<=160): bad['length_outside_80_160']+=1;continue
            eligible+=1
            item={'row':rowidx,'h':h,'l':l,'species':row.get('species','unknown'),
                  'h_v_call':row.get('h_v_call',''), 'l_v_call':row.get('l_v_call',''),
                  'cluster':row.get('ab_cluster_id','')}
            if len(sample)<args.pairs: sample.append(item)
            else:
                j=rng.randrange(eligible)
                if j<args.pairs: sample[j]=item
            if total%500000==0: print(f'scan rows={total}',flush=True)
    # Keep exact duplicates together and do not allow identical chains across splits.
    rng.shuffle(sample); seen=set(); selected=[]
    for x in sample:
        if x['h'] in seen or x['l'] in seen: bad['sample_duplicate_pair_or_chain']+=1;continue
        seen.update([x['h'],x['l']]);selected.append(x)
    items=[]
    for i,x in enumerate(selected):
        split='calibration' if i<args.calibration_pairs else 'test'
        for chain,key in [('VH','h'),('VL','l')]:
            ident=f'p{i:04d}_{chain}'
            items.append({'id':ident,'parent_id':ident,'pair_id':f'p{i:04d}', 'split':split,
                          'sequence':x[key],'chain':chain,'source_row':x['row'], 'species':x['species'],
                          'v_call':x[key+'_v_call'], 'source_cluster':x['cluster'],
                          'kind':'clean','region':'ALL','edits':[], 'affected_positions':[]})
    reviews=review_sequences(items,ncpu=args.cpus)
    byid={x['id']:x['review'] for x in reviews}
    variants=[]; skipped=Counter()
    test_pairs=[]
    for x in items:
        if x['split']!='test': continue
        if x['pair_id'] not in test_pairs: test_pairs.append(x['pair_id'])
        if test_pairs.index(x['pair_id'])>=args.mutation_pairs: continue
        rev=byid[x['id']]
        if rev['status']!='matched': skipped['baseline_not_matched']+=1;continue
        seq=x['sequence']; pos=positions(rev,seq)
        def add(kind,region,new,edits,affected,chain=None):
            assert new!=seq or chain is not None
            variants.append({**x,'id':f"{x['id']}__{region}__{kind}", 'sequence':new,
                             'chain':chain or x['chain'],'kind':kind,'region':region,
                             'edits':edits,'affected_positions':affected})
        for region,ps in pos.items():
            if not ps: skipped[region+'_empty']+=1;continue
            p,imgt,ins=rng.choice(ps); old=seq[p]
            for kind in ['random_sub1','conservative_sub1','ocr_like_sub1','random_sub3','delete1','insert1']:
                edits=[]; chars=list(seq); affected=[]
                if kind=='random_sub3':
                    chosen=rng.sample(ps,min(3,len(ps)))
                    for q,n,t in chosen:
                        new=rng.choice(AA.replace(seq[q],''));chars[q]=new
                        edits.append({'position':q+1,'imgt':str(n)+t,'old':seq[q],'new':new});affected.append(q+1)
                elif kind=='delete1':
                    del chars[p]; edits=[{'position':p+1,'imgt':str(imgt)+ins,'old':old,'new':''}]
                    # A deleted residue has no score; define junction +/- 1 residue.
                    affected=[q for q in [p,p+1] if 1<=q<=len(chars)]
                elif kind=='insert1':
                    new=rng.choice(AA); chars.insert(p,new);affected=[p+1]
                    edits=[{'position':p+1,'imgt':str(imgt)+ins,'old':'','new':new}]
                else:
                    if kind=='random_sub1': new=rng.choice(AA.replace(old,''))
                    elif kind=='ocr_like_sub1': new=OCR[old]
                    else:
                        choices=next(g for g in GROUPS if old in g).replace(old,'')
                        # C/G/P singletons are not a conservative chemistry group.
                        if old in 'CGP':
                            alternatives=[q for q,_,_ in ps if seq[q] not in 'CGP']
                            if not alternatives: skipped[region+'_no_conservative_site']+=1;continue
                            p=rng.choice(alternatives);old=seq[p];imgt,ins=next((n,t) for q,n,t in ps if q==p)
                            choices=next(g for g in GROUPS if old in g).replace(old,'')
                        new=rng.choice(choices)
                    chars[p]=new;affected=[p+1]
                    edits=[{'position':p+1,'imgt':str(imgt)+ins,'old':old,'new':new}]
                add(kind,region,''.join(chars),edits,affected)
        for anchor in [23,41,104,118]:
            found=[(r,q,n,t) for r,ps in pos.items() for q,n,t in ps if n==anchor and not t]
            if found:
                r,p,n,t=found[0];new='A' if seq[p]!='A' else 'D'
                add('anchor_'+str(anchor),r,seq[:p]+new+seq[p+1:],
                    [{'position':p+1,'imgt':str(anchor),'old':seq[p],'new':new}],[p+1])
        for n in [5,20]:
            add('truncate_N'+str(n),'ALL',seq[n:],[{'deleted_prefix':n}],list(range(1,min(4,len(seq)-n)+1)))
        add('invalid_O','ALL',seq[:20]+'O'+seq[21:],[{'position':21,'old':seq[20],'new':'O'}],[21])
        add('wrong_chain_label','ALL',seq,[],[], 'VL' if x['chain']=='VH' else 'VH')
        # Valid same-chain replacement: wrong sequence identity but still plausible.
        donor=next(z for z in items if z['chain']==x['chain'] and z['parent_id']!=x['parent_id'] and z['split']=='test')
        add('valid_sequence_swap','ALL',donor['sequence'],[{'donor_parent_id':donor['id']}],[])
    allitems=items+variants
    write_lines(args.out/'sequences.jsonl',allitems)
    write_lines(args.out/'clean_anarci.jsonl',reviews)
    stat=args.source.stat()
    dump(args.out/'dataset_manifest.json',{'source':str(args.source.resolve()),'source_size':stat.st_size,
        'source_mtime_ns':stat.st_mtime_ns,'source_rows':total,'eligible_rows':eligible,
        'species_counts':species,'exclusions':bad,'seed':args.seed,'sample_pairs':len(selected),
        'clean_chains':len(items),'synthetic_errors':len(variants),'skipped_mutations':skipped,
        'counts_by_kind':Counter(x['kind'] for x in allitems),'arguments':{k:str(v) for k,v in vars(args).items()},
        'sequence_manifest_sha256':digest((args.out/'sequences.jsonl').read_text()),
        'caveat':'Real OAS sequences are assumed-correct controls, not manually verified extraction ground truth. Row sampling preserves natural source multiplicity. Splits exact-chain-disjoint, not lineage-cluster-disjoint.'})
    print(f'prepared clean={len(items)} variants={len(variants)} source_rows={total}',flush=True)

def anarci_score(args):
    from eval.anarci import review_sequences
    items=load_lines(args.out/'sequences.jsonl')
    results=[]
    for start in range(0,len(items),256):
        chunk=items[start:start+256]; began=time.monotonic()
        results.extend(review_sequences(chunk,ncpu=args.cpus))
        print(f'anarci {min(start+256,len(items))}/{len(items)} seconds={time.monotonic()-began:.1f}',flush=True)
    write_lines(args.out/'anarci_scores.jsonl',results)

def esm_score(args):
    import torch
    import transformers
    from transformers import AutoTokenizer,EsmForMaskedLM
    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32=args.tf32
    from eval.runtime import default_model
    modelpath=args.model or default_model()
    if modelpath is None: raise RuntimeError('Local ESM2 checkpoint missing')
    tokenizer=AutoTokenizer.from_pretrained(modelpath,local_files_only=True)
    model=EsmForMaskedLM.from_pretrained(modelpath,local_files_only=True).to(args.device).eval()
    items=load_lines(args.out/'sequences.jsonl'); cachefile=args.out/'esm_scores.jsonl'
    previous=load_lines(cachefile); done={x['sha256'] for x in previous}
    sequences=list(dict.fromkeys(x['sequence'] for x in items if set(x['sequence'])<=set(AA)))
    pending=[s for s in sequences if digest(s) not in done]
    if args.limit: pending=pending[:args.limit]
    meta={'model_path':str(modelpath),'model_config_sha256':digest((modelpath/'config.json').read_text()),
          'torch':torch.__version__,'transformers':transformers.__version__,'device':args.device,
          'dtype':str(next(model.parameters()).dtype),'allow_tf32':torch.backends.cuda.matmul.allow_tf32,
          'method':'exact one residue masked per sequence copy; all 33 vocabulary logits normalized',
          'batch_size':args.batch_size, 'script_sha256':digest(Path(__file__).read_text()),
          'cuda_device':torch.cuda.get_device_name() if args.device.startswith('cuda') else None}
    if (args.out/'esm_runtime.json').exists():
        prior=json.loads((args.out/'esm_runtime.json').read_text())
        for key in ['model_path','model_config_sha256','dtype','method','allow_tf32']:
            if prior[key]!=meta[key]: raise ValueError('Cache model/runtime mismatch '+key)
    dump(args.out/'esm_runtime.json',meta)
    start=time.monotonic();ntokens=0
    with cachefile.open('a') as f,torch.inference_mode():
        # Cross-sequence batches reduce partial minibatches, without approximating PLL.
        for offset in range(0,len(pending),8):
            group=pending[offset:offset+8]
            encoded=tokenizer(group,add_special_tokens=True,padding=True,return_tensors='pt')
            ids=encoded['input_ids'].to(args.device);attention=encoded['attention_mask'].to(args.device)
            tasks=[(j,p) for j,s in enumerate(group) for p in range(1,len(s)+1)]
            values=[[] for s in group]
            for a in range(0,len(tasks),args.batch_size):
                t=tasks[a:a+args.batch_size]
                seqids=torch.tensor([j for j,p in t],device=args.device)
                cols=torch.tensor([p for j,p in t],device=args.device)
                batch=ids[seqids].clone();mask=attention[seqids];rows=torch.arange(len(t),device=args.device)
                truth=batch[rows,cols].clone();batch[rows,cols]=tokenizer.mask_token_id
                logits=model(input_ids=batch,attention_mask=mask).logits[rows,cols]
                lp=logits.float().log_softmax(-1).gather(1,truth[:,None]).squeeze(1).cpu().tolist()
                for (j,p),v in zip(t,lp): values[j].append(v)
                ntokens+=len(t)
            for s,lp in zip(group,values):
                assert len(s)==len(lp) and all(math.isfinite(v) and v<=0 for v in lp)
                record={'sha256':digest(s),'sequence':s,'log_probs':lp,'mean_nll':-sum(lp)/len(lp),
                        'max_site_nll':-min(lp),'pseudo_perplexity':math.exp(-sum(lp)/len(lp))}
                f.write(json.dumps(record,allow_nan=False)+'\n')
            f.flush()
            print(f'esm {min(offset+8,len(pending))}/{len(pending)} masks={ntokens} elapsed={time.monotonic()-start:.1f}s masks/s={ntokens/(time.monotonic()-start):.1f}',flush=True)
    dump(args.out/'esm_last_run.json',{'new_sequences':len(pending),'masked_positions':ntokens,'seconds':time.monotonic()-start})

def wilson(k,n):
    if not n:return [None,None]
    z=1.96;p=k/n;den=1+z*z/n;c=(p+z*z/(2*n))/den;d=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [max(0,c-d),min(1,c+d)]

def summarize(args):
    import numpy as np
    items=load_lines(args.out/'sequences.jsonl')
    ar={x['id']:x['review'] for x in load_lines(args.out/'anarci_scores.jsonl')}
    es={x['sha256']:x for x in load_lines(args.out/'esm_scores.jsonl')}
    es.update({x['sha256']:x for x in load_lines(args.out/'esm_fp32_boundary_overrides.jsonl')})
    if any(x['id'] not in ar or (set(x['sequence'])<=set(AA) and digest(x['sequence']) not in es) for x in items):
        raise RuntimeError('Scoring incomplete; resume score commands first')
    clean={x['id']:x for x in items if x['kind']=='clean'}
    # Calibration is on clean chains only and split before mutation. No test tuning.
    thresholds={}
    for chain in ['VH','VL']:
        cal=[x for x in clean.values() if x['split']=='calibration' and x['chain']==chain]
        thresholds[chain]={'n':len(cal)}
        for metric in ['max_site_nll','mean_nll']:
            thresholds[chain][metric]=float(np.quantile([es[digest(x['sequence'])][metric] for x in cal],.95,method='higher'))
    dump(args.out/'calibrated_thresholds.json',thresholds)
    rows=[]
    for x in items:
        s=es.get(digest(x['sequence'])); rev=ar[x['id']]; parent=clean[x['parent_id']]
        ps=es.get(digest(parent['sequence'])); th=thresholds[x['chain']]
        invalid=set(x['sequence'])-set(AA)
        af=rev['status']!='matched'
        siteflag=bool(s and s['max_site_nll']>=math.log(1000)); flags=[i+1 for i,v in enumerate(s['log_probs']) if v<=-math.log(1000)] if s else []
        hit=bool(set(flags)&set(x['affected_positions'])) if x['affected_positions'] and not invalid else None
        nll=s['mean_nll'] if s else None; maxnll=s['max_site_nll'] if s else None
        rows.append({'id':x['id'],'parent_id':x['parent_id'],'pair_id':x['pair_id'],'split':x['split'],
                     'chain':x['chain'],'species':x['species'],'kind':x['kind'],'region':x['region'],
                     'length':len(x['sequence']),'anarci_status':rev['status'],'anarci_flag':af,
                     'esm_scored':s is not None,'esm_site1000_flag':siteflag,'combined_flag':af or siteflag,
                     'mean_nll':nll,'pseudo_perplexity':s['pseudo_perplexity'] if s else None,'max_site_nll':maxnll,
                     'esm_site_cal95_flag':bool(s and maxnll>th['max_site_nll']),
                     'esm_mean_cal95_flag':bool(s and nll>th['mean_nll']),
                     'new_site1000_flag':bool(siteflag and ps['max_site_nll']<math.log(1000)),
                     'parent_site1000_flag':bool(ps['max_site_nll']>=math.log(1000)),
                     'parent_anarci_flag':ar[parent['id']]['status']!='matched',
                     'localized_hit':hit,'flagged_positions':json.dumps(flags),
                     'affected_positions':json.dumps(x['affected_positions']),
                     'delta_mean_nll':nll-ps['mean_nll'] if s else None,
                     'source_row':x['source_row']})
    csv_out(args.out/'per_sequence.csv',rows)
    summary=[]
    for fields in [('kind',),('kind','region'),('kind','chain'),('kind','chain','region')]:
        groups=defaultdict(list)
        for row in rows:
            if row['split']=='test':groups[tuple(row[k] for k in fields)].append(row)
        for key,g in groups.items():
            out={'grouping':'+'.join(fields),'kind':'ALL','chain':'ALL','region':'ALL',**dict(zip(fields,key)), 'n':len(g)}
            for metric in ['anarci_flag','esm_site1000_flag','combined_flag','esm_site_cal95_flag','esm_mean_cal95_flag']:
                n=sum(r['esm_scored'] for r in g) if metric.startswith('esm_') else len(g)
                k=sum(r[metric] for r in g)
                lo,hi=wilson(k,n)
                out.update({metric+'_count':k,metric+'_denominator':n,metric+'_rate':k/n if n else None,
                            metric+'_ci_low':lo,metric+'_ci_high':hi})
            localized=[r for r in g if r['localized_hit'] is not None]
            out['localized_n']=len(localized);out['localized_rate']=sum(r['localized_hit'] for r in localized)/len(localized) if localized else None
            eligible=[r for r in g if not r['parent_site1000_flag'] and r['esm_scored']]
            out['parent_passed_n']=len(eligible);out['new_site1000_rate']=sum(r['new_site1000_flag'] for r in eligible)/len(eligible) if eligible else None
            out['median_delta_mean_nll']=float(np.median([r['delta_mean_nll'] for r in g if r['delta_mean_nll'] is not None])) if any(r['delta_mean_nll'] is not None for r in g) else None
            summary.append(out)
    csv_out(args.out/'detection_summary.csv',summary)
    dump(args.out/'summary.json',{'rows':len(rows),'test_clean_chains':sum(x['kind']=='clean' and x['split']=='test' for x in rows),
         'definitions':{'esm_rate':'Only scored canonical-AA sequences in denominator; invalid O detected by ANARCI/alphabet check.',
                        'combined':'Current ANARCI != matched OR any ESM2 site PPL >= 1000.',
                        'localized_hit':'Flag overlaps an edited residue; deletion uses two residues adjoining the junction.',
                        'cal95':'Exploratory 95th clean-calibration quantile with strict >; not a validated production cutoff.',
                        'confidence_intervals':'Wilson binomial intervals are descriptive; mutants share parents and are not independent.',
                        'synthetic_errors':'Uniform random/chemical group/OCR-like errors are not a measured OCR error distribution.'},
         'overall':[x for x in summary if x['grouping']=='kind']})
    print(json.dumps([x for x in summary if x['grouping']=='kind'],ensure_ascii=False,indent=2))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage',choices=['prepare','anarci','esm','summarize'])
    p.add_argument('--out',type=Path,default=ROOT/'results'/'pilot_20261027')
    p.add_argument('--source',type=Path,default=ROOT.parent.parent/'AirGen-Dev/data/OAS/op_base/cleaned_merged_data_step_clustered_train_oas_label.csv')
    p.add_argument('--seed',type=int,default=20261027)
    p.add_argument('--pairs',type=int,default=48)
    p.add_argument('--calibration-pairs',type=int,default=32)
    p.add_argument('--mutation-pairs',type=int,default=16)
    p.add_argument('--cpus',type=int,default=4)
    p.add_argument('--model',type=Path);p.add_argument('--device',default='cuda:0')
    p.add_argument('--batch-size',type=int,default=64);p.add_argument('--limit',type=int)
    p.add_argument('--tf32',action='store_true',help='Allow Ampere TF32 matmul; record and validate separately')
    args=p.parse_args();args.out=args.out.resolve()
    if not args.out.is_relative_to(ROOT):raise ValueError('All outputs must stay inside PPL')
    args.out.mkdir(parents=True,exist_ok=True)
    if args.stage=='prepare' and (args.out/'sequences.jsonl').exists(): raise FileExistsError('Refusing to replace prepared experiment')
    {'prepare':prepare,'anarci':anarci_score,'esm':esm_score,'summarize':summarize}[args.stage](args)

if __name__=='__main__':main()
