"""Command-line entry for document OCR extraction and dataset construction."""

import argparse
import asyncio
from pathlib import Path
import sys

from document.table_ocr import OCR_BACKENDS
from pipeline import PipelineConfig, run_pipeline


def main(argv=None):
    project_dir = Path(__file__).resolve().parents[1]
    defaults = PipelineConfig
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-dir", type=Path, default=project_dir / "data")
    parser.add_argument("--output-dir", type=Path, default=project_dir / "results")
    parser.add_argument("--text-pages-jsonl", required=True, help="同一专利的逐页全文 OCR JSONL")
    parser.add_argument("--text-context-dir", help="本专利资料及 JSON 序列表目录，默认使用 image-dir")
    parser.add_argument("--extraction-mode", choices=("dataagent", "dataagent2", "document"), default="dataagent")
    parser.add_argument("--structured-dir", type=Path, help="复用 pdf2doc 的结构化结果及版面清单")
    parser.add_argument("--document-id")
    parser.add_argument("--dataagent-resume", action="store_true", help="校验同批 OCR 后复用 dataagent 模型缓存续跑")
    parser.add_argument("--semantic-provider", choices=("codex", "rules"))
    parser.add_argument("--llm-model")
    parser.add_argument("--llm-effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--llm-workers", type=int)
    parser.add_argument("--sequence-workers", type=int)
    parser.add_argument("--ocr-device", default=defaults.ocr_device, help="cpu 或 gpu:0 等单设备")
    parser.add_argument("--ocr-backend", choices=OCR_BACKENDS, default=defaults.ocr_backend)
    parser.add_argument("--reuse-table-ocr-from", help="复用指定目录的原始表格 OCR，严格校验身份与内容")
    parser.add_argument("--document-resume", help="复用同一输入下的文档模型检查点")
    parser.add_argument("--document-template-agent", action=argparse.BooleanOptionalAction,
                        default=defaults.document_template_agent, help="启用序列证据检索和关联核验")
    for name, description in {
        "document_repair_rounds": "未覆盖结果单元格的补提轮数",
        "template_agent_max_calls": "旧 document 模式的序列调用总上限；dataagent 模式仅使用 0 值关闭序列模型",
        "template_agent_context_chars": "旧 document 模式的序列上下文预算",
        "ocr_workers": "表格 OCR 独立进程数",
        "ocr_batch_size": "每次提交的表格图片数，与后端实际模型 batch 分别记录",
        "ocr_cpu_threads": "每个 OCR 进程的 CPU 线程数",
        "retries": "OCR 失败后的额外重试次数",
        "limit": "仅处理前 N 张表格裁图",
    }.items():
        parser.add_argument("--" + name.replace("_", "-"), type=int,
                            default=getattr(defaults, name), help=description)
    parser.add_argument("--retry-delay", type=float, default=defaults.retry_delay)
    arguments = vars(parser.parse_args(argv))
    image_dir, output_dir = arguments.pop("image_dir"), arguments.pop("output_dir")
    dataagent_options = {key: arguments.pop(key) for key in (
        "structured_dir", "document_id", "dataagent_resume", "semantic_provider", "llm_model", "llm_effort",
        "llm_workers", "sequence_workers")}
    if arguments["extraction_mode"] in {"dataagent", "dataagent2"}:
        if arguments["extraction_mode"] == "dataagent2":
            from dataagent2.studio_pipeline import run_studio_pipeline as run_dataagent_pipeline
        else:
            from dataagent.main_pipeline import run_dataagent_pipeline
        if arguments["limit"]:
            parser.error("dataagent 文档提取需要同篇全部表格，不支持 --limit")
        if arguments["document_resume"]:
            parser.error("dataagent 模式请使用 --dataagent-resume")
        summary = run_dataagent_pipeline(image_dir, output_dir, arguments["text_pages_jsonl"],
            table_ocr=arguments["reuse_table_ocr_from"], structured_dir=dataagent_options["structured_dir"],
            document_id=dataagent_options["document_id"], data_dir=arguments["text_context_dir"],
            resume=dataagent_options["dataagent_resume"], semantic_provider=dataagent_options["semantic_provider"],
            repair_rounds=arguments["document_repair_rounds"],
            extract_sequences=arguments["document_template_agent"] and arguments["template_agent_max_calls"] != 0,
            **{key: dataagent_options[key] for key in ("llm_model", "llm_effort", "llm_workers", "sequence_workers")
               if dataagent_options[key] is not None})
    else:
        try:
            config = PipelineConfig(**arguments)
        except ValueError as exc:
            parser.error(str(exc))
        summary = asyncio.run(run_pipeline(image_dir, output_dir, config))
    if summary["status"] == "failed":
        print(f"文档提取存在失败，详情见 {output_dir / '_batch_summary.json'}", file=sys.stderr)
        for document_id, error in summary["errors"].items():
            print(f"{document_id}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
