"""Describe the first unfinished extraction stage without hiding partial results."""


def failure_details(report):
    if report.get("benchmark_complete"):
        return None
    coverage = report.get("coverage", {})
    if not report.get("reader_complete"):
        stage, label = "reader", "全文读取"
        errors = {**coverage.get("failed_chunks", {}), **coverage.get("skipped_chunks", {})}
        error = "\n".join(f"{key}: {value}" for key, value in errors.items())
        reason = f"已完成 {coverage.get('completed_chunks', 0)}/{coverage.get('total_chunks', 0)} 个分块"
        paths = []
    elif not report.get("action_complete"):
        stage, label = "action", "ActionAgent 整合"
        status = report.get("action_agent", {})
        error = status.get("error", "")
        paths = status.get("validation_paths", [])
        reasons = []
        if "Sequence anchors absent or ambiguous" in error:
            reasons.append("序列片段无法唯一定位原文")
        if "Domain completeness review errors" in error:
            reasons.append("域复核引用或组装结果不一致")
        reason = "；".join(reasons) or error.split("\n", 1)[0][:300] or "未完成完整数据集校验"
    elif report.get("question_api_failures"):
        stage, label = "questions", "定向问答"
        error = reason = f"{report['question_api_failures']} 个问答请求失败"
        paths = []
    else:
        stage, label = "answer_action", "问答结果整合"
        status = report.get("answer_action", {})
        error = status.get("error", "")
        reason = error.split("\n", 1)[0][:300] or "问答结果未完成整合"
        paths = status.get("validation_paths", [])
    return {"stage": stage, "message": f"{label}失败：{reason}", "error": error,
            "validation_paths": paths,
            "reader_completed_chunks": coverage.get("completed_chunks", 0),
            "reader_total_chunks": coverage.get("total_chunks", 0)}
