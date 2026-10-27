# 序列评估

`src/eval` 独立运行 ANARCI/HMMER 和本地 ESM-2。输入是最终 VH/VL，不修改序列、SEQ 归属、
表格 OCR、实验指标或测量值。主流程在 QuestionAgent/AnswerAgent 和有限修复结束后执行一次，
随后刷新记录一致性与复核状态。规则模式跳过模型评估。

```bash
PYTHONPATH=src python -m eval \
  --input /path/to/native-run/resolved_sequences.json \
  --output /path/to/evaluation
```

也可以把原生运行目录传给 `--input`。独立命令只生成评估文件，不覆盖输入运行，不调用 OCR
或 LLM。主流程可通过 `--no-sequence-evaluation` 关闭。

## ANARCI

使用 [ANARCI 官方实现](https://github.com/oxpig/ANARCI) 和 HMMER 批量识别重链 H、轻链 K/L，
输出 IMGT 编号、命中分数、覆盖率和 FR1/CDR1/FR2/CDR2/FR3/CDR3/FR4。区域划分遵循
[IMGT 定义](https://www.imgt.org/IMGTScientificChart/Nomenclature/IMGT-FRCDRdefinition.html)，
保留插入位置及缺口编号。链型相符且单个域覆盖至少 90% 时通过；实际运行后发现链型冲突、
无法编号、多域、覆盖不足或非法序列时，将 ANARCI 标红。环境缺失或执行失败仍为待复核，
与实际编号失败区分。HMM 推测的物种仅用于评估，不写入实验物种标签。

## ESM-2

使用 [ESM-2](https://github.com/facebookresearch/esm) 的 `esm2_t33_650M_UR50D` 本地权重。
每次仅掩码一个氨基酸位置，按原始残基的预测对数概率计算平均 masked PLL、
伪困惑度 `exp(-mean PLL)`、最低位点分数、最低 10% 均值和低分位置。
同批相同序列仅计算一次；不跨运行缓存评分。输入哈希绑定每条结果，防止序列变化后沿用旧评分。

默认按**单个位点**进行风险筛选。对第 i 个掩码位置，位点伪困惑度为
`exp(-log_probability_i) = 1 / P(原残基 | 其余序列)`；默认任意位点达到 **1000**
（预测概率 ≤ 0.001），该链及构建体的 ESM-2 标红，记录链、1-based 位置、原残基、概率与
位点伪困惑度。整条链的伪困惑度仍为所有位点的几何平均，保留展示，但不会掩盖单点异常。
完整位置评分存入原生 JSON，网页只传递高风险位点和最低分位置，避免重复发送整个分数数组。

1000 是可调整的初筛参数，未作为经过本数据集校准的错误判定标准；红色表示模型风险，不能
单凭模型评分确认原序列有误，也不会自动替换低分残基。无位点超过阈值表示通过当前筛选。
用 `DATASETAGENT_ESM2_SITE_MAX_PPL` 调整全局阈值，或用
`DATASETAGENT_ESM2_VH_SITE_MAX_PPL`、`DATASETAGENT_ESM2_VL_SITE_MAX_PPL` 分别配置链。
用 `DATASETAGENT_ESM2_SITE_SCREEN_ENABLED=0` 关闭位点筛选，仅记录分数或应用平均分阈值。

如已有标注集确定的整链平均分阈值，可分别配置 VH/VL 的
`DATASETAGENT_ESM2_VH_PASS_PLL`、`DATASETAGENT_ESM2_VH_FAIL_PLL`、
`DATASETAGENT_ESM2_VL_PASS_PLL`、`DATASETAGENT_ESM2_VL_FAIL_PLL`。
每条链必须同时提供 pass/fail，且 fail < pass；两者之间待复核。位点风险优先于平均分通过。
ESM-2 评分衡量序列在模型下的合理性，不证明抗原结合活性或抽取忠实度。

## 本地环境

主程序保持轻量；两个 worker 可用独立 Python 环境。优先使用显式环境变量，随后发现同级
`envs/ab-data-validator`（ANARCI）、`envs/AbFlow`（ESM-2）和本地 Hugging Face 权重目录。
不会自动下载模型。配置项：

| 环境变量 | 用途 |
| --- | --- |
| `DATASETAGENT_ANARCI_PYTHON` | 安装了 ANARCI/Biopython 的 Python |
| `DATASETAGENT_HMMER_DIR` | 含 `hmmscan` 的目录 |
| `DATASETAGENT_ANARCI_CPUS` | HMMER CPU 数，默认 1 |
| `DATASETAGENT_ESM2_PYTHON` | 安装了 PyTorch/Transformers 的 Python |
| `DATASETAGENT_ESM2_MODEL` | 本地权重目录 |
| `DATASETAGENT_ESM2_DEVICE` | 默认 `cuda`，也可指定 `cuda:0` 或 `cpu` |
| `DATASETAGENT_ESM2_BATCH_SIZE` | 每批掩码位置数，默认 32 |
| `DATASETAGENT_ESM2_TIMEOUT_SECONDS` | worker 超时，默认 1800 秒 |
| `DATASETAGENT_ANARCI_ENABLED` / `DATASETAGENT_ESM2_ENABLED` | 默认自动发现；设为 `0` 关闭对应工具 |

环境缺失、超时或执行错误属于“未完成评估”，保留运行原因；不会伪装成通过，也不会据此
修正残基。当前抽取环境的轻量构建体检查仍可运行，最终评估通过独立 worker 完成。

## 产物

- `sequence_evaluation.json`：逐构建体、逐链结果、输入哈希、计数、运行时间与环境。
- `anarci_results.json`：去重后的完整编号及区域。
- `esm2_results.json`：去重后的评分及低分位置。
- `sequence_evaluation.csv`：每条构建体链的状态、哈希和主要数值。
- `*_input.json` / `*_stderr.log`：worker 输入快照及运行日志。

原生运行的 `eval/` 保存完整结果。SDA 导出及 Studio 保留实体对应的评估详情，网页“复核状态”
显示 ANARCI 结果和 ESM-2 已评分状态，详情展示各链区域和数值。原有来源冲突独立保留。
