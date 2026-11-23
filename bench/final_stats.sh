#!/bin/bash
cd "$(dirname "$0")"; PY="${PY:-python3}"   # stdlib only
S6=(large-9001 large-9002 large-9003 small-9001 small-9002 small-9003); SL=(large-9001 large-9002 large-9003); SS=(small-9001 small-9002 small-9003)
echo "##### TABLE six TOON sets (3 seeds x 2 regimes)"; $PY study_stats.py --table agent agent-nopreview code code-namesonly --sets "${S6[@]}"
echo "##### TABLE large regime";  $PY study_stats.py --table agent agent-nopreview code code-namesonly --sets "${SL[@]}"
echo "##### TABLE small regime";  $PY study_stats.py --table agent agent-nopreview code code-namesonly toon --sets "${SS[@]}"
echo "##### TABLE large-9001";    $PY study_stats.py --table agent agent-nopreview code toon --sets large-9001
echo "##### TABLE small-9001";    $PY study_stats.py --table agent toon toon-think --sets small-9001
echo "##### TABLE DataBench test"; $PY study_stats.py --table agent agent-nopreview code table --sets databench-test
cmp(){ echo; echo "##### $1"; shift; $PY study_stats.py "$1" "$2" --sets "${@:3}" | grep -vE "isCorrectLenient|no-cache"; }
cmp "H2/H3 six sets: agent vs agent-nopreview" agent agent-nopreview "${S6[@]}"
cmp "large regime: agent vs agent-nopreview" agent agent-nopreview "${SL[@]}"
cmp "small regime: agent vs agent-nopreview" agent agent-nopreview "${SS[@]}"
cmp "H4 DataBench: agent vs agent-nopreview" agent agent-nopreview databench-test
cmp "H1 large-9001: agent vs toon" agent toon large-9001
cmp "small regime: agent vs toon" agent toon "${SS[@]}"
cmp "small-9001: agent vs toon-think" agent toon-think small-9001
cmp "DataBench small tables: agent vs table" agent table databench-test
cmp "secondary six sets: code vs agent" code agent "${S6[@]}"
cmp "secondary six sets: code vs agent-nopreview" code agent-nopreview "${S6[@]}"
cmp "secondary DataBench: code vs agent" code agent databench-test
cmp "secondary DataBench: code vs agent-nopreview" code agent-nopreview databench-test
cmp "ablation six sets: code-namesonly vs code" code-namesonly code "${S6[@]}"
echo "##### JSON baselines (exploratory, small seed 9001, thinking off)"; $PY study_stats.py --table agent toon toon-think json-pretty json-compact --sets small-9001
cmp "agent vs json-pretty" agent json-pretty small-9001
cmp "agent vs json-compact" agent json-compact small-9001
cmp "toon vs json-pretty" toon json-pretty small-9001
cmp "toon vs json-compact" toon json-compact small-9001
