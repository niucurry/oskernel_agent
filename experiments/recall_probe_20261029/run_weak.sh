#!/bin/bash
# Same recorded post-report contexts, replayed against weaker SiliconFlow models.
ROOT=/home/niu/agent-recall-probe-20261029
PY=$ROOT/venv/bin/python
cd /home/niu/agent || exit 1
export PYTHONPATH=src
RUNS="--run $ROOT/exp-kernel/runs/20261029T210242-df29f869 --run $ROOT/exp-kernel/runs/20261029T212746-b14424eb --run $ROOT/exp-kernel/runs/20261029T213841-bdc79c9c"
COMMON="--api-key-file $ROOT/private/siliconflow_key --upstream https://api.siliconflow.cn/v1 --replay context,substituted,fresh --repeats 2 --workers 12 --max-tokens 1024"
one() {
  local model=$1 extra=$2
  echo "=== $(date +%T) start $model" >> $ROOT/weak.log
  $PY -m oskernel_agent.cli.recall_probe replay $COMMON $RUNS --model "$model" --extra-body "$extra" > /dev/null 2>> $ROOT/weak.log
  echo "=== $(date +%T) end $model exit=$?" >> $ROOT/weak.log
}
NOTHINK='{"enable_thinking": false}'
one Qwen/Qwen3-8B "$NOTHINK" &
one Qwen/Qwen3-14B "$NOTHINK" &
one Qwen/Qwen3.5-4B "$NOTHINK" &
one Qwen/Qwen3.5-9B "$NOTHINK" &
one Qwen/Qwen3-30B-A3B-Instruct-2507 '{}' &
one Qwen/Qwen2.5-72B-Instruct-128K '{}' &
wait
echo "=== $(date +%T) ALL DONE" >> $ROOT/weak.log
