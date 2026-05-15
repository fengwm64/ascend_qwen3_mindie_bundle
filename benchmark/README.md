# MindIE Qwen3 API Benchmark

针对本目录里部署的 6 个 Qwen3 MindIE 服务的端到端基准测试脚本。

## 测什么

- **模型**：`0.6B / 1.7B / 4B / 8B / 14B / 32B`（端口映射见根目录 `README.md`）
- **场景**（5 种）：
  - `short_in_short_out`：短输入 + 短输出（闲聊/分类）
  - `short_in_long_out`：短输入 + 长输出（生成/写作）
  - `long_in_short_out`：长输入 + 短输出（RAG 摘要/抽取）
  - `long_in_long_out`：长输入 + 长输出（长文档改写）
  - `mixed_random`：从前 4 种均匀随机采样（模拟真实流量）
- **并发**：`2, 4, 8, 16`
- **采样**：`N = concurrency × samples-per-concurrency`（默认 4，即并发 16 跑 64 次）
- **协议**：OpenAI 兼容 `/v1/chat/completions`，**SSE 流式**

## 指标

| 指标 | 含义 |
|---|---|
| TTFT | Time-To-First-Token，请求发出到第一个 `delta.content` 到达的耗时 |
| TPOT | (E2E − TTFT) / (output_tokens − 1)，每输出 token 的平均耗时 |
| E2E | 端到端延迟 |
| req/s | 该组成功请求数 ÷ 该组 wall time |
| out_tok/s | 该组成功请求的输出 token 总和 ÷ wall time |
| 成功率 | 成功请求 / 总请求 |

每个指标给出 mean / p50 / p90 / p99。

## 安装

```bash
cd benchmark
pip install -r requirements.txt
```

仅依赖 `httpx`，无需 `transformers` 或 tokenizer 文件。

## 运行

完整矩阵（6 模型 × 5 场景 × 4 并发，约 4800 次请求，耗时较长）：

```bash
python benchmark_mindie.py --host 127.0.0.1
```

冒烟测试（单模型 / 单场景 / 单并发）：

```bash
python benchmark_mindie.py --models 8B --scenarios short_in_short_out --concurrency 2 --samples-per-concurrency 2
```

只测部分模型 / 场景：

```bash
python benchmark_mindie.py --models 8B,32B --scenarios long_in_long_out,mixed_random --concurrency 4,16
```

## CLI 参数

| 参数 | 默认 | 含义 |
|---|---|---|
| `--host` | `127.0.0.1` | 服务主机（脚本会拼上各模型的端口） |
| `--models` | `all` | `all` 或逗号分隔，如 `8B,32B` |
| `--scenarios` | `all` | 同上 |
| `--concurrency` | `2,4,8,16` | 逗号分隔的并发数列表 |
| `--samples-per-concurrency` | `4` | 每组请求数 = 并发 × 该值 |
| `--short-tokens` | `128` | 短输入/输出近似 token 长度 |
| `--long-tokens` | `2048` | 长输入/输出近似 token 长度 |
| `--timeout` | `610` | 客户端超时（秒）；服务端 `e2eTimeout=600`，留一点余量 |
| `--output-dir` | `./results` | 报告输出目录 |
| `--seed` | `42` | mixed_random 与短问题采样随机种子 |
| `--no-warmup` | off | 关闭每个模型的 warmup 请求 |

## 输出

每次运行产出两份文件（按时间戳命名）：

- `results/YYYYMMDD_HHMMSS_bench.json` —— 完整明细：所有请求记录 + 配置 + 聚合
- `results/YYYYMMDD_HHMMSS_bench.md` —— 可读报告，按 `模型 → 场景` 分章节出汇总表

控制台会在每个 `模型×场景×并发` 跑完时打印一行简表，方便实时观察。

## 设计要点（如需修改）

- **滚动并发**：使用 `asyncio.Semaphore(concurrency)`，保证全程稳定占用 N 个 in-flight 请求，避免一次性 burst。
- **TTFT 边界**：取第一个 `choices[0].delta.content` 非空的事件时间戳，不是 HTTP 头到达时间。
- **token 数**：通过 `stream_options.include_usage=true` 让服务端在最后一帧返回 `usage`，避免引入 tokenizer 依赖。若 backend 不返回，本组的 `tpot` 与 `*_tok_mean` 会显示为 `-`。
- **失败处理**：不重试。每条失败记录原因（HTTP 状态码 / 超时 / 连接错误等），统计成功率与按类型计数。
- **warmup**：每个模型首次访问前发一个 32 token 的请求做激活；warmup 失败则跳过该模型的全部场景。
- **跨模型串行**：6 个模型挂在不同 NPU 上但共享本机 CPU/网络，因此一次只测一个模型，避免互相干扰。
- **可中断**：`Ctrl-C` 会触发 finally 分支，把已采集的数据落盘后退出。

## 解读建议

- **判断服务质量**：先看 `成功率` 列；非 100% 直接看错误分类。
- **看延迟稳定性**：对比 p50 vs p99，差距越大说明高分位长尾越严重，通常发生在并发上升、长输入或混合负载时。
- **看吞吐瓶颈**：`out_tok/s` 在并发翻倍时如果不再增长，说明已达到该模型的解码吞吐上限。
- **看 TTFT 退化**：并发翻倍后 TTFT 显著上升，说明 prefill 排队明显，可考虑调大 `maxPrefillBatchSize`。
