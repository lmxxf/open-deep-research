"""Explicit A/B pipelines. No framework hides model calls or research state."""

import asyncio
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .text import clean_surrogates, dumps, write_text

from . import prompts
from .llm import LLM
from .metrics import Metrics, save_json
from .web import Web, canonical_url


@dataclass
class Config:
    mode: str = "B"
    profile: str = "parallel-fast"
    query_count: int = 3
    max_pages: int = 6
    max_rounds: int = 1
    concurrency: int = 3
    timeout: float = 15
    model_timeout: float = 120
    max_calls: int = 32
    max_tokens: int = 8192
    deadline: float = 600
    model: str | None = None

    def __post_init__(self):
        if self.mode not in ("A", "B") or self.profile not in ("serial-thinking", "parallel-fast"):
            raise ValueError("未知 mode/profile")
        if not 0 <= self.max_rounds <= 2:
            raise ValueError("max_rounds 必须为0、1或2")
        if not 1 <= self.query_count <= 5 or not 1 <= self.max_pages <= 20:
            raise ValueError("query_count 为1到5，max_pages为1到20")
        if min(self.timeout, self.model_timeout, self.deadline, self.max_calls, self.max_tokens, self.concurrency) <= 0:
            raise ValueError("预算与超时必须为正数")


def strings(value, limit):
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(s.strip() for s in value if isinstance(s, str) and s.strip()))[:limit]


def locate_quote(text, quote):
    """Accept whitespace normalization only, never a paraphrase as a quotation."""
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 300:
        return None
    start = text.find(quote)
    if start >= 0:
        return start, start+len(quote)
    chars, offsets = [], []
    for i, char in enumerate(text):
        if not char.isspace():
            chars.append(char)
            offsets.append(i)
    needle = "".join(quote.split())
    pos = "".join(chars).find(needle)
    if pos < 0 or not needle:
        return None
    begin, end = offsets[pos], offsets[pos+len(needle)-1]+1
    return (begin, end) if end-begin <= 300 else None


def evidence_window(text, brief, limit=16000):
    """Deterministic query-aware paragraph selection; original text stays on disk."""
    if len(text) <= limit:
        return text
    terms = re.findall(r"[A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", brief)
    words = set(terms)
    for term in terms:
        if re.search(r"[\u4e00-\u9fff]", term):
            words.update(term[i:i+2] for i in range(len(term)-1))
    pieces = [text[i:i+1800] for i in range(0, len(text), 1600)]
    ranking = sorted(range(len(pieces)), key=lambda i: sum(w.lower() in pieces[i].lower() for w in words), reverse=True)
    selected = sorted(ranking[:max(1, limit//1800)])
    return "\n\n[原文选段]\n\n".join(pieces[i] for i in selected)


def validate_sections(value, allowed, require_citation=True):
    if not isinstance(value, list) or not value:
        raise ValueError("报告没有有效段落")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("段落必须为对象")
        sid, title, body = item.get("id"), item.get("title"), item.get("body")
        if not isinstance(sid, str) or not re.fullmatch(r"P[1-9]\d*", sid) or sid in seen:
            raise ValueError("段落ID错误或重复")
        if not isinstance(title, str) or not isinstance(body, str) or not body.strip():
            raise ValueError("段落标题/正文无效")
        refs = set(re.findall(r"\[([ES]\d+)\]", body))
        if refs-allowed:
            raise ValueError(f"报告含未知引用ID：{sorted(refs-allowed)}")
        # Bare technical endpoints (e.g. localhost:8000) are not bibliography.
        # Only the application renders source hyperlinks from verified IDs.
        if re.search(r"\]\(https?://", body):
            raise ValueError("writer 应引用来源ID，不能自行插入来源超链接")
        seen.add(sid)
        result.append(dict(id=sid, title=title, body=body))
    if require_citation and not any(re.search(r"\[[ES]\d+\]", s["body"]) for s in result):
        raise ValueError("报告完全没有证据引用")
    return result


def render(state):
    lines = ["# " + state["question"], ""]
    for section in state["sections"]:
        lines += [f"## {section['id']} · {section['title']}", "", section["body"], ""]
    lines += ["## 来源与证据", ""]
    cited = set(re.findall(r"\[([ES]\d+)\]", "\n".join(s["body"] for s in state["sections"])))
    sources = {s["id"]: s for s in state["sources"]}
    for ev in state.get("evidence", []):
        if ev["id"] in cited:
            source = sources[ev["source_id"]]
            lines += [f"- [{ev['id']}] [{source['title']}]({source['url']})；原文字符 {ev['start']}–{ev['end']}；{ev['conditions']}"]
    for sid, source in sources.items():
        if sid in cited:
            lines += [f"- [{sid}] [{source['title']}]({source['url']})"]
    return "\n".join(lines)+"\n"


class Research:
    def __init__(self, output: Path, config: Config):
        self.output, self.config = output, config
        self.metrics = Metrics(output)
        self.serial = config.profile == "serial-thinking"
        concurrency = 1 if self.serial else config.concurrency
        self.llm = LLM(self.metrics, config.model, self.serial, concurrency,
                       config.max_calls, config.model_timeout, config.max_tokens)
        config.model = self.llm.model
        self.web = Web(Path(__file__).resolve().parents[1]/".cache", config.timeout, concurrency)
        self.visited = set()
        self.sources, self.evidence, self.summaries = [], [], []
        self.queries = []
        self.rejected_quotes = 0
        self.search_log = []
        self.collection_join = None

    async def close(self):
        await self.llm.close()
        await self.web.close()

    async def collect(self, queries, parents, limit=None):
        """Search, interleave results fairly, then fetch. Failures are recorded."""
        self.queries.extend(q for q in queries if q not in self.queries)

        async def search(query, deps):
            event = self.metrics.start("search", "search", deps, query=query)
            try:
                results = await self.web.search(query, limit=4)
                self.metrics.end(event, result_count=len(results),
                                 providers=sorted({r.get("provider", "unknown") for r in results}))
                self.search_log.append(dict(query=query, results=results))
                return results, event
            except Exception as exc:
                self.metrics.end(event, "error", error_type=type(exc).__name__)
                self.search_log.append(dict(query=query, results=[], error_type=type(exc).__name__))
                return [], event

        results = []
        deps = list(parents)
        if self.serial:
            for query in queries:
                result = await search(query, deps)
                results.append(result)
                deps = [result[1]]
        else:
            results = await asyncio.gather(*(search(q, parents) for q in queries))
            deps = [r[1] for r in results]
        save_json(self.output/"search_results.json", self.search_log)
        # Round-robin prevents the first query consuming the entire page budget.
        candidates, selected = [], set(self.visited)
        for rank in range(4):
            for items, _ in results:
                if rank < len(items):
                    item = items[rank]
                    url = canonical_url(item["url"])
                    if url not in selected:
                        candidates.append(item)
                        selected.add(url)
        remaining = self.config.max_pages-len(self.sources)
        if limit is not None:
            remaining = min(remaining, limit)
        if remaining <= 0:
            return []

        async def fetch(item, dependencies):
            url = item["url"]
            self.visited.add(canonical_url(url))
            event = self.metrics.start("fetch", "fetch", dependencies, url=url)
            try:
                page = clean_surrogates(await self.web.fetch(url))
                if len(page.get("text", "").strip()) < 120:
                    raise ValueError("正文过短")
                self.metrics.end(event, cache_hit=page.get("cache_hit", False), chars=len(page["text"]))
                return page, event
            except Exception as exc:
                self.metrics.end(event, "error", error_type=type(exc).__name__)
                return None, event

        pages = []
        # Fetch limited batches; failed URLs can be replaced without overflowing the page cap.
        for offset in range(0, min(len(candidates), remaining*2), remaining):
            need = remaining-len(pages)
            if need <= 0:
                break
            batch = candidates[offset:offset+need]
            if self.serial:
                fetched = []
                for item in batch:
                    row = await fetch(item, deps)
                    fetched.append(row)
                    deps = [row[1]]
            else:
                fetched = await asyncio.gather(*(fetch(i, deps) for i in batch))
                deps = [row[1] for row in fetched]
            pages.extend(row for row in fetched if row[0])
        added = []
        for page, event in pages:
            if any(s.get("content_hash") == page.get("content_hash") and page.get("content_hash") for s in self.sources):
                continue
            sid = f"S{len(self.sources)+1}"
            # Preserve raw text locally, publish only metadata and selected short quotes.
            raw = self.output / "pages" / (sid+".txt")
            raw.parent.mkdir(exist_ok=True)
            write_text(raw, page["text"])
            source = dict(id=sid, url=page["url"], title=page.get("title") or page["url"],
                          fetched_at=page.get("fetched_at"), content_hash=page.get("content_hash"),
                          raw_path=str(raw), chars=len(page["text"]))
            self.sources.append(source)
            added.append((source, page["text"], event))
        self.collection_join = self.metrics.start("join", "collection_complete", deps)
        self.metrics.end(self.collection_join)
        return added

    async def process(self, pages, brief, parents=()):
        async def one(row, deps):
            source, text, fetch_event = row
            if self.config.mode == "A":
                data, event = await self.llm.ask("generic_summary", prompts.SUMMARIZE_A,
                    {"title": source["title"], "text": text[:16000]}, deps)
                return dict(source_id=source["id"], summary=str(data.get("summary", ""))), event
            data, event = await self.llm.ask("extract_evidence", prompts.EVIDENCE_B,
                {"brief": brief, "source_id": source["id"], "text": evidence_window(text, brief)}, deps)
            valid = []
            for entry in (data.get("evidence") or [])[:4]:
                if not isinstance(entry, dict):
                    self.rejected_quotes += 1
                    continue
                span = locate_quote(text, entry.get("quote"))
                if not span:
                    self.rejected_quotes += 1
                    continue
                start, end = span
                if entry.get("stance") not in ("support", "counter", "context"):
                    self.rejected_quotes += 1
                    continue
                valid.append(dict(source_id=source["id"], quote=text[start:end], start=start, end=end,
                                  claim=str(entry.get("claim", "")), conditions=str(entry.get("conditions", "未说明")),
                                  date=str(entry.get("date", "")), stance=entry.get("stance", "context")))
            return valid, event

        # This stage starts after collection's join, so all fetches are dependencies.
        deps = list(parents) + [row[2] for row in pages] + ([self.collection_join] if self.collection_join else [])
        results = []
        if self.serial:
            for row in pages:
                result = await one(row, deps)
                results.append(result)
                deps = [result[1]]
        else:
            # Chunking makes the concurrency budget and DAG scheduling explicit.
            for start in range(0, len(pages), self.config.concurrency):
                batch = await asyncio.gather(*(one(r, deps) for r in pages[start:start+self.config.concurrency]))
                results.extend(batch)
                deps = [r[1] for r in batch]
        for data, _ in results:
            if self.config.mode == "A":
                self.summaries.append(data)
            else:
                existing = {(e["source_id"], e["quote"]) for e in self.evidence}
                for entry in data:
                    if (entry["source_id"], entry["quote"]) not in existing:
                        entry["id"] = f"E{len(self.evidence)+1}"
                        self.evidence.append(entry)
                        existing.add((entry["source_id"], entry["quote"]))
        return [r[1] for r in results] or list(parents)

    async def run(self, question, brief="", answers="", style_sample="", interactive=False, accept_assumptions=False):
        question, brief, answers, style_sample = clean_surrogates((question, brief, answers, style_sample))
        state = dict(question=question, brief=brief, answers=answers, style_sample=style_sample,
                     config=asdict(self.config), created_at=datetime.now(timezone.utc).isoformat(),
                     sections=[], revision=0)
        save_json(self.output/"input.json", state)
        parents = []
        effective = question + "\n用户补充：\n" + brief + "\n用户回答：\n" + answers
        if self.config.mode == "B":
            alignment, event = await self.llm.ask("align", prompts.ALIGN,
                                                dict(question=question, brief=brief, answers=answers))
            parents = [event]
            print("研究计划：", flush=True)
            for item in strings(alignment.get("plan"), 5):
                print("- "+item, flush=True)
            questions = strings(alignment.get("questions"), 3)
            if questions and interactive:
                started = time.perf_counter()
                replies = []
                for q in questions:
                    response = clean_surrogates(await asyncio.to_thread(input, q+"\n> "))
                    replies.append(dict(question=q, answer=response))
                self.metrics.human_seconds += time.perf_counter()-started
                answers = dumps(replies, ensure_ascii=False)
                state["answers"] = answers
                effective += "\n"+answers
            elif questions and not accept_assumptions:
                state.update(status="needs_alignment", alignment=alignment)
                save_json(self.output/"state.json", state)
                return state
            state["alignment"] = alignment
            effective += "\n研究计划：" + dumps(alignment.get("plan", []), ensure_ascii=False)
            if questions and accept_assumptions:
                state["unanswered_questions"] = questions
                effective += "\n以下是未确认假设，不能冒充用户偏好："+dumps(alignment.get("assumptions", []), ensure_ascii=False)
            query_prompt = prompts._COMMON + """围绕给定brief中的实际问题产生至多query_count个搜索词。
优先一手资料，分别覆盖关键选型条件；两个及以上查询时第一个查询必须全英文，另一个中文。
每个查询只问一个具体问题，用4到8个关键词，避免长串关键词被搜索引擎弱化。
返回JSON对象 {"queries":["query"]}。不要先假定结论。"""
            plan, event = await self.llm.ask("plan", query_prompt,
                                          dict(brief=effective, query_count=self.config.query_count), parents)
        else:
            # A gets exactly the same optional user brief; it simply does not ask more questions.
            state["alignment"] = {"questions": [], "method": "skipped_real_user_clarification"}
            plan, event = await self.llm.ask("plan", prompts.PLAN_A,
                                          dict(question=effective, query_count=self.config.query_count), parents)
        queries = strings(plan.get("queries"), self.config.query_count)
        if not queries:
            raise ValueError("模型没有生成有效查询")
        if len(queries) >= 2 and not any(not re.search(r"[\u4e00-\u9fff]", q) for q in queries):
            translated, event = await self.llm.ask("english_query", prompts._COMMON +
                'Translate this search query into concise English. Return {"query":"English query"}.',
                {"query": queries[-1]}, [event])
            english = translated.get("query", "")
            if not isinstance(english, str) or not english.strip() or re.search(r"[\u4e00-\u9fff]", english):
                raise ValueError("无法生成有效英文查询")
            queries[-1] = english.strip()
        print(f"[{self.config.mode}] 搜索 {len(queries)} 个问题", flush=True)
        first_limit = max(1, self.config.max_pages-2) if self.config.mode == "B" and self.config.max_rounds else None
        pages = await self.collect(queries, [event], limit=first_limit)
        if not pages:
            raise ValueError("未获取有效网页正文；请检查 search_results.json 和网络，不会用模型记忆伪造研究")
        parents = await self.process(pages, effective, [event])
        if self.config.mode == "B":
            for _ in range(self.config.max_rounds):
                # max_pages is a run-wide cap, not a per-round multiplier.
                if len(self.sources) >= self.config.max_pages:
                    break
                gap, event = await self.llm.ask("gaps", prompts.GAPS_B,
                    dict(brief=effective, evidence=self.evidence, previous_queries=self.queries), parents)
                state.setdefault("gap_checks", []).append(gap)
                parents = [event]
                queries = [q for q in strings(gap.get("queries"), 2) if q not in self.queries]
                if not queries:
                    break
                pages = await self.collect(queries, parents)
                parents = await self.process(pages, effective, parents)
            if not self.evidence:
                raise ValueError("没有可逐字验证的相关证据；不生成无来源报告")
        print(f"[{self.config.mode}] 已读 {len(self.sources)} 页，开始写作", flush=True)
        data, event = await self.llm.ask("write", prompts.WRITE_A if self.config.mode == "A" else prompts.WRITE_B,
            dict(question=question, brief=effective, answers=answers, sources=self.sources,
                 summaries=self.summaries, evidence=self.evidence, style_sample=style_sample), parents)
        allowed = {s["id"] for s in self.sources} if self.config.mode == "A" else {e["id"] for e in self.evidence}
        # Paragraph identity is application state, not a creative model decision.
        sections = data.get("sections")
        if isinstance(sections, list):
            sections = [{**s, "id": f"P{i+1}"} if isinstance(s, dict) else s for i, s in enumerate(sections)]
        state["sections"] = validate_sections(sections, allowed)
        state.update(status="complete", sources=self.sources, evidence=self.evidence,
                     summaries=self.summaries, queries=self.queries, effective_brief=effective)
        save_json(self.output/"state.json", state)
        write_text(self.output/"report.md", render(state))
        return state

    async def revise(self, original, section_id, instruction, research=False):
        section_id, instruction = clean_surrogates((section_id, instruction))
        state = json.loads(dumps(original, ensure_ascii=False))
        self.sources, self.evidence = state["sources"], state.get("evidence", [])
        self.visited = {canonical_url(s["url"]) for s in self.sources}
        self.queries = list(state.get("queries", []))
        target = next((s for s in state["sections"] if s["id"] == section_id), None)
        if target is None:
            raise ValueError(f"段落 {section_id} 不存在")
        parents = []
        if research:
            data, event = await self.llm.ask("revision_queries", prompts.RESEARCH_REVISION,
                dict(instruction=instruction, section=target, evidence=self.evidence, previous_queries=self.queries))
            queries = strings(data.get("queries"), 2)
            parents = [event]
            if queries:
                # Revision has its own bounded incremental page allowance.
                self.config.max_pages += len(self.sources)
                pages = await self.collect(queries, parents)
                self.config.mode = "B"
                parents = await self.process(pages, state["effective_brief"]+"\n"+instruction, parents)
        allowed = {e["id"] for e in self.evidence} | {s["id"] for s in self.sources}
        data, event = await self.llm.ask("revise", prompts.REVISE,
            dict(instruction=instruction, section=target, brief=state["effective_brief"],
                 evidence=self.evidence, summaries=state.get("summaries", []),
                 style_sample=state.get("style_sample", "")), parents)
        replacement = validate_sections([dict(id=section_id, title=data.get("title"), body=data.get("body"))], allowed, require_citation=False)[0]
        untouched_before = [s for s in state["sections"] if s["id"] != section_id]
        state["sections"] = [replacement if s["id"] == section_id else s for s in state["sections"]]
        assert untouched_before == [s for s in state["sections"] if s["id"] != section_id]
        state.update(revision=state.get("revision", 0)+1, sources=self.sources, evidence=self.evidence, queries=self.queries)
        save_json(self.output/"revision.json", dict(section_id=section_id, instruction=instruction,
                  research=research, before=target, after=replacement, unchanged_sections_verified=True))
        save_json(self.output/"state.json", state)
        write_text(self.output/"report.md", render(state))
        return state
