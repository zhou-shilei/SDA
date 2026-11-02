import json,os,re,collections,pymupdf
from pathlib import Path
D=json.load(open('dataset.json'));doc=pymupdf.open('input.pdf')
processed=[];failures=[];low_conf_pages=[];chars=0
for n in range(1,len(doc)+1):
 try:
  r=json.load(open(f'work/ocr_{n:03}.json'))['res']
  if not r['rec_texts']:raise ValueError('no recognized text')
  assert len(r['rec_texts'])==len(r['rec_boxes'])==len(r['rec_scores'])
  processed.append(n);chars+=len(Path(f'work/text_{n:03}.txt').read_text())
  if any(x<0.8 for x in r['rec_scores']):low_conf_pages.append(n)
 except Exception as e:failures.append({'page':n,'reason':str(e)})
assert len(processed)==263,failures
ss={s['seq_id_no']:s['sequence'] for s in D['sequences']};assert set(ss)==set(range(1,174))
for a,b,c in [(137,138,139),(140,141,142),(143,141,144),(145,146,147),(148,141,149),(150,151,152),(153,154,155),(156,157,158),(159,160,161),(162,163,164),(165,166,167),(168,169,170),(171,172,173)]:assert ss[a]+ss[b]==ss[c]
vh=[25,27,29,31,50,51,53,55,56,57,96,98,100,101,102,103,104,105,106,107,108,110];vl=[25,26,27,28,29,30,48,50,87,88,89,90,91,92,93,94,95,96,97];vlmap=[20,21,20,22,23,24,25,20,20,26,27,28,29,30]
for e,a,b in [(2,137,138),(1,140,141),(8,143,141),(6,145,146),(3,148,141),(5,150,151),(7,153,154),(10,156,157),(13,159,160),(14,162,163),(12,165,166),(11,168,169),(4,171,172)]:
 assert ''.join(ss[e+3][i] for i in vh)==ss[a]
 assert ''.join(ss[vlmap[e-1]][i] for i in vl)==ss[b]
counts={k:len(v) for k,v in D.items() if isinstance(v,list)}
result_types={'experimental_numeric':0,'computational_numeric':0,'not_applicable_as_printed':0,'qualitative':0}
for m in D['measurements']:
 if m.get('status')=='not_applicable':result_types['not_applicable_as_printed']+=1
 elif m.get('result_kind')=='computational':result_types['computational_numeric']+=1
 elif m.get('result_kind','').startswith('qualitative'):result_types['qualitative']+=1
 else:result_types['experimental_numeric']+=1
R={
 'document':D['document'],
 'source_boundary':'The only literature source was the current-directory input.pdf. No prior OCR, seed/dataset, project data, experiment artifacts, external literature, external API/LLM or other agent was used. All OCR intermediates were generated in work/ during this extraction.',
 'status':'completed_with_limitations',
 'total_pages':len(doc),'processed_pages':processed,'unread_or_failed_pages':[],'page_failures':failures,
 'document_sections':[{'pages':[1],'section':'publication cover'},{'pages_from':2,'pages_to':215,'section':'description, embodiments, examples, sequence/result tables, Appendix C program listing'},{'pages_from':216,'pages_to':227,'section':'claims'},{'pages_from':228,'pages_to':254,'section':'27 drawing sheets (28 labeled figure panels)'},{'pages_from':255,'pages_to':263,'section':'international search report sheets'}],
 'extraction_methods':[
  'PyMuPDF inspected all 263 pages: no text layer and no embedded attachments.',
  'Rendered full pages locally at 2× PDF resolution (144 dpi); PaddleOCR 3.7.0 / PaddlePaddle 3.3.1 with pre-existing local PP-OCRv6_medium_det and PP-OCRv6_small_rec weights. CPU inference with MKLDNN disabled after an initial backend incompatibility. Later pages used detection max-side 960 while recognition used original-resolution crops. All failed initial attempts were reprocessed.',
  'Drawing pages were rotated 90° clockwise for OCR and full-size visual reading. Source sequence and result tables were all read as page images, with enlarged critical crops; OCR text/coordinates/confidences were retained in work/.',
  'Transcribed all printed sequence definitions and every nonblank result-table cell, including N/A and confidence intervals. Checked full-chain recurring literal text against OCR and images; kept VH/VL definitions from their own tables rather than inferring cleavage boundaries from complete chains.',
  'Reviewed full-document OCR for antibody names, sequence references, experiment and result statements; included the aggregate SEAP/other validation statement and the separately stated reporter stimulation EC50. General claim thresholds and reagent concentrations were kept outside empirical measurement records.'
 ],
 'ocr_text_characters':chars,
 'counts':counts,'measurement_record_types':result_types,
 'sequence_coverage':{'numbered_sequence_ids':list(range(1,174)),'missing_numbered_sequence_ids':[],'notes':'All 173 numbered definitions present in the PDF retained separately, including identical printed SEQ ID NO:87/88 and distinct IMGT/Kabat/Chothia region definitions. No external XML listing was used.'},
 'table_coverage':D['tables'],
 'validation':{'json_parse':'passed','entity_experiment_sequence_references':'passed','duplicate_ids':'none','evidence_page_range':'all within 1–263','result_table_cells':'Tables 6–14: 30, 30, 30, 72, 15, 50, 30, 10, 13 records respectively; blanks in Table 9 are explicitly described in issues','numbered_sequence_coverage':'1–173, no gaps','table14_sequence_checks':'All 13 vht/vlt pairs exactly match their respective combined vhvlt strings; all also match extraction from the separately printed VH/VL sequences at the positions explicitly listed in Appendix C. No model weights or affinity values were inferred.'},
 'completeness_limits':[
  'Raw graphical points/error bars in Figures 4–11 lack individually printed numbers. Exact tabulated fit results and textual qualitative results are included; raw A450, luciferase AU, TARC pg/mL and inhibition point coordinates were not digitized into exact values.',
  'The referenced external XML listing, Appendix A/B files and numerical Potts weights are not embedded in the supplied PDF. They were not fetched; model scores were taken from the printed table rather than recomputed.',
  'Except AB-2a, a-suffixed light-chain assignments rely on the shared unsuffixed family names and heavy-constant-region suffix definition; those links are marked provisional in sequence_links and issues. Tables 6–11 do not resolve a/b suffixes and remain linked only to unsuffixed entities.',
  'An extra Figure 10D legend label resembling PRO-171101-004 is uncertain; no alias or sequence identity is inferred for it.',
  'Original numerical, receptor-species, assay-title, table/figure-cross-reference and replicate-count inconsistencies are retained and detailed in the 16 issues. Blank cells and N/A are not treated as zero.',
  'All pages underwent full-page OCR, with scientific sequence/result tables visually verified. General legal prose and the program listing were not proofread word by word; the local OCR intermediates are not a guaranteed character-perfect edition of the patent.'
 ]}
json.dump(R,open('extraction_report.json','w'),ensure_ascii=False,indent=2)
print(json.dumps({'status':R['status'],'pages_processed':len(processed),'unread_or_failed':[],'counts':counts,'measurement_types':result_types,'file_sizes':{x:os.path.getsize(x) for x in ['dataset.json','extraction_report.json']}},ensure_ascii=False,indent=2))
