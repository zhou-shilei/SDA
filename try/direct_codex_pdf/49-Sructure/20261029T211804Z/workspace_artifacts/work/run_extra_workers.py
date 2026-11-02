import subprocess
ranges=[(15,30),(45,60),(75,90),(105,120),(135,150),(165,182),(225,236),(249,263)]
ps=[]
for a,b in ranges:
 log=open(f'work/extra_worker_{a}.log','w')
 ps.append(subprocess.Popen(['python','work/ocr_worker_one.py','--start',str(a),'--end',str(b)],stdout=log,stderr=subprocess.STDOUT))
for p in ps:p.wait()
print('OCR workers completed')
