#!/bin/bash
# Sequential live runs (disk allows one at a time). Each run: main task, live batch
# probe after the report, then replays (context x2, substituted, fresh, report_time).
ROOT=/home/niu/agent-recall-probe-20261029
PY=$ROOT/venv/bin/python
cd /home/niu/agent || exit 1
export PYTHONPATH=src
common="--api-key-file $ROOT/private/api_key --upstream https://api.deepseek.com --experiment $ROOT/exp-kernel --live-single none --repeats 2"
run() {
  local tag=$1; shift
  echo "=== $(date +%T) start $tag $*" >> $ROOT/main.log
  $PY -m oskernel_agent.cli.recall_probe run $common --tag "$tag" "$@" > /dev/null 2>> $ROOT/main.log
  echo "=== $(date +%T) end $tag exit=$?" >> $ROOT/main.log
  df -h / | tail -1 >> $ROOT/main.log
}
EXTRA="--extra-file kernel/vma.c --extra-file kernel/process.c --extra-file kernel/signal.c"
for i in 1 2 3 4; do run flash-base --model deepseek-flash; done
for i in 1 2 3; do run pro-base --model deepseek-v4-pro; done
for i in 1 2 3; do run flash-extra --model deepseek-flash $EXTRA; done
for i in 1 2 3; do run flash-compact --model deepseek-flash --compaction auto --context-limit 60000 --output-limit 8192; done
for i in 1 2; do run flash-informed --model deepseek-flash --informed; done
echo "=== $(date +%T) ALL DONE" >> $ROOT/main.log
