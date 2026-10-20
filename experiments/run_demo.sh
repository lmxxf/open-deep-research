#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"
# Run sequentially to avoid the arms competing for account/network capacity.
# Use a fresh output root for each repetition; never overwrite a previous result.
DEMO_ROOT="${1:-runs/hardware-repeat}"
COMMON=(--question-file experiments/question.txt --brief-file experiments/brief.txt --answers-file experiments/answers.txt
        --query-count 3 --max-pages 6 --max-rounds 1)
"$PYTHON" -m deep_research run --mode A --profile serial-thinking \
  "${COMMON[@]}" --max-tokens 32768 --model-timeout 240 --deadline 900 --output "$DEMO_ROOT/a-thinking"
"$PYTHON" -m deep_research run --mode A --profile parallel-fast \
  "${COMMON[@]}" --max-tokens 32768 --model-timeout 240 --deadline 900 --output "$DEMO_ROOT/a-fast"
"$PYTHON" -m deep_research run --mode B --profile parallel-fast \
  "${COMMON[@]}" --max-tokens 8192 --output "$DEMO_ROOT/b-fast"
"$PYTHON" -m deep_research revise --run-dir "$DEMO_ROOT/b-fast" --section P1 \
  --instruction '只把P1改成给管理层汇报的三点简短结论：推荐方案、推荐的适用前提、采购前必须验证的风险。保留已有引用与未知边界，不增加事实，不改其他段落。'
