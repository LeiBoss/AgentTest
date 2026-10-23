#!/bin/sh
set -eu

if [ -z "${CHAIN_YAML:-}" ] || [ -z "${SYMBOL:-}" ]; then
    echo "[worker] CHAIN_YAML 或 SYMBOL 未设置" >&2
    exit 1
fi

while true; do
    now=$(date +%s)
    target=$(date -d "today 07:30" +%s)
    if [ "$target" -le "$now" ]; then
        target=$(date -d "tomorrow 07:30" +%s)
    fi
    echo "[worker] 下次执行: $(date -d "@$target" '+%F %T')"
    sleep $((target - now))

    echo "[worker] $(date '+%F %T') 开始执行"
    python main.py "$CHAIN_YAML" "$SYMBOL" || true
    echo "[worker] $(date '+%F %T') 执行结束"
done
