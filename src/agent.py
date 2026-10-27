"""Codex CLI model adapter, request artifacts and actual token accounting."""

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from units.token_usage import parse_codex_events, record_current_usage


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
