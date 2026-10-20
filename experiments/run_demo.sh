#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"
# Run sequentially to avoid the arms competing for account/network capacity.
# Use a fresh output root for each repetition; never overwrite a previous result.
DEMO_ROOT="${1:-runs/demo}"
COMMON=(--question-file experiments/question.txt --brief-file experiments/brief.txt --answers-file experiments/answers.txt
        --query-count 3 --max-pages 6 --max-rounds 1 --deadline 600 --max-tokens 16384)
"$PYTHON" -m deep_research run --mode A --profile serial-thinking \
  "${COMMON[@]}" --output "$DEMO_ROOT/a-thinking"
"$PYTHON" -m deep_research run --mode A --profile parallel-fast \
  "${COMMON[@]}" --output "$DEMO_ROOT/a-fast"
"$PYTHON" -m deep_research run --mode B --profile parallel-fast \
  "${COMMON[@]}" --style-sample experiments/style_sample.txt --output "$DEMO_ROOT/b-fast"
"$PYTHON" -m deep_research revise --run-dir "$DEMO_ROOT/b-fast" --section P1 \
  --instruction '只改这一段：用平实中文压缩到150字左右，先说建议，再说条件，保留已有引用，不新增事实。'
