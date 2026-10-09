#!/bin/bash
# Pause transcription if the graphics card gets too hot, resume once it cools.
# (2026-10-01: a stuck GPU fan let the card reach 110 °C and the driver powered
# the PC off.) Pausing = SIGSTOP to whisper-cli and the speaker-label child, so
# no work is lost. Runs until run_all.py has been gone for 10 minutes, so start
# it right before run_all.py, not long before.
#   setsid nohup bash "scripts/gpu_guard.sh" >/dev/null 2>&1 &
# The launchers (run_redo.sh, run_backlog.sh) instead call
#   bash gpu_guard.sh --ensure || exit 1
# which starts the guard unless it is already running, waits 3 s, and fails if it
# isn't running then (no sensors found: see the log), so nothing runs unguarded.
HOT_J=100; HOT_M=95       # pause at (°C): chip hot spot / memory. Driver shuts down at 110 / 105.
COOL_J=80; COOL_M=85      # resume below
LOG="$(dirname "$0")/../data/work/logs/gpu_guard.log"
if [ "$1" = "--ensure" ]; then
    # The guard's own command line ends in gpu_guard.sh (this one ends in --ensure).
    running() { ps -eo args | grep -qE "[g]pu_guard\.sh$"; }
    running || { setsid nohup bash "$0" >/dev/null 2>&1 < /dev/null & }
    sleep 3
    running && exit 0
    echo "$(date '+%F %T') the guard isn't running, so the launcher won't start transcribing" >> "$LOG"
    echo "The GPU guard isn't running (see $LOG); not starting." >&2
    exit 1
fi
H=$(ls -d /sys/class/drm/card*/device/hwmon/hwmon* 2>/dev/null | head -1)
[ -n "$H" ] || { echo "$(date '+%F %T') no amdgpu sensors" >> "$LOG"; exit 1; }
# Find the sensors by their labels, not their numbers (amdgpu can renumber them).
sensor() { grep -lx "$1" "$H"/temp*_label 2>/dev/null | head -1 | sed 's/_label$/_input/'; }
JT=$(sensor junction); MT=$(sensor mem)
[ -n "$JT" ] && [ -n "$MT" ] || { echo "$(date '+%F %T') junction/mem sensors not found in $H; refusing to guard" >> "$LOG"; exit 1; }
pids() { ps -eo pid,args | grep -E "[w]hisper-cli|[t]ranscriber_diarize\.py --label" | awk '{print $1}'; }
paused=0; idle=0
echo "$(date '+%F %T') guard started ($H)" >> "$LOG"
while true; do
    j=$(( $(cat "$JT") / 1000 )); m=$(( $(cat "$MT") / 1000 ))
    if (( !paused && (j >= HOT_J || m >= HOT_M) )); then
        p=$(pids); [ -n "$p" ] && kill -STOP $p
        paused=1; echo "$(date '+%F %T') PAUSED: junction ${j}°C mem ${m}°C fan $(cat $H/fan1_input) rpm" >> "$LOG"
    elif (( paused && j < COOL_J && m < COOL_M )); then
        p=$(pids); [ -n "$p" ] && kill -CONT $p
        paused=0; echo "$(date '+%F %T') resumed: junction ${j}°C mem ${m}°C" >> "$LOG"
    fi
    # run_all.py, or a transcribe.py (Fish Stats' and the public copy's driver).
    if ps -eo args | grep -qE "[r]un_all\.py|/[t]ranscribe\.py"; then idle=0; else idle=$((idle + 2)); fi
    (( idle >= 600 )) && { echo "$(date '+%F %T') guard stopped (no run_all for 10 min)" >> "$LOG"; exit 0; }
    sleep 2
done
