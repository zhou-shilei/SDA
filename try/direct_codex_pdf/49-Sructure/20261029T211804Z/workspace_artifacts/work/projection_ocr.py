import os
os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True';os.environ['OMP_NUM_THREADS']='2'
import pymupdf,numpy as np,cv2,json,time,argparse
from paddleocr import TextRecognition
ap=argparse.ArgumentParser();ap.add_argument('--start',type=int,default=2);ap.add_argument('--end',type=int,default=182);args=ap.parse_args()
r=TextRecognition(model_name='PP-OCRv6_small_rec',model_dir='/root/.paddlex/official_models/PP-OCRv6_small_rec',device='cpu',cpu_threads=2,enable_mkldnn=False)
p=pymupdf.open('input.pdf')
for n in range(args.start,args.end+1):
 if os.path.exists(f'work/projection_{n:03}.json'):continue
 t=time.time();pix=p[n-1].get_pixmap(matrix=pymupdf.Matrix(2,2),alpha=False)
 a=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,3).copy()
 if 228<=n<=254:a=np.rot90(a,3).copy()
 g=cv2.cvtColor(a,cv2.COLOR_RGB2GRAY);mask=(g<170).astype('uint8')
 # Remove page-edge artifacts and grid rules from row detection.
 mask[:,:60]=0;mask[:,-30:]=0
 h=cv2.morphologyEx(mask,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(140,1)))
 v=cv2.morphologyEx(mask,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(1,80)))
 m=mask-h-v;m=np.maximum(m.astype('int16'),0)
 rows=np.flatnonzero(m.sum(axis=1)>3)
 groups=[]
 for y in rows:
  if not groups or y-groups[-1][-1]>4:groups.append([int(y)])
  else:groups[-1].append(int(y))
 boxes=[];ims=[]
 for ys in groups:
  if len(ys)<3:continue
  y0=max(0,ys[0]-3);y1=min(a.shape[0],ys[-1]+4)
  xs=np.flatnonzero(m[y0:y1].sum(axis=0)>0)
  if len(xs)==0:continue
  x0=max(0,int(xs[0])-3);x1=min(a.shape[1],int(xs[-1])+4)
  if x1-x0<5:continue
  boxes.append([x0,y0,x1,y1]);ims.append(a[y0:y1,x0:x1])
 res=r.predict(ims,batch_size=16)
 texts=[];scores=[]
 for it in res:
  obj=it.json;obj=obj.get('res',obj);texts.append(obj.get('rec_text',''));scores.append(obj.get('rec_score',0))
 out={'res':{'rec_texts':texts,'rec_scores':scores,'rec_boxes':boxes,'method':'projection segmentation + local PP-OCRv6_small_rec','rotation_clockwise':90 if 228<=n<=254 else 0}}
 json.dump(out,open(f'work/projection_{n:03}.json','w'),ensure_ascii=False)
 open(f'work/projection_{n:03}.txt','w').write('\n'.join(texts))
 print(n,len(texts),round(time.time()-t,2),flush=True)
