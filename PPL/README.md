# ANARCI + ESM2 序列识别错误检出实验

所有新增代码、抽样快照、评分、日志和报告均保存在此目录。只读复用 `../src/eval/anarci.py` 的现有判定，未修改 SDA 主流程。

真实数据：`AirGen-Dev/data/OAS/op_base/cleaned_merged_data_step_clustered_train_oas_label.csv`。项目统计为 2,486,442 个真实 OAS 配对记录，主体为 human，另有 rat/mouse。不要混用 `oas_strict_novel_hl_pair_2500091.csv`：项目将其标为 B_virtual，其轻链来自生成器。

## 复现

从 SDA 工作目录运行，Python 环境均为相邻 `envs/AbFlow`；本地模型自动发现，不下载权重。设置 `PYTHONDONTWRITEBYTECODE=1` 防止复用模块在目录外生成缓存。

```bash
mkdir -p PPL/tmp PPL/logs
export TMPDIR="$PWD/PPL/tmp"
export PYTHONDONTWRITEBYTECODE=1
../envs/AbFlow/bin/python PPL/benchmark.py prepare
../envs/AbFlow/bin/python PPL/benchmark.py anarci
../envs/AbFlow/bin/python PPL/benchmark.py esm --tf32
../envs/AbFlow/bin/python PPL/audit.py --boundary
../envs/AbFlow/bin/python PPL/benchmark.py summarize
MPLCONFIGDIR="$PWD/PPL/tmp/matplotlib" ../envs/AbFlow/bin/python PPL/report.py
```

`esm` 支持中断后续跑：仅重算缺失的序列 SHA256，分数来自精确逐位单掩码，不使用未掩码近似。参数为 FP32；本轮运行指定 `--tf32` 允许 A100 TF32 矩阵乘法加速，并记录本地 checkpoint、配置哈希、软件版本及运行参数。不同序列共用 minibatch 并使用 padding attention mask；数值验证对比 SDA 原实现和禁用 TF32 的结果。`audit.py --boundary` 对任意位点 NLL 距离 log(1000) 小于0.05的序列，使用原始 SDA 实现禁用 TF32 复算；汇总优先读取边界复算结果。该0.05是数值复核余量，不是统计筛选阈值。探索性的校准分位数仍基于这些分数，不保证其排序完全不受数值精度影响。

扩大实验时用新的 `--out PPL/results/<name>`；`prepare` 的 `--pairs`、`--calibration-pairs`、`--mutation-pairs` 分别控制水塘抽样的配对数、清洁阈值校准配对数和注错配对数。需保证清洁测试集中有足够配对用于注错。准备阶段不会覆盖已存在的序列快照。

## 设计与解读

- 对完整真实源做固定 seed 的水塘抽样，保留自然行权重；抽到的完全相同链不跨校准/测试集。未按家族/谱系聚类隔离，也不能保证与模型预训练集独立。
- 在基线 ANARCI 编号的 FR1/CDR1/FR2/CDR2/FR3/CDR3/FR4 分别注入随机单替换、化学分组保守单替换、人工设定的 OCR 形近替换、随机三替换、单删除、单插入。形近替换表只是压力测试假设，不代表实测 OCR 混淆矩阵。
- 用 C23/W41/C104/F或W118 锚点突变、N端截断、非法字符、链型标签颠倒及合法同链序列错换做补充。所有注错保留原序列与编辑记录。
- 采用原 SDA 的 ANARCI 判定：链型相符、单域、编号覆盖率 ≥90% 为 matched。缺失末端可能仍有 100% 的剩余序列覆盖率，所以覆盖率不等于全域完整性。
- 基线编号后的区域映射逐残基校验；注错区域按原序列定义，避免 indel 后重编号移动区域。沿用 SDA 的 FR4 118–128 端点（IMGT 官方表允许至129）；超出该端点的残基仍计入整链 ESM2。
- ESM2 全链 PPPL = exp(mean(-log P_i))；单点 PPPL_i = 1/P_i。任意单点 PPPL_i ≥1000 为当前风险筛选命中。
- 另以独立清洁校准集、分别 VH/VL 的 95% 分位数比较 max site NLL 与 mean NLL；这是探索阈值，样本小，不能当作已验证的生产阈值。测试集不参与选阈值。
- 分开报告含错链报警率、错误位点命中率、原链未报警时的新报警率、未注错测试链的报警率。删除不存在可评分残基，定位只按缺口左右紧邻残基计算；截断定位只按新 N端前四位计算。
- 同一父序列的多个突变并非独立样本；Wilson 区间仅作逐行描述，不能代替按父序列/谱系聚类的置信区间。每种注错/区域/链型的样本数见 CSV，勿用合并总数夸大统计把握。
- OAS 原序列仅作为“假定正确”的对照，未逐条回查测序来源；清洁报警率是代理误报率。真实抽取错误的精确率和召回率还需要人工核对文献原图/表格的标注集。
- 没有超过阈值只表示当前模型未发现风险，不能证明识别正确；合法同链换序列、同链配对错位、抗体归属错、某些 CDR 合理替换无法单靠这两种序列分数确认。

## 文件

`results/pilot_20261027/` 中保存数据来源清单、`sequences.jsonl` 原序列/注错记录、逐链 ANARCI 结果、按序列哈希缓存的逐位 ESM2 结果、逐序列 CSV、检出率 CSV 和阈值。`logs/` 保存实际运行日志。最终结论见 `REPORT_zh.md`。

参考：[ANARCI 官方实现](https://github.com/oxpig/ANARCI)、[IMGT 区域与锚点](https://www.imgt.org/IMGTScientificChart/Nomenclature/IMGT-FRCDRdefinition.html)、[ESM 官方变异评分实现](https://github.com/facebookresearch/esm/blob/main/examples/variant-prediction/predict.py)。这些工具估计抗体类型/序列合理性，不是图文抽取正确性的直接证据。

## Ophiuchus-Ab 扩展

同一冻结数据集另加入Ophiuchus-Ab单链和真实伙伴条件评分，单链用于与ESM2输入相同的比较，配对模式单列报告。复现与精度说明见 [README_ophiuchus.md](README_ophiuchus.md)，完整对照见 [REPORT_ophiuchus_zh.md](REPORT_ophiuchus_zh.md)。新代码为`ophiuchus.py`、`compare_models.py`及`test_ophiuchus.py`；新结果使用`ophiuchus_*`、`model_comparison_*`前缀。原实验结果保留。
