# PDF → OCR → 结构化文本

本目录集中保存 PDF 版面、页面方向、整页 OCR、表格 OCR 和跨页表头处理代码。
结构化文本指带页码、坐标、阅读顺序的文字块及 HTML 表格；抗体实体、VH/VL 和实验数据集属于后续语义提取。

| 文件 | 职责 |
| --- | --- |
| `pdf2doc.py` | 统一实现加载、方向校正、版面与裁图、正文/表格 OCR、结构组装和导出 |
| `paten_ocr.py` | 整页 OCR 的兼容入口，调用 `pdf2doc.py` 中的实现 |
| `document_vl.py` | 整页 VL 配置和结果转换的兼容导入入口 |
| `pdf2fig.py` | 裁图的旧导入和命令行兼容入口，调用 `pdf2doc.py` 中的实现 |
| `pdf_orientation.py` | 方向校正的旧导入兼容入口，以及已有工作目录的校验辅助函数 |
| `table_ocr.py` | 表格裁图 OCR 的兼容入口，保留 HTML 和单元格地址 |
| `sequence_ocr.py` | 默认表格序列复识别：按单元格定位原图文字行，由 PP-OCRv6 medium 识别后回填，保留原始 PPVL 转录及裁图证据 |
| `table_continuation.py` | 单元格解析、续表验证及已有规则提取函数的兼容导入入口 |
| `document_context.py` | 表题、上下文及续表候选函数的兼容导入入口 |

## 统一运行全部六步

```bash
python src/document/pdf2doc.py --input /path/to/source.pdf \
  --output-dir results/pdf2doc/example --device gpu:0
```

也可在 Python 中调用 `run_pdf2doc(pdf_path, output_dir, config=OCRConfig(...))`。
输出目录必须是新目录或空目录；源文件保持不变。默认自动校正方向，
使用 PP-DocLayoutV3 定位表格，整页与表格 OCR 均默认使用本地 VL。
`--backend paddle` 切换普通正文 OCR；`--table-backend` 独立选择表格后端。
`--vl-model-dir` 同时指定整页和表格 VL 权重，`--text-workers` 控制整页 OCR 进程数，
`--table-batch-size` 控制表格图片提交批大小。
版面阶段默认在独立进程中结束并释放检测模型，然后正文与表格 OCR 使用两个独立 spawn 进程并行。
结构组装等待两路全部返回，再验证续表；两路分别写 `page_ocr/` 和 `table_ocr/`。
默认正文、表格各一个模型进程，`--table-workers` 可增加表格工作进程。
`--ocr-execution serial`（Python：`ocr_parallel=False`）可切回串行。
同一 GPU 的实际提速取决于显存与负载；`recognition_summary.json` 记录实际执行模式及两路起止时间。

根目录包含统一的 `document.json`、`pages.jsonl`、`document.md`、`document.txt`、
`summary.json`、`source_info.json` 和 `run_summary.json`。
`layout/` 保留校正工作 PDF、裁图、坐标变换和候选清单；`page_ocr/` 保留逐页原始结果、
Markdown 及区域图片；`table_ocr/` 保留逐表证据和 `tables.jsonl`。

统一 JSON 包含页面、阅读顺序、正文区块、物理表格、单元格网格及验证后的逻辑表分组。
源坐标和工作坐标分别记录；验证通过的跨页表头保存在 `inherited_header`，
原始 HTML 单独保留，物理行和单元格引用不被改写。未恢复表格网格、方向待复核或 OCR 失败时标记为 partial/failed。
默认表格 VL 路径对序列单元格增加 PP-OCRv6 medium 复识别：有效 HTML 的序列字符回填，原始 HTML 单独保存在 `vl_text`，物理行列及来源 ID 保持不变。`sequence_ocr` 记录失败状态和行图片证据；正文差异只提示，不覆盖有效表格序列。
仅处理部分页面时，`unselected_pages` 与 `full_document_complete` 明确记录覆盖范围。
`--no-table-ocr` 可跳过裁图识别，复用整页 VL 已恢复的表格；候选关系不会直接作为已验证续表。

网页上传通过 `run_document_pdf.py` 调用同一六步入口，统一结果保存到运行目录的 `structured/`。
随后 `graph.py` 严格复用本轮 OCR 进行业务抽取，不再重复识别裁图。
网页保留当前全文 Paddle / 表格 VL 的默认组合，并开启自动方向校正和两路并行；可分别配置后端。

## 只运行到结构化文本

在仓库根目录执行，使用已经安装 OCR 依赖的 Python 环境：

```bash
python src/document/paten_ocr.py /path/to/source.pdf \
  --output-dir results/pdf_ocr/example --device gpu:0 --backend vl
```

该入口使用本地版面和 VL 模型，保留文字块、阅读顺序及表格 HTML；不会调用外部 LLM 或生成抗体 dataset。
输出包括 `document.json`、`pages.jsonl`、`document.md`、`document.txt`、`summary.json`，以及逐页 JSON、图片和 VL 原始产物。
模型安装和参数说明见仓库 `environment.yml`、`docs/paten_ocr.txt` 和 `docs/table_ocr.txt`。

如需单独裁出表格并进一步识别，可使用 `pdf2fig.py` 和 `table_ocr.py`；各自支持 `--help`。
两者是可选路径，整页 VL 本身已能输出表格结构。

统一文件中的 `extract_pdf()` 可单独执行区域检测、高清裁图、续表候选和来源清单。
也可用 `python src/document/pdf2doc.py --layout-only --input source.pdf --output-dir output` 执行这一阶段。
如果前一步已经生成校正后的工作 PDF，将其作为输入并设置 `--orientation off`，避免重复校正。

## 后续语义提取

本目录原有的 `document_extraction.py` 会调用 LLM 做抗体/实验语义提取，
`run_document_pdf.py` 会继续运行到 `dataset.json`，两者不属于上面的纯 OCR 入口。
它们仍依赖上级 `src/` 中的模板、流程等模块。

上述兼容模块不再保留重复实现，核心集中在 `pdf2doc.py`；`src/paten_ocr.py` 已删除。
本目录 OCR 代码内部引用使用 `document.*`，不依赖 `src/tables/`；运行仍需要外部 Python 包、模型权重，以及可选方向检测的 Tesseract。
