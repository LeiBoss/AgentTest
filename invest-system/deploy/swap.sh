#!/usr/bin/env bash
# 创建 2GB swap 并持久化（需 root 执行）
set -euo pipefail

SWAP_FILE=/swapfile
SWAP_SIZE=2G

if swapon --show=NAME --noheadings | grep -qx "$SWAP_FILE"; then
    echo "[swap] $SWAP_FILE 已启用，跳过"
    exit 0
fi

if [ ! -f "$SWAP_FILE" ]; then
    fallocate -l "$SWAP_SIZE" "$SWAP_FILE" || dd if=/dev/zero of="$SWAP_FILE" bs=1M count=2048
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE"
fi

swapon "$SWAP_FILE"

if ! grep -qE "^[[:space:]]*$SWAP_FILE[[:space:]]" /etc/fstab; then
    echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
fi

if [ -f /etc/sysctl.d/99-swappiness.conf ]; then
    sed -i 's/^vm\.swappiness.*/vm.swappiness = 10/' /etc/sysctl.d/99-swappiness.conf
else
    echo "vm.swappiness = 10" > /etc/sysctl.d/99-swappiness.conf
fi
sysctl -p /etc/sysctl.d/99-swappiness.conf

echo "[swap] 完成"
swapon --show
free -h
