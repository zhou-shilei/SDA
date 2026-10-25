import json
import asyncio
import subprocess
import re
import math
import tempfile
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from costructure import construct_records, get_document_id
from token_usage import parse_codex_events, record_current_usage, usage_scope


async def _chat_with_usage(llm, stage, *, system, user):
    with usage_scope(stage=stage):
        return await llm.chat(system=system, user=user)

# codex for llm
class codexllm:
    cache_identity = {"provider": "codex_cli", "model": "gpt-6-luna", "effort": "low", "version": 1}
    REQUEST_TIMEOUT_SECONDS = 180
    PROCESS_STOP_TIMEOUT_SECONDS = 5

    def __init__(self, usage_recorder=None, *, timeout=None, artifact_dir=None, output_schema=None,
                 model="gpt-6-luna", effort="low", transport=None):
        if transport not in {None, "http"}:
            raise ValueError("transport must be None or 'http'")
        self.model = model
        self.effort = effort
        self.transport = transport
        self.cache_identity = {"provider": "codex_cli", "model": model, "effort": effort, "version": 1}
        self.usage_recorder = usage_recorder
        self.timeout = timeout or self.REQUEST_TIMEOUT_SECONDS
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.request_index = 0
        self.output_schema = output_schema

    async def _stop_and_collect(self, process, communication):
        """回收 CLI，同时尽量保留超时/取消前已输出的实际用量。"""
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=self.PROCESS_STOP_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        try:
            return await asyncio.wait_for(
                asyncio.shield(communication), timeout=self.PROCESS_STOP_TIMEOUT_SECONDS
            )
        except BaseException:
            # 子进程退出后，继承管道的后代进程仍可能阻止 EOF；不要无限等待。
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)
            return b"", b""

    async def chat(self, *, system, user):
        self.request_index += 1
        artifact = None
        if self.artifact_dir:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            artifact = self.artifact_dir / f"request-{self.request_index:03d}"
            artifact.with_suffix(".input.txt").write_text(f"{system}\n{user}", encoding="utf-8")
        # 原生异步子进程避免默认线程池限制高并发；取消任务时同时回收进程。
        command = ["codex", "exec", "--json",
                   "--model", self.model,
                   "-c", f"model_reasoning_effort={self.effort}",
                   "--sandbox", "read-only",
                   "--skip-git-repo-check", "-"]
        if self.transport == "http":
            command[-1:-1] = ["-c", 'model_provider="sda-http"',
                               "-c", 'model_providers.sda-http.name="OpenAI HTTP"',
                               "-c", 'model_providers.sda-http.wire_api="responses"',
                               "-c", "model_providers.sda-http.requires_openai_auth=true",
                               "-c", "model_providers.sda-http.supports_websockets=false",
                               "-c", f"model_providers.sda-http.stream_idle_timeout_ms={int(self.timeout * 1000)}",
                               "-c", "model_providers.sda-http.stream_max_retries=0"]
        schema_temporary = None
        if self.output_schema:
            if artifact:
                schema_path = artifact.with_suffix(".schema.json")
            else:
                schema_temporary = tempfile.TemporaryDirectory(prefix="sda-output-schema-")
                schema_path = Path(schema_temporary.name) / "schema.json"
            schema_path.write_text(json.dumps(self.output_schema, ensure_ascii=False), encoding="utf-8")
            command[-1:-1] = ["--output-schema", str(schema_path)]
        stdout, stderr, parsed = b"", b"", None
        process, communication = None, None
        status, error_type = "failed", None
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            # shield 保留 communicate 的缓冲区，超时/取消后仍可收集已产生的事件。
            communication = asyncio.create_task(
                process.communicate(f"{system}\n{user}".encode("utf-8"))
            )
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communication), timeout=self.timeout
            )
            if process.returncode:
                raise subprocess.CalledProcessError(
                    process.returncode, command, output=stdout.decode("utf-8", errors="replace"),
                    stderr=stderr.decode("utf-8", errors="replace"),
                )
            parsed = parse_codex_events(stdout.decode("utf-8", errors="replace"))
            reply = parsed.get("reply")
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("Codex CLI 未返回 assistant 消息")
            status = "ok"
            return reply.strip()
        except BaseException as exc:
            error_type = type(exc).__name__
            if isinstance(exc, asyncio.CancelledError):
                status = "cancelled"
            elif isinstance(exc, asyncio.TimeoutError):
                status = "timeout"
            if process is not None and communication is not None:
                cleanup = asyncio.create_task(self._stop_and_collect(process, communication))
                # 再次取消也要先完成回收，避免留下继续产生费用的 CLI 子进程。
                while True:
                    try:
                        stdout, stderr = await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        if cleanup.cancelled():
                            raise
                        continue
            raise
        finally:
            if artifact:
                artifact.with_suffix(".events.jsonl").write_bytes(stdout)
                artifact.with_suffix(".stderr.log").write_bytes(stderr)
            if schema_temporary:
                schema_temporary.cleanup()
            parsed = parsed or parse_codex_events(stdout.decode("utf-8", errors="replace"))
            record_current_usage(
                parsed.get("usage"), recorder=self.usage_recorder,
                model=self.model, effort=self.effort, status=status, error_type=error_type,
                thread_id=parsed.get("thread_id"), parser_warnings=parsed.get("warnings", []),
            )

# 简单agent的实现
class TableClassificationAgent:
    CATEGORIES = {"sequence", "assay", "other", "uncertain"}

    def __init__(self, llm):
        self.llm = llm
        prompt_path = Path(__file__).resolve().parents[1] / "prompt" / "table_classification.txt"
        self.system_prompt = prompt_path.read_text(encoding="utf-8")

    async def run(self, table_text):
        results, errors = await self.run_batch([
            {"table_id": "single", "table_text": table_text}
        ])
        if errors:
            raise ValueError(errors["single"])
        return results["single"]

    async def run_batch(self, tables):
        """按 ID 匹配结果；有效项保留，无效项交给调度器单独重试。"""
        expected = {table["table_id"] for table in tables}
        if len(expected) != len(tables):
            raise ValueError("输入 table_id 重复")
        response = await _chat_with_usage(self.llm, "classification",
            system=self.system_prompt,
            user=json.dumps({"tables": tables}, ensure_ascii=False),
        )
        payload = json.loads(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("分类响应必须包含 results 数组")
        rows = payload["results"]
        if any(not isinstance(row, dict) or not isinstance(row.get("table_id"), str)
               for row in rows):
            raise ValueError("分类结果缺少字符串 table_id")
        unknown = {row["table_id"] for row in rows} - expected
        if unknown:
            raise ValueError(f"分类响应包含未知 table_id：{sorted(unknown)}")
        counts = Counter(row["table_id"] for row in rows)
        results = {}
        errors = {table_id: "分类响应缺少此表格" for table_id in expected - counts.keys()}
        for row in rows:
            table_id = row["table_id"]
            if counts[table_id] != 1:
                errors[table_id] = "分类响应的 table_id 重复"
            elif (not isinstance(row.get("category"), str)
                  or row["category"] not in self.CATEGORIES
                  or not isinstance(row.get("reason"), str)):
                errors[table_id] = "分类标签或 reason 无效"
            else:
                results[table_id] = {"category": row["category"], "reason": row["reason"]}
        return results, errors

class AssayExtractionAgent:
    """从表格文字中提取实验数据，返回记录列表。"""

    def __init__(self, llm):
        self.llm = llm
        prompt_path = Path(__file__).resolve().parents[1] / "prompt" / "assay_extraction.txt"
        self.system_prompt = prompt_path.read_text(encoding="utf-8")

    async def run(self, table_text, *, table_context=None):
        payload = {"table_text": table_text}
        if table_context and table_context.get("support_tables"):
            payload["extraction_context"] = table_context
        response = await _chat_with_usage(self.llm, "assay_extraction",
            system=self.system_prompt,
            user=json.dumps(payload, ensure_ascii=False),
        )
        records = json.loads(response)["records"]
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError("提取响应的 records 必须为对象数组")
        return records


class SequenceExtractionAgent:
    """从表格文字中提取抗体序列及编号，返回记录列表。"""

    def __init__(self, llm):
        self.llm = llm
        prompt_path = Path(__file__).resolve().parents[1] / "prompt" / "sequence_extraction.txt"
        self.system_prompt = prompt_path.read_text(encoding="utf-8")

    async def run(self, table_text, *, table_context=None):
        payload = {"table_text": table_text}
        if table_context and table_context.get("support_tables"):
            payload["extraction_context"] = table_context
        response = await _chat_with_usage(self.llm, "sequence_extraction",
            system=self.system_prompt,
            user=json.dumps(payload, ensure_ascii=False),
        )
        records = json.loads(response)["records"]
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError("提取响应的 records 必须为对象数组")
        return records


class UnifiedTableExtractionAgent:
    """一次读取当前表格，同时提取实验与序列；没有对应数据时返回空数组。"""

    RECORD_FIELDS = {
        "assay_records": {"name", "metric", "value", "unit"},
        "sequence_records": {"name", "region", "sequence", "seq_id"},
    }

    def __init__(self, llm):
        self.llm = llm
        self.system_prompt = (Path(__file__).resolve().parents[1] / "prompt"
                              / "unified_table_extraction.txt").read_text(encoding="utf-8")

    @classmethod
    def validate_response(cls, payload):
        if not isinstance(payload, dict) or set(payload) != set(cls.RECORD_FIELDS):
            raise ValueError("统一提取响应必须且只能包含 assay_records 和 sequence_records")
        for kind, fields in cls.RECORD_FIELDS.items():
            rows = payload[kind]
            if not isinstance(rows, list):
                raise ValueError(f"{kind} 必须为对象数组")
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or set(row) != fields:
                    raise ValueError(f"{kind}[{index}] 必须且只能包含 {sorted(fields)}")
                for field, value in row.items():
                    if value is None or isinstance(value, str):
                        continue
                    if (kind == "assay_records" and field == "value"
                            and type(value) in (int, float)
                            and (not isinstance(value, float) or math.isfinite(value))):
                        continue
                    raise ValueError(f"{kind}[{index}].{field} 的类型或数值无效")
        return payload

    async def run(self, table_text, *, table_context=None):
        payload = {"table_text": table_text}
        if table_context and table_context.get("support_tables"):
            payload["extraction_context"] = table_context
        response = await _chat_with_usage(self.llm, "unified_extraction",
            system=self.system_prompt, user=json.dumps(payload, ensure_ascii=False))
        return self.validate_response(json.loads(response))


class TextSequenceExtractionAgent:
    """从正文块发现新序列与编号关系，并校验逐字来源。"""

    def __init__(self, llm):
        self.llm = llm
        self.system_prompt = (Path(__file__).resolve().parents[1] / "prompt"
                              / "text_sequence_extraction.txt").read_text(encoding="utf-8")

    async def run(self, chunk):
        from text_sequences import validate_extraction

        response = await _chat_with_usage(self.llm, "text_sequences", system=self.system_prompt,
                                       user=json.dumps(chunk, ensure_ascii=False))
        return validate_extraction(json.loads(response), chunk)


class DataConstructionAgent:
    """关联同一文档的单表提取结果，返回六字段最终记录。

    用法：records = await DataConstructionAgent(llm).run(table_results)
    table_results 是同一文档的表格结果列表，可包含 table_text、assay_records、
    sequence_records 及来源元数据。每条 assay 输出一行，顺序与输入一致。
    METRIC 保存指标，VALUE 保存测量值，UNIT 单独保存单位。
    """

    FIELDS = {"NAME", "VH", "VL", "METRIC", "VALUE", "UNIT"}

    @staticmethod
    def _same_numeric_value(actual, value):
        """接受数值等价的 JSON 数字，拒绝布尔值、非有限值及字符串。"""
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in (actual, value)):
            return False
        try:
            candidate, expected = Decimal(str(actual)), Decimal(str(value))
        except InvalidOperation:
            return False
        return candidate.is_finite() and expected.is_finite() and candidate == expected

    def __init__(self, llm):
        self.llm = llm
        prompt_path = Path(__file__).resolve().parents[1] / "prompt" / "data_construction.txt"
        self.system_prompt = prompt_path.read_text(encoding="utf-8")

    async def run(self, table_results):
        if not isinstance(table_results, list) or any(not isinstance(table, dict) for table in table_results):
            raise ValueError("拼接输入必须为同一文档的表格结果数组")
        document_ids = {get_document_id(table) for table in table_results}
        if len(document_ids) > 1 or (len(table_results) > 1 and None in document_ids):
            raise ValueError("多表拼接必须能确认所有输入属于同一文档")
        assays = []
        for table in table_results:
            if table.get("error") or table.get("error_stage"):
                raise ValueError("拼接输入包含失败表格，请先处理或移除失败项")
            for field in ("assay_records", "sequence_records"):
                rows = table.get(field, [])
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError(f"{field} 必须为对象数组")
            assays.extend(table.get("assay_records", []))
        if not assays:
            return []
        # 明确同名的完整可变区由程序建立约束；冲突字段必须为null。
        construction = construct_records(table_results)
        constraints = [{field: row.get(field) for field in ("VH", "VL")}
                       for row in construction["records"]]
        response = await _chat_with_usage(self.llm, "construction",
            system=self.system_prompt,
            user=json.dumps({
                "tables": table_results,
                "sequence_constraints": [
                    {**values, "conflicting_regions": list(row["sequence_conflicts"])}
                    for values, row in zip(constraints, construction["records"])
                ],
                "measurement_templates": [
                    {"NAME": row.get("name"), "METRIC": row.get("metric"),
                     "VALUE": row.get("value"), "UNIT": row.get("unit")}
                    for row in assays
                ],
            }, ensure_ascii=False),
        )
        payload = json.loads(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ValueError("拼接响应必须包含 records 数组")
        records = payload["records"]
        if len(records) != len(assays):
            raise ValueError("拼接结果必须为每条实验记录保留一行，不得遗漏、合并或新增")
        for index, (record, assay) in enumerate(zip(records, assays)):
            if not isinstance(record, dict) or set(record) != self.FIELDS:
                raise ValueError(f"拼接记录 {index} 必须且只能包含 NAME、VH、VL、METRIC、VALUE、UNIT")
            if any(value is not None and (not isinstance(value, str) or not value.strip())
                   for key, value in record.items() if key != "VALUE"):
                raise ValueError(f"拼接记录 {index} 的非 VALUE 字段必须为非空字符串或 null")
            actual_value = record["VALUE"]
            if actual_value is not None and not (
                (isinstance(actual_value, str) and actual_value.strip())
                or self._same_numeric_value(actual_value, actual_value)
            ):
                raise ValueError(f"拼接记录 {index} 的 VALUE 必须为有限数字、非空字符串或 null")
            # 已有名称、指标、数值和单位不能被模型改写；缺失字段允许依据原表恢复。
            name, metric, value, unit = (assay.get(key) for key in ("name", "metric", "value", "unit"))
            if name is not None and name != "" and record["NAME"] != name:
                raise ValueError(f"拼接记录 {index} 名称或顺序不一致：期望 {name!r}，实际 {record['NAME']!r}")
            if metric is not None and metric != "" and record["METRIC"] != metric:
                raise ValueError(f"拼接记录 {index} 指标或顺序不一致：期望 {metric!r}，实际 {record['METRIC']!r}")
            if value is not None and value != "":
                matches = (isinstance(value, str) and actual_value == value) or self._same_numeric_value(actual_value, value)
                if not matches:
                    raise ValueError(f"拼接记录 {index} 数值或顺序不一致：期望 {value!r}，实际 {actual_value!r}")
                record["VALUE"] = value
            if unit is not None and unit != "" and record["UNIT"] != unit:
                raise ValueError(f"拼接记录 {index} 单位不一致：期望 {unit!r}，实际 {record['UNIT']!r}")
            for field in ("VH", "VL"):
                if field in construction["records"][index]["sequence_conflicts"]:
                    record[field] = None
                elif constraints[index][field] is not None:
                    record[field] = re.sub(r"\s+", "", constraints[index][field])
        return records


# main
if __name__ == "__main__":
    llm = codexllm()
    TableClassificationAgent = TableClassificationAgent(llm=llm)
    table_text = """抗体名称 | IC50 (nM)
抗体 A | 0.12
抗体 B | 0.35
"""
    result = asyncio.run(TableClassificationAgent.run(table_text))
    print(json.dumps(result, ensure_ascii=False, indent=2))
