import os
os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True'
os.environ['OMP_NUM_THREADS']='2'
os.environ['FLAGS_enable_pir_api']='0'
import pymupdf, json, time, argparse
import numpy as np
from paddleocr import PaddleOCR
ap=argparse.ArgumentParser(); ap.add_argument('--start',type=int,default=1);ap.add_argument('--end',type=int,default=263); args=ap.parse_args()
ocr=PaddleOCR(text_detection_model_name='PP-OCRv6_medium_det',text_detection_model_dir='/root/.paddlex/official_models/PP-OCRv6_medium_det',text_recognition_model_name='PP-OCRv6_small_rec',text_recognition_model_dir='/root/.paddlex/official_models/PP-OCRv6_small_rec',use_doc_orientation_classify=False,use_doc_unwarping=False,use_textline_orientation=False,device='cpu',cpu_threads=2,enable_mkldnn=False,text_recognition_batch_size=16)
doc=pymupdf.open('input.pdf')
order=list(range(args.start,args.end+1))
if args.end>3: order=sorted(order,key=lambda n: (0 if 183<=n<=209 else 1,n))
for n in order:
 out=f'work/ocr_{n:03}.json'
 if os.path.exists(out):continue
 t=time.time()
 try:
  pix=doc[n-1].get_pixmap(matrix=pymupdf.Matrix(2,2),alpha=False)
  ar=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,3)
  if 228<=n<=254: ar=np.rot90(ar,k=3).copy()
  r=ocr.predict(ar)[0].json
  if callable(r):r=r()
  if isinstance(r,str):r=json.loads(r)
  with open(out,'w') as f:json.dump(r,f,ensure_ascii=False)
  rr=r.get('res',r)
  texts=rr.get('rec_texts',[])
  with open(f'work/text_{n:03}.txt','w') as f:f.write('\n'.join(texts))
  print(f'page {n}: {len(texts)} lines, {time.time()-t:.1f}s',flush=True)
 except Exception as e:
  print(f'FAIL {n}: {type(e).__name__}: {e}',flush=True)
