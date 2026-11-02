#!/usr/bin/env python3
"""Combine three controlled Template inputs and the non-human PDF reference."""
import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(HERE.parent)]
from compare_text_ablation import entity_name, read
from dataagent2.inputs import load_input
from dataagent2.pipeline import question_tasks


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')


def build(prior, title_run, destination, replays=None):
    before = read(prior / 'comparison.json')
    title = read(title_run / 'comparison.json')
    if title['experiment']['status'] != 'complete' or title['experiment']['code_changed_during_run']:
        raise ValueError('Controlled title experiment must be complete with unchanged code')
    if before['source_pdf_provenance'] != title['source_pdf_provenance']:
        raise ValueError('Source/reference provenance differs')
    body = read(prior / 'with_text/controlled_input.json')['payload']['tables']
    captioned = read(title_run / 'with_titles/controlled_input.json')['payload']['tables']
    without_caption = lambda tables: [{k: v for k, v in t.items() if k != 'caption'} for t in tables]
    if without_caption(body) != without_caption(captioned):
        raise ValueError('Inputs differ beyond the title field')
    result = copy.deepcopy(before)
    result['arms'] = {'tables_only': before['arms']['tables_only'],
                      'with_titles': title['arms']['with_titles'],
                      'with_text': before['arms']['with_text']}
    result['title_experiment'] = title['experiment']
    result['methodology']['limits'][0] = 'One real model answer per input variant; prose answer reused, table-only and title calls run separately. No multi-run stochastic accuracy estimate.'
    result['methodology']['initial_dataset_goal'] = 'Preserve entity, sequence-ID ownership and measurement indexes for later chunk Readers; complete residue sequences are not a prerequisite at this stage.'
    result['title_validation_replay'] = None
    title_original = title_run / 'with_titles/dataset1.json'
    title_current = title_original
    if replays:
        corrected = read(replays / 'with_titles/dataset1.json')
        original = read(title_original)
        def content(value):
            if isinstance(value, dict):
                return {k: content(v) for k, v in value.items() if k not in (
                    'refs', 'evidence_refs', 'original_refs', 'annotations', 'sequences', 'sequence_links')}
            if isinstance(value, list):
                return [content(v) for v in value]
            return value
        for key in ('entities', 'observations', 'experiments'):
            if content(original[key]) != content(corrected[key]):
                raise ValueError('Caption validation replay unexpectedly changed ' + key)
        template = read(replays / 'with_titles/shared_seed/document_template.json')
        owners = {e['entity_id']: entity_name(e['canonical_name']) for e in template['entity_template']['entities']}
        core = {f'ab-{i}' for i in range(1, 15)} | {'reference antibody'}
        ref = read(Path(before['experiment']['reference']))
        expected = {(entity_name(l['entity_id']), l['region_type'], int(l['sequence_id'].split('-')[-1]))
                    for l in ref['sequence_links'] if l['region_type'] in ('VH', 'VL') and entity_name(l['entity_id']) in core}
        actual = {(owners[l['entity_id']], l['chain'], l['seq_id']) for l in template['sequence_links']
                  if l['scope'] == 'variable' and owners[l['entity_id']] in core}
        score = copy.deepcopy(result['arms']['with_titles'])
        original_count = score['core_variable_links']['retained_agreement']
        score['core_variable_links'].update(retained_agreement=len(expected & actual), retained_candidates=len(actual),
                                          missing=[list(x) for x in sorted(expected - actual)])
        score['total_links'] = len(corrected['links'])
        score['host_followup_questions'] = len(question_tasks(corrected, load_input(replays / 'with_titles/shared_seed')))
        # Check that the added rule did not alter either earlier control arm.
        control_checks = {}
        for name in ('tables_only', 'with_text'):
            previous, current = read(prior / name / 'dataset1.json'), read(replays / name / 'dataset1.json')
            control_checks[name] = all(previous[key] == current[key] for key in ('entities', 'links', 'observations', 'experiments'))
        if not all(control_checks.values()):
            raise ValueError('Earlier control arms changed under the updated validator')
        result['arms']['with_titles'] = score
        result['title_validation_replay'] = {'original_core_links': original_count,
            'corrected_core_links': len(expected & actual), 'original_total_links': len(original['links']),
            'corrected_total_links': len(corrected['links']), 'model_calls': 0,
            'control_arms_unchanged': control_checks,
            'policy': 'Same saved model answers, identical input evidence. Recover a missing supplied same-table caption citation only when it proves chain/scope with the cited same-row name and exact SEQ ID. Explicit comma lists preserve membership without merging identities. Caption text is retained as a Reader source; prior cell IDs are unchanged.',
            'replay_root': str(replays), 'helper_sha256': hashlib.sha256((ROOT / 'src/dataagent/sequence_caption_evidence.py').read_bytes()).hexdigest()}
        title_current = replays / 'with_titles/dataset1.json'
    copies = {'tables-only-dataset1.json': prior / 'tables_only/dataset1.json',
              'with-titles-dataset1.json': title_current,
              'with-text-dataset1.json': prior / 'with_text/dataset1.json',
              'full-pdf-model-reference.json': Path(before['experiment']['reference'])}
    if replays:
        copies['with-titles-original-validation-dataset1.json'] = title_original
    packages = {filename: read(path) for filename, path in copies.items()}
    packages['comparison.json'] = result
    html = (HERE / 'comparison.html').read_text()
    replacements = {
        '49-Sructure · 有无正文的三组数据对照': '49-Sructure · 表标题能否建立初级 Dataset',
        '/ 三组数据对照': '/ 表标题实验',
        '少读正文，<br>会少得到哪些信息？': '补上表标题，<br>能否形成初级 Dataset？',
        '相同的 28 张表，一组不提供正文，一组加入相关正文。两组都停在 Reader 之前，与此前完整 PDF 提取结果逐项对照。': '同样的 28 张表，比较仅表格、表格＋标题、表格＋正文三种输入；以初级 Dataset 的索引和证据为重点，另附完整 PDF 模型参考。',
        '查看三组结果': '查看四组对照',
        '三个 Dataset，并排看。': '三种初级 Dataset，加一份完整 PDF 参考。',
        '三份 Dataset 都可以下载查看。': '三组初级 Dataset 与参考都可以下载。',
        '第三组由用户指定': '参考组由用户指定',
        '两组共用同一套校验和导出器': '三组共用同一套校验和导出器',
        '先读表格 → 列出缺少的语义字段': '先读表格＋标题 → 列出缺少的语义字段',
    }
    for old, new in replacements.items():
        html = html.replace(old, new)
    html = re.sub(r'<aside class="experiment-note">.*?</aside>', '<aside class="experiment-note"><strong>只新增标题组的一次真实模型调用。</strong>单元格与前两组一致，只增加 14 个打印表标题，共 554 字符。标题 1 的 OCR「Ví」经原图核对转写为 VH；没有正文、方法段落、脚注，也没有补造续页标题。原规则结果先冻结，随后如有标题联合校验修正，使用同一份回答重新处理，额外模型调用为 0。</aside>', html, count=1, flags=re.S)
    html = html.replace('两组都复用已有 OCR，本次没有重新读图或重做 PDF OCR。', '三组复用已有表格 OCR；标题组另核对了 14 个标题原图区域。未重做整篇 OCR。')
    html = html.replace('初始 Dataset 中没有序列定义，完整序列配对为 0，并不等于所有序列映射都没被模型看懂；还要分别看原始回答和校验后保留量。', '初级 Dataset 不必已有完整氨基酸序列；这里优先检查名称—编号、链类型和数值索引是否保留，序列正文由后续 chunk Reader 补齐。')
    proof = '<details class="evidence-details"><summary>查看这 14 个标题的 PDF 原图核对记录</summary><p class="fineprint">标题 1 的 H 是下标；转写为 VH。其余标题按原文保留，包括表 11 的 STAT5 标题，未按实验正文改写。</p><img style="display:block;width:100%;max-width:900px;height:auto;margin-top:15px" alt="14 个表标题的原 PDF 裁图，含页码" src="data:image/png;base64,' + base64.b64encode((HERE.parent / 'title_evidence_20261031/title-contact-sheet.png').read_bytes()).decode() + '"></details>'
    html = html.replace('<section class="chapter" id="quality">', proof + '<section class="chapter" id="quality">', 1)
    html = html.replace('<section class="chapter" id="decision">', '<aside class="error-note">标题也可能与真实实验读数存在冲突：Table 11 写的是 STAT5 signaling，而完整 PDF 参考按正文将其解释为 mDC CCL17 分泌抑制。标题适合建立初级索引；后续 Reader 仍应核对实验语义。</aside><section class="chapter" id="decision">', 1)
    html = html.replace('<div class="downloads">', '<p class="fineprint">标题组实验使用人工核对的 14 个打印标题；当前 v7 代码已接入表题解析并采用“表格＋标题”输入，但尚未再次调用模型。正文组沿用此前回答，仍受原始 VH OCR 与引用问题影响；当前数字不表示正文导致识别变差。</p><div class="downloads">', 1)
    pieces = {'STYLE': (HERE / 'style.css').read_text(), 'APP': (HERE / 'title_comparison.js').read_text(),
              'DATA': encode(result), 'DOWNLOADS': encode(packages)}
    html = re.sub(r'@@(STYLE|APP|DATA|DOWNLOADS)@@', lambda m: pieces[m[1]], html)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'title-comparison.html').write_text(html)
    (title_run / 'combined-comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    assets = destination / 'title-comparison-data'
    assets.mkdir(exist_ok=True)
    for filename, value in packages.items():
        (assets / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    index = destination / 'index.html'
    source = index.read_text()
    entry = '<aside id="title-comparison-entry" style="margin:0 0 18px;padding:16px 20px;border:1px solid #c6d7c0;border-left:4px solid #236b53;border-radius:5px;background:#edf2e6"><a href="title-comparison.html" style="font-weight:650;font-size:15px;text-decoration:none">新增标题实验：仅表格 / 表格＋标题 / 表格＋正文，能否形成初级 Dataset？ →</a><p style="font-size:12px;color:#67756b;margin-top:5px">14 个原图核对的标题，只增加 554 字符；查看模型识别、程序校验和初级数据集的实际差别。</p></aside>'
    if 'id="title-comparison-entry"' in source:
        source = re.sub(r'<aside id="title-comparison-entry".*?</aside>', lambda _: entry, source, count=1, flags=re.S)
    else:
        marker = '    <aside id="text-comparison-entry"'
        if marker not in source:
            marker = '    <section id="overview-view">'
        source = source.replace(marker, '    ' + entry + '\n' + marker, 1)
    index.write_text(source)
    for filename in ('text-comparison.html', 'template-audit.html'):
        page = destination / filename
        old = page.read_text()
        if 'id="title-comparison-link"' not in old:
            old = old.replace('<main>', '<main><aside id="title-comparison-link" style="margin-top:24px;padding:14px;background:#eaf0e3;border:1px solid #cfdcc8"><a href="title-comparison.html">新增：表格＋表标题的真实实验与四组对照 →</a></aside>', 1)
            page.write_text(old)
    print(destination / 'title-comparison.html')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--titles', type=Path, required=True)
    parser.add_argument('--destination', type=Path, default=HERE.parent / 'snapshots/49-Sructure')
    parser.add_argument('--replays', type=Path)
    args = parser.parse_args()
    build(args.prior.resolve(), args.titles.resolve(), args.destination.resolve(), args.replays.resolve() if args.replays else None)
