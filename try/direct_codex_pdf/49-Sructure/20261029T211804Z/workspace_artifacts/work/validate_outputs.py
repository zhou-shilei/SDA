import json,collections,re,os
D=json.load(open('dataset.json'));R=json.load(open('extraction_report.json'))
errors=[]
for a in ['entities','sequences','sequence_links','experiments','measurements','relations','issues']:
 if not isinstance(D.get(a),list):errors.append(f'{a} is not an array')
ids={k:{o['id'] for o in D[k]} for k in ['entities','sequences','experiments','measurements','issues']}
for k,v in ids.items():
 if len(v)!=len(D[k]):errors.append('duplicate ids '+k)
for m in D['measurements']:
 for key,kind in [('entity_id','entities'),('experiment_id','experiments')]:
  if m[key] not in ids[kind]:errors.append(f'{m["id"]}: unresolved {key} {m[key]}')
for l in D['sequence_links']:
 for key,kind in [('entity_id','entities'),('sequence_id','sequences')]:
  if l[key] not in ids[kind]:errors.append(f'unresolved {key} {l[key]}')
allids=set().union(*ids.values())
for r in D['relations']:
 for key in ['subject_id','object_id']:
  if r[key] not in allids:errors.append(f'relation unresolved {key} {r[key]}')
 if r.get('experiment_id') and r['experiment_id'] not in ids['experiments']:errors.append('relation bad experiment')
for f in D.get('figures',[]):
 for key,kind in [('entity_ids','entities'),('experiment_ids','experiments')]:
  for x in f[key]:
   if x not in ids[kind]:errors.append(f'figure bad {key}: {x}')
missing=sorted(set(range(1,174))-{s['seq_id_no'] for s in D['sequences']})
if missing:errors.append('missing sequence ids '+repr(missing))
for s in D['sequences']:
 if not s['sequence']:errors.append('empty sequence '+s['id'])
 if s.get('sequence_kind')!='consensus_template' and re.search('[^ACDEFGHIKLMNPQRSTVWY]',s['sequence']):errors.append('non-amino-acid character '+s['id'])
expected={'Table 6':30,'Table 7':30,'Table 8':30,'Table 9':72,'Table 10':15,'Table 11':50,'Table 12':30,'Table 13':10,'Table 14':13}
actual=collections.Counter(m['evidence'][0].get('table') for m in D['measurements'])
for t,n in expected.items():
 if actual[t]!=n:errors.append(f'{t}: {actual[t]} != {n}')
# Every record must have a page-addressable short quotation.
for k in ['entities','sequences','sequence_links','experiments','measurements','relations','issues']:
 for i,o in enumerate(D[k]):
  for e in o.get('evidence',[]):
   if not isinstance(e.get('page'),int) or not 1<=e['page']<=263:errors.append(f'{k}[{i}] invalid page')
   if not e.get('quote'):errors.append(f'{k}[{i}] empty quote')
  if not o.get('evidence'):errors.append(f'{k}[{i}] no evidence')
print(json.dumps({'errors':errors,'counts':{k:len(v) for k,v in D.items() if isinstance(v,list)},'sequence_id_coverage':'1–173' if not missing else missing,'table_measurement_counts':{t:actual[t] for t in expected},'ocr_pages':len([n for n in range(1,264) if os.path.exists(f'work/ocr_{n:03}.json')])},ensure_ascii=False,indent=2))
if errors:raise SystemExit(1)
