# SDA

从专利 PDF 提取抗体序列和实验测量数据，保存原始证据，并通过本地网页审阅。

主流程：PDF → 版面检测与表格裁图 → 全文及表格 OCR → dataagent 表格分类、
全文证据检索、实验绑定与序列提取 → 记录关联和校验 → `dataset.json`。
标题、实验方法和序列证据均检索本轮已保存的全文 OCR，不重复识别页面。

## 安装与准备

```bash
conda env create -f environment.yml
conda activate sda
```

运行需要可用的 Codex CLI 与模型访问权限，以及本地 OCR 权重：
PP-DocLayoutV3、PP-OCRv6 的检测/识别模型、PaddleOCR-VL-1.6。
模型目录及运行参数见 [OCR 说明](docs/paten_ocr.txt) 和 [表格识别说明](docs/table_ocr.txt)。
YOLO、TATR 等可选版面后端依赖列在 `requirements-pdf2fig-extra.txt`。

## PDF 到数据集

```bash
python src/document/run_document_pdf.py /path/to/source.pdf \
  --document-id WO2026086808A1 \
  --run-dir /path/to/new/run \
  --device gpu:0
```

输出位于 `<run-dir>/results/<document-id>/`，包括 `dataset.json`、
`template.completed.json`、逐表 OCR、覆盖与缺失清单以及 `report.html`。
每次首次运行使用新的输出目录；已有文档模型检查点可通过 `--resume --run-dir` 续跑。
默认使用 `--extraction-mode dataagent`，输出继续兼容 `antibody-dataset-2.0` 和网页审阅。
原生 dataagent 结果和各次尝试保存在结果目录的 `dataagent/` 子目录。
已有两路 OCR 可直接运行 `src/graph.py --extraction-mode dataagent`；参数及独立入口见
[dataagent 说明](src/dataagent/README.md)。旧全文一次性提取可显式选择 `--extraction-mode document`。

本版支持 `--extraction-mode dataagent2`：复用共享 OCR、表格分类与模板阶段，
通过分块 Reader、原文锚点序列提取和有限问答生成结果，并导出 SDA 审阅格式。
网页上传可选择 dataagent1 / dataagent2，已有任务可复用 OCR 新建对比。
运行方式和输出说明见 [dataagent2 文档](src/dataagent2/README.md)。

网页入口：

```bash
bash studio/studio.sh start
```

访问 `http://127.0.0.1:4182/studio/`，远程环境可转发 4182 端口。
部署与审阅说明见 [Studio 文档](studio/README.md)。

## 代码结构

| 目录或文件 | 内容 |
| --- | --- |
| `src/document/run_document_pdf.py` | PDF 流程总入口、续跑及运行报告 |
| `src/graph.py`、`src/dataagent/` | 主流程分发、dataagent 语义处理和 SDA 导出适配 |
| `src/dataagent2/` | 分块 Reader、序列原文定位、有限问答与 Studio 集成 |
| `src/pipeline.py`、`src/document/document_extraction.py` | 旧 document 模式调度；共享模板转换和覆盖审计 |
| `src/template_agent.py`、`src/schema_pipeline.py` | 固定模板、字段状态与导出 |
| `src/template_sequence_agent.py` | 序列证据检索与关联 |
| `studio/` | 本地网页及服务 |
| `prompt/`、`schemas/` | 提示词、输出结构与领域定义 |
| `tests/` | 主流程回归测试 |
| `docs/` | 模块与运行说明 |

完整调用关系见 [主流程说明](docs/pdf_pipeline.txt)。
本地 PDF、模型、数据库和实验产物由 `.gitignore` 排除，运行时按需准备或生成。

## 验证

```bash
python -m unittest discover -s tests -v
PYTHONPATH=src python -m unittest dataagent2.test_dataagent2 -v
```

数据集会保留证据、冲突和缺失状态。OCR 一致性及程序校验不等同于独立的语义准确率，
存在缺口的提取结果应结合原始 PDF 审阅。
