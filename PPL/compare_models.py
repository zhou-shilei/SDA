#!/usr/bin/env python3
"""Compare ANARCI/ESM2/Ophiuchus on identical frozen synthetic errors."""
from __future__ import annotations
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import sys
sys.dont_write_bytecode = True
import benchmark as b
from ophiuchus import targets
from report import LABELS, fmt

MODELS = ['esm2', 'ophiuchus_single', 'ophiuchus_paired']
NAMES = {'esm2': 'ESM2单链', 'ophiuchus_single': 'Ophiuchus单链', 'ophiuchus_paired': 'Ophiuchus配对条件'}
KINDS = ['random_sub1', 'conservative_sub1', 'ocr_like_sub1', 'random_sub3', 'delete1', 'insert1']
RULES = ['site1000', 'max_cal95', 'mean_cal95']


def rate(rows, metric):
    eligible = [r for r in rows if r.get(metric) is not None]
    return sum(r[metric] for r in eligible) / len(eligible) if eligible else None


def main():
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, default=b.ROOT / 'results/pilot_20261027')
    p.add_argument('--paired-fp16', action='store_true', help='Use separately validated FP16-autocast paired scores')
    args = p.parse_args(); out = args.out.resolve()
    if not out.is_relative_to(b.ROOT):
        raise ValueError('All outputs must stay in PPL')
    prefixes = {mode: f'ophiuchus_{mode}' + ('_fp16' if mode == 'paired' and args.paired_fp16 else '')
                for mode in ['single', 'paired']}
    items = b.load_lines(out / 'sequences.jsonl')
    clean = {x['id']: x for x in items if x['kind'] == 'clean'}
    anarci = {x['id']: x['review'] for x in b.load_lines(out / 'anarci_scores.jsonl')}
    assert set(anarci) == {x['id'] for x in items}
    # Validation must finish before results are published.
    for mode in ['single', 'paired']:
        validation = json.loads((out / f'{prefixes[mode]}_validation.json').read_text())
        assert validation['all_clean_controls_fp32']
        assert validation['all_sites_checked_at_calibrated_cutoff']
        assert validation['batch_vs_single_max_abs_logprob_diff'] < .001
    esm = {s['sha256']: s for s in b.load_lines(out / 'esm_scores.jsonl')}
    esm.update({s['sha256']: s for s in b.load_lines(out / 'esm_fp32_boundary_overrides.jsonl')})
    scores = {'esm2': {x['id']: esm.get(b.digest(x['sequence'])) for x in items}}
    for mode in ['single', 'paired']:
        mapping = targets(items, mode)
        cache = {s['cache_key']: s for s in b.load_lines(out / f'{prefixes[mode]}_scores.jsonl')}
        cache.update({s['cache_key']: s for s in b.load_lines(out / f'{prefixes[mode]}_fp32_overrides.jsonl')})
        assert not {x['cache_key'] for x in mapping.values()} - set(cache)
        scores['ophiuchus_' + mode] = {x['id']: cache[mapping[x['id']]['cache_key']] if x['id'] in mapping else None for x in items}
    thresholds = {}
    for model in MODELS:
        thresholds[model] = {}
        for chain in ['VH', 'VL']:
            cal = [scores[model][x['id']] for x in clean.values() if x['split'] == 'calibration' and x['chain'] == chain]
            assert all(cal)
            thresholds[model][chain] = {'n': len(cal), **{metric: float(np.quantile([s[metric] for s in cal], .95, method='higher'))
                                                        for metric in ['max_site_nll', 'mean_nll']}}
    b.dump(out / 'model_comparison_thresholds.json', thresholds)
    rows = []
    for x in items:
        parent = clean[x['parent_id']]
        for model in MODELS:
            s = scores[model][x['id']]; ps = scores[model][parent['id']]
            if set(x['sequence']) <= set(b.AA):
                assert s and s['sequence'] == x['sequence']
            assert ps and len(ps['log_probs']) == len(parent['sequence'])
            th = thresholds[model][x['chain']]
            for rule in RULES:
                t = math.log(1000) if rule == 'site1000' else th['max_site_nll' if rule == 'max_cal95' else 'mean_nll']
                def decision(z):
                    return z['max_site_nll'] >= t if rule == 'site1000' else z['max_site_nll' if rule == 'max_cal95' else 'mean_nll'] > t
                flags = ([i + 1 for i, v in enumerate(s['log_probs']) if (-v >= t if rule == 'site1000' else -v > t)]
                         if s and rule != 'mean_cal95' else [])
                affected = set(x['affected_positions'])
                top = sorted(range(1, len(x['sequence']) + 1), key=lambda i: s['log_probs'][i - 1]) if s else []
                flagged = decision(s) if s else None
                # Compare to the parent's own label-specific calibrated threshold.
                pth = thresholds[model][parent['chain']]
                pt = math.log(1000) if rule == 'site1000' else pth['max_site_nll' if rule == 'max_cal95' else 'mean_nll']
                pflag = ps['max_site_nll'] >= pt if rule == 'site1000' else ps['max_site_nll' if rule == 'max_cal95' else 'mean_nll'] > pt
                aflag = anarci[x['id']]['status'] != 'matched'
                rows.append({'id': x['id'], 'parent_id': parent['id'], 'pair_id': x['pair_id'], 'split': x['split'],
                             'chain': x['chain'], 'species': x['species'], 'v_call': x['v_call'],
                             'kind': x['kind'], 'region': x['region'], 'model': model, 'rule': rule,
                             'scored': s is not None, 'threshold_nll': t, 'flag': flagged,
                             'anarci_flag': aflag, 'combined_flag': aflag or bool(flagged),
                             'parent_flag': pflag, 'new_flag': flagged if s and not pflag else None,
                             'localized_hit': bool(affected & set(flags)) if affected and s and rule != 'mean_cal95' else None,
                             'top1_hit': bool(affected & set(top[:1])) if affected and s else None,
                             'top5_hit': bool(affected & set(top[:5])) if affected and s else None,
                             'mean_nll': s['mean_nll'] if s else None,
                             'max_site_nll': s['max_site_nll'] if s else None,
                             'pseudo_perplexity': s['pseudo_perplexity'] if s else None,
                             'delta_mean_nll': s['mean_nll'] - ps['mean_nll'] if s else None,
                             'flagged_positions': json.dumps(flags), 'affected_positions': json.dumps(sorted(affected)),
                             'source_row': x['source_row']})
    b.csv_out(out / 'model_comparison_per_sequence.csv', rows)
    test = [r for r in rows if r['split'] == 'test']
    summaries = []
    for extra in [(), ('region',), ('chain',), ('chain', 'region')]:
        fields = ('model', 'rule', 'kind') + extra
        groups = defaultdict(list)
        for r in test:
            groups[tuple(r[k] for k in fields)].append(r)
        for key, group in groups.items():
            summary = {'grouping': '+'.join(fields), 'chain': 'ALL', 'region': 'ALL', **dict(zip(fields, key)), 'n': len(group)}
            for metric in ['flag', 'combined_flag', 'localized_hit', 'top1_hit', 'top5_hit', 'new_flag']:
                eligible = [r for r in group if r[metric] is not None]
                k = sum(r[metric] for r in eligible); n = len(eligible)
                lo, hi = b.wilson(k, n)
                summary.update({metric + '_count': k, metric + '_n': n, metric + '_rate': k / n if n else None,
                                metric + '_wilson_low': lo, metric + '_wilson_high': hi})
            summaries.append(summary)
    b.csv_out(out / 'model_comparison_summary.csv', summaries)
    # Resample whole original pairs, retaining dependencies among their mutants.
    rng = np.random.default_rng(20261027); boots = []
    groups = defaultdict(list)
    for r in test:
        groups[(r['model'], r['rule'], r['kind'])].append(r)
    for (model, rule, kind), group in groups.items():
        pairs = sorted({r['pair_id'] for r in group})
        draws = rng.integers(0, len(pairs), (2000, len(pairs)))
        for metric in ['flag', 'localized_hit', 'new_flag']:
            eligible = [r for r in group if r[metric] is not None]
            if not eligible:
                continue
            counts = np.array([sum(r[metric] for r in eligible if r['pair_id'] == pair) for pair in pairs])
            ns = np.array([sum(r['pair_id'] == pair for r in eligible) for pair in pairs])
            den = ns[draws].sum(1); valid = den > 0
            lo, hi = np.quantile(counts[draws].sum(1)[valid] / den[valid], [.025, .975])
            boots.append({'model': model, 'rule': rule, 'kind': kind, 'metric': metric, 'parent_pairs': len(pairs),
                          'eligible_rows': len(eligible), 'rate': counts.sum() / ns.sum(), 'bootstrap95_low': float(lo), 'bootstrap95_high': float(hi)})
    b.csv_out(out / 'model_comparison_pair_bootstrap.csv', boots)
    # Which errors does adding Ophiuchus catch beyond the current ESM screen?
    indexed = {(r['id'], r['model'], r['rule']): r for r in test}
    complements = []
    for rule in RULES:
        for kind in ['clean'] + KINDS + ['anchor_23', 'anchor_41', 'anchor_104', 'anchor_118', 'truncate_N5', 'truncate_N20', 'valid_sequence_swap', 'wrong_chain_label']:
            group = [x for x in items if x['split'] == 'test' and x['kind'] == kind]
            for added in ['ophiuchus_single', 'ophiuchus_paired']:
                old = [indexed[(x['id'], 'esm2', rule)] for x in group]
                new = [indexed[(x['id'], added, rule)] for x in group]
                eligible = [(a, c) for a, c in zip(old, new) if a['scored'] and c['scored']]
                n = len(eligible)
                if not n:
                    continue
                union = sum(a['flag'] or c['flag'] for a, c in eligible)
                only = sum(not a['flag'] and c['flag'] for a, c in eligible)
                newloc = sum(c['localized_hit'] and not a['localized_hit'] for a, c in eligible
                             if c['localized_hit'] is not None and a['localized_hit'] is not None)
                complements.append({'kind': kind, 'rule': rule, 'added_model': added, 'n': n,
                                    'esm_flagged': sum(a['flag'] for a, _ in eligible), 'added_only_flagged': only,
                                    'union_flagged': union, 'union_rate': union / n,
                                    'added_only_localized': newloc,
                                    'both_parents_passed_n': sum(not a['parent_flag'] and not c['parent_flag'] for a, c in eligible),
                                    'union_new_flagged': sum((a['flag'] or c['flag']) and not a['parent_flag'] and not c['parent_flag'] for a, c in eligible)})
    b.csv_out(out / 'model_comparison_complementarity.csv', complements)
    workflows = []
    for rule in RULES:
        for kind in LABELS:
            group = [x for x in items if x['split'] == 'test' and x['kind'] == kind]
            for added in ['ophiuchus_single', 'ophiuchus_paired']:
                base_count = joint_count = new_count = eligible_count = 0
                for x in group:
                    a = indexed[(x['id'], 'esm2', rule)]; c = indexed[(x['id'], added, rule)]
                    baseline = a['combined_flag']; joint = baseline or c['combined_flag']
                    base_count += baseline; joint_count += joint
                    if not a['parent_flag'] and not c['parent_flag'] and anarci[x['parent_id']]['status'] == 'matched':
                        eligible_count += 1; new_count += joint
                workflows.append({'rule': rule, 'kind': kind, 'added_model': added, 'n': len(group),
                                  'anarci_esm_flagged': base_count, 'anarci_esm_rate': base_count / len(group),
                                  'anarci_esm_ophiuchus_flagged': joint_count, 'anarci_esm_ophiuchus_rate': joint_count / len(group),
                                  'parent_joint_passed_n': eligible_count, 'new_joint_flagged': new_count,
                                  'new_joint_rate': new_count / eligible_count if eligible_count else None})
    b.csv_out(out / 'model_comparison_joint_workflows.csv', workflows)
    sweep = []
    for model in MODELS:
        for kind in ['clean'] + KINDS:
            group = [r for r in test if r['model'] == model and r['kind'] == kind and r['rule'] == 'site1000' and r['scored']]
            for t in [100, 300, 1000, 3000, 10000, 30000, 100000]:
                sweep.append({'model': model, 'kind': kind, 'site_ppl_threshold': t, 'n': len(group),
                              'rate': sum(r['max_site_nll'] >= math.log(t) for r in group) / len(group)})
    b.csv_out(out / 'model_comparison_threshold_sweep.csv', sweep)
    # Real controls: preserve IMGT locations for model-specific false-alarm review.
    sites = []
    for x in clean.values():
        if anarci[x['id']]['status'] != 'matched':
            continue
        mapping = {q + 1: (r, str(n) + t) for r, ps in b.positions(anarci[x['id']], x['sequence']).items() for q, n, t in ps}
        for model in MODELS:
            s = scores[model][x['id']]; th = thresholds[model][x['chain']]['max_site_nll']
            for i, lp in enumerate(s['log_probs'], 1):
                if -lp < math.log(1000) and -lp <= th:
                    continue
                region, imgt = mapping.get(i, ('UNMAPPED', ''))
                sites.append({'id': x['id'], 'split': x['split'], 'chain': x['chain'], 'model': model,
                              'position': i, 'region': region, 'imgt': imgt, 'residue': x['sequence'][i - 1],
                              'site_ppl': math.exp(-lp), 'site1000': -lp >= math.log(1000), 'max_cal95': -lp > th})
    b.csv_out(out / 'model_comparison_clean_flagged_sites.csv', sites)
    byid = {x['id']: x for x in items}; examples = []
    for model in MODELS:
        for kind in KINDS + ['anchor_23', 'anchor_41', 'anchor_104', 'anchor_118', 'truncate_N5', 'truncate_N20', 'valid_sequence_swap']:
            for outcome in [False, True]:
                options = [r for r in test if r['model'] == model and r['rule'] == 'max_cal95'
                           and r['kind'] == kind and r['scored'] and r['flag'] == outcome]
                if not options:
                    continue
                options.sort(key=lambda r: (r['parent_flag'], r['id']))
                r = options[0]; x = byid[r['id']]; parent = clean[x['parent_id']]
                example = {**r, 'edits': x['edits'], 'original_sequence': parent['sequence'],
                           'evaluated_sequence': x['sequence'],
                           'affected_site_nll': {str(i): -scores[model][x['id']]['log_probs'][i - 1] for i in x['affected_positions']}}
                if model == 'ophiuchus_paired':
                    example['partner_sequence'] = targets([z for z in items if z['kind'] == 'clean'] + [x], 'paired')[x['id']]['partner_sequence']
                examples.append(example)
    b.dump(out / 'model_comparison_examples.json', examples)
    # English labels keep the exported figures independent of CJK font availability.
    fig, axes = plt.subplots(2, 3, figsize=(17, 9), layout='constrained')
    for col, model in enumerate(MODELS):
        for rownum, metric in enumerate(['flag', 'localized_hit']):
            ax = axes[rownum, col]
            mat = np.array([[100 * rate([r for r in test if r['model'] == model and r['rule'] == 'max_cal95'
                                         and r['kind'] == kind and r['region'] == region], metric)
                             for region in b.REGIONS] for kind in KINDS])
            im = ax.imshow(mat, vmin=0, vmax=100, cmap='YlOrRd')
            ax.set_xticks(range(7), b.REGIONS, rotation=45); ax.set_yticks(range(6), KINDS, fontsize=9)
            ax.set_title(model + (' / chain flagged' if rownum == 0 else ' / edited position hit'), fontsize=10)
            for (i, j), value in np.ndenumerate(mat):
                ax.text(j, i, f'{value:.0f}', ha='center', va='center', color='white' if value > 65 else 'black', fontsize=9)
    fig.colorbar(im, ax=axes, label='Rate (%)', shrink=.8)
    fig.suptitle('Independent VH/VL clean-calibrated max-site thresholds (small pilot)')
    fig.savefig(out / 'model_comparison_regions.png', dpi=180)
    fig.savefig(out / 'model_comparison_regions.pdf'); plt.close(fig)
    overall = {(s['model'], s['rule'], s['kind']): s for s in summaries if s['grouping'] == 'model+rule+kind'}
    report = ['# 加入 Ophiuchus-Ab：序列识别错误检出对照', '',
              '复用原实验冻结的47对真实OAS序列、32对校准/15对测试划分，以及1530个注错样本。没有重新挑选有利于某个模型的序列。', '',
              '## 评分和比较口径', '',
              '- **ESM2单链**：沿用原实验的精确逐位单掩码分数，读取FP32边界复算覆盖。',
              '- **Ophiuchus单链**：同一条链单独输入，同样逐位单掩码；用于输入相同的直接比较。此模式没有配对链上下文。',
              '- **Ophiuchus配对条件**：VH/VL联合输入，每次只掩目标链一个残基，配对链保留原始真实序列。仅对目标链残基取平均/最大值。此结果同时改变了模型和可用上下文，不能把改善全部归因于模型参数。',
              '- **PPPL** = exp(mean(-log p(x_i | x_其他位点)))；位点PPPL=1/p。使用模型完整33词表归一化，仅评分20种标准氨基酸，排除CLS/EOS/PAD。不把扩散模型的单掩码伪似然称为精确序列似然或扩散ELBO。',
              '- 每个模型、每种输入模式及VH/VL分别用原校准集最大位点NLL的95%分位数（higher）设阈值，严格大于才报警。另报告整链平均NLL阈值和固定的位点PPPL≥1000；1000只用于并排观察，不能视为跨模型等误报阈值。',
              '- 非法O不进入模型评分；链型标错由ANARCI判断。Ophiuchus配对输入使用生物学原链槽位，标签颠倒不改变分数，避免把标签错和重新排列输入混为一谈。', '',
              '## 未注错真实测试链的报警率', '',
              '| 模型/输入 | n | 位点1000 | 校准max-site | 校准整链mean |', '|---|---:|---:|---:|---:|']
    for model in MODELS:
        values = [overall[(model, rule, 'clean')] for rule in RULES]
        report.append(f'| {NAMES[model]} | {values[0]["n"]} | ' + ' | '.join(fmt(v['flag_rate']) for v in values) + ' |')
    report += ['', '上表是“假定正确”的OAS对照上的代理误报；原数据未人工逐条复核。32个配对校准样本和15个测试配对不足以证明生产环境5%误报控制。', '',
               '## 同一校准策略下的检出和定位', '',
               '每格依次为 **整链报警 / 命中至少一个编辑位置 / 原链未报警时的新增报警**。三点替换的定位只要求命中其中至少一处，不代表三个错误全部检出。第三项仅在该模型原链通过阈值时计入分母；不同模型分母可能不同，见CSV。删除定位按缺口紧邻残基，截断按新N端前4位；这两者是定位代理。', '',
               '| 错误类型 | n | ESM2单链 | Ophiuchus单链 | Ophiuchus配对条件 |', '|---|---:|---:|---:|---:|']
    for kind in [k for k in LABELS if k != 'clean']:
        values = [overall[(model, 'max_cal95', kind)] for model in MODELS]
        report.append(f'| {LABELS[kind]} | {values[0]["n"]} | ' + ' | '.join(
            ' / '.join(fmt(v[k]) for k in ['flag_rate', 'localized_hit_rate', 'new_flag_rate']) for v in values) + ' |')
    report += ['', '非法字符行的模型检出分母为0，因此显示“—”；ANARCI/字母表检查仍会报警。错误归属/合法同链序列错换即使偶然触发模型，也不代表识别出了归属错误。错换供体按原实验固定规则选择，存在供体重复，不能视为30个独立错配供体。配对条件下的错换会构成与原配对链的新组合，但本实验没有独立的配对分类器。', '',
               '![区域检出与定位对照](results/pilot_20261027/model_comparison_regions.png)', '',
               '上排为含错链报警，下排为编辑位置命中。每个错误类型/区域30条（VH/VL各15），变体来自相同的15个父配对。详细链型分层和所有区域值见汇总CSV。', '',
               '## 固定位点1000下的检出', '',
               '| 情况 | ESM2报警/定位 | Ophiuchus单链报警/定位 | Ophiuchus配对报警/定位 |', '|---|---:|---:|---:|']
    for kind in ['clean'] + KINDS + ['anchor_23', 'anchor_41', 'anchor_104', 'anchor_118', 'truncate_N5', 'truncate_N20']:
        values = [overall[(model, 'site1000', kind)] for model in MODELS]
        report.append(f'| {LABELS[kind]} | ' + ' | '.join(fmt(v['flag_rate']) + ' / ' + fmt(v['localized_hit_rate']) for v in values) + ' |')
    report += ['', '## 加入现有ANARCI流程后的整链报警', '',
               '本表采用各模型独立校准的max-site阈值，保留ANARCI的确定性检查。列中的“+Ophiuchus”表示在ANARCI+ESM2基础上再取OR。未注错行是联合规则的代理误报，不能忽略。', '',
               '| 情况 | n | ANARCI+ESM2 | 再加Ophiuchus单链 | 再加Ophiuchus配对 |', '|---|---:|---:|---:|---:|']
    for kind in LABELS:
        values = [next(w for w in workflows if w['rule'] == 'max_cal95' and w['kind'] == kind and w['added_model'] == model)
                  for model in ['ophiuchus_single', 'ophiuchus_paired']]
        report.append(f'| {LABELS[kind]} | {values[0]["n"]} | {fmt(values[0]["anarci_esm_rate"])} | {fmt(values[0]["anarci_esm_ophiuchus_rate"])} | {fmt(values[1]["anarci_esm_ophiuchus_rate"])} |')
    report += ['', '## Ophiuchus在ESM2之外增加了哪些报警？', '',
               '下表均采用各自独立校准的max-site阈值。新增报警指ESM2通过、Ophiuchus报警的样本；新增定位指编辑位置被Ophiuchus命中而ESM2未命中。联合报警使用OR，通常也会改变原序列报警率。', '',
               '| 情况 | 加入模型 | n | 额外报警条数 | 额外定位条数 | 两模型联合报警率 |', '|---|---|---:|---:|---:|---:|']
    for c in complements:
        if c['rule'] == 'max_cal95' and c['kind'] in ['clean'] + KINDS:
            report.append(f'| {LABELS[c["kind"]]} | {NAMES[c["added_model"]]} | {c["n"]} | {c["added_only_flagged"]} | {c["added_only_localized"]} | {fmt(c["union_rate"])} |')
    # Data-derived conclusions avoid assuming the antibody-specific model wins.
    report += ['', '## 本轮结果如何解读', '']
    for model in MODELS:
        g = [r for r in test if r['model'] == model and r['rule'] == 'max_cal95' and r['kind'] in KINDS]
        report.append(f'- {NAMES[model]}：六类区域注错共{len(g)}条，校准max-site下整链报警{fmt(rate(g,"flag"))}，编辑位置命中{fmt(rate(g,"localized_hit"))}；原链通过时新增报警{fmt(rate(g,"new_flag"))}（n={sum(r["new_flag"] is not None for r in g)}）。同一原链的变体相关，合并比例仅描述本实验构成。')
    report += ['- 判断某个区域是否容易检出，应优先看定位和新增报警，不能只看含错链报警率。罕见但真实的残基也可能低概率；符合模型先验的错误也可能漏过。',
               '- 替换会留下一个可被模型赋予低条件概率的残基；删除的残基本身已不存在，只能间接观察连接处是否异常。本协议的PPPL仅平均现有氨基酸位点，不计EOS概率或独立的完整长度概率。因此删除/截断即使违背来源中的完整序列，也可能保持低PPPL；完整性仍需额外检查IMGT域跨度和来源预期长度。',
               '- 单链模式与ESM2输入一致；配对模式假设另一个链已正确，真实应用若两条链均有错误或配对关系不确定，表现可能不同。合法同链换序列的30个控制不等同于系统性的配对错位验证。',
               '- ANARCI提供链型与编号约束，语言模型提供序列合理性线索。两者均缺乏文献原图/SEQ ID身份信息，仍需回查来源。当前结果不支持自动改写残基。',
               '- 真实OAS源与Ophiuchus训练数据可能重叠，本实验不是模型泛化或预训练污染的独立检验。清洁划分仅精确链去重，未做谱系隔离。', '',
               '## 数值验证和产物', '',
               'Ophiuchus严格加载全部checkpoint参数（无缺失/额外键），保存权重SHA256、源文件哈希、软件版本及输入快照哈希。批量精确掩码与单序列FP32结果对照；全部原始对照、位点1000附近及校准阈值附近的变体用禁用TF32的FP32复算。复核逐位检查校准阈值，包含非最大残基；各模式的位点/均值NLL复核余量见下表述，均不是统计筛选阈值。配对FP16在首轮审计发现最大位点差约0.0975，因此将位点复核余量从0.05扩大到0.2后重审。覆盖后的分数用于本报告。', '']
    for mode in ['single', 'paired']:
        v = json.loads((out / f'{prefixes[mode]}_validation.json').read_text())
        report.append(f'- {mode}：批量与单序列最大位点log-prob差={v["batch_vs_single_max_abs_logprob_diff"]:.3g}；FP32复算{v["fp32_sequences"]}条；加速推理（{v["fast_inference"]}）/FP32最大差={v["tf32_vs_fp32_max_abs_logprob_diff"]:.3g}；位点/均值NLL复核余量={v["boundary_margin_nll"]}/{v["mean_boundary_margin_nll"]}；位点1000判定修正{v["site1000_decision_changes"]}处。')
    report += ['', '- `model_comparison_per_sequence.csv`：所有模型、规则、原链状态、定位和完整来源索引。',
               '- `model_comparison_summary.csv`：错误类型/链型/区域分层，包含分子分母；逐行Wilson区间只作描述。',
               '- `model_comparison_pair_bootstrap.csv`：按原始配对聚类的2000次bootstrap区间。',
               '- `model_comparison_thresholds.json`、`model_comparison_threshold_sweep.csv`：校准阈值和固定阈值扫描；测试集未用于选阈值。',
               '- `model_comparison_complementarity.csv`、`model_comparison_clean_flagged_sites.csv`：互补检出与真实对照报警位置。',
               '- `model_comparison_joint_workflows.csv`：ANARCI+ESM2与加入Ophiuchus后的联合规则，包括非法字符、链型错标和原链通过时的新增报警。',
               '- `model_comparison_examples.json`：各模型检出/漏检的原序列、注错序列、编辑记录、伙伴及编辑位置分数。',
               '- `ophiuchus_*_scores.jsonl`及`*_fp32_overrides.jsonl`：逐位分数及精度覆盖；配对缓存包含两条序列和目标链。本报告实际采用的缓存前缀见`model_comparison_manifest.json`。',
               '- 所有代码、结果、日志位于PPL；主流程和AirGen文件仅只读复用。复现见`README_ophiuchus.md`。', '',
               '模型依据：[Ophiuchus-Ab官方实现](https://github.com/Ophiuchus-Team/Ophiuchus-Ab)。本报告的单掩码PPPL是用于本地对照的评分协议，检出率全部来自此处保存的实验结果。', '']
    (b.ROOT / 'REPORT_ophiuchus_zh.md').write_text('\n'.join(report))
    b.dump(out / 'model_comparison_manifest.json', {'models': MODELS, 'rows': len(rows), 'test_rows': len(test),
           'ophiuchus_cache_prefixes': prefixes,
           'bootstrap_seed': 20261027, 'bootstrap_replicates': 2000,
           'report': 'PPL/REPORT_ophiuchus_zh.md', 'sequence_manifest_sha256': b.digest((out / 'sequences.jsonl').read_text()),
           'overall': [s for s in summaries if s['grouping'] == 'model+rule+kind']})
    print('Ophiuchus comparison report, figures and CSVs saved in PPL.')


if __name__ == '__main__':
    main()
