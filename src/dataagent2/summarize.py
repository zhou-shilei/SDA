"""Render saved benchmark progress without making model requests."""

import argparse
from pathlib import Path

from .inputs import read
from .provider import write_json


def summarize(output):
    output = Path(output)
    rows = []
    for folder in sorted(output.iterdir()):
        if not folder.is_dir() or not (folder / "manifest.json").is_file():
            continue
        manifest = read(folder / "manifest.json")
        progress = read(folder / "progress.json", {})
        report = read(folder / "report.json", {})
        usage = read(folder / "token_usage.json", {})
        coverage = progress.get("coverage", progress)
        finished = progress.get("phase") == "finished" and report.get("benchmark_complete", False)
        rows.append({
            "document": manifest["document_id"],
            "complete": bool(finished),
            "completed_chunks": coverage.get("completed_chunks", 0),
            "total_chunks": len(manifest["chunks"]),
            "known_new_tokens": usage.get("total_tokens", 0),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "unknown_attempts": usage.get("unknown_requests", 0),
            "reused_tokens": manifest["baseline"]["shared_stage_tokens"],
            "comparable_tokens": report.get("comparable_model_total_tokens") if finished else None,
            "old_tokens": manifest["baseline"]["total_tokens"],
            "first_pass": report.get("first_pass") if finished else None,
            "final": report.get("final") if finished else None,
            "reference_comparison": report.get("reference_comparison") if finished else None,
            "reader_failures": len(coverage.get("failed_chunks", {})),
        })

    def number(value):
        return f"{value:,}" if value is not None else "待完成"

    lines = ["# dataagent2 实测记录", "",
        "已知用量直接来自模型终态事件；未知尝试未记为零，因此存在未知值时只能报告已知下限。", "",
        "| 专利 | 完成主块 | 新流程已知 token | 未知用量尝试 | 复用前置 token | 可比较合计 | 历史基线 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in rows:
        prefix = "≥" if row["unknown_attempts"] else ""
        lines.append(f"| {row['document']} | {row['completed_chunks']}/{row['total_chunks']} | "
            f"{prefix}{number(row['known_new_tokens'])} | {row['unknown_attempts']} | "
            f"{number(row['reused_tokens'])} | "
            f"{prefix if row['comparable_tokens'] is not None else ''}{number(row['comparable_tokens'])} | "
            f"{number(row['old_tokens'])} |")
    lines += ["", "可比较合计只在全文阅读结束后显示，是新增请求用量加复用的历史分类/模板成本；未重新运行 OCR 或前置阶段。", "",
        "| 专利 | 首轮 VH/VL 链 | 补全后链 | 原始模板实体链 | 完整配对 | 测量记录 | 未解决问题 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in rows:
        if not row["complete"]:
            lines.append(f"| {row['document']} | 待完成 | 待完成 | 待完成 | 待完成 | 待完成 | 待完成 |")
            continue
        report = read(output / row["document"] / "report.json")
        first, final = row["first_pass"], row["final"]
        lines.append(f"| {row['document']} | {first['variable_chains']} | {final['variable_chains']} | "
            f"{final['seed_variable_chains']} | {final['complete_pairs']} | {final['observations']} | "
            f"{report['questions_remaining']} |")
    lines += ["", "链数是实体×VH/VL 的已支持槽位数，共享序列会计入每个使用实体；不是不同氨基酸字符串的个数。",
        "测量数包括复用的原始表格测量。后续修复结果只用于离线对比，不进入提示词，也不是人工金标准。",
        "两份旧基线的修复范围不同；提取覆盖降低时，不能把用量降低称为同等质量的改进。", ""]
    (output / "BENCHMARK_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(output / "benchmark_summary.json", rows)
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    print(summarize(parser.parse_args().output))
