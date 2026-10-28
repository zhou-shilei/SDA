# Ophiuchus-Ab 对照复现

新增内容全部位于PPL。`benchmark.py`生成的`sequences.jsonl`保持不变，复用47个真实OAS配对、原校准/测试划分、1530个注错样本及ANARCI结果。ESM2沿用原缓存及FP32边界覆盖。

`ophiuchus.py`只读加载AirGen-Dev中的本地原始Ophiuchus-Ab checkpoint及MINT模型源码，不加载后续SFT/RL checkpoint，不下载权重。严格加载全部参数；缓存以输入模式、目标序列、配对序列和生物学目标链槽位共同索引。单链模式仅由序列决定；配对模式不能复用另一配对关系的分数。

单链与配对条件模式均精确逐位单掩码，完整词表softmax，不使用未掩码或多掩码近似。配对模式每次仅掩目标链一个残基，真实原始配对链保持不变，目标链PPPL不平均配对链分数。采用原AirGen布局（VH含特殊符号不超过150个token，VL不超过128个token）；长度超限直接报错，不静默截断。非法字符不映射成正常氨基酸。

在SDA目录运行：

```bash
mkdir -p PPL/tmp PPL/logs
export TMPDIR="$PWD/PPL/tmp"
export MPLCONFIGDIR="$PWD/PPL/tmp/matplotlib"
export PYTHONDONTWRITEBYTECODE=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

../envs/AbFlow/bin/python PPL/test_ophiuchus.py
../envs/AbFlow/bin/python PPL/ophiuchus.py score --mode single --batch-size 64 --tf32
../envs/AbFlow/bin/python PPL/ophiuchus.py score --mode paired --batch-size 64 --amp-fp16
../envs/AbFlow/bin/python PPL/ophiuchus.py validate --mode single --batch-size 64
../envs/AbFlow/bin/python PPL/ophiuchus.py validate --mode paired --batch-size 64 --amp-fp16
../envs/AbFlow/bin/python PPL/compare_models.py --paired-fp16
../envs/AbFlow/bin/python PPL/check_comparison.py
```

`score`支持续跑；运行元数据验证权重/源码/输入哈希和数值精度，拒绝不兼容缓存。`--limit 8`可先试跑；最后仍需移除limit完成全部输入。单链按序列去重，配对按目标序列和真实伙伴去重，所以两种模式的唯一输入数量不必相同。

本轮单链采用FP32参数及TF32矩阵运算；配对模式采用FP32参数及FP16 autocast加速，log-softmax始终在FP32计算。配对FP16缓存使用`ophiuchus_paired_fp16_*`前缀，与试跑的原生TF32缓存分开保存。原生配对TF32缓存只是一部分输入的数值参考，不作为完整实验汇总来源。需要全部采用TF32时，去掉`--amp-fp16`，评分时加`--tf32`，汇总时去掉`--paired-fp16`。

`validate`始终禁用autocast及TF32，比较同一输入的批量/单序列分数，复算所有原始对照，并逐位筛查位点1000或校准max/mean阈值附近的变体（包括非最大残基）。TF32模式的位点NLL余量为0.05；FP16模式在首轮审计观察到约0.0975的最大位点差后，将位点余量扩大到0.2；整链平均NLL余量均为0.005。同时核验实际差异小于这些复核余量。FP32参考支持断点续算，覆盖保存在独立文件，原始加速缓存保留。配对模式额外核验更换伙伴确实改变条件概率。校准阈值来自原校准集的独立VH/VL分布，测试集不参与选阈值。

输出报告为`REPORT_ophiuchus_zh.md`；结果位于`results/pilot_20261027/`，使用`ophiuchus_*`和`model_comparison_*`文件名，原报告仍是`REPORT_zh.md`。逐序列CSV为长格式（模型×规则×样本），`flag`与`localized_hit`空值表示不适用或未评分，不能当作已评分但未检出。`new_flag`只在原链未报警时有值，统计分母在汇总CSV中给出。

配对模式多用了真实伙伴的信息，不能作为与单链ESM2纯粹的模型架构比较。单掩码PPPL也不是扩散模型的精确序列似然。OAS对照未逐条人工复核且可能与预训练数据重叠；所有误报和检出结论都是当前小规模注错pilot的测量。

模型参考：[Ophiuchus-Ab官方实现](https://github.com/Ophiuchus-Team/Ophiuchus-Ab)。本地权重SHA256及源码版本记录在`ophiuchus_*_runtime.json`。
