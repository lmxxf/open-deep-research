"""Summarize recorded runs, without inventing quality scores or billing data."""

import argparse
import json
from pathlib import Path


def compare(root: Path, destination: Path):
    arms = ["a-thinking", "a-fast", "b-fast", "b-fast/revision-1"]
    rows = []
    sources = {}
    states = {}
    for arm in arms:
        folder = root / arm
        metrics = json.loads((folder / "metrics.json").read_text())
        state = json.loads((folder / "state.json").read_text())
        states[arm] = state
        config = {**state.get("config", {}), **metrics.get("config", {})}
        wall = metrics["wall_seconds"]
        human = metrics.get("human_wait_seconds", 0)
        automated = metrics.get("automated_wall_seconds", wall-human)
        sources[arm] = {s["url"] for s in state["sources"]}
        output = metrics["output_tokens"]
        low = (metrics["input_cache_hit_tokens"]*.003 + metrics["input_cache_miss_tokens"]*.15 + output*.6)/1_000_000
        priced = config.get("model") == "deepseek-flash" and metrics["token_totals_complete"]
        rows.append(dict(arm=arm, status=metrics["status"], wall_seconds=wall,
                         human_wait_seconds=human, automated_wall_seconds=automated,
                         profile=config.get("profile"), max_tokens=config.get("max_tokens"), model=config.get("model"),
                         has_style_sample=bool(state.get("style_sample")),
                         calls=metrics["model_calls"], serial_rounds=metrics["critical_path_model_rounds"],
                         input_tokens=metrics["input_tokens"], reasoning_tokens=metrics["reasoning_tokens"],
                         output_tokens=output, visible_output_tokens=metrics["visible_output_tokens"],
                         fetched_urls=metrics["fetched_urls"], cache_hits=metrics["fetch_cache_hits"],
                         source_count=metrics["source_count"], evidence_count=metrics["valid_evidence_count"],
                         body_characters=sum(len(s["body"]) for s in state["sections"]),
                         estimated_usd_range=[round(low, 6), round(low*2, 6)] if priced else None))
    initial_arms = arms[:3]
    input_checks = {field: dict(
        exact_match=all(field in states[a] for a in initial_arms) and
                    all(states[a].get(field) == states[initial_arms[0]].get(field) for a in initial_arms),
        missing_arms=[a for a in initial_arms if field not in states[a]],
        values={a: states[a].get(field) for a in initial_arms},
    ) for field in ("question", "brief", "answers")}
    data = dict(results=rows, input_checks=input_checks,
                comparison_scope="首稿三组精确比较输入；修订单独列出，不参与初始输入一致性判断",
                pricing_snapshot="2026-10-20", pricing_source="https://api-docs.deepseek.com/quick_start/pricing/",
                pricing_note="Flash peak/off-peak range, not the provider invoice; output already includes reasoning",
                shared_sources_a_fast_b_fast=sorted(sources["a-fast"] & sources["b-fast"]))
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "comparison.json").write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n")
    lines = ["# 同题运行记录", "", "这些数字由各运行目录的 metrics.json 汇总，不是手填。", "",
             "| 配置 | 总墙钟秒 | 人工等待秒 | 自动运行秒 | 模型调用 | 串行模型阶段 | Input | Reasoning | Output（含Reasoning） | 成功抓页/缓存命中 | 留存证据 | 正文字数（字符） |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['arm']} | {r['wall_seconds']} | {r['human_wait_seconds']} | {r['automated_wall_seconds']} | {r['calls']} | {r['serial_rounds']} | {r['input_tokens']} | {r['reasoning_tokens']} | {r['output_tokens']} | {r['fetched_urls']}/{r['cache_hits']} | {r['evidence_count']} | {r['body_characters']} |")
    lines += ["", "总墙钟包含终端澄清的人工等待；自动运行时间扣除这部分等待，仍包含网络与调度，不是纯模型计算时间。旧记录缺少时间细分时，人工等待按0、自动时间按墙钟回退。",
              "首稿与修订分别列出，修订行不是整条交付链的累计时间。修订行的留存证据数包含复用的旧证据，不代表重新抽取；其抓取数是本次增量。",
              "A留存证据数为0，表示它输出通用摘要而非可验证原文片段，不表示A没有读网页。",
              "", "## 对照条件", "",
              "以下一致性检查只比较三组首稿保存的原始字符串；answers即使是JSON字符串也不解析、归一化或重排后再比较。", ""]
    for field, check in input_checks.items():
        lines.append(f"- {field}：" + ("精确相同" if check["exact_match"] else "不相同或字段缺失") +
                     (f"；缺失组：{', '.join(check['missing_arms'])}" if check["missing_arms"] else ""))
    lines += ["", "| 运行 | profile | model | max_tokens | 文风样本 |",
              "|---|---|---|---:|---|"]
    for r in rows:
        lines.append(f"| {r['arm']} | {r['profile']} | {r['model']} | {r['max_tokens']} | {'有' if r['has_style_sample'] else '无'} |")
    token_caps = {r["max_tokens"] for r in rows[:3]}
    lines += ["", "首稿 max_tokens " + ("相同。" if len(token_caps) == 1 else "不同，输出预算差异也是混杂因素，不能忽略。"),
              "这不是严格单变量消融。",
              "A-thinking与A-fast同时改变思考开关及并发，不是单变量思考消融。",
              "A-fast与B-fast同为非思考并发，但仍改变对齐、查询、摘要/证据、补查和写作；不同检索结果不是固定语料对照。",
              "运行依次执行，网页缓存及API前缀缓存会影响后运行的组；缓存命中数已记录。",
              "只有一个题、一次采样，没有人为盲评或重复运行，不能据此给模型/开源项目排名。",
              "", "## 模型费用估算", "",
              "按[2026-10-20官方Flash价格](https://api-docs.deepseek.com/quick_start/pricing/)计算峰谷区间，实际扣费以服务端账单为准；不含人工时间。", ""]
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
