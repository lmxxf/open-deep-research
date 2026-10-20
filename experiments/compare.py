"""Summarize recorded runs, without inventing quality scores or billing data."""

import argparse
import json
from pathlib import Path


def compare(root: Path, destination: Path):
    arms = ["a-thinking", "a-fast", "b-fast", "b-fast/revision-1"]
    rows = []
    sources = {}
    for arm in arms:
        folder = root / arm
        metrics = json.loads((folder / "metrics.json").read_text())
        state = json.loads((folder / "state.json").read_text())
        sources[arm] = {s["url"] for s in state["sources"]}
        output = metrics["output_tokens"]
        low = (metrics["input_cache_hit_tokens"]*.003 + metrics["input_cache_miss_tokens"]*.15 + output*.6)/1_000_000
        priced = metrics["config"]["model"] == "deepseek-flash" and metrics["token_totals_complete"]
        rows.append(dict(arm=arm, status=metrics["status"], wall_seconds=metrics["wall_seconds"],
                         calls=metrics["model_calls"], serial_rounds=metrics["critical_path_model_rounds"],
                         input_tokens=metrics["input_tokens"], reasoning_tokens=metrics["reasoning_tokens"],
                         output_tokens=output, visible_output_tokens=metrics["visible_output_tokens"],
                         fetched_urls=metrics["fetched_urls"], cache_hits=metrics["fetch_cache_hits"],
                         source_count=metrics["source_count"], evidence_count=metrics["valid_evidence_count"],
                         body_characters=sum(len(s["body"]) for s in state["sections"]),
                         estimated_usd_range=[round(low, 6), round(low*2, 6)] if priced else None))
    data = dict(results=rows, pricing_snapshot="2026-10-20", pricing_source="https://api-docs.deepseek.com/quick_start/pricing/",
                pricing_note="Flash peak/off-peak range, not the provider invoice; output already includes reasoning",
                shared_sources_a_fast_b_fast=sorted(sources["a-fast"] & sources["b-fast"]))
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "comparison.json").write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n")
    lines = ["# 同题运行记录", "", "这些数字由各运行目录的 metrics.json 汇总，不是手填。", "",
             "| 配置 | 秒 | 模型调用 | 串行模型阶段 | Input | Reasoning | Output（含Reasoning） | 成功抓页/缓存命中 | 留存证据 | 正文字数（字符） |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['arm']} | {r['wall_seconds']} | {r['calls']} | {r['serial_rounds']} | {r['input_tokens']} | {r['reasoning_tokens']} | {r['output_tokens']} | {r['fetched_urls']}/{r['cache_hits']} | {r['evidence_count']} | {r['body_characters']} |")
    lines += ["", "注意：修订行的留存证据数包含复用的旧证据，不代表重新抽取；其抓取数是本次增量。",
              "A留存证据数为0，表示它输出通用摘要而非可验证原文片段，不表示A没有读网页。",
              "", "## 对照条件", "",
              "三组用相同问题、简报、补充答案和模型别名；B额外使用明确标注的自写语态样本。",
              "A-thinking与A-fast同时改变思考开关及并发，不是单变量思考消融。",
              "A-fast与B-fast同为非思考并发，但仍改变对齐、查询、摘要/证据、补查和写作；不同检索结果不是固定语料对照。",
              "运行依次执行，网页缓存及API前缀缓存会影响后运行的组；缓存命中数已记录。",
              "只有一个题、一次采样，没有人为盲评或重复运行，不能据此给模型/开源项目排名。",
              "", "## 模型费用估算", "",
              "按[2026-10-20官方Flash价格](https://api-docs.deepseek.com/quick_start/pricing/)计算峰谷区间，实际扣费以服务端账单为准；不含人工时间。"]
    for r in rows:
        cost = r["estimated_usd_range"]
        lines.append(f"- {r['arm']}: " + (f"USD {cost[0]:.6f}–{cost[1]:.6f}" if cost else "无法完整估算"))
    lines += ["", "引用ID和原文匹配通过不等于每个结论都被引文支持；请结合各报告和evidence.json审阅。", ""]
    (destination/"comparison.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    compare(args.root, args.destination)
