# SDA Studio

从 `dataagent/studio` 迁入原有 HTML、CSS 和 JavaScript，保留“数据集 / 数据录入 / 数据审阅”、三栏审阅布局、关系图、中英文、列宽调整、PDF 缩放、人工修订和导出。原始文件哈希记录在 `migration_source.json`。

在 SDA 根目录启动：

```bash
bash studio/studio.sh start
```

打开 `http://127.0.0.1:4182/studio/`。远程 IDE 转发 **4182** 端口；code-server 也可使用 `/proxy/4182/studio/`。4180 和 4181 已有其他服务，因此这里默认使用 4182。

VS Code 运行任务 **SDA: 在 VS Code 内查看提取结果**，会先启动服务，再请求内置预览打开本轮专利报告。
也可执行 **Simple Browser: Show**，填入 `http://127.0.0.1:4182/studio/run.html`。
4182 端口已配置 `openPreview`，该端口链接默认交给 Simple Browser 打开。
顶部“运行概览”进入新的文档报告：总耗时、实际 token、实验关键词/标签、序列、
分子关系、覆盖缺口与原页证据。完整 PDF 新运行另外生成内嵌数据的 `report.html`，
可以使用已有 HTML Live Preview 打开，无需请求 JSON 后端。

本轮固定报告为 `validation_results/document_pipeline/WO2025108394A1/report.html`。
打开文件后按 **Ctrl+Shift+V**（macOS：**Cmd+Shift+V**）使用已安装的 HTML 预览扩展。
该报告预先写入表格，预览禁止脚本时仍可阅读 15 个实验和 94 条测量；筛选、导出和点击原页证据请使用上面的内置浏览器入口。

```bash
bash studio/studio.sh status
bash studio/studio.sh logs
bash studio/studio.sh stop
```

也可前台运行 `python studio/server.py --port 4182`。运行环境需要 SDA 的 `lxml`、`pymupdf`；打开网页不会加载 OCR 模型或调用 LLM。

## SDA 数据连接

- 自动发现 `results/*/dataset.json`；读取同目录 `template.completed.json`、单表 JSON、缺失清单和耗时报告。首次打开进入数据审阅，直接显示当前专利的真实记录。
- 使用 SDA 本地 PDF 按需渲染页面，依据本地 manifest 提供表格区域与裁图，不依赖 DataAgent 的源文件、后端或旧示例。
- 每条实验使用 `document_id + observation_id` 作为稳定标识，保留同一实体的多次实验。数值列适用于 IC50、EC50、KD 等指标，实验组显示指标名称。
- 仅使用已有来源和明确的 OCR 行匹配。HTML 结构不用于推测单元格矩形；缺少位置时保留表格级证据。缺失值与未执行的审核保留待复核状态。
- 数据集文件夹与发布快照、审核人记录保存在 `.studio/dataset.sqlite`。初始 SDA 文件夹为空；在审阅页勾选记录并点击“推送所选记录”后，数据集页才会出现发布快照。
- 原网页的字段修订、标记和手动画框继续保存在浏览器中，使用独立的 `sda.studio.*` 存储名称；源提取文件不被覆盖。

## PDF 导入

数据录入页支持点击或拖入 PDF，再点击“导入 PDF 并开始解析”。后台执行
`src/run_document_pdf.py`：全页文字 OCR 与表格 OCR 之后做一次全文提取和有预算的补全，
复用现有模板导出，默认不启用独立表格分类。页面显示真实的版面、
OCR/抽取、模板、TST 和构建阶段，失败原因直接展示，运行日志可打开和刷新。
每次运行使用新的产物目录；网页不会自动重跑已有专利。

已有结果可选择“重新解析 PDF”完整重跑，或“复用 OCR 重新解析”跳过版面与表格
OCR，只读取保存的原始 HTML/文字及证据，重新调用抽取和补全。OCR 缓存缺失、
跨专利或不匹配时会报错，不自动回退到重新识别。两种操作都保留之前的运行目录，
内置样例不会覆盖 `data/`、`results/` 原始产物，也保持删除保护。
复用 OCR 重新解析时采用文档级提取；已有全文 OCR 可复用，否则额外执行全文文字 OCR。
此模式在报告中明确标为“重新提取”，不当作整篇 PDF 首次解析耗时。

上传资料按 `.studio/patents/<id>/runs/<run>/data/<id>` 组织，PDF 文字层保存为可追溯
的本地正文，供 TST 使用；扫描页不被标记为已读取正文。复用 OCR 时保留原裁图输入，
新运行单独准备正文与序列资料。正文检查检查本地资料，不代表已执行在线专利检索。

```bash
SDA_STUDIO_PYTHON=/path/to/sda/bin/python \
SDA_STUDIO_OCR_DEVICE=gpu:0 \
bash studio/studio.sh start
```

可设置 `SDA_STUDIO_PORT`、`SDA_STUDIO_OCR_BACKEND=vl|paddle`；CPU 可设 `SDA_STUDIO_OCR_DEVICE=cpu`。模型权重与 Codex 配置沿用 SDA 现有环境。上传解析会执行真实 OCR 和模型请求，错误写入 `.studio/patents/<id>/runs/<run>/pipeline.log`，也可通过 `/api/patents/<id>/log` 查看。

上传的源文件和产物放在 `.studio/patents/`，删除上传专利时移入 `.studio/trash/`，现有 `data/` 和 `results/` 的示例受保护。服务停止会终止其解析子进程；重启后中断任务标为失败，保留产物，可手动重试。仍存活的旧进程结束前不会重复提交。

DataAgent 专属的 QuestionAgent 采集问答、ANARCI/ESM-2、所选记录的逐字段重解析尚未迁入
SDA，WIPO XML 上传入口和在线正文下载也尚未接入。对应入口隐藏或禁用，未运行的检查
不显示通过。现有“复用 OCR 重新解析”会重做整篇已有表格的提取，不冒充逐字段操作。

## 验证

`python -m unittest discover -s tests -v` 执行当前测试，本轮 55 项通过。
文档接入与网页数据适配测试位于 `tests/test_document_extraction.py`，使用临时目录和
替代模型调用，覆盖实验/实体身份、别名、零值、引用归属、续跑输入一致性、累计用量、
原生输出 schema、原页几何约束去重、覆盖分母、明确的序列表关联及完整批处理导出。

本轮浏览器检查了真实运行的 94 条记录、15 个实验定义、m8 筛选、16.4 pM 结果到
PDF 第 13 页的联动、CSV 导出、430px 窄窗口、主界面入口和无需后端的独立 HTML。
没有浏览器控制台异常或失败 HTTP 请求；报告在
`.studio/previews/document-browser-report.json`，截图在 `.studio/previews/document-run.png`。

浏览器已检查真实专利的 62 条记录、40 页 PDF、m8 / 16.4 pM 到第 13 页的联动、CSV 导出、中英文和三个主页面；文件夹创建、记录推送及审核人写入另在隔离样例中验证。没有为网页迁移重新运行整篇 OCR/LLM。
