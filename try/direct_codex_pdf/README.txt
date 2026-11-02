最简 Codex 数据提取 baseline

只向 Codex 提供原始 PDF 和目标数据集描述，不提供既有 OCR、模板、seed 或提取结果。
数据需求在 prompt/codex_baseline.txt；不规定提取步骤或 JSON Schema。

从仓库根目录运行：

python try/direct_codex_pdf/run_simple_baseline.py /path/to/source.pdf --output baseline/simple_codex/new-run

修改数据需求可直接编辑提示词，或用 --requirements /path/to/requirements.txt。
输出目录必须是新目录。dataset.json 是 Codex 原始产物；prompt.txt、events.jsonl、
last_message.txt 和 manifest.json 保留提示词、真实调用、最终说明及实际用量。
中间文件保留在 manifest.json 的 working_directory，便于核查。
运行器只检查 JSON 可解析且为非空对象；内容完整性与准确性需另行核对。
