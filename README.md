# SDA

从专利 PDF 提取抗体序列和实验测量数据，保存原始证据，并通过本地网页审阅。

主流程：PDF → 版面检测与表格裁图 → 全文及表格 OCR → 文档级语义抽取 →
证据校验与缺失补提 → 序列关联 → `dataset.json`。

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
python src/run_document_pdf.py /path/to/source.pdf \
  --document-id WO2026086808A1 \
  --run-dir /path/to/new/run \
  --device gpu:0
```

输出位于 `<run-dir>/results/<document-id>/`，包括 `dataset.json`、
`template.completed.json`、逐表 OCR、覆盖与缺失清单以及 `report.html`。
每次首次运行使用新的输出目录；已有文档模型检查点可通过 `--resume --run-dir` 续跑。

网页入口：

```bash
bash studio/studio.sh start
```

访问 `http://127.0.0.1:4182/studio/`，远程环境可转发 4182 端口。
部署与审阅说明见 [Studio 文档](studio/README.md)。

## 代码结构

| 目录或文件 | 内容 |
| --- | --- |
| `src/run_document_pdf.py` | PDF 流程总入口、续跑及运行报告 |
| `src/graph.py`、`src/pipeline.py` | 处理图与批次调度 |
| `src/document_extraction.py` | 文档级抽取、输出协议及证据校验 |
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
```

数据集会保留证据、冲突和缺失状态。OCR 一致性及程序校验不等同于独立的语义准确率，
存在缺口的提取结果应结合原始 PDF 审阅。
