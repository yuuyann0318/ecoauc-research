#!/bin/bash
# エコリング仕入れリサーチを起動（ダブルクリック）
cd "$(dirname "$0")"
python3 server.py &
SERVER_PID=$!
sleep 2
open "http://localhost:8781/"
echo "起動しました。止めるには、この画面で Ctrl+C を押してください。"
wait $SERVER_PID
