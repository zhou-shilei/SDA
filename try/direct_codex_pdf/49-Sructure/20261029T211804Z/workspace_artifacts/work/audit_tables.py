import json,pymupdf,cv2,numpy as np,re,difflib
D=json.load(open('dataset.json'));seqs={s['seq_id_no']:s['sequence'] for s in D['sequences']}
p=pymupdf.open('input.pdf')
for n in range(196,205):
 path=f'work/ocr_{n:03}.json'
 if not __import__('os').path.exists(path):continue
 r=json.load(open(path))['res']
 pix=p[n-1].get_pixmap(matrix=pymupdf.Matrix(2,2));a=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3)
 g=cv2.cvtColor(a,cv2.COLOR_RGB2GRAY);b=(g<130).astype('uint8')
 hh=cv2.morphologyEx(b,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(500,1)))
 ys=np.flatnonzero(hh.sum(1)>600);groups=[]
 for y in ys:
  if not groups or y-groups[-1][-1]>4:groups.append([int(y)])
  else:groups[-1].append(int(y))
 lines=[sum(x)/len(x) for x in groups]
 for lo,hi in zip(lines,lines[1:]):
  txt=[];ids=[]
  for text,box in zip(r['rec_texts'],r['rec_boxes']):
   cy=(box[1]+box[3])/2;cx=(box[0]+box[2])/2
   if lo<cy<hi:
    if box[0]>355:txt.append(text)
    else:ids.append(text)
  if not txt:continue
  raw=''.join(txt); s=re.sub('[^A-Z]','',raw)
  nums=re.findall(r'NO\s*[:;]?\s*(\d+)', ' '.join(ids))
  sid=int(nums[0]) if nums else None
  if sid in seqs:
   expected=seqs[sid];ratio=difflib.SequenceMatcher(None,expected,s,autojunk=False).ratio()
   ops=[(expected[i:j],s[k:l]) for op,i,j,k,l in difflib.SequenceMatcher(None,expected,s,autojunk=False).get_opcodes() if op!='equal']
   print(n,sid,'len',len(expected),len(s),'ratio',round(ratio,3),'diff',ops)
  else:print(n,'unmatched',ids,raw[:65])
