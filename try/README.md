# PPVL 序列裁切识别实验

所有实验代码、图片、原始识别结果和报告均在此目录；不修改原 PDF、原 OCR、正式 dataset 或网页任务。

输入是 `../report/images/` 中 PDF 第 196、229、230 页的三张真实 OCR 裁图。`report.md` 在本次实验开始时为空，因此从这三张保留图片读取，并与旧 OCR 中记录的图片 SHA-256 校验一致。

测试 AB-1、AB-2、AB-5 的 VH / SEQ ID 4、5、8。参考序列是本次助手对原始图片的目视转录，保存在 `ppvl_crop_results/references.json`，在运行模型前冻结；不是独立人工金标准，也没有使用旧修复结果。

使用当前安装的 PaddleOCR 3.7.0 / PaddleX 3.7.2、原生 PaddleOCR-VL 1.6、本机 A100 和现有权重 `/root/.paddlex/official_models/PaddleOCR-VL-1.6`。模型配置和权重哈希记录在 `settings.json`。没有调用 Codex 或其他 LLM 服务。

## 运行

在 SDA 根目录执行：

```bash
/project_c20250601/252705034/envs/sda/bin/python try/ppvl_crop_experiment.py prepare
/project_c20250601/252705034/envs/sda/bin/python -u try/ppvl_crop_experiment.py run
/project_c20250601/252705034/envs/sda/bin/python try/ppvl_crop_experiment.py summarize
/project_c20250601/252705034/envs/sda/bin/python -m unittest discover -s try -p 'test_*.py'
```

## 官方 PP-OCRv6 medium 对照

直接复用上面实验的15张单行图片（序列表9行、比对图6行），使用官方单行识别模块，不另跑文字检测，batch_size 固定为1：

```bash
/project_c20250601/252705034/envs/sda/bin/python -u try/ppocr_v6_experiment.py
```

输出在 [PP-OCRv6 medium 报告](ppocrv6_medium_results/REPORT.md)，逐图原始结果在 `ppocrv6_medium_results/raw/`。该实验与 PPVL 使用相同的图片哈希及冻结参考，原始输出只去除排版空白，保留其他所有字符。原图中的下划线、斜体和粗体也未作修复。

## 后续裁剪边界诊断

后续检查发现，上述历史裁图的第三行底部截掉了 Q 尾部少量原始像素，并保留大量右侧空白。旧图片和旧结果继续保留，不能把这处错误完全归因于模型能力。对照代码在 `crop_boundary_experiment.py`：

```bash
/project_c20250601/252705034/envs/sda/bin/python -u try/crop_boundary_experiment.py
```

[边界对照报告](crop_boundary_results/REPORT.md)分别测试原图、只去右侧空白、只补白边、恢复原始底部像素、恢复底部并去右侧空白等条件。恢复像素从原始第196页裁图直接读取，没有生成或修改残基笔画。该诊断只针对这三个相同尾行，不构成通用裁图算法或全量准确率结论。

## 修正裁图后的同图模型对照

```bash
/project_c20250601/252705034/envs/sda/bin/python -u try/corrected_crop_comparison.py
```

[修正后的同图对照报告](corrected_crop_comparison/REPORT.md)：将第196页全部9张序列行裁图恢复底部8行原始像素、裁去多余右侧空白，再让 PPVL 1.6 和官方 PP-OCRv6 medium 读取相同图片；另外6张比对图序列裁图保持原样。共30次本地模型调用，继续使用原先冻结参考，没有替换残基或用其中一个模型的输出当作另一个模型的答案。

模型结果按输入图片哈希、提示模式、像素预算及模型设置校验后复用。运行期间每次调用结束都会更新 `progress.json`、`scores.json` 和 `REPORT.md`。原始输出在 `raw/`，不对错误残基作自动替换。

## 对照条件

- 已有历史整表 OCR，以及本次同权重重新识别的原始整图。
- 两张比对图转正后仍按整表识别；记录名称/SEQ ID 错配，不能只按看似合理的字符排序修复模型输出。
- 第 196 页的单条三行序列裁图，分别用 `ocr` 和 `table` 提示。
- 三个物理行分别用 PPVL 识别后拼接；末尾短行保留。
- FIG. 2A、2B 分别裁出同一抗体的序列片段，按前后顺序拼接。
- 同一裁图使用默认视觉像素预算，或 `min_pixels=501760 / 1003520`，检查视觉分辨率设置的影响。输入像素本身没有修复。

第 196 页使用已有单元格坐标；比对图转正后通过像素行投影和核对过的行序定位。图中序列的横向范围为本次样本明确指定的范围，并不是通用序列区域检测器。裁图没有去下划线或按抗体基序补字。

## 阅读结果

打开 [实验报告](ppvl_crop_results/REPORT.md)。每种方法报告完整序列与参考逐字一致的数量、Levenshtein 编辑距离及字符错误率，并保留每条输出和原始识别结果。字符错误率包含插入、删除、替换，不等同于生物学验证或全篇专利准确率。

只有三个样本，参考又未经独立人工审阅；结果用于诊断裁图方案，不能直接作为论文质量结论。
