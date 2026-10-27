#!/usr/bin/env bash
# MartiQuant live paper launcher (multi-instance by tag).
#
# Each instance is identified by a tag. State files live in runs/_live_logs/:
#   <tag>.pid   process id
#   <tag>.log   stdout+stderr (nohup)
#   <tag>.cmd   full command line (used by `watch` to restart on crash)
#
# Subcommands:
#   start <tag> [extra args ...]   launch a new instance with that tag
#   stop <tag>                     SIGTERM → wait 10s → SIGKILL
#   status [tag]                   list running instances (or just one)
#   restart <tag> [extra args ...] stop then start (needs full args again)
#   watch                          relaunch any dead instance (for cron)
#   list                           show all remembered launch commands
#
# Usage examples:
#   bash martiquant/live/run.sh start paper-cb-100k --equity-usd 100000
#   bash martiquant/live/run.sh start paper-nocb-100k --equity-usd 100000 --no-circuit-breaker
#   bash martiquant/live/run.sh status
#   bash martiquant/live/run.sh stop paper-cb-100k
#
# Cron entry for auto-restart (every minute, silent if alive):
#   * * * * * cd /Users/zhangjiawei/repos/MartiQuant && bash martiquant/live/run.sh watch
#
set -euo pipefail

cd "$(dirname "$0")/../.."
RUNS_DIR="runs/_live_logs"
mkdir -p "$RUNS_DIR"

pidfile() { echo "$RUNS_DIR/$1.pid"; }
logfile() { echo "$RUNS_DIR/$1.log"; }
cmdfile() { echo "$RUNS_DIR/$1.cmd"; }

usage() {
    sed -n '2,25p' "$0"
    exit 1
}

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

start() {
    local tag="$1"; shift
    local pf lf cf
    pf=$(pidfile "$tag"); lf=$(logfile "$tag"); cf=$(cmdfile "$tag")

    if alive "$pf"; then
        echo "[$tag] already running (PID $(cat "$pf"))"
        return 1
    fi

    local cmd="python3 -m martiquant.live --tag $tag $*"
    echo "$cmd" > "$cf"
    echo "Starting [$tag]: $cmd"
    nohup $cmd >> "$lf" 2>&1 &
    echo $! > "$pf"
    sleep 0.5
    if alive "$pf"; then
        echo "Started [$tag] PID $(cat "$pf"). Log: $lf"
    else
        echo "[$tag] failed to start — check $lf" >&2
        rm -f "$pf" "$cf"
        return 1
    fi
}

stop() {
    local tag="$1"
    local pf cf
    pf=$(pidfile "$tag"); cf=$(cmdfile "$tag")

    if [ ! -f "$pf" ]; then
        echo "[$tag] PID file not found"
        return 1
    fi

    local pid
    pid=$(cat "$pf")
    if kill -0 "$pid" 2>/dev/null; then
        echo "[$tag] stopping PID $pid..."
        kill "$pid"
        for _ in $(seq 1 10); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "[$tag] force killing"
            kill -9 "$pid"
        fi
        echo "[$tag] stopped"
    else
        echo "[$tag] process $pid not running (stale pid)"
    fi
    rm -f "$pf" "$cf"
}

status_one() {
    local tag="$1" pf
    pf=$(pidfile "$tag")
    if alive "$pf"; then
        echo "[$tag] running (PID $(cat "$pf"))"
    elif [ -f "$pf" ]; then
        echo "[$tag] DEAD (stale pid $(cat "$pf"))"
    else
        echo "[$tag] not running"
    fi
}

status_all() {
    local found=0
    shopt -s nullglob
    for pf in "$RUNS_DIR"/*.pid; do
        found=1
        status_one "$(basename "$pf" .pid)"
    done
    shopt -u nullglob
    if [ $found -eq 0 ]; then echo "no instances"; fi
}

watch() {
    shopt -s nullglob
    for cf in "$RUNS_DIR"/*.cmd; do
        local tag pf lf cmd
        tag=$(basename "$cf" .cmd)
        pf=$(pidfile "$tag"); lf=$(logfile "$tag")
        if alive "$pf"; then continue; fi
        cmd=$(cat "$cf")
        echo "[watch $(date -u +%FT%TZ)] [$tag] dead, restarting: $cmd" >> "$lf"
        rm -f "$pf"
        nohup $cmd >> "$lf" 2>&1 &
        echo $! > "$pf"
    done
    shopt -u nullglob
}

list_known() {
    shopt -s nullglob
    local found=0
    for cf in "$RUNS_DIR"/*.cmd; do
        found=1
        echo "$(basename "$cf" .cmd): $(cat "$cf")"
    done
    shopt -u nullglob
    if [ $found -eq 0 ]; then echo "no remembered commands"; fi
}

sub="${1:-}"; [ $# -gt 0 ] && shift || true
case "$sub" in
    start)
        [ $# -ge 1 ] || usage
        start "$@"
        ;;
    stop)
        [ $# -eq 1 ] || usage
        stop "$1"
        ;;
    status)
        if [ $# -eq 0 ]; then status_all; else status_one "$1"; fi
        ;;
    restart)
        [ $# -ge 1 ] || usage
        tag="$1"; shift
        stop "$tag" || true
        sleep 1
        start "$tag" "$@"
        ;;
    watch) watch ;;
    list) list_known ;;
    *) usage ;;
esac
