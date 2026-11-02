import json, collections, shutil
from pathlib import Path
root=Path(__file__).resolve().parent
manifest=json.loads((root/'manifest.json').read_text());work=Path(manifest['working_directory'])
result={'note':'Structure and claimed coverage checks only; record counts do not establish correctness or equivalence to the SDA pipeline.','files':{},'checks':{}}
for name in ['dataset.json','extraction_report.json']:
 p=work/name
 if not p.is_file():result['files'][name]={'exists':False};continue
 shutil.copyfile(p,root/name)
 try:d=json.loads(p.read_text())
 except (ValueError,UnicodeError) as e:result['files'][name]={'exists':True,'valid_json':False,'error':str(e)};continue
 result['files'][name]={'exists':True,'valid_json':True,'bytes':p.stat().st_size,'top_level_keys':list(d) if isinstance(d,dict) else None}
 if name=='extraction_report.json':result['agent_report']=d;continue
 if not isinstance(d,dict):continue
 result['dataset_counts']={k:len(v) for k,v in d.items() if isinstance(v,list)}
 duplicates={}
 for k,v in d.items():
  if not isinstance(v,list):continue
  ids=[str(row['id']) for row in v if isinstance(row,dict) and row.get('id') is not None]
  duplicate_ids=[key for key,n in collections.Counter(ids).items() if n>1]
  if duplicate_ids:duplicates[k]=duplicate_ids
 result['checks']['duplicate_ids_by_array']=duplicates
 entities={str(x['id']) for x in d.get('entities',[]) if isinstance(x,dict) and 'id' in x}
 experiments={str(x['id']) for x in d.get('experiments',[]) if isinstance(x,dict) and 'id' in x}
 bad_refs=[]
 for key in ['measurements','sequence_links']:
  for i,row in enumerate(d.get(key,[])):
   if not isinstance(row,dict):continue
   for field,allowed in [('entity_id',entities),('experiment_id',experiments)]:
    if row.get(field) is not None and str(row[field]) not in allowed:bad_refs.append({'array':key,'index':i,'field':field,'value':row[field]})
 result['checks']['unmatched_entity_or_experiment_refs']=bad_refs
 pages=[]
 def visit(x):
  if isinstance(x,dict):
   for k,v in x.items():
    if k in ['page','pdf_page','page_number','physical_page'] and type(v) is int:pages.append(v)
    else:visit(v)
  elif isinstance(x,list):
   for v in x:visit(v)
 visit(d)
 result['checks']['evidence_pages_in_dataset']=sorted(set(pages));result['checks']['invalid_evidence_pages']=sorted({p for p in pages if not 1<=p<=263})
 result['checks']['sequence_region_counts']=dict(collections.Counter(str(x.get('region') or x.get('region_type') or x.get('type') or 'unspecified') for x in d.get('sequences',[]) if isinstance(x,dict)))
ocr=list((work/'work').glob('ocr_[0-9]*.json'));result['local_ocr_file_pages']=sorted(int(p.stem.split('_')[-1]) for p in ocr)
result['local_ocr_page_count']=len(ocr)
(root/'validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
