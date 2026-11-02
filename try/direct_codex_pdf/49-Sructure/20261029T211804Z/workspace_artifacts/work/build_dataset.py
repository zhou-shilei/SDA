import json,re,os,collections
D={'document':{'publication_number':'WO 2024/020591 A9','application_number':'PCT/US2023/070790','title':'ANTIGEN BINDING MOLECULES TARGETING THYMIC STROMAL LYMPHOPOIETIN (TSLP)','source':'input.pdf','evidence':[{'page':1,'quote':'WO 2024/020591 A9; PCT/US2023/070790'}]},'entities':[],'sequences':[],'sequence_links':[],'experiments':[],'measurements':[],'relations':[],'issues':[],'tables':[]}
def ev(p,q,**kw):return dict(page=p,quote=q,**kw)
def entity(n,role,p,q,aliases=None,**kw):
 D['entities'].append(dict(id=n,name=n,aliases=aliases or [],role=role,evidence=[ev(p,q)],**kw));return n
def seq(n,typ,s,p,q=None,**kw):
 D['sequences'].append(dict(id=f'SEQ-{n}' if isinstance(n,int) else n,seq_id_no=n if isinstance(n,int) else None,region_type=typ,sequence=s,evidence=[ev(p,q or s)],**kw))
def link(e,s,reg,p,q,**kw):D['sequence_links'].append(dict(entity_id=e,sequence_id=f'SEQ-{s}' if isinstance(s,int) else s,region_type=reg,evidence=[ev(p,q)],**kw))
def rel(a,pred,b,p,q,**kw):D['relations'].append(dict(subject_id=a,predicate=pred,object_id=b,evidence=[ev(p,q)],**kw))
def issue(typ,desc,p=None,q=None,**kw):D['issues'].append(dict(id='ISSUE-'+str(len(D['issues'])+1),type=typ,description=desc,evidence=[ev(p,q)] if p else [],**kw))
def exp(i,name,method,target,species,system,conditions,p,q,tables=None,**kw):
 D['experiments'].append(dict(id=i,name=name,method=method,target=target,species=species,system=system,conditions=conditions,result_tables=tables or [],evidence=[ev(p,q)],**kw))
def meas(e,x,m,v,u,c,p,q,table=None,**kw):
 D['measurements'].append(dict(id='M-'+str(len(D['measurements'])+1),entity_id=e,experiment_id=x,metric=m,value=v,unit=u,condition=c,evidence=[ev(p,q,**({'table':table,'row':e,'column':m} if table else {}))],**kw))
for n in range(1,15):
 entity(f'AB-{n}','anti-TSLP antibody candidate',196,f'AB-{n} NO:{n+3}',molecular_format='VH/VL-defined antibody; table results do not specify a/b suffix')
 for suffix in ['a','b']:
  entity(f'AB-{n}{suffix}','anti-TSLP full-length antibody construct',199 if n<6 and suffix=='a' else (200 if n<12 and suffix=='a' else (201 if suffix=='a' else (201 if n<4 else (202 if n<10 else 203)))),f'AB-{n}{suffix} NO:{90+n if suffix=="a" else 104+n}')
  rel(f'AB-{n}{suffix}','same_variable_region_design_as',f'AB-{n}',196,f'AB-{n} VH sequence in Table 1; AB-{n}{suffix} heavy chain in Table 4')
entity('Reference Antibody','reference anti-TSLP antibody',196,'Reference NO:3',aliases=['Reference'])
entity('Consensus','consensus sequence design',196,'Consensus NO:2')
entity('Human IgG1 isotype','isotype control',208,'Human Isotype IgG1')
entity('Human IgG2 isotype','isotype control',208,'Human Isotype IgG2')
entity('human TSLP','target cytokine',228,'(SEQ ID NO:1)',species='Homo sapiens')
entity('cynomolgus TSLP','target cytokine',187,'Cynomolgus Monkey TSLP Binding ELISA',species='Macaca fascicularis')
entity('mouse TSLP','specificity control antigen',191,'human TSLP, cyno TSLP, mouse TSLP, and human IL-7',species='Mus musculus')
entity('human IL-7','specificity control antigen',191,'human TSLP, cyno TSLP, mouse TSLP, and human IL-7',species='Homo sapiens')
entity('hIL-7Rα-P2A-hTSLPR','expression construct',190,'A hIL-7Rα-P2A-hTSLPR construct was synthesized and cloned into a pcDNA3.1(+)/Hygro plasmid')
entity('cynoIL-7Rα-P2A-cynoTSLPR','expression construct',194,'A cynoIL-7Rα-P2A-cynoTSLPR construct was synthesized and cloned into a pcDNA3.1(+)/Hygro plasmid')
entity('Ba/F3-STAT5-Luc-hTSLPR/hIL-7Rα','reporter cell line',190,'Generation of the Ba/F3-STAT5-Luciferase-hTSLPR/hIL-7R Cell Line')
entity('Ba/F3-STAT5-Luc-cynoTSLPR/cynoIL-7Rα','reporter cell line',195,'Ba/F3-STAT5-cynoTSLPR/cynoIL-7R-Luc cells')
rel('Ba/F3-STAT5-Luc-hTSLPR/hIL-7Rα','expresses','hIL-7Rα-P2A-hTSLPR',190,'create a stable cell line expressing hTSLPR and hIL-7Rα')
rel('Ba/F3-STAT5-Luc-cynoTSLPR/cynoIL-7Rα','expresses','cynoIL-7Rα-P2A-cynoTSLPR',194,'create a stable cell line expressing cynomolgus monkey TSLPR and cynoIL-7Rα')
exp('EX1','Example 1: Production of Full-Length IgG Molecules','Transient mammalian expression and Protein A purification','TSLP',None,'mammalian host (such as CHO)','VH/VL expression plasmids; CMV promoter and secretion signal; fed-batch; Protein A; elution ~pH 3.5, neutralize pH 7.5 with 2 M Tris',184,'to be expressed as full-length human immunoglobulin 1 (IgG1) antibodies')
exp('EX2','Example 2: Polyspecific Reactivity','low-stringency ELISA; DELFIA time-resolved fluorescence','salmon sperm DNA',None,'384-well DNA-coated plate','10 µg/mL DNA overnight 4°C; block 5% BSA 1h 37°C; IgG 100 nM in duplicate 1h 37°C; Eu-N1 anti-human IgG 100 ng/mL; enhancement 15min; raw signal divided by buffer-only signal',185,'Polyspecific binding to DNA was quantified by dividing the raw signal by signal from buffer-only wells',['Table 6'])
exp('EX3','Example 3: Self-Association','AC-SINS; nanoparticle absorbance shift','self-interaction',None,'gold nanoparticles coated with goat anti-human Fc capture antibodies','capture:noncapture 4:1 v/v, total 0.4 mg/mL; gold:coating 9:1; 0.1 µM thiolated PEG; 0.22 µm filter; antibodies incubated 2h RT; absorption 510–570 nm in 2 nm increments',186,'increased maximum absorbance wavelength ... compared to spectra of nanoparticles alone (ΔλMAX)',['Table 6'])
for sp,label in [('human','h'),('cynomolgus monkey','cyno')]:
 exp('EX4-'+label,'Example 4: '+sp+' TSLP binding','indirect ELISA','TSLP',sp,'recombinant TSLP-coated 96-well half-area plate','antigen 2 µg/mL overnight; block 1h; serial antibody concentrations 1h 37°C; anti-human IgG Fc-HRP 1:2000 30min 37°C; TMB; A450; four-parameter nonlinear regression of two technical replicates',186 if label=='h' else 187,'Human TSLP (hTSLP) Binding ELISA' if label=='h' else 'Cynomolgus Monkey TSLP Binding ELISA',['Table 7'])
 exp('EX5-'+label,'Example 5: '+sp+' TSLP blocking','receptor-blocking ELISA','TSLP binding to TSLPR/IL-7Rα',sp,'Fc-tagged heterodimeric receptor-coated 96-well half-area plate','50 µL receptor 2 µg/mL overnight 4°C; antibody plus biotin-TSLP preincubate 30min 37°C; on plate 1h 37°C; streptavidin-HRP 1:2000 30min; TMB 2–3min RT; A450; % inhibition and IC50',187 if label=='h' else 188,'Human TSLP Blocking ELISA' if label=='h' else 'Cynomolgus Monkey TSLP Blocking ELISA',['Table 8'])
 exp('EX6-'+label,'Example 6: '+sp+' TSLP affinity and kinetics','KinExA 3200 solution equilibrium and direct association','TSLP',sp,'TSLP-coated azlactone beads','25°C; PBS + 1 mg/mL BSA + 0.02% sodium azide; twofold TSLP titration at fixed antibody; ≥36h equilibration; Alexa Fluor647 goat anti-human IgG(H+L); N-curve 1:1 model, titrant reference concentration; direct ka measured at ~80% CBP bound; kd = ka × KD',189,'For each antibody-TSLP interaction, multiple curves from independent experiments were analyzed using the N-curve analysis tool',['Table 9'])
exp('EX7','Example 7: Inhibition of STAT5-Luciferase Signal','Ba/F3 STAT5-luciferase inhibition','human TSLP/TSLPR/IL-7Rα','human receptors in murine Ba/F3','Ba/F3-STAT5-Luc-hTSLPR/hIL-7Rα','serial antibodies 25 µL + 25 µL 4 ng/mL hTSLP 30min 37°C; 20,000 cells in 50 µL; 4h; luciferase reagent 100 µL, 15min RT; baseline-corrected percent inhibition; nonlinear IC50 fit',190,'The IC50 values were obtained by a non-linear curve fit (Table 10)',['Table 10'])
exp('EX8','Example 8: Myeloid Dendritic Cell TSLP Stimulation','mDC CCL17 secretion inhibition; MSD','human TSLP-induced CCL17 (TARC)','human','primary blood mDCs from 5 independent donors','Ficoll PBMCs; EasySep mDC enrichment; antibodies 25 µL + 25 µL 1 ng/mL hTSLP 30min 37°C; 1E5 mDCs; 48h 37°C; supernatant CCL17 measured by MSD; nonlinear IC50 fit',191,'Media were collected, and the concentrations of CCL17 (TARC) were measured on an MSD instrument',['Table 11'],source_heading='Example 8. Inhibition of STAT5-Luciferase Signal')
for label,sp,reps in [('h','human','4'),('cyno','cynomolgus monkey','3'),('mouse','mouse','3'),('IL7','human IL-7','3')]:
 exp('EX9-'+label,'Example 9: '+sp+' specificity/binding ELISA','indirect ELISA; GraphPad Prism 9.2.0 four-parameter fit',sp if label=='IL7' else sp+' TSLP',sp,'96-well protein-coated plate','2 µg/mL recombinant protein overnight 4°C; block 1h RT; 8-point 1:3 antibody dilution from 10 µg/mL, triplicate, 1h RT; anti-human IgG-HRP 1:5000 30min RT; TMB; A450; '+reps+' independent experiments, 3 technical replicates; EC50/80/90 with 95% CI for h/cyno, mean±SEM curves for mouse/IL-7',191,'Antibody binding was assessed using an 8-point titration curve in triplicates (1:3 serial dilution starting at 10 µg/mL)',['Table 12'] if label in ['h','cyno'] else [],figures=['10A' if label=='h' else '10B' if label=='cyno' else '10C' if label=='mouse' else '10D'])
for label,sp,page in [('h','human',193),('cyno','cynomolgus monkey',195)]:
 exp('EX10' if label=='h' else 'EX11','Example '+('10' if label=='h' else '11')+': '+sp+' (TSLPR/IL-7Rα) Ba/F3 assay','STAT5 luciferase inhibition; Prism 9.2.0 nonlinear fit','TSLP/TSLPR/IL-7Rα',sp+' ligand and receptors in murine Ba/F3','Ba/F3-STAT5-Luc-'+('hTSLPR/hIL-7Rα' if label=='h' else 'cynoTSLPR/cynoIL-7Rα'),'24h cytokine deprivation; final TSLP 1 ng/mL; 25 µL 4X antibodies +25 µL 4 ng/mL TSLP 30min 37°C; 20,000 cells in 50 µL; 4h; luciferase reagent 100 µL, 15min RT; read within 1h; 3 independent experiments ×3 technical replicates; IC50 µg/mL (95% CI)',page,'serial dilutions of 4X anti-TSLP antibodies',['Table 13'])
exp('CBO','Table 14: Potts model scoring','computational binding optimization (CBO) Potts model','TSLP binding sequence design',None,'VH/VL selected positions; combined vhvlt sequences','Appendix C claim.py uses threshold -7.7; scores are computational outputs, not measured binding affinities',209,'Table 14: Example Sequences and Potts Model Scores',['Table 14'])
clones=[f'AB-{n}' for n in range(1,15)]+['Reference Antibody']
# All numeric cells in Tables 6–11 are transcribed from the page images.
for e,a,b in zip(clones,'7 4 6 6 7 7 4 5 6 6 4 6 5 5 5'.split(),'4 4 4 4 3 3 4 4 4 2 4 4 3 4 2'.split()):
 meas(e,'EX3','AC-SINS Score',a,None,'as reported; score scale not defined',205,f'{e}: {a} | {b}','Table 6')
 meas(e,'EX2','PSR Score',b,None,'as reported; score scale not defined',205,f'{e}: {a} | {b}','Table 6')
for tab,page,x,metric,hs,cs in [(7,205,'EX4','EC50','0.2651 0.2037 0.2369 0.2595 0.2635 0.3967 0.2898 0.2484 0.3881 0.2856 0.1734 0.2912 0.2303 0.264 0.2387','0.4464 0.4019 0.5755 0.3314 0.3152 0.4243 0.6176 0.4624 0.4803 0.4352 0.336 0.4571 0.448 0.3674 0.5242'),(8,206,'EX5','IC50','0.1693 0.29 0.2318 0.1704 0.2457 0.2224 0.1624 0.1995 0.2265 0.2505 0.2427 0.29 0.2232 0.1865 0.2939','0.1782 0.3181 0.2574 0.2017 0.2045 0.2492 0.2051 0.2041 0.2597 0.2379 0.287 0.2538 0.2449 0.2293 0.2796')]:
 for e,h,c in zip(clones,hs.split(),cs.split()):
  for lab,v,cond in [('h',h,'human TSLP'),('cyno',c,'cynomolgus monkey TSLP')]:meas(e,x+'-'+lab,metric,v,'nM',cond,page,f'{e}: {h} | {c}',f'Table {tab}')
kin='''AB-1 .208 2.66E+07 5.53E-06 3.28 1.34E+07 4.41E-05
AB-2 .123 2.38E+07 2.93E-06 1.25 5.25E+07 6.56E-05
AB-3 .302 1.65E+07 4.99E-06 1.72 1.98E+07 3.40E-06
AB-4 .208 2.72E+07 5.66E-06 .929 3.06E+07 2.84E-05
AB-5 .14 5.83E+07 8.17E-06 2.01 2.58E+07 6.37E-05
AB-6 .133 2.01E+08 2.65E-05 1.97 1.97E+07 3.83E-05
AB-7 .184 2.21E+07 4.06E-06 .754 2.71E+07 2.05E-05
AB-8 .335 2.45E+07 5.62E-06 1.56 1.91E+07 2.97E-05
AB-9 .496 1.73E+07 8.57E-06 2.17 3.47E+07 7.52E-05
AB-10 .395 2.40E+07 9.48E-06
AB-11 .754 2.23E+07 1.68E-05
AB-12 1.020 2.29E+07 2.33E-05
AB-14 .877 2.63E+07 2.31E-05
Reference 2.15 1.54E+07 3.30E-05 9.05 9.80E+06 8.87E-05'''
for row in kin.splitlines():
 row=re.sub(r'(?<= )\.', '0.', row)
 ss=row.split();e=ss[0] if ss[0]!='Reference' else 'Reference Antibody'
 for j,v in enumerate(ss[1:]):
  if v.startswith('.'):v='0'+v
  meas(e,'EX6-'+('h' if j<3 else 'cyno'),['KD','ka','kd'][j%3],v,['pM','1/Ms','1/s'][j%3],'human TSLP' if j<3 else 'cynomolgus monkey TSLP',206,row,'Table 9',result_kind='derived' if j%3==2 else 'experimental_fit')
for e,v in zip(clones,'0.6233 0.5868 0.4106 1.106 2.329 0.5309 0.7316 0.667 0.2872 1.993 1.164 2.784 3.333 3.942 1.931'.split()):meas(e,'EX7','IC50',v,'nM','human TSLP; STAT5-luciferase',207,f'{e} {v}','Table 10')
mdc='''AB-1 0.1675 0.6433 0.4553 0.1368 0.4494
AB-2 0.2402 0.349 0.1774 0.1427 0.3544
AB-3 0.1051 0.3053 0.1471 0.1326 0.1888
AB-4 0.1909 0.6764 0.1842 0.1994 0.4266
AB-5 0.1891 1.152 0.3254 0.4966 0.6762
AB-6 0.2321 0.7187 0.3392 0.2735 0.4777
AB-7 0.3685 1.31 0.6447 0.4997 0.7363
AB-8 0.1487 0.579 0.2684 0.2153 0.3164
AB-9 0.1958 0.7094 0.3161 0.1567 0.4098
Reference 1.158 1.402 1.132 1.044 1.774'''
for row in mdc.splitlines():
 s=row.split();e=s[0] if s[0]!='Reference' else 'Reference Antibody'
 for j,v in enumerate(s[1:]):meas(e,'EX8','IC50',v,'nM',{'donor':j+1,'readout':'CCL17 (TARC) secretion by human mDCs'},207,row,'Table 11')
# CI-bearing values are kept verbatim, including N/A.
t12=[('h','EC50',['0.03 (0.03 to 0.04)','0.03 (0.02 to 0.05)','0.06 (0.06 to 0.07)']),('h','EC80',['0.09 (0.06 to 0.13)','0.10 (0.06 to 0.19)','0.14 (0.11 to 0.18)']),('h','EC90',['0.15 (0.08 to 0.27)','0.18 (0.09 to 0.45)','0.23 (0.16 to 0.31)']),('cyno','EC50',['0.04 (0.03 to 0.04)','0.04 (0.03 to 0.04)','0.06 (0.05 to 0.07)']),('cyno','EC80',['0.10 (0.07 to 0.13)','0.10 (0.08 to 0.14)','0.18 (0.13 to 0.24)']),('cyno','EC90',['0.17 (0.11 to 0.26)','0.19 (0.13 to 0.29)','0.33 (0.22 to 0.50)'])]
for lab,m,vals in t12:
 for e,v in zip(['AB-2a','AB-2b','Reference Antibody','Human IgG1 isotype','Human IgG2 isotype'],vals+['N/A','N/A']):
  meas(e,'EX9-'+lab,m,v,'µg/mL',{'species':'human' if lab=='h' else 'cynomolgus monkey','interval':'95% confidence interval'},208,f'{m}: '+' | '.join(vals+['N/A','N/A']),'Table 12',status='not_applicable' if v=='N/A' else 'reported')
for lab,vals in [('h',['0.02 (0.004 to 0.12)','0.03 (0.006 to 0.18)','0.17 (0.05 to 0.65)']),('cyno',['0.03 (0.01 to 0.07)','0.04 (0.02 to 0.07)','0.14 (0.05 to 0.40)'])]:
 for e,v in zip(['AB-2a','AB-2b','Reference Antibody','Human IgG1 isotype','Human IgG2 isotype'],vals+['N/A','N/A']):meas(e,'EX10' if lab=='h' else 'EX11','IC50',v,'µg/mL',{'species':lab,'interval':'95% confidence interval'},208,'IC50: '+' | '.join(vals+['N/A','N/A']),'Table 13',status='not_applicable' if v=='N/A' else 'reported')
# Exact displayed Potts scores, not recast as wet-lab measurements.
for n,v in [(2,'-4.8525944'),(1,'-2.9628694'),(8,'-2.9442153'),(6,'-2.3772101'),(3,'-2.1100748'),(5,'-1.8960481'),(7,'-1.6204025'),(10,'-1.4744515'),(13,'-1.3488145'),(14,'-1.1853998'),(12,'-0.6803217'),(11,'-0.6461612'),(4,'-0.62289006')]:meas(f'AB-{n}','CBO','potts_model_score',v,None,'Table 14; vhvlt',209,f'AB-{n} {v}','Table 14',result_kind='computational')
issue('source_table_blank_cells','Table 9 contains no values for AB-13 (all columns) or for cynomolgus TSLP AB-10, AB-11, AB-12, AB-14. Blank cells are not zero and are not inferred.',206,'Table 9. KinExA Binding Affinities')
issue('source_numerical_inconsistency','For AB-3 cynoTSLP, Table 9 prints KD 1.72 pM, ka 1.98E+07, kd 3.40E-06. These do not satisfy the stated kd=ka×KD relationship; original values retained.',206,'AB-3 | 0.302 | 1.65E+07 | 4.99E-06 | 1.72 | 1.98E+07 | 3.40E-06')
issue('source_heading_readout_conflict','Example 8 heading and Table 11 title mention STAT5 signaling, but the described measured endpoint is mDC CCL17 secretion. The experiment is structured using the described endpoint and the original titles are preserved.',191,'concentrations of CCL17 (TARC) were measured on an MSD instrument')
issue('unresolved_result_construct_identity','Tables 6–11 use unsuffixed AB-1–AB-14. Their values are assigned only to the unsuffixed entities; an a/b constant-region construct cannot be assigned without explicit evidence.',205,'clone | AC-SINS Score | PSR Score')
issue('score_scale_not_defined','Table 6 provides AC-SINS Score and PSR Score without units or mapping from the raw ΔλMAX or fluorescence ratio. These scores are not labeled nm or fold.',205,'AC-SINS Score | PSR Score')
# This script is extended with sequence and full-document extraction below.
# Tables 1–3: all printed sequence definitions, visually transcribed.
h2={33:'IWYDGSNK',34:'VWYSGSYT',35:'VWYDGSYT',36:'IWYDGTYT',37:'IWYSGSDT',38:'VWYDGYDT',39:'VWYDGSDI',40:'IWYSGAYI',41:'IWYDGSYT',42:'IWYSGTTT',43:'ISYSGSYI',44:'VWYDGSNT',45:'ISYDGSTK',46:'IWYDGSTK'}
h3={48:'ARAPQWELVHEAFDI',49:'SRSPQWEDIFEAMDI',50:'ARSPQWEEIFEAMDI',51:'ARSPQWESIFEAFDI',52:'ARSPQWEDIFESMDV',53:'SRSPQWEDIFESMDI',54:'ARSPQWEDIFEAFDI',55:'TRSPQWEEVYEALDI',56:'ARAPQWESVHEAFDI',57:'ARAPQWESVYEAFDI'}
l1={59:'NLGSKS',60:'NIGSKS',61:'YIGSFS',62:'NLGSYS',63:'NIGSKN',64:'NIGSFS',65:'NIGRFS',66:'NLGSFS'}
l3={69:'QVWDSSSDHVV',70:'QVWDSSSSLVV',71:'QIWDSSSSLVV',72:'QIYVSASRLVV',73:'QVWDMSSDLVV',74:'QIWDSSSDHVV',75:'QVWDMSSSLVV',76:'QVWDSSDFLVV',77:'QVWSSTSRHVV',78:'QVWVSTSDKVV',79:'QVWDSSEDLVV',80:'QVWDESSDEVV'}
seq(31,'HCDR1','GFTFRTYG',197,'Consensus, Reference, AB-1-14 | NO:31 | GFTFRTYG',numbering_scheme='IMGT')
seq(32,'HCDR2','X1X2YX3GX4X5X6',197,'Consensus | NO:32 | X1X2YX3GX4X5X6',numbering_scheme='IMGT',sequence_kind='consensus_template')
for n,s in h2.items():seq(n,'HCDR2',s,197 if n<=43 else 198,f'NO:{n} | {s}',numbering_scheme='IMGT')
seq(47,'HCDR3','X7RX8PQWEX9X10X11EX12X13DX14',198,'Consensus | NO:47 | X7RX8PQWEX9X10X11EX12X13DX14',numbering_scheme='IMGT',sequence_kind='consensus_template')
for n,s in h3.items():seq(n,'HCDR3',s,198,f'NO:{n} | {s}',numbering_scheme='IMGT')
seq(58,'LCDR1','X15X16GX17X18X19',198,'Consensus | NO:58 | X15X16GX17X18X19',numbering_scheme='IMGT',sequence_kind='consensus_template')
for n,s in l1.items():seq(n,'LCDR1',s,198,f'NO:{n} | {s}',numbering_scheme='IMGT')
seq(67,'LCDR2','DDS',198,'Consensus, Reference, AB-1-14 | NO:67 | DDS',numbering_scheme='IMGT')
seq(68,'LCDR3','QX20X21X22X23X24X25X26X27VV',198,'Consensus | NO:68 | QX20X21X22X23X24X25X26X27VV',numbering_scheme='IMGT',sequence_kind='consensus_template')
for n,s in l3.items():seq(n,'LCDR3',s,198,f'NO:{n} | {s}',numbering_scheme='IMGT')
# The factorization below transcribes the recurring literal strings in the printed VH/VL table cells.
# It does not infer a variable region by trimming a full chain.
hp='QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYGMHWVRQAPGKGLEWVAV'
hm='HYADSVKGRFTITRDNSKNTLNLQMNSLRAEDTAVYYC';he='WGQGTMVTVSS'
VH={3:hp+h2[33]+hm+h3[48]+he}
h2map=[34,35,36,37,38,39,40,41,41,42,43,44,45,46]
h3map=[49,50,51,52,53,53,49,54,51,53,55,56,57,48]
for i,(a,b) in enumerate(zip(h2map,h3map),1):VH[i+3]=hp+h2[a]+hm+h3[b]+he
seq(2,'VH',hp+'X1X2YX3GX4X5X6'+hm+'X7RX8PQWEX9X10X11EX12X13DX14'+he,196,'Consensus | NO:2 | QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYGMHWVRQAPGKGLEWVA...',sequence_kind='consensus_template')
for n,s in VH.items():seq(n,'VH',s,196,f'{"Reference" if n==3 else "AB-"+str(n-3)} | NO:{n} | {s}',transcription='Table 1 full VH cell')
lp='SYVLTQPPSVSVAPGQTARITCGGN';lm='VHWYQQKPGQAPVLVVYDDSDRPSWIPERFSGSNSGNTATLTISRGEAGDEADYYC';le='FGGGTKLTVL'
VL={19:lp+l1[59]+lm+l3[69]+le}
l1map={20:60,21:60,22:61,23:59,24:60,25:62,26:63,27:64,28:65,29:66,30:63}
for n,a in l1map.items():VL[n]=lp+l1[a]+lm+l3[n+50]+le
seq(18,'VL',lp+'X15X16GX17X18X19'+lm+'QX20X21X22X23X24X25X26X27VV'+le,197,'Consensus | NO:18 | SYVLTQPPSVSVAPGQTARITCGGN...',sequence_kind='consensus_template')
for n,s in VL.items():seq(n,'VL',s,197,f'NO:{n} | {s}',transcription='Table 2 full VL cell')
vlmap=[20,21,20,22,23,24,25,20,20,26,27,28,29,30]
for e,vh,vl,a,b in [('Reference Antibody',3,19,33,48)]+[(f'AB-{i}',i+3,vlmap[i-1],h2map[i-1],h3map[i-1]) for i in range(1,15)]:
 link(e,vh,'VH',196,f'{e} | NO:{vh}')
 link(e,vl,'VL',197,f'{e} | NO:{vl}')
 for sn,reg in [(31,'HCDR1'),(a,'HCDR2'),(b,'HCDR3'),(59 if vl==19 else l1map[vl],'LCDR1'),(67,'LCDR2'),(69 if vl==19 else vl+50,'LCDR3')]:
  link(e,sn,reg,197 if sn<=43 else 198,f'Table 3: {e} | NO:{sn}',numbering_scheme='IMGT')
for sn,reg in [(2,'VH'),(18,'VL'),(31,'HCDR1'),(32,'HCDR2'),(47,'HCDR3'),(58,'LCDR1'),(67,'LCDR2'),(68,'LCDR3')]:link('Consensus',sn,reg,196 if sn==2 else 197 if sn<44 else 198,'Consensus | NO:'+str(sn))
# Table 14 reduced position strings: vh is not a whole VH, vl is not a whole VL.
t14='''2|137|GTRYVWDSYTASQWEEIFEAMI|138|NIGSKSDSQIWDSSSSLVV|139|GTRYVWDSYTASQWEEIFEAMINIGSKSDSQIWDSSSSLVV
1|140|GTRYVWSSYTSSQWEDIFEAMI|141|NIGSKSDSQVWDSSSSLVV|142|GTRYVWSSYTSSQWEDIFEAMINIGSKSDSQVWDSSSSLVV
8|143|GTRYIWDSYTASQWEDIFEAFI|141|NIGSKSDSQVWDSSSSLVV|144|GTRYIWDSYTASQWEDIFEAFINIGSKSDSQVWDSSSSLVV
6|145|GTRYVWDSDISQWEDIFESMI|146|NIGSKSDSQIWDSSSDHVV|147|GTRYVWDSDISQWEDIFESMINIGSKSDSQIWDSSSDHVV
3|148|GTRYIWDTYTASQWESIFEAFI|141|NIGSKSDSQVWDSSSSLVV|149|GTRYIWDTYTASQWESIFEAFINIGSKSDSQVWDSSSSLVV
5|150|GTRYVWDYDTSSQWEDIFESMI|151|NLGSKSDSQVWDMSSDLVV|152|GTRYVWDYDTSSQWEDIFESMINLGSKSDSQVWDMSSDLVV
7|153|GTRYIWSAYISSQWEDIFEAMI|154|NLGSYSDSQVWDMSSSLVV|155|GTRYIWSAYISSQWEDIFEAMINLGSYSDSQVWDMSSSLVV
10|156|GTRYIWSTTTSSQWEDIFESMI|157|NIGSKNDSQVWDSSDFLVV|158|GTRYIWSTTTSSQWEDIFESMINIGSKNDSQVWDSSDFLVV
13|159|GTRYISDSTKAAQWESVYEAFI|160|NLGSFSDSQVWDSSEDLVV|161|GTRYISDSTKAAQWESVYEAFINLGSFSDSQVWDSSEDLVV
14|162|GTRYIWDSTKAAQWELVHEAFI|163|NIGSKNDSQVWDESSDEVV|164|GTRYIWDSTKAAQWELVHEAFINIGSKNDSQVWDESSDEVV
12|165|GTRYVWDSNTAAQWESVHEAFI|166|NIGRFSDSQVWVSTSDKVV|167|GTRYVWDSNTAAQWESVHEAFINIGRFSDSQVWVSTSDKVV
11|168|GTRYISSSYITSQWEEVYEALI|169|NIGSFSDSQVWSSTSRHVV|170|GTRYISSSYITSQWEEVYEALINIGSFSDSQVWSSTSRHVV
4|171|GTRYIWSSDTASQWEDIFESMV|172|YIGSFSDSQIYVSASRLVV|173|GTRYIWSSDTASQWEDIFESMVYIGSFSDSQIYVSASRLVV'''
seen=set()
for row in t14.splitlines():
 a=row.split('|');e='AB-'+a[0]
 for j,typ in [(1,'vht selected VH positions'),(3,'vlt selected VL positions'),(5,'vhvlt concatenated selected VH/VL positions')]:
  n=int(a[j]);s=a[j+1]
  if n not in seen:seq(n,typ,s,209,f'{e} | {typ.split()[0]} | {s} (SEQ ID NO:{n})',sequence_kind='model_input_selected_positions');seen.add(n)
  link(e,n,typ,209,f'{e} | {s} (SEQ ID NO:{n})')
# Explicit constant-domain sequences, paragraphs [00379]–[00397].
C={
84:'''ASTKGPSVFPLAPSSKSTSGGTAALGCLVKDYFPEPVTVSWNSGALTSGV
HTFPAVLQSSGLYSLSSVVTVPSSSLGTQTYICNVNHKPSNTKVDKRVEPKSCDKTHT
CPPCPAPELLGGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSHEDPEVKFNWYVDGV
EVHNAKTKPREEQYNSTYRVVSVLTVLHQDWLNGKEYKCKVSNKALPAPIEKTISK
AKGQPREPQVYTLPPSREEMTKNQVSLTCLVKGFYPSDIAVEWESNGQPENNYKTTP
PVLDSDGSFFLYSKLTVDKSRWQQGNVFSCSVMHEALHNHYTQKSLSLSPGK''',
85:'''ASTKGPSVFPLAPSSKSTSGGTAALGCLVKDYFPEPVTVSWNSGALTSGV
HTFPAVLQSSGLYSLSSVVTVPSSSLGTQTYICNVNHKPSNTKVDKRVEPKSCDKTHT
CPPCPAPELLGGPSVFLFPPKPKDTLYITREPEVTCVVVDVSHEDPEVKFNWYVDGVE
VHNAKTKPREEQYNSTYRVVSVLTVLHQDWLNGKEYKCKVSNKALPAPIEKTISKA
KGQPREPQVYTLPPSREEMTKNQVSLTCLVKGFYPSDIAVEWESNGQPENNYKTTPP
VLDSDGSFFLYSKLTVDKSRWQQGNVFSCSVMHEALHNHYTQKSLSLSPGK''',
86:'''ASTKGPSVFPLAPSSKSTSGGTAALGCLVKDYFPEPVTVSWNSGALTSGV
HTFPAVLQSSGLYSLSSVVTVPSSSLGTQTYICNVNHKPSNTKVDKKVEPKSCDKTHT
CPPCPAPELLGGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSHEDPEVKFNWYVDGV
EVHNAKTKPREEQYNSTYRVVSVLTVLHQDWLNGKEYKCKVSNKALPAPIEKTISK
AKGQPREPQVYTLPPSRDELTKNQVSLTCLVKGFYPSDIAVEWESNGQPENNYKTTP
PVLDSDGSFFLYSKLTVDKSRWQQGNVFSCSVMHEALHNHYTQKSLSLSPGK''',
87:'''ASTKGPSVFPLAPCSRSTSESTAALGCLVKDYFPEPVTVSWNSGALTSGVH
TFPAVLQSSGLYSLSSVVTVPSSNFGTQTYTCNVDHKPSNTKVDKTVERKCCVECPP
CPAPPVAGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSHEDPEVQFNWYVDGVEVHN
AKTKPREEQFNSTFRVVSVLTVVHQDWLNGKEYKCKVSNKGLPAPIEKTISKTKGQP
REPQVYTLPPSREEMTKNQVSLTCLVKGFYPSDIAVEWESNGQPENNYKTTPPMLDS
DGSFFLYSKLTVDKSRWQQGNVFSCSVMHEALHNHYTQKSLSLSPGK''',
88:'''ASTKGPSVFPLAPCSRSTSESTAALGCLVKDYFPEPVTVSWNSGALTSGVH
TFPAVLQSSGLYSLSSVVTVPSSNFGTQTYTCNVDHKPSNTKVDKTVERKCCVECPP
CPAPPVAGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSHEDPEVQFNWYVDGVEVHN
AKTKPREEQFNSTFRVVSVLTVVHQDWLNGKEYKCKVSNKGLPAPIEKTISKTKGQP
REPQVYTLPPSREEMTKNQVSLTCLVKGFYPSDIAVEWESNGQPENNYKTTPPMLDS
DGSFFLYSKLTVDKSRWQQGNVFSCSVMHEALHNHYTQKSLSLSPGK''',
89:'''RTVAAPSVFIFPPSDEQLKSGTASVVCLLNNFYPREAKVQWKVDNALQSG
NSQESVTEQDSKDSTYSLSSTLTLSKADYEKHKVYACEVTHQGLSSPVTKSFNRGEC''',
90:'''GQPKAAPSVTLFPPSSEELQANKATLVCLISDFYPGAVTVAWKADSSPVK
AGVETTTPSKQSNNKYAASSYLSLTPEQWKSHRSYSCQVTHEGSTVEKTVAPTECS'''}
C={k:re.sub(r'\s','',s) for k,s in C.items()}
for n,s in C.items():
 seq(n,'heavy_chain_constant_domain' if n<89 else 'light_chain_constant_domain',s,81 if n<=86 else 82 if n<=88 else 85,f'{s[:38]} ... (SEQ ID NO:{n})',isotype='IgG1' if n<=86 else 'IgG2' if n<=88 else 'κ' if n==89 else 'λ')
 if n==86:D['sequences'][-1]['evidence'].append(ev(82,'CPPCPAPELLGGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSHEDPEVKFNWYVDGV'))
for n in range(1,15):
 for suf,cnum,hnum in [('a',84,90+n),('b',85,104+n)]:
  e=f'AB-{n}{suf}'
  pg=(199 if n<=5 else 200 if n<=11 else 201) if suf=='a' else (201 if n<=3 else 202 if n<=9 else 203)
  full=VH[n+3]+C[cnum]
  seq(hnum,'full_heavy_chain',full,pg,f'{e} | NO:{hnum} | {full[:35]}...',transcription='Table 4 full-chain cell; recurring literal constant-region strings also printed as SEQ ID NO:'+str(cnum))
  link(e,hnum,'full_heavy_chain',pg,f'{e} | NO:{hnum}')
  link(e,cnum,'heavy_chain_constant_domain',83 if suf=='a' else 84,('AB-[]a' if suf=='a' else 'AB-[]b')+f' ... comprise the heavy chain constant domain amino acid sequence of SEQ ID NO:{cnum}')
  # VH/VL are separate explicitly printed sequences, not derived from full-chain cleavage.
  link(e,n+3,'VH',196,f'AB-{n} VH NO:{n+3}; Table 4 {e} has the corresponding full sequence',support='matching explicitly printed Table 1 VH and Table 4 chain sequence')
  lnum=vlmap[n-1]+99
  link(e,lnum,'full_light_chain',203 if lnum==119 else 204,f'Table 5: AB-{n} | NO:{lnum}')
  link(e,vlmap[n-1],'VL',197,f'Table 2: AB-{n} | NO:{vlmap[n-1]}',support='matching explicitly printed Table 2 VL and Table 5 light-chain sequence')
  for obj in D['entities']:
   if obj['id']==e:obj.update(isotype='human IgG1',constant_region_seq_id=f'SEQ-{cnum}',constant_region_variant='M135, S137, T139' if suf=='a' else 'Y135, T137, E139 (YTE)');obj['evidence'].append(ev(83 if suf=='a' else 84,('AB-[]a' if suf=='a' else 'AB-[]b')+f' ... SEQ ID NO:{cnum}'))
seq(81,'full_heavy_chain',VH[3]+C[87],198,'Reference | NO:81 | QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYG...',transcription='Table 4 full cell continued on physical page 199')
D['sequences'][-1]['evidence'].append(ev(199,'Reference heavy chain continuation: WNSGALTSGVHTFPAVLQSSGLYSLSSVVTVPSSNFGTQTYTCNVDHKPSNTKV'))
seq(82,'full_light_chain',VL[19]+C[90],203,'Reference | NO:82 | SYVLTQPPSVSVAPGQTARITCGGNNLGSKS...',transcription='Table 5 full light-chain cell')
link('Reference Antibody',81,'full_heavy_chain',198,'Reference | NO:81')
link('Reference Antibody',82,'full_light_chain',203,'Reference | NO:82')
for n in range(20,31):
 num=n+99;s=VL[n]+C[90]
 seq(num,'full_light_chain',s,203 if num==119 else 204,f'NO:{num} | {s[:37]}...',transcription='Table 5 full cell; SEQ ID NO:119 continues from physical page 203 to 204')
 for i,v in enumerate(vlmap,1):
  if v==n:link(f'AB-{i}',num,'full_light_chain',203 if num==119 else 204,f'AB-{i} | NO:{num}')
link('Reference Antibody',87,'heavy_chain_constant_domain',198,'Reference Table 4 sequence matches explicitly printed SEQ ID NO:87',support='literal sequence comparison')
link('Reference Antibody',90,'light_chain_constant_domain',203,'Reference Table 5 sequence matches explicitly printed SEQ ID NO:90',support='literal sequence comparison')
for n in range(1,15):
 for suf in ['a','b']:rel(f'AB-{n}{suf}','isotype_variant_of',f'AB-{n}',83 if suf=='a' else 84,('AB-[]a' if suf=='a' else 'AB-[]b')+' constant domain designation')
seq(1,'full_length_human_TSLP','MFPFALLYVLSVSFRKIFILQLVGLVLTYDFTNCDFEKIKAAYLSTISKDLITYMSGTKSTEFNNTVSCSNRPHCLTEIQSLTFNPTAGCASLAKEMFAMKTKAALAIWCPGYSETQINATQAMKKRRKRKVTTNKCLEQVSQLQGLWRRFNRPLLKQQ',228,'MFPFALLYVLSVSFRKIFILQLVGLVLTYDFTNCDFEKIK...KQQ (SEQ ID NO:1)',molecule_type='protein')
link('human TSLP',1,'full_length_human_TSLP',228,'(SEQ ID NO:1)')
for n,reg,s,p,scheme in [(130,'HCDR1','TYGMH',121,'Kabat'),(131,'HCDR2','VVWYDGSYTHYADSVKG',121,'Kabat'),(132,'HCDR3','SPQWEEIFEAMDI',121,'Kabat and Chothia'),(133,'LCDR1','GGNNIGSKSVH',121,'Kabat and Chothia'),(134,'LCDR2','DDSDRPS',121,'Kabat and Chothia'),(135,'HCDR1','GFTFRTY',122,'Chothia'),(136,'HCDR2','WYDGSY',122,'Chothia')]:
 seq(n,reg,s,p,f'{reg}: {s} (SEQ ID NO:{n})',numbering_scheme=scheme)
 for e in ['AB-2','AB-2a','AB-2b']:link(e,n,reg,p,f'AB-2 CDR amino acid sequences as determined by {scheme} numbering; {reg}: {s} (SEQ ID NO:{n})',numbering_scheme=scheme)
for scheme,p in [('Kabat',121),('Chothia',122)]:
 for e in ['AB-2','AB-2a','AB-2b']:link(e,71,'LCDR3',p,'LCDR3: QIWDSSSSLVV (SEQ ID NO:71)',numbering_scheme=scheme)
# Re-read Table 14 at enlarged resolution: the AB-6 cell includes two consecutive S residues.
for s in D['sequences']:
 if s['seq_id_no'] in [145,147]:
  s['sequence']=s['sequence'].replace('GTRYVWDSDISQ','GTRYVWDSDISSQ');s['evidence'][0]['quote']=s['evidence'][0]['quote'].replace('GTRYVWDSDISQ','GTRYVWDSDISSQ')
for l in D['sequence_links']:
 if l['sequence_id'] in ['SEQ-145','SEQ-147']:l['evidence'][0]['quote']=l['evidence'][0]['quote'].replace('GTRYVWDSDISQ','GTRYVWDSDISSQ')
entity('Isotype Control (Fig. 8)','isotype control, subclass unspecified',242,'Isotype Control')
entity('PRO-171101-004 [uncertain]','unresolved figure legend label',248,'PRO-171101-004',name_uncertain=True)
issue('uncertain_figure_legend','Figure 10D has an extra small legend label resembling PRO-171101-004 and misaligned subsequent labels. Its identity and any alias relationship to AB-2a/AB-2b cannot be resolved from the PDF.',248,'PRO-171101-004',affected_entities=['PRO-171101-004 [uncertain]'])
exp('EX7-stimulation','Example 7: TSLP stimulation of reporter cells','TSLP dose response, STAT5-luciferase','human TSLP','human TSLP receptors in murine Ba/F3','Ba/F3-STAT5-Luc-hTSLPR/hIL-7Rα','TSLP concentration series; Figure 8A',190,'The hTSLPR/hIL-7R-expressing Ba/F3-STAT5-Luc cells had a dose dependent response to hTSLP with an EC50 of 0.03 nM (FIG. 8A)')
meas('human TSLP','EX7-stimulation','EC50','0.03','nM','stimulation of Ba/F3-STAT5-luciferase cells expressing hTSLPR/hIL-7Rα',190,'dose dependent response to hTSLP with an EC50 of 0.03 nM (FIG. 8A)')
for e in ['AB-2a','AB-2b','Reference Antibody']:
 for lab in ['mouse','IL7']:meas(e,'EX9-'+lab,'binding','lack of binding',None,'mouse TSLP' if lab=='mouse' else 'human IL-7',192,'The ELISA results of AB-2a, AB-2b and the Reference Antibody demonstrate a lack of binding to mouse TSLP cytokine and human IL-7.',result_kind='qualitative')
for x,p in [('EX10',194),('EX11',195)]:rel('AB-2b','improved_functional_potency_vs','Reference Antibody',p,'AB-2b has improved functional potency compared to the Reference Antibody.',experiment_id=x)
issue('receptor_species_conflict','Example 5 paragraph [00777] says cynomolgus TSLPR, while Figures 7A–7B explicitly label hTSLPR-IL7Ra/cynoTSLP. Preserve both; the receptor species of Table 8 cyno results is unresolved.',239,'hTSLPR-IL7Ra/cynoTSLP Blocking ELISA')
issue('figure_citation_mismatch','Example 8 text cites Figures 9B–9C, whereas the supplied figure pages are labeled 9A and 9B (both donor 3). No Figure 9C is present.',191,'FIGs. 9B-9C show effects of the anti-TSLP antibodies')
issue('external_material_not_in_input','The PDF incorporates an external XML sequence listing and model weights by reference. Those external files were not read or fetched. Only sequences explicitly present in this PDF can be extracted; the numerical Potts weight table cannot be reconstructed.',2,'57081061005_Sequence_Listing.xml; created July 22, 2023, 212,394 Bytes in size.')
issue('curves_not_pointwise_digitized','Figures 4–11 give curves/markers without printed individual point values. Exact tabulated EC/IC values and qualitative results were extracted. Raw A450, % inhibition, luciferase AU, TARC pg/mL, and graphical error-bar point coordinates were not converted into invented exact numeric measurements.',233,'A450; mAb (nM)')
seq(83,'full_length_cynomolgus_TSLP','MKSLGQSKKEEVSFRKFFIFQLVGLVLTYDFTNCDFQKIEADYLRTISKDLITYMSGTKSTDFNNTVSCSNRPHCLTEIQSLTFNPTPRCASLAKEMFARKTKATLALWCPGYSETQINATQAMKKRRKRKVTTNKCLEQVSQLLGLWRRFIRTLLKKQ',27,'MKSLGQSKKEEVSFRKFFIFQLVGLVLTYDFTNCDFQKIEADYLRTISKDLI ... (SEQ ID NO:83)',accession_as_printed='EHH54440.1')
link('cynomolgus TSLP',83,'full_length_cynomolgus_TSLP',27,'A non-limiting example of cyno monkey TSLP sequences is GenBank: EHH54440.1 (SEQ ID NO:83)')
for s in D['sequences']:
 if s['seq_id_no']==1:s['evidence'].append(ev(27,'[00117] MFPFALLYVLSVSFRKIFILQLVGLVLTYDFTNCDFEKIK... (SEQ ID NO:1)'))
for e in D['entities']:
 if e['id']=='Reference Antibody':e['evidence'].append(ev(30,'The Reference Antibody comprises: a heavy chain ... SEQ ID NO:81; and a light chain ... SEQ ID NO:82.'));e['isotype']='IgG2 (constant sequence matching SEQ ID NO:87)'
D['epitope_definitions']=[{'target_entity_id':'human TSLP','sequence_id':'SEQ-1','residues':['N64','N65','T66','V67','S68','C69','S70','N71','R72','H74','C75','E78','R150','F151','R153'],'scope':'claimed binding to one or more of these residues; not an experimental residue-by-residue measurement','evidence':[ev(28,'selected from N64, N65, T66, V67, S68, C69, S70, N71, R72, H74, C75, E78, R150, F151 and R153 of SEQ ID NO:1')]}]
D['figures']=[]
def fig(n,p,kind,exps,ents,desc):
 D['figures'].append({'figure':n,'page':p,'kind':kind,'experiment_ids':exps,'entity_ids':ents,'description':desc,'evidence':[ev(p,'FIG. '+n)]})
fig('1',228,'target sequence',[],['human TSLP'],'SEQ ID NO:1 with epitope residues marked by asterisks')
for n,p in [('2A',229),('2B',230),('3A',231),('3B',232)]:fig(n,p,'sequence alignment',[],clones,'Reference and AB-1–AB-14 VH (2A/B) or VL (3A/B), CDRs underlined; sequence definitions retained in sequences')
A=['AB-7','AB-1','AB-9','AB-6','AB-10','AB-4','AB-14','Reference Antibody']
B=['AB-12','AB-5','AB-3','AB-8','AB-13','AB-11','AB-2','Reference Antibody']
for n,p,x,es,ds in [('4A',233,'EX4-h',A,'human TSLP binding A450 vs antibody nM'),('4B',234,'EX4-h',B,'human TSLP binding A450 vs antibody nM'),('5A',235,'EX4-cyno',A,'cyno TSLP binding A450 vs antibody nM'),('5B',236,'EX4-cyno',B,'cyno TSLP binding A450 vs antibody nM'),('6A',237,'EX5-h',A,'human TSLP blocking % inhibition vs antibody nM'),('6B',238,'EX5-h',B,'human TSLP blocking % inhibition vs antibody nM'),('7A',239,'EX5-cyno',A,'cyno TSLP blocking; graph title labels human receptor'),('7B',240,'EX5-cyno',B,'cyno TSLP blocking; graph title labels human receptor'),('8B',242,'EX7',A+['Isotype Control (Fig. 8)'],'Ba/F3 STAT5 luciferase % inhibition vs antibody nM'),('8C',243,'EX7',B+['Isotype Control (Fig. 8)'],'Ba/F3 STAT5 luciferase % inhibition vs antibody nM'),('9A',244,'EX8',['AB-8','AB-5','AB-7','AB-1','Reference Antibody'],'mDC TARC pg/mL vs antibody nM; donor 3'),('9B',245,'EX8',['AB-2','AB-4','AB-3','AB-6','AB-9','Reference Antibody'],'mDC TARC pg/mL vs antibody nM; donor 3')]:fig(n,p,'dose-response graph',[x],es,ds)
fig('8A',241,'dose-response graph',['EX7-stimulation'],['human TSLP'],'luciferase AU vs TSLP nM; EC50 stated in text on p190')
for n,p,x in [('10A',246,'EX9-h'),('10B',247,'EX9-cyno'),('10C',248,'EX9-mouse'),('10D',248,'EX9-IL7'),('11A',249,'EX10'),('11B',250,'EX11')]:fig(n,p,'dose-response graph',[x],['AB-2a','AB-2b','Reference Antibody','Human IgG1 isotype','Human IgG2 isotype']+(['PRO-171101-004 [uncertain]'] if n=='10D' else []),'A450 vs antibody µg/mL (Figure 10) or % inhibition vs antibody µg/mL (Figure 11); exact fit estimates in Tables 12/13; no exact numeric raw points printed')
for n,p,ds in [('12A',251,'zero/first/second order model schematic'),('12B',252,'score sequence against predetermined threshold'),('13',253,'network computer-system schematic'),('14',254,'processor/memory/storage schematic')]:fig(n,p,'computational schematic',['CBO'],[],ds)
# Link every experiment participant supported by an actual record or figure.
seenpairs=set()
for m in D['measurements']:
 pair=(m['entity_id'],m['experiment_id'])
 if pair not in seenpairs:
  D['relations'].append({'subject_id':pair[0],'predicate':'tested_or_scored_in','object_id':pair[1],'evidence':m['evidence']});seenpairs.add(pair)
for f in D['figures']:
 for e in f['entity_ids']:
  for x in f['experiment_ids']:
   if (e,x) not in seenpairs:rel(e,'shown_in_experiment',x,f['page'],'FIG. '+f['figure']);seenpairs.add((e,x))
for n,(title,pages) in enumerate([('VH Amino Acid Sequences',[196]),('VL Amino Acid Sequences',[197]),('CDR Amino Acid Sequences as Determined by IMGT Numbering',[197,198]),('Heavy Chain Amino Acid Sequences',[198,199,200,201,202,203]),('Light Chain Amino Acid Sequences',[203,204]),('Self-association Properties',[205]),('TSLP Binding ELISA',[205]),('TSLP Blocking ELISA',[206]),('KinExA Binding Affinities',[206]),('Inhibition of TSLP binding',[207]),('Inhibition of TSLP-Mediated STAT5 Signaling',[207]),('TSLP Binding ELISA',[208]),('TSLPR/IL-7Rα BAF/3 Assay',[208]),('Example Sequences and Potts Model Scores',[209])],1):
 D['tables'].append({'table':'Table '+str(n),'title':title,'pages':pages,'evidence':[ev(pages[0],f'Table {n}. {title}')],'measurement_count':sum(m['evidence'][0].get('table')=='Table '+str(n) for m in D['measurements'])})
exec(open('work/augment_dataset.py').read())
# Maintain a reviewable result even if subsequent OCR/verification is interrupted.
D['sequences'].sort(key=lambda s:(s['seq_id_no'] or 10000,s['id']))
for m in D['measurements']:
 if m['evidence'][0].get('table'):m['evidence'][0]['quote']=m['evidence'][0]['quote'].replace('Reference Antibody','Reference')
json.dump(D,open('dataset.json','w'),ensure_ascii=False,indent=2)
processed=[n for n in range(1,264) if os.path.exists(f'work/ocr_{n:03}.json')]
R={'document':D['document'],'total_pages':263,'processed_pages':processed,'unread_or_failed_pages':[n for n in range(1,264) if n not in processed],'status':'in_progress','methods':['PyMuPDF text-layer check: 0 characters on every page','Local PyMuPDF rendering at 144 dpi and locally installed PaddleOCR PP-OCRv6_medium_det + PP-OCRv6_small_rec CPU models; no network or external API','Full-size visual reading of every result and sequence table; enlarged crops for critical characters; rotated figure pages','Literal sequence transcription, separately preserving chain, variable-domain, constant-domain, IMGT/Kabat/Chothia CDR, consensus-template and model-position definitions'],'counts':{k:len(v) for k,v in D.items() if isinstance(v,list)},'completeness_limits':['Still processing full-document OCR; see unread_or_failed_pages','Graphical raw data points are not printed as exact numbers and were not pointwise digitized','External XML sequence listing and model weight files referenced by the PDF were not accessed','Source inconsistencies and uncertain legend are retained in dataset.issues']}
json.dump(R,open('extraction_report.json','w'),ensure_ascii=False,indent=2)
print('Written',R['counts'],'OCR pages',len(processed))
