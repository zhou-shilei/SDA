import subprocess
ranges=[(1,30),(31,60),(61,90),(91,120),(121,150),(151,182),(210,236),(237,263)]
ps=[]
for a,b in ranges:
 log=open(f'work/worker_{a}.log','w')
 ps.append(subprocess.Popen(['python','work/ocr_worker.py','--start',str(a),'--end',str(b)],stdout=log,stderr=subprocess.STDOUT))
for p in ps:p.wait()
print('OCR workers completed')
