# PP-OCRv6 medium 序列裁图实测

测试 AB-1、AB-2、AB-5。直接复用 PPVL 实验的9张序列表文字行裁图和6张比对图序列裁图，不新增裁剪、不更换参考。

使用官方 PP-OCRv6_medium_rec 的单行 TextRecognition 模块；batch_size=1，不额外运行文字检测或 LLM。模型原始输出只去排版空白，不替换氨基酸。

参考是本次助手对原始图片的核读转录，尚非独立人工金标准。以下为三个诊断样本的观察，不能外推全篇准确率。

本次已识别 15/15 张图片，识别调用耗时合计 0.994 秒（不含加载模型）。

| 相同裁图方式 | 模型 | 整条完全一致 | 编辑距离合计 | CER |
|---|---|---:|---:|---:|
| 序列表逐行识别后拼接 | PPVL 默认 OCR 提示 | 0/3 | 4 | 1.09% |
| 序列表逐行识别后拼接 | PP-OCRv6 medium | 0/3 | 3 | 0.82% |
| 两幅图的序列裁图识别后拼接 | PPVL 默认 OCR 提示 | 0/3 | 10 | 2.73% |
| 两幅图的序列裁图识别后拼接 | PP-OCRv6 medium | 3/3 | 0 | 0.00% |

## 逐条输出与差异

### physical_lines_p196 / AB-1

完全一致：False；编辑距离：1。

```text
QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYGMHWVRQAPGKGLEWVAVVWYSGSYTHYADSVKGRFTITRDNSKNTLNLQMNSLRAEDTAVYYCSRSPQWEDIFEAMDIWGOGTMVTVSS
```

- 参考位置 114 附近：`Q` → `O`。

### sequence_crops_figures / AB-1

完全一致：True；编辑距离：0。

```text
QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYGMHWVRQAPGKGLEWVAVVWYSGSYTHYADSVKGRFTITRDNSKNTLNLQMNSLRAEDTAVYYCSRSPQWEDIFEAMDIWGQGTMVTVSS
```


### physical_lines_p196 / AB-2

完全一致：False；编辑距离：1。

```text
QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYGMHWVRQAPGKGLEWVAVVWYDGSYTHYADSVKGRFTITRDNSKNTLNLQMNSLRAEDTAVYYCARSPQWEEIFEAMDIWGOGTMVTVSS
```

- 参考位置 114 附近：`Q` → `O`。

### sequence_crops_figures / AB-2

完全一致：True；编辑距离：0。

```text
QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYGMHWVRQAPGKGLEWVAVVWYDGSYTHYADSVKGRFTITRDNSKNTLNLQMNSLRAEDTAVYYCARSPQWEEIFEAMDIWGQGTMVTVSS
```


### physical_lines_p196 / AB-5

完全一致：False；编辑距离：1。

```text
QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYGMHWVRQAPGKGLEWVAVVWYDGYDTHYADSVKGRFTITRDNSKNTLNLQMNSLRAEDTAVYYCSRSPQWEDIFESMDIWGOGTMVTVSS
```

- 参考位置 114 附近：`Q` → `O`。

### sequence_crops_figures / AB-5

完全一致：True；编辑距离：0。

```text
QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYGMHWVRQAPGKGLEWVAVVWYDGYDTHYADSVKGRFTITRDNSKNTLNLQMNSLRAEDTAVYYCSRSPQWEDIFESMDIWGQGTMVTVSS
```


## 原始结果

逐图转录、模型识别分数、输入哈希与模型原始结果在 `raw/`；模型版本、配置和权重哈希在 `settings.json`。模型分数并不代表序列正确概率。
