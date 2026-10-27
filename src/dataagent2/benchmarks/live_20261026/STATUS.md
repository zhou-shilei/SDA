# 自动续跑状态

进度快照：2026-10-26T17:41:13.921102+08:00。独立后台进程 PID 2736107，第 2/8 轮，状态 waiting。
下一次尝试安排在 2026-10-26T17:44:42.001568+08:00。实时状态见 supervisor.json，后续轮次优先处理未读完的专利。
任务会在本次对话返回后继续运行；每轮结束会自动更新 BENCHMARK_REPORT.md。

| 专利 | 正文主块 | 当前结果 |
| --- | ---: | --- |
| WO2026086808A1 | 17/17 | 正式 VH/VL 槽位 4，纯文本补全目前未新增；问答阶段尚有失败请求待续跑 |
| WO2024020591A9 | 0/40 | 服务限流后尚未获得成功块，下一轮优先执行 |

第一篇正文首轮已知用量 548,274 tokens（输入 450,553、输出 97,721），
加上已发生的问答，目前新增流程累计已知用量 768,050。仍有未知用量尝试，以上均为已知下限。
完整流程比较尚未完成，不能报告同等质量的最终节省比例。

独立可选结果：第一篇的 dataset.with_computational_domains.json 包含 4 个正式槽位和
32 个 computed_domain 计算槽位，计入计算槽位后可组成 13 对。每条保留原始字符坐标、
SEQ ID 的 full_chain 范围和 ANARCI/IMGT 边界方法，不改写正式 dataset.json。
32 个计算候选中有 29 个与后续修复参考逐字符相同，3 个不同；这不是准确率，
参考也不是人工金标准。固定输入快照见 domain_probe_input.json。

代码已通过 28 项离线测试和编译检查，测试结果快照在 validation.json。
自动任务保留既往轮次，总共最多 8 轮，限流时逐步延长等待，遇到其他错误会停止。
所有成功的请求均从缓存复用；完整日志、token 和失败尝试保留在每篇输出目录中。

```bash
cat src/dataagent2/benchmarks/live_20261026/supervisor.json
PYTHONPATH=src python -m dataagent2.summarize src/dataagent2/benchmarks/live_20261026
```

在输出目录创建 supervisor.stop 可以阻止下一轮启动，当前批次会正常结束。
