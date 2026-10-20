"""Append-only spans and a dependency DAG, not a guessed 'agent round' count."""

import json
import time
from pathlib import Path


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class Metrics:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.started = time.perf_counter()
        self.events = []
        self.human_seconds = 0.0

    def start(self, kind, name, parents=(), **details):
        event = dict(id=f"n{len(self.events)+1}", kind=kind, name=name,
                     parents=list(dict.fromkeys(p for p in parents if p)),
                     start_s=round(time.perf_counter()-self.started, 6),
                     status="running", **details)
        self.events.append(event)
        return event["id"]

    def end(self, event_id, status="ok", **details):
        event = next(e for e in self.events if e["id"] == event_id)
        event.update(details)
        event.update(status=status, end_s=round(time.perf_counter()-self.started, 6))
        event["elapsed_s"] = round(event["end_s"]-event["start_s"], 6)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False)+"\n")

    def summary(self):
        # Parents always refer to earlier-created spans. A model attempt weighs one;
        # search/fetch/cache/validation nodes weigh zero but retain dependencies.
        rounds, elapsed = {}, {}
        for e in self.events:
            rounds[e["id"]] = max((rounds[p] for p in e["parents"]), default=0) + (e["kind"] == "model")
            elapsed[e["id"]] = max((elapsed[p] for p in e["parents"]), default=0) + e.get("elapsed_s", 0)
        model = [e for e in self.events if e["kind"] == "model"]
        usages = [e["usage"] for e in model if e.get("usage")]
        missing = len(model)-len(usages)
        reasoning_known = all("reasoning_tokens" in u.get("completion_tokens_details", {}) for u in usages)
        output = sum(u.get("completion_tokens", 0) for u in usages)
        reasoning = sum(u.get("completion_tokens_details", {}).get("reasoning_tokens", 0) for u in usages)
        wall = time.perf_counter()-self.started
        fetches = [e for e in self.events if e["kind"] == "fetch"]
        return dict(
            wall_seconds=round(wall, 3), human_wait_seconds=round(self.human_seconds, 3),
            automated_wall_seconds=round(wall-self.human_seconds, 3),
            critical_path_model_rounds=max(rounds.values(), default=0),
            dependency_path_seconds=round(max(elapsed.values(), default=0), 3),
            model_calls=len(model), successful_model_calls=sum(e["status"] == "ok" for e in model),
            input_tokens=sum(u.get("prompt_tokens", 0) for u in usages),
            output_tokens=output, reasoning_tokens=reasoning if reasoning_known else None,
            visible_output_tokens=output-reasoning if reasoning_known else None,
            input_cache_hit_tokens=sum(u.get("prompt_cache_hit_tokens", 0) for u in usages),
            input_cache_miss_tokens=sum(u.get("prompt_cache_miss_tokens", 0) for u in usages),
            calls_without_usage=missing, token_totals_complete=not missing,
            fetch_attempted_urls=len({e.get("url") for e in fetches}),
            fetched_urls=len({e.get("url") for e in fetches if e["status"] == "ok"}),
            fetch_cache_hits=sum(bool(e.get("cache_hit")) for e in fetches),
            search_calls=sum(e["kind"] == "search" for e in self.events),
            model_versions=sorted({e.get("response_model") for e in model if e.get("response_model")}),
            notes=["output_tokens includes reasoning; do not add reasoning twice",
                   "critical_path_model_rounds is the longest dependency path, including retries",
                   "dependency_path_seconds excludes uninstrumented scheduling and human input",
                   "missing usage is unknown, not free"],
        )

    def finish(self, **extra):
        summary = self.summary()
        summary.update(extra)
        save_json(self.directory / "metrics.json", summary)
        save_json(self.directory / "trace.json", self.events)
        return summary
