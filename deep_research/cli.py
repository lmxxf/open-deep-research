"""CLI entry points, bounded runs, and safe allowlisted experiment export."""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from . import prompts
from .engine import Config, Research
from .metrics import save_json


def read(path):
    return Path(path).read_text(encoding="utf-8") if path else ""


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def require_empty(directory):
    if directory.exists() and any(directory.iterdir()):
        raise ValueError(f"目录已有文件，不覆盖：{directory}")


def options(parser):
    parser.add_argument("--profile", choices=["serial-thinking", "parallel-fast"])
    parser.add_argument("--model")
    parser.add_argument("--max-pages", type=int, default=6)
    parser.add_argument("--query-count", type=int, default=3)
    parser.add_argument("--max-rounds", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--model-timeout", type=float, default=120)
    parser.add_argument("--deadline", type=float, default=600)
    parser.add_argument("--max-calls", type=int, default=32)
    parser.add_argument("--max-tokens", type=int, default=8192)


def make_parser():
    p = argparse.ArgumentParser(description="轻量Deep Research；显式A/B流程与局部改稿")
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("run", "align"):
        q = commands.add_parser(name)
        group = q.add_mutually_exclusive_group(required=True)
        group.add_argument("--question")
        group.add_argument("--question-file")
        q.add_argument("--output", type=Path, required=True, help="新运行目录，不覆盖旧结果")
        q.add_argument("--brief", default="")
        q.add_argument("--brief-file")
        q.add_argument("--answers", default="")
        q.add_argument("--answers-file")
        q.add_argument("--style-sample", help="UTF-8中文样稿文件")
        q.add_argument("--interactive", action="store_true")
        q.add_argument("--accept-assumptions", action="store_true", help="明确接受未澄清范围，记录为假设")
        q.add_argument("--mode", choices=["A", "B"], default="B")
        options(q)
    q = commands.add_parser("revise")
    q.add_argument("--run-dir", type=Path, required=True)
    q.add_argument("--section", required=True)
    q.add_argument("--instruction", required=True)
    q.add_argument("--research", action="store_true")
    q.add_argument("--output", type=Path)
    options(q)
    q = commands.add_parser("chat")
    q.add_argument("--run-dir", type=Path, required=True)
    q = commands.add_parser("export", help="仅导出公开报告/计量/短证据，不导出.env或原网页")
    q.add_argument("--run-dir", type=Path, required=True)
    q.add_argument("--output", type=Path, required=True)
    return p


async def execute(args):
    mode = getattr(args, "mode", "B")
    profile = args.profile or ("serial-thinking" if mode == "A" else "parallel-fast")
    config = Config(mode=mode, profile=profile, **{k: getattr(args, k) for k in (
        "query_count", "max_pages", "max_rounds", "concurrency", "timeout", "model_timeout", "max_calls", "max_tokens", "deadline", "model")})
    if args.command == "revise":
        original = load(args.run_dir/"state.json")
        if original.get("status") != "complete":
            raise ValueError("只能修订已完成的报告")
        # Carry forward the original selected model unless explicitly overridden.
        config.model = config.model or original["config"].get("model")
        number = original.get("revision", 0)+1
        output = args.output or args.run_dir / f"revision-{number}"
    else:
        output = args.output
    require_empty(output)
    runner = Research(output, config)
    state, status = None, "failed"
    try:
        if args.command == "revise":
            state = await asyncio.wait_for(runner.revise(original, args.section, args.instruction, args.research), config.deadline)
        else:
            question = args.question or read(args.question_file)
            brief = args.brief+read(args.brief_file)
            answers = args.answers+read(args.answers_file)
            if args.command == "align":
                alignment, _ = await runner.llm.ask("align", prompts.ALIGN,
                                                  dict(question=question, brief=brief, answers=answers))
                state = dict(status="alignment_only", question=question, brief=brief, answers=answers, **alignment)
                save_json(output/"alignment.json", state)
                print(json.dumps(state, ensure_ascii=False, indent=2))
            else:
                state = await asyncio.wait_for(runner.run(question, brief, answers, read(args.style_sample),
                    args.interactive, args.accept_assumptions), config.deadline)
        status = state.get("status", "complete")
        if status == "needs_alignment":
            print("请回答这些问题后用 --answers-file 重跑，或显式 --accept-assumptions：")
            for question in state["alignment"]["questions"]:
                print("- "+question)
        else:
            print(f"结果：{output}")
        return 2 if status == "needs_alignment" else 0
    except BaseException as exc:
        # A failed attempt is evidence too; never silently rewrite it as a successful run.
        save_json(output/"failure.json", dict(error_type=type(exc).__name__,
                  message=str(exc).replace(runner.llm.key, "[REDACTED]"), status="failed"))
        save_json(output/"partial.json", dict(sources=runner.sources, evidence=runner.evidence, queries=runner.queries))
        raise
    finally:
        runner.metrics.finish(status=status, config={**vars(config), "model": runner.llm.model},
            valid_evidence_count=len(runner.evidence), rejected_quote_count=runner.rejected_quotes,
            source_count=len(runner.sources),
            evidence_note="A produces generic summaries, not verified quote evidence" if mode == "A" else "quotes match stored source text; entailment still needs human review")
        await runner.close()


def export_run(source, output):
    """Allowlist only. Raw prompts, webpage text and secrets never get copied."""
    require_empty(output)
    state = load(source/"state.json")
    if state.get("status") != "complete":
        raise ValueError("只能导出完整运行")
    metadata = {k: v for k, v in state.items() if k not in ("sources", "summaries")}
    metadata["sources"] = [{k: v for k, v in s.items() if k != "raw_path"} for s in state["sources"]]
    # API keys are never part of this schema; reject accidental pasted credentials too.
    files = {"report.md": read(source/"report.md"), "metrics.json": read(source/"metrics.json"),
             "events.jsonl": read(source/"events.jsonl"),
             "evidence.json": json.dumps(metadata, ensure_ascii=False, indent=2)}
    if (source/"revision.json").exists():
        files["revision.json"] = read(source/"revision.json")
    for text in files.values():
        if re.search(r"sk-[A-Za-z0-9_-]{16,}", text) or (os.environ.get("DEEPSEEK_API_KEY") and os.environ["DEEPSEEK_API_KEY"] in text):
            raise ValueError("导出内容疑似包含密钥，已拒绝写出")
    output.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (output/name).write_text(text, encoding="utf-8")
    print(f"公开结果已导出：{output}（不含原网页和完整Prompt）")


def chat(run_dir):
    current = run_dir
    print("输入 P2 修改要求，只改该段；补查用 /research P2 问题；/quit 退出。")
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            return
        if line in ("/quit", "/exit"):
            return
        research = line.startswith("/research ")
        if research:
            line = line[len("/research "):]
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            print("格式：P2 修改要求")
            continue
        argv = ["revise", "--run-dir", str(current), "--section", parts[0], "--instruction", parts[1]]
        if research:
            argv.append("--research")
        args = make_parser().parse_args(argv)
        try:
            number = load(current/"state.json").get("revision", 0)+1
            code = asyncio.run(execute(args))
            if code == 0:
                current = current/f"revision-{number}"
        except (RuntimeError, ValueError, asyncio.TimeoutError) as exc:
            print(f"修订未完成：{exc}")


def main():
    args = make_parser().parse_args()
    try:
        if args.command == "export":
            export_run(args.run_dir, args.output)
            return 0
        if args.command == "chat":
            chat(args.run_dir)
            return 0
        return asyncio.run(execute(args))
    except KeyboardInterrupt:
        print("\n已停止，已完成的调用记录保留。", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, OSError, asyncio.TimeoutError) as exc:
        print(f"未完成：{exc}", file=sys.stderr)
        return 1
