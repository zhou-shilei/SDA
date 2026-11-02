# Executed by the builder after core tables and sequences have been assembled.
D['document'].update(applicant='FLAGSHIP PIONEERING INNOVATIONS V, LLC',international_publication_date='2024-01-25',corrected_version_publication_date='2024-11-14')
# Broader claims are kept as definitions rather than empirical measurements.
consensus_general={1:'IV',2:'WS',3:'DS',4:'STYA',5:'NYDT',6:'KTI',7:'AST',8:'AS',9:'LDES',10:'VI',11:'HFY',12:'AS',13:'FML',14:'IV',15:'NY',16:'LI',17:'SR',18:'KFY',19:'SN',20:'VI',21:'WY',22:'DVS',23:'SME',24:'SAT',25:'SDE',26:'DSRF',27:'HLKE'}
D['sequence_constraints']=[{'sequence_ids':['SEQ-2','SEQ-18','SEQ-32','SEQ-47','SEQ-58','SEQ-68'],'kind':'consensus placeholder alternatives','variables':{'X'+str(k):list(v) for k,v in consensus_general.items()},'scope':'Claim 6 (VH) and Claim 8 (VL); specific narrower embodiments are alternatives, not extra measured variants','evidence':[ev(218,'X1 is I or V; X2 is W or S; X3 is D or S; X4 is S, T, Y or A'),ev(219,'X13 is F, M or L; X14 is I or V; X15 is N or Y; X16 is L or I'),ev(220,'X22 is D, V or S; X23 is S, M or E; X24 is S, A or T; X25 is S, D or E; X26 is D, S, R or F; X27 is H, L, K or E')]}]
# Regions explicitly printed in multiple naming systems are not merged.
D['claimed_properties']=[{'scope':'disclosed polypeptides; embodiments, not measured records','properties':['TSLP binding KD ≤10 pM (KinExA)','binding specificity to AB-loop region and C-terminal region of helix D','neutralization of TSLP and inhibition of TSLP-mediated signaling'],'evidence':[ev(4,'a binding affinity for a TSLP characterized by a KD of 10 pM or less; a binding specificity for the AB-loop region and the C-terminal region of helix D')]}, {'scope':'non-limiting embodiment ranges','properties':['TSLP-receptor blocking IC50 about 500 pM or less','TSLP biological-activity neutralization IC50 about 500 pM or less'],'evidence':[ev(104,'reduces binding of a TSLP protein to a TSLPR with an IC50 of about 500 pM or less'),ev(105,'neutralizes a TSLP-mediated activity with an IC50 of about 500 pM or less')]}]
# Explicitly mentioned experimental reagents and receptor components.
for n,role,p,q,kw in [
 ('goat anti-human IgG Fcγ capture antibody','AC-SINS capture reagent',186,'Polyclonal goat anti-human IgG Fcγ antibodies ... #109-005-098',{'vendor':'Jackson ImmunoResearch','catalog':'109-005-098'}),
 ('non-specific goat antibodies','AC-SINS non-capture reagent',186,'non-specific goat antibodies ... Catalog #005-000-003',{'vendor':'Jackson ImmunoResearch','catalog':'005-000-003'}),
 ('DELFIA Eu-N1 anti-human IgG','PSR detection antibody',185,'DELFIA ... Eu-N1 anti-human IgG secondary',{'vendor':'PerkinElmer'}),
 ('anti-human IgG Fc-HRP (Promega)','ELISA detection antibody',187,'secondary antibody (Promega, Madison, WI) at 1:2000 dilution',{'vendor':'Promega'}),
 ('Alexa Fluor647 AffiniPure goat anti-human IgG (H+L)','KinExA detection antibody',189,'Alexa Fluor647 AffiniPure goat anti-human IgG (H+L) secondary antibody',{}),
 ('anti-human IgG-HRP (Jackson)','Example 9 ELISA detection antibody',192,'ImmunoResearch Laboratories, Inc.) was added to wells at a 1:5000 dilution',{'vendor':'Jackson ImmunoResearch'}),
 ('human TSLPR','receptor subunit',190,'Human TSLP Receptor (CRLF2)',{'species':'human','aliases':['CRLF2','hTSLPR']}),
 ('human IL-7Rα','receptor subunit',190,'IL-7Rα (CD127)',{'species':'human','aliases':['CD127','hIL-7Rα']}),
 ('cynomolgus TSLPR','receptor subunit',194,'expressing cynomolgus monkey TSLP receptor and IL-7Rα',{'species':'cynomolgus monkey','aliases':['cynoTSLPR']}),
 ('cynomolgus IL-7Rα','receptor subunit',194,'cynoIL-7Rα-P2A-cynoTSLPR construct',{'species':'cynomolgus monkey','aliases':['cynoIL-7Rα']}),
 ('pcDNA3.1(+)/Hygro','expression vector',190,'cloned into a pcDNA3.1(+)/Hygro plasmid',{}),
 ('CHO','protein production host cell',185,'Chinese hamster ovary - CHO S cell line',{'species':'Chinese hamster','aliases':['CHO S']}),
 ('salmon sperm DNA','PSR non-specific target',185,'10 µg/mL salmon sperm DNA',{}),
 ('CBO validation sequence set (~100)','experimentally validated sequence collection; individual membership unspecified',127,'experimental validation of approximately 100 sequences',{})]:
 entity(n,role,p,q,**kw)
for a,b,p,q in [('hIL-7Rα-P2A-hTSLPR','human TSLPR',190,'hIL-7Rα-P2A-hTSLPR'),('hIL-7Rα-P2A-hTSLPR','human IL-7Rα',190,'hIL-7Rα-P2A-hTSLPR'),('cynoIL-7Rα-P2A-cynoTSLPR','cynomolgus TSLPR',194,'cynoIL-7Rα-P2A-cynoTSLPR'),('cynoIL-7Rα-P2A-cynoTSLPR','cynomolgus IL-7Rα',194,'cynoIL-7Rα-P2A-cynoTSLPR')]:rel(a,'encodes_component',b,p,q)
for a,p,q in [('hIL-7Rα-P2A-hTSLPR',190,'cloned into a pcDNA3.1(+)/Hygro plasmid'),('cynoIL-7Rα-P2A-cynoTSLPR',194,'cloned into a pcDNA3.1(+)/Hygro plasmid')]:rel(a,'cloned_into','pcDNA3.1(+)/Hygro',p,q)
for e,x,p,q in [('goat anti-human IgG Fcγ capture antibody','EX3',186,'pre-coat the gold nanoparticle'),('non-specific goat antibodies','EX3',186,'ratio between capture and non-capture antibodies'),('DELFIA Eu-N1 anti-human IgG','EX2',185,'DELFIA ... anti-human IgG secondary'),('salmon sperm DNA','EX2',185,'plate was coated with 10 µg/mL salmon sperm DNA'),('Alexa Fluor647 AffiniPure goat anti-human IgG (H+L)','EX6-h',189,'detected using Alexa Fluor647 AffiniPure goat anti-human IgG (H+L)'),('Alexa Fluor647 AffiniPure goat anti-human IgG (H+L)','EX6-cyno',189,'detected using Alexa Fluor647 AffiniPure goat anti-human IgG (H+L)'),('CHO','EX1',185,'expression host systems (such as the Chinese hamster ovary - CHO S cell line)')]:rel(e,'reagent_or_system_in',x,p,q)
exp('CBO-validation','Experimental validation used to generate Potts model','polyspecific reactivity, self-aggregation, expression, and SEAP reporter validation','anti-TSLP antibody function',None,'SEAP reporter assay; detailed system not stated','approximately 100 sequences; individual sequence identities, assay conditions and numeric outputs not supplied in this passage',127,'These sequences when assayed had acceptable polyspecific reactivity, self-aggregation propensity, and expression. Additionally, they all possessed biological function as determined by a SEAP reporter assay.')
for metric,v in [('polyspecific reactivity','acceptable'),('self-aggregation propensity','acceptable'),('expression','acceptable'),('biological function (SEAP reporter)','all possessed biological function')]:meas('CBO validation sequence set (~100)','CBO-validation',metric,v,None,'aggregate qualitative statement for approximately 100 sequences',127,'These sequences when assayed had acceptable polyspecific reactivity, self-aggregation propensity, and expression. Additionally, they all possessed biological function as determined by a SEAP reporter assay.',result_kind='qualitative_aggregate')
rel('CBO validation sequence set (~100)','validated_in','CBO-validation',127,'experimental validation of approximately 100 sequences')
issue('unspecified_validation_members','The approximately 100 sequences underlying the Potts model are described only as an aggregate. The PDF does not enumerate their sequence identities, per-sequence assay outputs, or SEAP assay conditions; no individual records were fabricated.',127,'experimental validation of approximately 100 sequences')
issue('replicate_count_conflict','Example 9 [00792] first says three independent experiments, then gives N=4 for human TSLP and N=3 for cyno TSLP. The later antigen-specific N values are retained in experiment conditions with this conflict noted.',192,'three independent experiments ... N=4 independent experiments ... N=3 independent experiments')
for i in D['issues']:
 if i['type']=='score_scale_not_defined':
  i['type']='table_score_units_and_background_definition'
  i['description']='Table 6 does not print units. [00517] defines DNA PSR score as ELISA/DELFIA signal divided by secondary-antibody background; Example 2 describes division by buffer-only wells. AC-SINS is defined as an absorption maximum wavelength shift. The table values are preserved as scores without inventing unprinted units.'
  i['evidence'] += [ev(106,'A DNA PSR score is calculated by dividing an ELISA or DELFIA score by a background score of a secondary antibody alone'),ev(185,'dividing the raw signal by signal from buffer-only wells')]
# Additional original-source support and explicit uncertainty on figure/text mapping.
for x in D['experiments']:
 if x['id']=='EX5-cyno':x['receptor_species_uncertainty']='Paragraph [00777] says cyno TSLPR; Figures 7A/B label hTSLPR-IL7Ra. See issues.'
 if x['id']=='CBO':x['model_details']={'weights_file_as_printed':'fit_model.etab (external, not accessed)','reference_VH_sequence_id':'SEQ-3','reference_VL_sequence_id':'SEQ-19','VH_positions_as_printed':[25,27,29,31,50,51,53,55,56,57,96,98,100,101,102,103,104,105,106,107,108,110],'VL_positions_as_printed':[25,26,27,28,29,30,48,50,87,88,89,90,91,92,93,94,95,96,97],'position_indexing':'Appendix C Python list positions; retain as printed, do not relabel as antibody numbering','threshold_as_printed':'-7.7'};x['evidence'].append(ev(210,'THRESHOLD = -7.7; VH_POSITIONS = [25, 27, 29, 31, 50, 51, 53, 55, 56, 57, 96, 98, 100, 101, 102, 103, 104, 105, 106, 107, 108, 110]'))
for e in D['entities']:
 if e['id']=='AB-2a':e['experimental_lots']=[{'lot':'REQ0000500','evidence':[ev(192,'AB-2a (Lot# REQ0000500)')]}]
 if e['id']=='AB-2b':e['experimental_lots']=[{'lot':'1098-260922-02','production':'GFA-002','evidence':[ev(192,'AB-2b (lot#1098-260922-02, production#GFA-002)')]}]
 if e['id']=='Reference Antibody':e['experimental_lots']=[{'lot':'1143666','serial':'100000005714','GTIN':'00355513112017','evidence':[ev(192,'lot# 1143666, SN:100000005714, GTIN:00355513112017')]}]
 if e['id']=='Human IgG1 isotype':e.update(vendor='BioLegend',catalog='403502',lot='B372653');e['evidence'].append(ev(192,'IgG1 isotype (BioLegend, Cat#403502, Lot# B372653)'))
 if e['id']=='Human IgG2 isotype':e.update(vendor='BioLegend',catalog='400264',lot='B341585');e['evidence'].append(ev(192,'IgG2 isotype (BioLegend, Cat#400264, Lot# B341585)'))
issue('identical_printed_sequence_ids','SEQ ID NO:87 and SEQ ID NO:88 are printed with identical amino-acid text in paragraphs [00382] and [00383]. Both numbered entries are retained separately; no intended mutation was inferred.',82,'CPAPPVAGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSHEDPEVQFNWYVDGVEVHN')
D['sequence_constraints'] += [
 {'sequence_ids':['SEQ-2'],'kind':'narrower consensus alternatives','scope':'Claim 7; alternative embodiment, do not combine with other constraints as an AND requirement','variables':{'X'+str(k):list(v) for k,v in {1:'V',2:'S',3:'S',4:'TYA',5:'YDT',6:'TI',7:'ST',8:'S',9:'DES',10:'I',11:'FY',12:'S',13:'ML',14:'V'}.items()},'evidence':[ev(219,'X1 is V; X2 is S; X3 is S; X4 is T, Y or A; X5 is Y, D or T; X6 is T or I; X7 is S or T; X8 is S; X9 is D, E or S; X10 is I; X11 is F or Y; X12 is S; X13 is M or L; X14 is V')]},
 {'sequence_ids':['SEQ-18'],'kind':'narrower consensus alternatives','scope':'Claim 9; alternative embodiment','variables':{'X'+str(k):list(v) for k,v in {15:'Y',16:'I',17:'R',18:'FY',19:'N',20:'I',21:'Y',22:'VS',23:'ME',24:'AT',25:'DE',26:'SRF',27:'LKE'}.items()},'evidence':[ev(220,'X15 is Y; X16 is I; X17 is R; X18 is F or Y; X19 is N; X20 is I; X21 is Y; X22 is V or S; X23 is M or E; X24 is A or T; X25 is D or E; X26 is S, R or F; X27 is L, K or E')]}]
issue('ELISA_method_label_conflict','The drawing descriptions call the binding assays direct ELISA, while Examples 4 and 9 describe an indirect ELISA with an anti-human IgG secondary antibody. Experiment methods follow the detailed protocols and preserve this terminology conflict.',13,'generated using a direct enzyme-linked immunosorbent assay (ELISA)')
for e in D['entities']:
 if e['id']=='Reference Antibody':e['evidence'].append(ev(13,'In the drawings, “Reference” refers to the Reference Antibody.'))
D['epitope_definitions'][0]['reference_antibody_annotation']={'entity_id':'Reference Antibody','evidence':[ev(13,'The epitope residues bound by the Reference Antibody disclosed herein are indicated by asterisks')]}
for s in D['sequences']:
 if s['seq_id_no']==71:s['numbering_scheme']=['IMGT','Kabat','Chothia'];s['evidence'] += [ev(121,'LCDR3: QIWDSSSSLVV (SEQ ID NO:71)'),ev(122,'LCDR3: QIWDSSSSLVV (SEQ ID NO:71)')]
 if s.get('sequence_kind')!='consensus_template':s['length']=len(s['sequence'])
for x in D['experiments']:
 if x['id']=='EX9-IL7':x['species']='human'
for s in D['sequences']:
 if s['seq_id_no']==1:s['accession_as_printed']='AAK67490.1'
D['claimed_properties'].append({'scope':'model verification assertion for AB-1–AB-14, including AB-9; conditional language preserved, not independently recomputed','entity_ids':[f'AB-{n}' for n in range(1,15)],'properties':['All proteins AB-1 through AB-14 were or would be verified by the script because their Potts model scores were above -7.7.'],'evidence':[ev(131,'All of the proteins AB-1 through AB-14 were or would be verified by the script because their Potts model scores were above -7.7.')]})
issue('Potts_table_reference_and_omitted_row','Paragraphs [00654]–[00655] refer to trimmed sequences/Potts scores as Table 10, but the actual score table is Table 14. The prose says AB-1–AB-14 were or would be verified, whereas Table 14 has 13 rows and no AB-9 numeric score. No AB-9 score was imputed.',130,'Table 10 illustrates the trimmed sequences (vlt, vht) ... resulting Potts model scores')
# Distinguish literal pairing statements from pairing by the shared unsuffixed family name.
for l in D['sequence_links']:
 e=l['entity_id']
 if e.endswith('b') and e.startswith('AB-') and l['region_type']=='full_light_chain':
  n=int(e[3:-1]);p=99 if n<=2 else 100 if n<=11 else 101
  q=f'heavy chain ... SEQ ID NO:{104+n}; light chain ... SEQ ID NO:{vlmap[n-1]+99} ({e})'
  l['evidence'].append(ev(p,q));l['assignment_status']='explicit heavy/light pairing in paragraphs [00468]–[00481]'
  if n==11:l['evidence'].append(ev(101,'light chain comprising the amino acid sequence of SEQ ID NO:126 (AB-11b)'))
 if e=='AB-2a' and l['region_type']=='full_light_chain':l['evidence'].append(ev(120,'heavy chain ... SEQ ID NO:92; light chain ... SEQ ID NO:120 (AB-2a)'));l['assignment_status']='explicit pairing'
 if re.fullmatch(r'AB-\d+a',e) and e!='AB-2a' and l['region_type'] in ['VL','full_light_chain']:
  l['assignment_status']='provisional shared-family-name assignment; a-suffixed light-chain pairing not individually stated'
  l['support']='Table 2/5 gives the unsuffixed AB-n light chain; [00389] defines a suffix by heavy-chain constant region. Do not treat this as an individually printed a-suffixed chain-pair statement.'
  l['evidence'].append(ev(83,'Antibodies disclosed herein using a designation “AB-[]a” ... comprise the heavy chain constant domain amino acid sequence of SEQ ID NO:84'))
issue('a_variant_light_chain_pairing_scope','Except AB-2a (explicit HC92/LC120), the a-suffixed VL/light-chain links use the shared AB-n family name of Tables 2/5 and the heavy-constant-region suffix definition. Those links are marked provisional because no individual a-suffixed heavy/light pairing statement was located. The b-suffixed pairs are explicit in [00468]–[00481].',83,'Antibodies disclosed herein using a designation “AB-[]a” ... comprise the heavy chain constant domain amino acid sequence of SEQ ID NO:84')
for r in D['relations']:
 if r['predicate']=='same_variable_region_design_as':r['predicate']='shares_VH_sequence_with';r['support']='literal Table 1 VH / Table 4 full-heavy-chain sequence match'
