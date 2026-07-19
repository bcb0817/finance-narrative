#!/usr/bin/env bash
# 本番常駐（実投稿ON）。Linux/macOS 用。
set -euo pipefail
cd "$(dirname "$0")"
export POST_ENABLED=true
exec python local_finance_bot.py daemon
