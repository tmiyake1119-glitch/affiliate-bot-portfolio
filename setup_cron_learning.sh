#!/bin/bash
# VPS上で実行してlearning_agentのcronを登録するスクリプト
# 使い方: bash setup_cron_learning.sh

CRON_LINE="0 3 * * 1 cd /home/affiliate_bot && python3 agents/learning_agent.py >> logs/learning_agent.log 2>&1"

# 既に登録済みか確認
if crontab -l 2>/dev/null | grep -q "learning_agent.py"; then
    echo "[INFO] learning_agent.py のcronは既に登録されています:"
    crontab -l | grep "learning_agent.py"
else
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "[OK] 以下のcronを追加しました:"
    echo "$CRON_LINE"
fi
