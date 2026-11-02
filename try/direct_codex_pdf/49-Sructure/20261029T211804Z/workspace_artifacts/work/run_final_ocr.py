import subprocess
ranges=[(1,33),(34,66),(67,99),(100,132),(133,165),(166,198),(199,231),(232,263)]
ps=[]
for a,b in ranges:
 log=open(f'work/final_worker_{a}.log','w')
 ps.append(subprocess.Popen(['python','work/ocr_fast_local.py','--start',str(a),'--end',str(b)],stdout=log,stderr=subprocess.STDOUT))
for p in ps:p.wait()
print('OCR workers completed')
