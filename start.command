#!/bin/bash
# エコリング仕入れリサーチ（入札受付中）をローカル起動（ダブルクリック）
# 自分のPC内専用なのでログイン画面は出ず、そのまま一覧が開きます（http://localhost:8781/）。
cd "$(dirname "$0")"
python3 server.py &
SERVER_PID=$!
sleep 2
open "http://localhost:8781/"
echo "起動しました（ログインなしで一覧が開きます）。止めるには、この画面で Ctrl+C を押してください。"
wait $SERVER_PID
