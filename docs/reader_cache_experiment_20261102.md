Reader 输入缓存实验，2026-11-02

结论：本轮没有验证出值得接入正式 Reader 的缓存收益。保留现有 Reader 输入与调用方式；本次只新增独立实验脚本、测试和报告。实验前的工作已在 `90c5131` 提交并推送到 `feat/reader-relation-index`。

使用 `codex-cli 0.160.0`、`gpt-6.1-sol`、`xhigh`、同一输出 Schema、完整 initial_dataset 和同一原始 OCR 来源。全部 62 个 chunk 通过字段和值的还原检查，保留数组顺序、来源 ID、原文、seed、上下文范围和元数据。真实调用有 18 次，包含 1 次 setup、1 次用量校准和 1 次超时；没有运行 OCR、Template、Action 或 Answer。所有原始实验产物均位于 `try/agent_handoff/runs/` 的独立目录。

先对 c7、c26 测试三种输入。比例为两次真实调用的缓存读取 tokens / 输入 tokens，包含第一块，不代表稳态保证。

| 输入方式 | 输入 tokens | 缓存读取 tokens | 读取比例 |
| --- | ---: | ---: | ---: |
| 原版单 user 消息、每次独立目录 | 77,234 | 18,688 | 24.20% |
| 公共字段前置、固定目录 | 77,226 | 9,344 | 12.10% |
| 公共字段独立为 developer 消息、固定目录 | 77,392 | 18,688 | 24.15% |

后两种方式均未观察到公共 seed 带来的额外命中。样本很小、服务器路由与负载会波动；这不是“所有前缀缓存均无效”的结论。原版也已有部分缓存读取，不能将其描述为完全没有缓存。

进一步选取 c7、c26、c44、c46、c47、c51，覆盖普通文字、名称/关联、实验描述、密集序列表及混合测量表。会话方案先建立仅有完整公共背景的 setup，要求输出所有数组为空，再让每个 Reader 从同一个 setup 会话 fork。Reader 之间不继承其他 chunk 的提取结果。setup 实际消耗 33,882 输入 tokens、98 输出 tokens、16.562 秒。

原版 c51 在 600 秒超时，没有终端用量，保留为未知。以下只比较成功配对的五块 c7、c26、c44、c46、c47；原版另外那次失败的成本不在表中，不能由此推算整轮费用。

| 五块配对结果 | 输入 tokens | 缓存读取 tokens | 未复用输入 tokens |
| --- | ---: | ---: | ---: |
| 原版 Reader | 197,456 | 19,712 | 177,744 |
| fork Reader，未加 setup | 199,909 | 37,376 | 162,533 |
| fork Reader，加一次 setup | 233,791 | 37,376 | 196,415 |

加 setup 后未复用输入增加 10.50%。缓存读取量最高仍为每次 9,344，没有观察到覆盖完整公共 seed 的大段新增缓存。CLI 没有在终端事件中分别报告缓存写入计费；“输入减缓存读取”可能包含缓存写入，不能直接折算金额。负载、推理长度及并发会影响耗时，本轮不宣称吞吐或整篇处理速度提升。

用量校准确认 fork 的 `turn.completed.usage` 包含 setup 的历史累计量。每个 child 的实际新增用量必须扣除 setup 快照，setup 另计一次。独立校准的累计输入为 68,101，减 setup 33,882 后为 34,219，和持久化 rollout 的 `last_token_usage.input_tokens` 一致；其他用量字段也逐项一致。报告同时保留原始累计值和扣除后的值，重复生成报告不会再次扣除。校准调用本身另有 34,219 输入、42 输出 tokens，不属于拟议运行方案的必要 setup。

质量核对结果：

- 同期成功配对的五块中，66 条已接受的序列签名逐字符一致；数值测量未见差异。两版都拒绝了 c47 中包含非法原始 OCR 字符的 SEQ 91、93 定义，未放宽校验。
- 六块另外都有一轮同输入的历史原版响应，逐块验证了证据、完整 payload、指令、Schema、模型和 effort 的请求指纹。与该历史参考比较，76 条已接受序列的定义和值全部一致，数值测量未见差异。历史响应属于辅助比较，不能替代同期性能对照或人工金标准。
- 本轮 fork 的 c26 漏提取 `Pitrakinra`，同期及历史原版都包含它，原文来源 4482 明确提及。不能承诺改变消息结构后精度不变，也不能将这处差异归因于缓存计算本身。
- 定性测量有 `a lack of binding` / `lack of binding` 的字面差异；实验描述、标签和显式关系边也不同，部分关系仍由 observation.experiment 引用。差异数量不等于错误数量，尚未逐条完成人工语义判定。
- 本轮未执行全篇 Action 整合，不构成最终 Dataset2 的精度保证。

建议先在能够检查真实请求、设置适合模型的缓存边界的调用层验证稳定命中，再做完整质量对照。此次不把字段重排、developer 拆分或会话 fork 默认启用。[官方缓存边界说明](https://developers.openai.com/api/docs/guides/prompt-caching#gotchas)

复现入口：

```bash
python try/agent_handoff/benchmark_reader_cache.py \
  --baseline /path/to/original/dataagent2 \
  --output /path/to/new/cache-probe --chunks c7 c26

python try/agent_handoff/benchmark_reader_fork.py \
  --baseline /path/to/original/dataagent2 \
  --probe /path/to/new/cache-probe \
  --output /path/to/new/cache-fork --workers 6
```

实验以新输出目录隔离。重启时保留已实际执行调用的原始用量和耗时，不把本地响应重放记成新增模型缓存命中。失败请求用量不补零。新一轮随机重复应使用新目录。

结构化快照见 `try/agent_handoff/benchmarks/49-Sructure-reader-cache-20261102.json`。原始请求、CLI 参数、响应、终端事件、逐块差异、拒绝记录及校准 rollout 保存在同名前缀的本地实验目录。相关 50 项测试通过。
