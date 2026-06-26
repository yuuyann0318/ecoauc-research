#!/bin/bash
# 【最初に1回】エコリングへのログインを確認する
cd "$(dirname "$0")"
echo "════════════════════════════════════════════"
echo "  エコリング ログイン設定"
echo "════════════════════════════════════════════"
echo ""
echo "① まず config.txt にあなたの会員メール・パスワードを書いて保存してください。"
echo "   （このPC内だけに保存され、外部には送信されません）"
echo ""
# config.txt がプレースホルダのままなら開いてあげる
if grep -q "your-email@example.com" config.txt 2>/dev/null; then
  echo "→ config.txt を開きます。書き換えて保存したら、もう一度このファイルを実行してください。"
  open -e config.txt
  exit 0
fi
echo "② ログインを試します …"
echo ""
python3 collector.py --setup
echo ""
echo "成功と出たら start.command で使い始められます。"
