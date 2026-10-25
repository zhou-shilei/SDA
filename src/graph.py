import asyncio
import argparse
import sys
import tempfile
import inspect
from functools import wraps
from time import perf_counter
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agent import (
    AssayExtractionAgent,
    DataConstructionAgent,
    SequenceExtractionAgent,
    TableClassificationAgent,
    TextSequenceExtractionAgent,
    UnifiedTableExtractionAgent,
    codexllm,
)
from table_ocr import OCR_BACKENDS, extract_table_evidence
from pipeline import PipelineConfig, run_pipeline
from costructure import construct_records, get_document_id
from text_sequences import TextSequenceProcessor, load_document
from schema_pipeline import build_document_dataset, load_sequence_catalog
from template_agent import TemplateAgent, template_to_tables
from token_usage import TokenUsageRecorder, usage_scope


# 1. 定义共享数据。total=False 表示可以先只传图片路径，其他字段逐步补上。
class TableState(TypedDict, total=False):
    document_id: str
    patent_id: str
    table_id: str
    image_path: str
    table_text: str
    table_evidence: dict
    classification: dict[str, str]
    classify_tables: bool  # 可选兼容开关，默认跳过独立分类。
    extraction_context: dict
    extraction_method: str
    assay_records: list[dict]
    sequence_records: list[dict]
    table_results: list[dict]  # 可选：其他已提取表格，供单图调用跨表关联。
    construction: dict
    data_records: list[dict]
    text_context_dir: str  # 可选：本专利正文所在目录；单图调用显式提供。
    text_sequence_result: dict
    text_sequence_source: dict
    schema_template: dict
    schema_result: dict
    schema_tables: list[dict]
    legacy_text_sequences: bool
    timings: dict
    token_usage: dict


def timed_node(function):
    """单图Graph的节点耗时；批处理另保存完整请求与阶段报告。"""
    def finish(state, result, started, recorder):
        report = recorder.report()
        report["started_at_utc"] = state.get("token_usage", {}).get("started_at_utc", report["started_at_utc"])
        return {**result, "token_usage": report, "timings": {**state.get("timings", {}),
                function.__name__.removesuffix("_node"): {"wall_seconds": perf_counter() - started}}}

    def prepare(state):
        recorder = TokenUsageRecorder(state.get("token_usage", {}).get("events", []))
        return recorder, usage_scope(recorder=recorder, stage=function.__name__.removesuffix("_node"),
                                     item_id=state.get("table_id") or get_document_id(state),
                                     document_id=get_document_id(state))

    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(state):
            started = perf_counter()
            recorder, scope = prepare(state)
            with scope:
                result = await function(state)
            return finish(state, result, started, recorder)
        return asynchronous

    @wraps(function)
    def synchronous(state):
        started = perf_counter()
        recorder, scope = prepare(state)
        with scope:
            result = function(state)
        return finish(state, result, started, recorder)
    return synchronous


# 2. OCR 节点：读取图片路径，返回识别出的文字。
@timed_node
def ocr_node(state: TableState):
    evidence = extract_table_evidence(state["image_path"],
        {"id": state.get("table_id") or Path(state["image_path"]).stem,
         "document_id": get_document_id(state)})
    return {"table_text": evidence["text"], "table_evidence": evidence,
            "assay_records": [], "sequence_records": []}


def route_after_ocr(state: TableState):
    return "classify" if state.get("classify_tables", False) else "extract_table"


# 3. 可选分类节点：只在显式开启旧分流方式时执行。
@timed_node
async def classify_node(state: TableState):
    agent = TableClassificationAgent(llm=codexllm())
    classification = await agent.run(state["table_text"])
    return {"classification": classification}


# 4. 序列表格单独提取；other 结束；assay 和 uncertain 沿用实验数据提取。
def route_after_classification(state: TableState):
    category = state["classification"]["category"]
    if category == "other":
        return "text_sequences" if state.get("legacy_text_sequences") else (
            "template" if state.get("text_context_dir") else "end")
    if category == "sequence":
        return "extract_sequence"
    return "extract_assay"


# 5. 实验数据提取节点：读取 OCR 文字，返回结构化记录。
@timed_node
async def extract_assay_node(state: TableState):
    agent = AssayExtractionAgent(llm=codexllm())
    records = await agent.run(state["table_text"])
    return {"assay_records": records}


# 6. 序列提取节点：读取 OCR 文字，返回序列及编号。
@timed_node
async def extract_sequence_node(state: TableState):
    agent = SequenceExtractionAgent(llm=codexllm())
    records = await agent.run(state["table_text"])
    return {"sequence_records": records}


@timed_node
async def extract_table_node(state: TableState):
    """默认一次抽取两类信息，混合表不经过单选类别分流。"""
    agent = UnifiedTableExtractionAgent(llm=codexllm())
    records = await agent.run(state["table_text"], table_context=state.get("extraction_context"))
    return {**records, "extraction_method": "unified_table"}


def document_inputs(state: TableState):
    if "schema_tables" in state:
        return state["schema_tables"]
    current = {key: value for key, value in state.items() if key not in {
        "table_results", "construction", "data_records", "text_sequence_source", "text_sequence_result", "timings",
        "schema_template", "schema_result", "schema_tables", "token_usage", "classify_tables"}}
    tables = [*state.get("table_results", []), current]
    if state.get("text_sequence_source"):
        tables.append(state["text_sequence_source"])
    return tables


@timed_node
async def text_sequences_node(state: TableState):
    """单图接口按需传入正文目录；批处理在pipeline中按专利统一执行。"""
    if not state.get("text_context_dir"):
        return {"text_sequence_source": {}, "text_sequence_result": {"status": "skipped"}}
    document_id = get_document_id(state)
    if document_id is None:
        raise ValueError("正文提取需要明确document_id")
    document = load_document(state["text_context_dir"], document_id)
    if document is None:
        raise ValueError("指定的正文目录中未找到全文")
    # 单图调用不隐式向结果目录写入；调用方可以保存返回的完整结果。
    with tempfile.TemporaryDirectory(prefix="sda-text-sequences-") as temporary:
        result = await TextSequenceProcessor(TextSequenceExtractionAgent(codexllm())).run(
            document, [table for table in document_inputs(state)
                       if table.get("source_type") != "validated_text_sequences"], temporary)
    result["synthetic_input"].pop("result_file", None)
    if any(issue["severity"] == "error" for issue in result["issues"]):
        raise ValueError(f"正文序列提取不完整：{result['errors']}")
    # 单图临时目录即将清理，待核对问题通过返回状态交给调用方保存。
    result.pop("issues_file", None)
    if result["status"] == "partial":
        print(f"{document_id} 正文部分完成，{len(result['issues'])} 个问题保存在 text_sequence_result.issues。", flush=True)
    return {"text_sequence_source": result["synthetic_input"], "text_sequence_result": result}


def schema_data_directory(state):
    return Path(state["text_context_dir"]) if state.get("text_context_dir") else Path(state["image_path"]).resolve().parent


@timed_node
def template_node(state: TableState):
    """固定领域结构由 schema 定义；先装入表格证据，再列出缺失槽。"""
    document_id = get_document_id(state)
    if document_id is None:
        raise ValueError("schema 模板需要明确 document_id")
    catalog = load_sequence_catalog(schema_data_directory(state), document_id)
    return {"schema_template": TemplateAgent().run(document_id, document_inputs(state), sequence_catalog=catalog)}


@timed_node
async def tst_completion_node(state: TableState):
    """按缺失槽检索正文，先数动态实验实例，再按序号抽取并核验。"""
    tables = document_inputs(state)
    document_id = get_document_id(state)
    with tempfile.TemporaryDirectory(prefix="sda-schema-tst-") as temporary:
        result = await build_document_dataset(
            document_id, tables, schema_data_directory(state), temporary,
            complete_text=bool(state.get("text_context_dir")),
            initial_template=state["schema_template"])
    # 单图接口只返回状态，不向调用方暴露已经清理的临时文件引用。
    result["dataset"].pop("provenance_file", None)
    if result["report"]["status"] == "failed":
        raise ValueError(f"TST 补全失败：{result['report'].get('errors', [])}")
    return {"schema_template": result["template"], "schema_result": result,
            "schema_tables": template_to_tables(result["template"], tables)}


def route_after_extraction(state: TableState):
    return "text_sequences" if state.get("legacy_text_sequences") else "template"


@timed_node
def costructure_node(state: TableState):
    """关联当前表和调用方提供的其他表；批处理在所有表完成后统一关联。"""
    result = construct_records(document_inputs(state))
    return {"construction": result}


@timed_node
async def data_construction_node(state: TableState):
    """读取当前文档的原始表格及上下文，让 Agent 输出六字段记录。"""
    if state.get("schema_result"):
        return {"data_records": state["schema_result"]["records"]}
    tables = document_inputs(state)
    agent = DataConstructionAgent(llm=codexllm())
    records = await agent.run(tables)
    return {"data_records": records}


# 7. 添加节点，并连接执行顺序。
builder = StateGraph(TableState)
builder.add_node("ocr", ocr_node)
builder.add_node("classify", classify_node)
builder.add_node("extract_table", extract_table_node)
builder.add_node("extract_assay", extract_assay_node)
builder.add_node("extract_sequence", extract_sequence_node)
builder.add_node("text_sequences", text_sequences_node)
builder.add_node("template", template_node)
builder.add_node("tst_completion", tst_completion_node)
builder.add_node("costructure", costructure_node)
builder.add_node("data_construction", data_construction_node)

builder.add_edge(START, "ocr")
builder.add_conditional_edges("ocr", route_after_ocr,
                              {"classify": "classify", "extract_table": "extract_table"})

builder.add_conditional_edges(
    "classify",
    route_after_classification,
    {
        "extract_assay": "extract_assay",
        "extract_sequence": "extract_sequence",
        "end": END,
        "text_sequences": "text_sequences",
        "template": "template",
    },
)
for extraction in ("extract_table", "extract_assay", "extract_sequence"):
    builder.add_conditional_edges(extraction, route_after_extraction,
                                  {"text_sequences": "text_sequences", "template": "template"})
builder.add_conditional_edges("text_sequences", lambda state: "costructure" if state.get("legacy_text_sequences") else "template",
                              {"costructure": "costructure", "template": "template"})
builder.add_edge("template", "tst_completion")
builder.add_edge("tst_completion", "costructure")
builder.add_edge("costructure", "data_construction")
builder.add_edge("data_construction", END)

graph = builder.compile()


# 8. 单图可传入 table_results 提供其他表；批处理统一在提取完成后拼接。
async def run_batch(image_dir: Path, output_dir: Path, config=None):
    return await run_pipeline(image_dir, output_dir, config)


def main():
    project_dir = Path(__file__).resolve().parents[1]
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(description="并行 OCR、统一表格提取及跨表拼接；可选独立分类")
    parser.add_argument("--image-dir", type=Path, default=project_dir / "data")
    parser.add_argument("--output-dir", type=Path, default=project_dir / "results")
    parser.add_argument("--ocr-device", default=defaults.ocr_device, help="cpu 或 gpu:0 等单设备")
    parser.add_argument("--ocr-backend", choices=OCR_BACKENDS, default=defaults.ocr_backend,
                        help="vl：PaddleOCR-VL-1.6（默认）；paddle：普通 OCR")
    parser.add_argument("--reuse-table-ocr-from", help="复用指定结果目录的原始表格 OCR；缺项或身份冲突即失败，不重新 OCR")
    parser.add_argument("--extraction-mode", choices=("table", "document"), default=defaults.extraction_mode)
    parser.add_argument("--document-repair-rounds", type=int, default=defaults.document_repair_rounds)
    parser.add_argument("--document-resume", help="在输入完全一致时复用已保存的文档模型结果，只补未覆盖项")
    parser.add_argument("--document-sequence-mapping", action="store_true", default=defaults.document_sequence_mapping,
                        help="文档模式额外运行局部序列映射；默认复用全文已提取的映射")
    parser.add_argument("--document-template-agent", action=argparse.BooleanOptionalAction,
                        default=defaults.document_template_agent,
                        help="文档提取后运行 TemplateAgent 的序列检索、覆盖检查及关联核验（默认开启）")
    parser.add_argument("--template-agent-max-calls", type=int, default=defaults.template_agent_max_calls,
                        help="TemplateAgent 模型调用总上限，包含提议及独立核验；0 仅做清单审计")
    parser.add_argument("--template-agent-context-chars", type=int, default=defaults.template_agent_context_chars,
                        help="TemplateAgent 每次请求的证据上下文字符预算")
    parser.add_argument("--classify-tables", action=argparse.BooleanOptionalAction,
                        default=defaults.classify_tables,
                        help="启用独立表格分类后分流（默认关闭，直接统一抽取序列与实验数据）")
    for name, help_text in {
        "ocr_workers": "OCR 独立进程数",
        "ocr_batch_size": "每次提交的表格图片数；native VL 实际模型 batch 可能固定为 1",
        "ocr_cpu_threads": "每个 OCR 进程的 CPU 线程数",
        "classification_batch_size": "开启分类时每批的最大表格数",
        "classification_max_chars": "开启分类时每批 OCR 字符预算；超长单表不截断",
        "classification_concurrency": "开启分类时同时运行的分类请求数，含单表重试",
        "extraction_concurrency": "同时运行的单表提取请求数",
        "construction_concurrency": "同时运行 DataConstructionAgent 的专利数",
        "text_sequence_max_chars": "正文块JSON字符预算（含段落元数据）",
        "text_sequence_overlap_chars": "正文块之间重复保留的相邻段落字符预算",
        "text_sequence_concurrency": "正文分块提取并发数；0表示全部文本块并发（默认）",
        "tst_concurrency": "TST 补全请求并发数",
        "tst_max_tasks": "每篇专利最多处理的缺失字段任务数；剩余任务明确保留",
        "page_ocr_max_pages": "额外页面 OCR 上限；0 仅使用现有文字层和指定的 OCR",
        "sequence_mapping_max_calls": "每篇专利的页面序列映射请求上限",
        "retries": "每张表格每阶段的额外重试次数",
        "limit": "仅处理排序后的前 N 张，用于小批量验证",
    }.items():
        parser.add_argument("--" + name.replace("_", "-"), type=int,
                            default=getattr(defaults, name), help=help_text)
    parser.add_argument("--classification-flush-seconds", type=float,
                        default=defaults.classification_flush_seconds,
                        help="开启分类时从本批第一张到达开始的最长凑批等待时间")
    parser.add_argument("--retry-delay", type=float, default=defaults.retry_delay,
                        help="重试的初始间隔秒数，之后翻倍")
    parser.add_argument("--text-context-dir", help="可选：正文资料根目录，默认使用image-dir")
    parser.add_argument("--text-pages-jsonl", help="可选：同篇专利的逐页 OCR JSONL，供 TST 检索")
    parser.add_argument("--no-page-evidence", dest="page_evidence", action="store_false",
                        help="关闭额外 PDF 页面证据与序列映射")
    parser.add_argument("--legacy-text-sequences", dest="template_completion", action="store_false",
                        help="沿用原全文序列扫描与六字段 LLM 拼接；默认使用固定 schema 与 TST")
    parser.add_argument("--no-text-sequences", dest="text_sequences", action="store_false",
                        help="关闭正文模型调用，仍建立固定 schema、查询 WIPO 序列表并检查缺失")
    args = vars(parser.parse_args())
    image_dir, output_dir = args.pop("image_dir"), args.pop("output_dir")
    try:
        config = PipelineConfig(**args)
    except ValueError as exc:
        parser.error(str(exc))
    summary = asyncio.run(run_batch(image_dir, output_dir, config))
    for document_id, issues in summary["text_sequences"].get("warnings", {}).items():
        print(f"{document_id} 正文部分完成，{len(issues)} 个问题已备注："
              f"{output_dir / document_id / 'review_issues.txt'}", flush=True)
    failed = summary["failed"] or summary["data_construction"]["failed"] or summary["text_sequences"]["failed"]
    if failed:
        print("批处理已结束，但有阶段未全部成功；已保存的结果保留。", file=sys.stderr, flush=True)
        print(f"图片失败 {summary['failed']} 张，最终构建失败 {summary['data_construction']['failed']} 篇，"
              f"正文处理失败 {summary['text_sequences']['failed']} 篇。",
              file=sys.stderr, flush=True)
        print(f"详情见：{output_dir / '_batch_summary.json'}", file=sys.stderr, flush=True)
        for document_id, error in summary["data_construction"]["errors"].items():
            print(f"{document_id}: {error}", file=sys.stderr, flush=True)
        for document_id, error in summary["text_sequences"]["errors"].items():
            if isinstance(error, dict):
                for chunk_id, detail in error.items():
                    print(f"{document_id}/{chunk_id} 正文提取失败：{detail}", file=sys.stderr, flush=True)
            else:
                print(f"{document_id} 正文序列: {error}", file=sys.stderr, flush=True)
        print("退出码 1 表示本批存在处理失败；正文部分校验失败且有可用结果时，会备注问题并正常结束。",
              file=sys.stderr, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
