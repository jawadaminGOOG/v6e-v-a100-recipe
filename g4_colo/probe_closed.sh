#!/usr/bin/env bash
# Closed-loop streaming capacity probe split over 2 client pods x 3 procs.
# usage: probe_closed.sh LABEL CONCURRENCY NUM_MESSAGES [OUT_JSON]
set -euo pipefail
LABEL=$1; C=$2; N=$3; OUTJ=${4:-}
IP=$(kubectl -n colo get pod vllm -o jsonpath='{.status.podIP}')
START=$(python3 -c "import time;print(time.time()+20)")
j=0
for pod in client-0 client-1; do
  for p in 0 1 2; do
    c=$(( C / 6 + (j < C % 6 ? 1 : 0) ))
    kubectl -n colo exec $pod -- bash -c "cd /tmp && mkdir -p /tmp/probe/$LABEL && setsid nohup python colo_bench.py --input-file /tmp/synthetic_yelp.xlsx --output-dir /tmp/probe/$LABEL --url http://$IP:8000/v1/completions --label $LABEL.p$j --mode closed --concurrency $c --num-messages $((N/6)) --seed $((42+j)) --start-at $START </dev/null >/tmp/probe/$LABEL.p$j.log 2>&1 &" </dev/null >/dev/null 2>&1 &
    sleep 0.5
    j=$((j+1))
  done
done
sleep 25
for i in $(seq 1 120); do
  n=0
  for pod in client-0 client-1; do n=$((n + $(kubectl -n colo exec $pod -- bash -c "ls /tmp/probe/$LABEL/*.summary.json 2>/dev/null | wc -l"))); done
  [ $n -ge 6 ] && break; sleep 5
done
for pod in client-0 client-1; do kubectl -n colo exec $pod -- bash -c "cat /tmp/probe/$LABEL/*.summary.json"; done | OUTJ="$OUTJ" LABEL="$LABEL" C="$C" python3 -c "
import sys,json,os
txt=sys.stdin.read(); objs=[json.loads('{'+x) for x in txt.split('{')[1:]]
wall=max(o['wall_s'] for o in objs)
if os.environ['OUTJ']:
    json.dump({'label':os.environ['LABEL'],'concurrency':int(os.environ['C']),'procs':len(objs),'ok':sum(o['n_ok'] for o in objs),
               'failed':sum(o['n_failed'] for o in objs),'rps':sum(o['n_ok'] for o in objs)/wall,
               'ttft_p50_mean':sum(o['ttft_p50'] for o in objs)/len(objs),'e2e_p50_mean':sum(o['e2e_p50'] for o in objs)/len(objs)},open(os.environ['OUTJ'],'w'))
print('procs',len(objs),'ok',sum(o['n_ok'] for o in objs),'failed',sum(o['n_failed'] for o in objs))
wall=max(o['wall_s'] for o in objs)
print('agg_rps',round(sum(o['n_ok'] for o in objs)/wall,1),'tok_s',round(sum(o['output_tok_s']*o['wall_s'] for o in objs)/wall,1))
for k in ['ttft_p50','ttft_p99','itl_p50','e2e_p50','e2e_p99']: print(k, [round(o[k],4) for o in objs])
"
