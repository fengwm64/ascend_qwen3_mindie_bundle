#!/usr/bin/env python3
"""MindIE / Qwen3 OpenAI 兼容 API 基准测试。

矩阵：模型 × 场景 × 并发，使用 SSE 流式协议测量 TTFT / TPOT / E2E。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

# 把脚本所在目录加入 sys.path，便于 `python benchmark/benchmark_mindie.py` 也能 import prompts
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompts import make_prompt

# ---------------------------------------------------------------------------
# 模型 / 端口注册表（与 README 表格保持一致）
# ---------------------------------------------------------------------------
MODEL_REGISTRY: dict[str, tuple[str, int]] = {
    "0.6B": ("Qwen3-0.6B-Mindie", 38001),
    "1.7B": ("Qwen3-1.7B-Mindie", 38003),
    "4B":   ("Qwen3-4B-Mindie",   38002),
    "8B":   ("Qwen3-8B-Mindie",   38000),
    "14B":  ("Qwen3-14B-Mindie",  38004),
    "32B":  ("Qwen3-32B-Mindie",  38005),
}

SCENARIOS = [
    "short_in_short_out",
    "short_in_long_out",
    "long_in_short_out",
    "long_in_long_out",
    "mixed_random",
]

DEFAULT_CONCURRENCIES = [2, 4, 8, 16]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class RequestRecord:
    model: str
    scenario: str
    concurrency: int
    sub_scenario: str
    ok: bool
    ttft_s: float | None
    e2e_s: float | None
    tpot_s: float | None
    input_tokens: int | None
    output_tokens: int | None
    error: str | None


# ---------------------------------------------------------------------------
# 场景参数解析
# ---------------------------------------------------------------------------
def scenario_params(
    scenario: str,
    short_tokens: int,
    long_tokens: int,
    rng: random.Random,
) -> tuple[int, int, str]:
    """返回 (input_tokens, max_output_tokens, sub_scenario_label)。"""
    if scenario == "short_in_short_out":
        return short_tokens, short_tokens, scenario
    if scenario == "short_in_long_out":
        return short_tokens, long_tokens, scenario
    if scenario == "long_in_short_out":
        return long_tokens, short_tokens, scenario
    if scenario == "long_in_long_out":
        return long_tokens, long_tokens, scenario
    if scenario == "mixed_random":
        sub = rng.choice(SCENARIOS[:4])
        in_tok, out_tok, _ = scenario_params(sub, short_tokens, long_tokens, rng)
        return in_tok, out_tok, sub
    raise ValueError(f"unknown scenario: {scenario}")


# ---------------------------------------------------------------------------
# 单次请求（SSE 流式）
# ---------------------------------------------------------------------------
async def make_single_call(
    client: httpx.AsyncClient,
    base_url: str,
    model_name: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    """发起一次 chat.completions 流式调用，返回采样字段字典。"""
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    t_first: float | None = None
    chunk_with_content = 0
    usage: dict[str, Any] | None = None

    try:
        async with client.stream(
            "POST",
            f"{base_url}/v1/chat/completions",
            json=payload,
            timeout=timeout,
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:200]
                return {
                    "ok": False,
                    "ttft_s": None,
                    "e2e_s": time.perf_counter() - t0,
                    "tpot_s": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "error": f"HTTP_{resp.status_code}: {body}",
                }

            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj.get("usage"), dict):
                    usage = obj["usage"]
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    if t_first is None:
                        t_first = time.perf_counter()
                    chunk_with_content += 1
    except httpx.TimeoutException as e:
        return {
            "ok": False,
            "ttft_s": (t_first - t0) if t_first else None,
            "e2e_s": time.perf_counter() - t0,
            "tpot_s": None,
            "input_tokens": None,
            "output_tokens": None,
            "error": f"Timeout: {e!s}",
        }
    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
        return {
            "ok": False,
            "ttft_s": (t_first - t0) if t_first else None,
            "e2e_s": time.perf_counter() - t0,
            "tpot_s": None,
            "input_tokens": None,
            "output_tokens": None,
            "error": f"{type(e).__name__}: {e!s}",
        }
    except Exception as e:
        return {
            "ok": False,
            "ttft_s": (t_first - t0) if t_first else None,
            "e2e_s": time.perf_counter() - t0,
            "tpot_s": None,
            "input_tokens": None,
            "output_tokens": None,
            "error": f"{type(e).__name__}: {e!s}",
        }

    t_end = time.perf_counter()
    if t_first is None:
        return {
            "ok": False,
            "ttft_s": None,
            "e2e_s": t_end - t0,
            "tpot_s": None,
            "input_tokens": (usage or {}).get("prompt_tokens"),
            "output_tokens": (usage or {}).get("completion_tokens") or 0,
            "error": "no_content_received",
        }

    completion_tokens = (usage or {}).get("completion_tokens")
    prompt_tokens = (usage or {}).get("prompt_tokens")
    e2e = t_end - t0
    ttft = t_first - t0
    tpot = None
    if completion_tokens and completion_tokens > 1:
        tpot = (e2e - ttft) / (completion_tokens - 1)
    return {
        "ok": True,
        "ttft_s": ttft,
        "e2e_s": e2e,
        "tpot_s": tpot,
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens if completion_tokens is not None else None,
        "error": None,
    }


# ---------------------------------------------------------------------------
# 单组（模型 × 场景 × 并发）跑批
# ---------------------------------------------------------------------------
async def run_group(
    client: httpx.AsyncClient,
    base_url: str,
    model_key: str,
    model_name: str,
    scenario: str,
    concurrency: int,
    n_requests: int,
    short_tokens: int,
    long_tokens: int,
    timeout: float,
    rng: random.Random,
) -> tuple[list[RequestRecord], float]:
    sem = asyncio.Semaphore(concurrency)
    records: list[RequestRecord] = []

    async def one() -> None:
        in_tok, out_tok, sub = scenario_params(scenario, short_tokens, long_tokens, rng)
        want_long_out = sub.endswith("long_out")
        system, user = make_prompt(in_tok, want_long_out, rng)
        async with sem:
            r = await make_single_call(
                client, base_url, model_name, system, user, out_tok, timeout
            )
        records.append(RequestRecord(
            model=model_key,
            scenario=scenario,
            concurrency=concurrency,
            sub_scenario=sub,
            ok=r["ok"],
            ttft_s=r["ttft_s"],
            e2e_s=r["e2e_s"],
            tpot_s=r["tpot_s"],
            input_tokens=r["input_tokens"],
            output_tokens=r["output_tokens"],
            error=r["error"],
        ))

    t_start = time.perf_counter()
    tasks = [asyncio.create_task(one()) for _ in range(n_requests)]
    await asyncio.gather(*tasks)
    wall = time.perf_counter() - t_start
    return records, wall


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------
def _percentile(sorted_vals: list[float], p: float) -> float:
    """取经验分位。sorted_vals 必须已经排序、非空。"""
    idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * p))
    return sorted_vals[idx]


def _stats(values: list[float], percentiles: tuple[float, ...] = (0.5, 0.9, 0.99)) -> dict[str, float | None]:
    if not values:
        return {"mean": None, **{f"p{int(p*100)}": None for p in percentiles}}
    s = sorted(values)
    return {
        "mean": statistics.mean(s),
        **{f"p{int(p*100)}": _percentile(s, p) for p in percentiles},
    }


def aggregate(records: list[RequestRecord], wall_time: float) -> dict[str, Any]:
    ok = [r for r in records if r.ok]
    err = [r for r in records if not r.ok]
    error_counts: dict[str, int] = {}
    for r in err:
        key = (r.error or "unknown").split(":")[0]
        error_counts[key] = error_counts.get(key, 0) + 1

    out_toks = [r.output_tokens for r in ok if r.output_tokens]
    in_toks = [r.input_tokens for r in ok if r.input_tokens]

    return {
        "count_total": len(records),
        "count_ok": len(ok),
        "success_rate": (len(ok) / len(records)) if records else 0.0,
        "wall_time_s": wall_time,
        "ttft_s": _stats([r.ttft_s for r in ok if r.ttft_s is not None]),
        "e2e_s": _stats([r.e2e_s for r in ok if r.e2e_s is not None]),
        "tpot_s": _stats([r.tpot_s for r in ok if r.tpot_s is not None], (0.5, 0.9)),
        "input_tokens_mean": statistics.mean(in_toks) if in_toks else None,
        "output_tokens_mean": statistics.mean(out_toks) if out_toks else None,
        "throughput_req_per_s": (len(ok) / wall_time) if wall_time > 0 else 0.0,
        "throughput_output_tok_per_s": (sum(out_toks) / wall_time) if wall_time > 0 and out_toks else 0.0,
        "error_counts": error_counts,
    }


# ---------------------------------------------------------------------------
# 输出：控制台 / JSON / Markdown
# ---------------------------------------------------------------------------
def fmt(v: float | None, spec: str = ".3f") -> str:
    if v is None:
        return "  -  "
    return format(v, spec)


def print_group_row(model_key: str, scenario: str, concurrency: int, agg: dict[str, Any]) -> None:
    ttft = agg["ttft_s"]
    e2e = agg["e2e_s"]
    tpot = agg["tpot_s"]
    print(
        f"  [{model_key:>5}|{scenario:<19}|c={concurrency:>2}] "
        f"ok={agg['count_ok']:>2}/{agg['count_total']:<2} "
        f"({agg['success_rate']*100:5.1f}%)  "
        f"TTFT mean/p90={fmt(ttft['mean'])}/{fmt(ttft['p90'])}s  "
        f"E2E mean/p90={fmt(e2e['mean'])}/{fmt(e2e['p90'])}s  "
        f"TPOT mean={fmt(tpot['mean'])}s  "
        f"req/s={agg['throughput_req_per_s']:.2f}  "
        f"out_tok/s={agg['throughput_output_tok_per_s']:.1f}"
    )


def write_json_report(
    path: Path,
    config: dict[str, Any],
    records: list[RequestRecord],
    aggregates: dict[str, dict[str, Any]],
) -> None:
    payload = {
        "config": config,
        "aggregates": aggregates,
        "records": [asdict(r) for r in records],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def write_markdown_report(
    path: Path,
    config: dict[str, Any],
    aggregates: dict[str, dict[str, Any]],
) -> None:
    lines: list[str] = []
    lines.append(f"# MindIE Qwen3 Benchmark Report")
    lines.append("")
    lines.append(f"- 运行时间：{config['started_at']}")
    lines.append(f"- 主机：{config['host']}")
    lines.append(f"- 模型：{', '.join(config['models'])}")
    lines.append(f"- 场景：{', '.join(config['scenarios'])}")
    lines.append(f"- 并发：{config['concurrencies']}")
    lines.append(f"- 短/长 token：{config['short_tokens']} / {config['long_tokens']}")
    lines.append(f"- 每组请求数 = 并发 × {config['samples_per_concurrency']}")
    lines.append(f"- 超时：{config['timeout']}s")
    lines.append(f"- 总耗时：{config['total_wall_time_s']:.1f}s")
    lines.append("")

    # 按 model 分组
    by_model: dict[str, dict[str, dict[int, dict[str, Any]]]] = {}
    for key, agg in aggregates.items():
        model, scenario, conc_str = key.split("|")
        by_model.setdefault(model, {}).setdefault(scenario, {})[int(conc_str)] = agg

    for model in config["models"]:
        if model not in by_model:
            continue
        lines.append(f"## 模型 {model}（{MODEL_REGISTRY[model][0]} @ port {MODEL_REGISTRY[model][1]}）")
        lines.append("")
        for scenario in config["scenarios"]:
            if scenario not in by_model[model]:
                continue
            lines.append(f"### 场景 `{scenario}`")
            lines.append("")
            lines.append(
                "| 并发 | 成功/总数 | 成功率 | TTFT mean | TTFT p90 | TTFT p99 | "
                "E2E mean | E2E p90 | E2E p99 | TPOT mean | TPOT p90 | "
                "in_tok | out_tok | req/s | out_tok/s | 错误 |"
            )
            lines.append(
                "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"
            )
            for conc in sorted(by_model[model][scenario].keys()):
                a = by_model[model][scenario][conc]
                ttft, e2e, tpot = a["ttft_s"], a["e2e_s"], a["tpot_s"]
                errs = ", ".join(f"{k}×{v}" for k, v in a["error_counts"].items()) or "—"
                in_tok = a["input_tokens_mean"]
                out_tok = a["output_tokens_mean"]
                lines.append(
                    f"| {conc} | {a['count_ok']}/{a['count_total']} | "
                    f"{a['success_rate']*100:.1f}% | "
                    f"{fmt(ttft['mean'])} | {fmt(ttft['p90'])} | {fmt(ttft['p99'])} | "
                    f"{fmt(e2e['mean'])} | {fmt(e2e['p90'])} | {fmt(e2e['p99'])} | "
                    f"{fmt(tpot['mean'])} | {fmt(tpot['p90'])} | "
                    f"{fmt(in_tok, '.0f')} | {fmt(out_tok, '.0f')} | "
                    f"{a['throughput_req_per_s']:.2f} | {a['throughput_output_tok_per_s']:.1f} | "
                    f"{errs} |"
                )
            lines.append("")
    lines.append("")
    lines.append("> 单位：秒。TTFT = 首 token 到达；TPOT = (E2E − TTFT) / (output_tokens − 1)；")
    lines.append("> out_tok/s 为该组所有成功请求的输出 token 总量 / wall time。")
    path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Warmup
# ---------------------------------------------------------------------------
async def warmup(
    client: httpx.AsyncClient,
    base_url: str,
    model_name: str,
    short_tokens: int,
    timeout: float,
) -> tuple[bool, str | None]:
    system, user = make_prompt(short_tokens, want_long_output=False, rng=random.Random(0))
    r = await make_single_call(client, base_url, model_name, system, user, max_tokens=32, timeout=timeout)
    return r["ok"], r["error"]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="MindIE Qwen3 API benchmark")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--models", default="all", help='"all" 或逗号分隔，如 "8B,32B"')
    ap.add_argument("--scenarios", default="all", help='"all" 或逗号分隔')
    ap.add_argument("--concurrency", default=",".join(map(str, DEFAULT_CONCURRENCIES)),
                    help='逗号分隔，如 "2,4,8,16"')
    ap.add_argument("--samples-per-concurrency", type=int, default=4,
                    help="N = concurrency * 该值")
    ap.add_argument("--short-tokens", type=int, default=128)
    ap.add_argument("--long-tokens", type=int, default=2048)
    ap.add_argument("--timeout", type=float, default=610.0)
    ap.add_argument("--output-dir", default=str(Path(__file__).resolve().parent / "results"))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-warmup", action="store_true")
    return ap.parse_args()


def resolve_list(arg: str, full: list[str]) -> list[str]:
    if arg == "all":
        return list(full)
    out = [s.strip() for s in arg.split(",") if s.strip()]
    bad = [s for s in out if s not in full]
    if bad:
        raise SystemExit(f"unknown items: {bad}; valid: {full}")
    return out


async def main_async(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    models = resolve_list(args.models, list(MODEL_REGISTRY.keys()))
    scenarios = resolve_list(args.scenarios, SCENARIOS)
    concurrencies = [int(c.strip()) for c in args.concurrency.split(",") if c.strip()]

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"{run_id}_bench.json"
    md_path = out_dir / f"{run_id}_bench.md"

    print(f"=== MindIE Qwen3 Benchmark @ {run_id} ===")
    print(f"host={args.host}  models={models}  scenarios={scenarios}  concurrency={concurrencies}")
    print(f"short/long tokens = {args.short_tokens}/{args.long_tokens}  "
          f"samples/conc = {args.samples_per_concurrency}  timeout={args.timeout}s")
    print(f"output: {json_path}\n        {md_path}")

    all_records: list[RequestRecord] = []
    aggregates: dict[str, dict[str, Any]] = {}
    t_total_start = time.perf_counter()

    limits = httpx.Limits(
        max_connections=max(concurrencies) * 2,
        max_keepalive_connections=max(concurrencies),
    )

    try:
        # trust_env=False: 忽略系统的 HTTP(S)_PROXY / SOCKS_PROXY 环境变量，
        # 这些代理对打 localhost 的基准没有意义，反而可能因为依赖缺失（如 socksio）报错。
        async with httpx.AsyncClient(limits=limits, trust_env=False) as client:
            for model_key in models:
                model_name, port = MODEL_REGISTRY[model_key]
                base_url = f"http://{args.host}:{port}"
                print(f"\n--- Model {model_key} ({model_name}) @ {base_url} ---")

                if not args.no_warmup:
                    print(f"  [warmup] ", end="", flush=True)
                    ok, err = await warmup(client, base_url, model_name,
                                           args.short_tokens, args.timeout)
                    if not ok:
                        print(f"FAILED: {err}; 跳过该模型")
                        continue
                    print("ok")

                for scenario in scenarios:
                    for conc in concurrencies:
                        n = conc * args.samples_per_concurrency
                        records, wall = await run_group(
                            client, base_url, model_key, model_name,
                            scenario, conc, n,
                            args.short_tokens, args.long_tokens,
                            args.timeout, rng,
                        )
                        all_records.extend(records)
                        agg = aggregate(records, wall)
                        key = f"{model_key}|{scenario}|{conc}"
                        aggregates[key] = agg
                        print_group_row(model_key, scenario, conc, agg)
    except KeyboardInterrupt:
        print("\n!! 被中断，落盘已采集数据 ...")
    finally:
        total_wall = time.perf_counter() - t_total_start
        config = {
            "started_at": run_id,
            "host": args.host,
            "models": models,
            "scenarios": scenarios,
            "concurrencies": concurrencies,
            "samples_per_concurrency": args.samples_per_concurrency,
            "short_tokens": args.short_tokens,
            "long_tokens": args.long_tokens,
            "timeout": args.timeout,
            "seed": args.seed,
            "warmup": not args.no_warmup,
            "total_wall_time_s": total_wall,
        }
        write_json_report(json_path, config, all_records, aggregates)
        write_markdown_report(md_path, config, aggregates)
        print(f"\n=== Done in {total_wall:.1f}s. Reports written: ===")
        print(f"  {json_path}")
        print(f"  {md_path}")
    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
