import importlib.util
import json
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "experiment_compare", Path(__file__).parents[1] / "experiments" / "compare.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_comparison_preserves_times_inputs_and_actual_conditions(tmp_path):
    root = tmp_path / "runs"
    answers = '[{"question":"用途？","answer":"选型"},{"question":"预算？","answer":"有限"},{"question":"范围？","answer":"硬件"}]'
    arms = ("a-thinking", "a-fast", "b-fast", "b-fast/revision-1")
    for arm in arms:
        directory = root / arm
        directory.mkdir(parents=True, exist_ok=True)
        metrics = dict(status="complete", wall_seconds=10, model_calls=2,
                       critical_path_model_rounds=2, input_tokens=20,
                       input_cache_hit_tokens=0, input_cache_miss_tokens=20,
                       output_tokens=30, reasoning_tokens=None, visible_output_tokens=None,
                       fetched_urls=1, fetch_cache_hits=0, source_count=1,
                       valid_evidence_count=1, token_totals_complete=True,
                       config=dict(model="deepseek-flash", profile="parallel-fast",
                                   max_tokens=8192 if arm.startswith("b-fast") else 16384))
        if arm == "b-fast":
            metrics.update(wall_seconds=201.23, human_wait_seconds=159.015,
                           automated_wall_seconds=42.216)
        state = dict(question="硬件如何选？", brief="选型报告", answers=answers,
                     style_sample="", sources=[{"url": "https://example.com"}],
                     sections=[{"body": "报告正文"}])
        (directory / "metrics.json").write_text(json.dumps(metrics))
        (directory / "state.json").write_text(json.dumps(state))

    output = tmp_path / "export"
    module.compare(root, output)
    data = json.loads((output / "comparison.json").read_text())
    b = data["results"][2]
    assert (b["wall_seconds"], b["human_wait_seconds"], b["automated_wall_seconds"]) == (201.23, 159.015, 42.216)
    assert data["results"][0]["human_wait_seconds"] == 0
    assert data["results"][0]["automated_wall_seconds"] == 10
    assert b["max_tokens"] == 8192
    assert not b["has_style_sample"]
    assert all(check["exact_match"] for check in data["input_checks"].values())
    assert data["input_checks"]["answers"]["values"]["b-fast"] == answers
    markdown = (output / "comparison.md").read_text()
    assert "人工等待秒" in markdown and "自动运行秒" in markdown
    assert "自写语态样本" not in markdown and "自写样稿" not in markdown
    assert "输出预算差异" in markdown

    # Whitespace differences in an answers JSON string remain a real mismatch.
    state_path = root / "a-fast" / "state.json"
    state = json.loads(state_path.read_text())
    state["answers"] += " "
    state_path.write_text(json.dumps(state))
    module.compare(root, output)
    data = json.loads((output / "comparison.json").read_text())
    assert not data["input_checks"]["answers"]["exact_match"]
    assert data["input_checks"]["question"]["exact_match"]
