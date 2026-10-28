#!/usr/bin/env python3
"""Create Chinese findings, threshold sweep, examples and shareable figures."""
import argparse
from collections import Counter,defaultdict
import csv
import json
import math
from pathlib import Path
import sys
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
import benchmark as b

LABELS={'clean':'未注错真实序列','random_sub1':'随机单点替换','conservative_sub1':'保守单点替换',
        'ocr_like_sub1':'形近单点替换','random_sub3':'同区域三点替换','delete1':'单残基删除',
        'insert1':'单残基插入','anchor_23':'C23锚点替换','anchor_41':'41位锚点替换',
        'anchor_104':'C104锚点替换','anchor_118':'118位锚点替换','truncate_N5':'N端截去5残基',
        'truncate_N20':'N端截去20残基','invalid_O':'非法字母O','wrong_chain_label':'VH/VL标签颠倒',
        'valid_sequence_swap':'换成另一条真实同链序列'}

def fmt(rate):return '—' if rate is None else f'{100*rate:.1f}%'
def main():
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=b.ROOT/'results/pilot_20261027')
    args=p.parse_args();out=args.out.resolve()
    if not out.is_relative_to(b.ROOT):raise ValueError('Output must stay in PPL')
    with (out/'per_sequence.csv').open() as f:raw=list(csv.DictReader(f))
    bools=['anarci_flag','esm_scored','esm_site1000_flag','combined_flag','esm_site_cal95_flag','esm_mean_cal95_flag','parent_site1000_flag','new_site1000_flag']
    for r in raw:
        for k in bools:r[k]=r[k]=='True'
        for k in ['mean_nll','max_site_nll','delta_mean_nll']:r[k]=float(r[k]) if r[k] else None
        r['localized_hit']=None if not r['localized_hit'] else r['localized_hit']=='True'
    rows=[r for r in raw if r['split']=='test']
    summary=json.loads((out/'summary.json').read_text());overall={x['kind']:x for x in summary['overall']}
    manifest=json.loads((out/'dataset_manifest.json').read_text())
    thresholds=json.loads((out/'calibrated_thresholds.json').read_text())
    # Same model outputs, fixed thresholds, no extra model inference.
    sweep=[]
    for chain in ['ALL','VH','VL']:
        for kind in LABELS:
            g=[r for r in rows if r['kind']==kind and r['esm_scored'] and (chain=='ALL' or r['chain']==chain)]
            if not g:continue
            for t in [100,300,1000,3000,10000,30000,100000]:
                k=sum(r['max_site_nll']>=math.log(t) for r in g)
                sweep.append({'chain':chain,'kind':kind,'threshold_site_ppl':t,'n':len(g),'flagged':k,'rate':k/len(g)})
    b.csv_out(out/'threshold_sweep.csv',sweep)
    # Bootstrap whole original pairs to retain the dependence among variants.
    boot=[];rng=np.random.default_rng(20261027)
    for fields in [('kind',),('kind','region')]:
        groups=defaultdict(list)
        for r in rows:groups[tuple(r[k] for k in fields)].append(r)
        for key,g in groups.items():
            pairs=sorted({r['pair_id'] for r in g});draws=rng.integers(0,len(pairs),size=(2000,len(pairs)))
            for metric in ['combined_flag','esm_site1000_flag','localized_hit']:
                eligible=[r for r in g if (r['esm_scored'] or metric=='combined_flag') and r[metric] is not None]
                if not eligible:continue
                counts=np.array([sum(r[metric] for r in eligible if r['pair_id']==pair) for pair in pairs])
                ns=np.array([sum(r['pair_id']==pair for r in eligible) for pair in pairs])
                den=ns[draws].sum(1);valid=den>0
                estimates=counts[draws].sum(1)[valid]/den[valid]
                low,high=np.quantile(estimates,[.025,.975])
                boot.append({'grouping':'+'.join(fields),'kind':key[0],'region':key[1] if len(key)>1 else 'ALL',
                             'metric':metric,'source_pairs':len(pairs),'rows':len(eligible),
                             'rate':counts.sum()/ns.sum(),'pair_bootstrap95_low':float(low),'pair_bootstrap95_high':float(high)})
    b.csv_out(out/'pair_cluster_bootstrap.csv',boot)
    # Baseline flags by original ANARCI region, preserving all flagged sites.
    items=b.load_lines(out/'sequences.jsonl');ar={r['id']:r['review'] for r in b.load_lines(out/'anarci_scores.jsonl')}
    es={r['sha256']:r for r in b.load_lines(out/'esm_scores.jsonl')};es.update({r['sha256']:r for r in b.load_lines(out/'esm_fp32_boundary_overrides.jsonl')})
    sites=[];examples=[]
    for x in items:
        if x['kind']!='clean':continue
        rev=ar[x['id']]
        if rev['status']!='matched':continue
        mapping={q+1:(region,n,t) for region,ps in b.positions(rev,x['sequence']).items() for q,n,t in ps}
        for pos,v in enumerate(es[b.digest(x['sequence'])]['log_probs'],1):
            if -v<math.log(1000):continue
            region,n,t=mapping.get(pos,('UNMAPPED',0,''))
            sites.append({'id':x['id'],'split':x['split'],'chain':x['chain'],'position':pos,'region':region,
                          'imgt':str(n)+t,'residue':x['sequence'][pos-1],'site_ppl':math.exp(-v),
                          'source_row':x['source_row']})
    b.csv_out(out/'clean_flagged_sites.csv',sites)
    byid={x['id']:x for x in items}
    for kind in ['random_sub1','conservative_sub1','random_sub3','delete1','insert1','anchor_23','truncate_N20','valid_sequence_swap']:
        for flag in [False,True]:
            options=[r for r in rows if r['kind']==kind and r['esm_scored'] and r['combined_flag']==flag]
            if not options:continue
            r=options[0];x=byid[r['id']];parent=byid[x['parent_id']]
            examples.append({**r,'edits':x['edits'],'original_sequence':parent['sequence'],'evaluated_sequence':x['sequence']})
    b.dump(out/'examples.json',examples)
    kinds=['random_sub1','conservative_sub1','ocr_like_sub1','random_sub3','delete1','insert1']
    fig,axes=plt.subplots(1,3,figsize=(15,4.9),layout='constrained')
    for ax,metric,title in zip(axes,['esm_site1000_flag','localized_hit','esm_mean_cal95_flag'],
                                ['Any site PPL >= 1000','Edited site / deletion junction hit','Chain mean PPL: clean-calibrated 95%']):
        mat=[]
        for kind in kinds:
            line=[]
            for region in b.REGIONS:
                g=[r for r in rows if r['kind']==kind and r['region']==region and r[metric] is not None]
                line.append(sum(r[metric] for r in g)/len(g) if g else 0.)
            mat.append(line)
        im=ax.imshow(np.array(mat)*100,vmin=0,vmax=100,cmap='YlOrRd')
        ax.set_xticks(range(7),b.REGIONS,rotation=45);ax.set_yticks(range(6),kinds,fontsize=9)
        ax.set_title(title,fontsize=10)
        for i,line in enumerate(mat):
            for j,v in enumerate(line):ax.text(j,i,f'{v*100:.0f}',ha='center',va='center',fontsize=8,color='white' if v>.65 else 'black')
    fig.colorbar(im,ax=axes,label='Detection / localization (%)',shrink=.8)
    fig.suptitle('Synthetic extraction errors: region-wise ESM2 screening (pilot)')
    fig.savefig(out/'region_detection.png',dpi=190);fig.savefig(out/'region_detection.pdf');plt.close(fig)
    # ROC-like operating points, with actual proxy false alarms on test clean chains.
    fig,ax=plt.subplots(figsize=(7,5),layout='constrained')
    for kind in ['random_sub1','random_sub3','delete1','insert1']:
        ts=[100,300,1000,3000,10000,30000,100000]
        def get(k,t):return next(r['rate'] for r in sweep if r['chain']=='ALL' and r['kind']==k and r['threshold_site_ppl']==t)
        xs=[100*get('clean',t) for t in ts];ys=[100*get(kind,t) for t in ts]
        ax.plot(xs,ys,'o-',label=kind)
        i=ts.index(1000);ax.annotate('1000',(xs[i],ys[i]),fontsize=8)
    ax.set(xlabel='Unedited real test chains flagged (%)',ylabel='Synthetic error chains flagged (%)',
           title='Site threshold trade-off (small pilot; real controls assumed correct)',xlim=(-2,102),ylim=(-2,102))
    ax.grid(alpha=.2);ax.legend();fig.savefig(out/'threshold_tradeoff.png',dpi=190);plt.close(fig)
    report=['# ANARCI + ESM2：哪些序列识别错误能被发现？', '',
      '本轮实测的结论是：ANARCI 主要提供链型/可编号性检查；ESM2 能筛出一部分不符合序列上下文的错误，但既会漏掉合理替换，也会把原始真实序列标成风险。两者都不能证明序列与文献原图一致。', '',
      '## 数据和实验范围','',
      f"扫描真实 OAS 源 **{manifest['source_rows']:,} 对**，固定 seed=20261027 水塘抽样，去除抽中的重复链后保留 **{manifest['sample_pairs']} 对 / {manifest['clean_chains']} 条链**。其中32对用于清洁阈值校准，15对为未参与阈值选择的测试父序列；在测试父序列上构造 **{manifest['synthetic_errors']:,} 个含错样本**。这是小规模 pilot，没有对250万条序列全部进行模型推理。",'',
      '真实源路径、大小、修改时间、全量物种计数及抽样序列哈希见 [dataset_manifest.json](results/pilot_20261027/dataset_manifest.json)。源为 `AirGen-Dev/data/OAS/op_base/cleaned_merged_data_step_clustered_train_oas_label.csv`。名字含2500091的另一文件属于生成轻链的虚拟数据，本轮未采用。', '',
      '注错区域按原始序列 ANARCI/IMGT 编号定位，包含插入编号；基线编号与原残基逐一核对。ANARCI 沿用 SDA 当前规则；ESM2 使用本地650M模型，精确逐位单掩码，记录整链与每个位点概率。', '',
      '## 当前规则的实测检出率','',
      '下表中 ESM2 使用“任意位点 PPL≥1000”。联合表示 ANARCI 报错或 ESM2 报警。未注错对照行的报警率应视为代理误报率，其他行是含错链被标记的比例；定位率另列，避免原序列自身异常被算作找到新错误。','',
      '| 情况 | 样本数 | ANARCI | ESM2位点1000 | 联合 | 命中错误位置 |',
      '|---|---:|---:|---:|---:|---:|']
    for kind,label in LABELS.items():
        x=overall[kind]
        report.append(f"| {label} | {x['n']} | {fmt(x['anarci_flag_rate'])} | {fmt(x['esm_site1000_flag_rate'])} | {fmt(x['combined_flag_rate'])} | {fmt(x['localized_rate'])} |")
    base=overall['clean']
    test_sites=[s for s in sites if s['split']=='test']
    native_pattern=Counter((s['chain'],s['region'],s['imgt'],s['residue']) for s in test_sites)
    repeated='；'.join(f'{chain}/{region}/IMGT{num}{aa}：{n}个位点' for (chain,region,num,aa),n in native_pattern.most_common(3))
    report+=['',f"未注错测试链有 **{base['esm_site1000_flag_count']}/{base['n']}（{fmt(base['esm_site1000_flag_rate'])}）**被当前ESM2阈值标记。因此不能把1000当作已校准的“识别错误”阈值，更不能把上表含错链报警率直接当作新增错误检出率。原始链已经报警时，再注入错误后仍报警，并不说明新增错误被定位。",'',
      f'对照中的报警也不只发生在CDR。本轮重复出现的位置包括：{repeated}。这是原始OAS序列在当前模型下的实际分数，不能据此判断文献识别错误；应进一步按链型/家族/编号位置核查模型偏差和源数据质量。', '',
      '## CDR 与 FR 的差异','',
      '每个区域、每类错误共30个样本（VH/VL各15）。数值是本轮实测，不代表所有抗体家族。','',
      '| 区域 | 随机单替换 | 保守单替换 | 形近单替换 | 三替换 | 删除1 | 插入1 |',
      '|---|---:|---:|---:|---:|---:|---:|']
    for region in b.REGIONS:
        vals=[]
        for kind in kinds:
            g=[r for r in rows if r['kind']==kind and r['region']==region]
            vals.append(fmt(sum(r['esm_site1000_flag'] for r in g)/len(g)))
        report.append('| '+region+' | '+' | '.join(vals)+' |')
    report+=['','![区域检出与定位](results/pilot_20261027/region_detection.png)','',
      '左图是整链是否出现报警，中央图只统计编辑残基/删除连接处是否报警，右图是用校准集确定阈值后的整链平均分报警。比较区域难度应同时看定位图；原有的其他区域报警可能抬高整链报警率。', '',
      '## 什么情况下更可能检出、什么情况下无能为力','',
      '- **非法字符与链型错标**：先做确定性的字母表检查、ANARCI链型检查。本轮非法O与VH/VL标签颠倒均能检出；O不能交给ESM2伪装成正常残基评分。',
      '- **破坏强上下文约束的替换或局部片段**：ESM2在原残基的条件概率很低时有机会报警。保守FR位点、锚点突变通常值得重点核对；具体检出率以上表为准，锚点改变仍可能通过ANARCI。',
      '- **CDR中的合理替换，尤其高多样性CDR3**：真实多样性会削弱“低概率=错误”的关系。错误若仍符合模型先验，可以完全漏过；真实稀有CDR也可能低概率。不能用正常常见残基自动替换罕见残基。',
      '- **单插入/删除与截断**：可能影响附近概率而被ESM2发现，也可能仍然很合理。删除残基本身没有可计算的概率。本轮N端截去5/20位全部通过ANARCI，说明当前90%覆盖率不能保证完整域；需结合IMGT起止编号与来源预期长度单列完整性检查。',
      '- **换成另一条合法同链序列、VH/VL配对或抗体归属错误**：单链序列模型没有文献身份信息。它们可能碰巧报警，但不是识别出了“归属错”。核验需要SEQ ID、构建体、图表行列与原始来源证据。',
      '- **能否恢复正确残基**：低分位置与模型推荐替换只是复核线索。检出、定位、恢复是三个任务，本轮测量前两者，没有把模型偏好当作纠错真值。', '',
      '## 阈值与误报的取舍','',
      '全链 PPPL = exp(mean(-log P_i))；位点 PPPL_i = 1/P_i。若长度120的链只有一个位置NLL增加6，其直接平均效应约为6/120=0.05，全链PPPL仅乘exp(0.05)≈1.051；其余位置也可能随上下文改变，实际整链变化见逐序列CSV。因此平均分可能稀释单点异常，而最大位点分又容易受真实稀有位点影响。', '',
      '另用校准集分别为VH/VL设置95%清洁分位数（严格大于阈值才报警），测试集不参与阈值选择。每条链型仅32个校准对照，下面只是比较分数选择，不能声称已控制到5%误报，更不能外推到1%。','',
      '| 情况 | 位点1000 | 校准max-site | 校准全链mean | 原链未报警时的新增位点报警 |',
      '|---|---:|---:|---:|---:|']
    for kind in ['clean']+kinds:
        x=overall[kind]
        report.append(f"| {LABELS[kind]} | {fmt(x['esm_site1000_flag_rate'])} | {fmt(x['esm_site_cal95_flag_rate'])} | {fmt(x['esm_mean_cal95_flag_rate'])} | {fmt(x['new_site1000_rate'])}（n={x['parent_passed_n']}） |")
    report+=['','![阈值取舍](results/pilot_20261027/threshold_tradeoff.png)','',
      '固定阈值扫描见 [threshold_sweep.csv](results/pilot_20261027/threshold_sweep.csv)，每个阈值都列出真实测试链报警率与注错链报警率；不根据测试集选择生产阈值。', '',
      '## 实际应用建议与限制','',
      '保留三级证据：字母/链型/域结构检查 → ESM2位点风险和定位 → 回查原图/表/SEQ归属。建议将当前“ESM2标红”明确展示为“需回查的模型风险”。下一轮应使用人工确认的真实抽取错误，按链型、CDR/FR、错误类型、物种与家族分层；以可接受的整链误报率为约束，在独立校准集选阈值，再于隔离测试集报告召回率。', '',
      '本轮仅15个测试配对父样本，变体共享父序列；不能把1530个变体当作1530个独立抗体。按原始配对聚类的2000次bootstrap区间见 [pair_cluster_bootstrap.csv](results/pilot_20261027/pair_cluster_bootstrap.csv)。逐行Wilson区间仅作描述。抽样为真实源自然行权重，未做到谱系隔离，模型预训练数据也可能重叠。OAS原序列是假定正确对照，可能本身含测序异常或截断，故这里的误报是代理估计。', '',
      '单点“保守替换”按粗化学分组定义，人工形近替换表不等于实际OCR错误分布；三替换是同区域的三处替换，未要求相邻。没有测整段CDR互换、复杂多字符OCR错误及所有长indel，也没有证明任何功能变化或结合能力。', '',
      '## 产物与验证','',
      '- [逐序列评分与编辑定位](results/pilot_20261027/per_sequence.csv)、[按错误/区域/链型汇总](results/pilot_20261027/detection_summary.csv)、[原始真实序列的报警位置](results/pilot_20261027/clean_flagged_sites.csv)、[漏检/检出实例](results/pilot_20261027/examples.json)。',
      '- [实验脚本](benchmark.py)、[数据及边界检查脚本](audit.py)、[报告/作图脚本](report.py)、[复现说明](README.md)。原序列、注错序列与完整逐位分数均在结果目录。',
      '- 数据检查验证唯一ID、精确链隔离、注错重建、IMGT区域与替换组。推理将不同序列放入同一batch但逐副本只掩一个位置；与SDA原实现做数值对照。运行使用FP32参数并允许A100 TF32矩阵乘法加速，未掩码或多位掩码近似均未采用。边界样本回用禁用TF32的原实现复算，数值细节见 [numerical_validation.json](results/pilot_20261027/numerical_validation.json) 和 [audit.json](results/pilot_20261027/audit.json)。', '',
      '依据：[ANARCI官方实现](https://github.com/oxpig/ANARCI)、[IMGT区域与锚点定义](https://www.imgt.org/IMGTScientificChart/Nomenclature/IMGT-FRCDRdefinition.html)、[ESM官方变异评分实现](https://github.com/facebookresearch/esm/blob/main/examples/variant-prediction/predict.py)。具体检出率均来自本地实验，不由工具文档推断。','']
    (b.ROOT/'REPORT_zh.md').write_text('\n'.join(report))
    b.dump(out/'report_manifest.json',{'report':'PPL/REPORT_zh.md','bootstrap_seed':20261027,'bootstrap_replicates':2000,
                                     'test_pair_count':len({r['pair_id'] for r in rows}),'calibration_thresholds':thresholds})
    print('Report, figures, examples, threshold sweep and pair bootstrap saved in PPL.')

if __name__=='__main__':main()
