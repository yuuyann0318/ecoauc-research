#!/bin/bash
# ============================================================
# 【先生用】エコリング会員限定・共有サイトを公開する
#   ・先生のアカウントでデータ収集
#   ・認証サーバーを常時稼働で起動（Mac起動時に自動・落ちたら自動再起動）
#   ・Tailscale Funnel で固定の公開URLを発行
#   生徒は そのURL を開き、各自のエコリング会員ログインで閲覧します。
# ============================================================
cd "$(dirname "$0")"
DIR="$(pwd)"; TS="/usr/local/bin/tailscale"; LA="$HOME/Library/LaunchAgents"; PY="$(command -v python3)"

# 0) 先生のエコリング会員情報
if grep -q "your-email@example.com" config.txt 2>/dev/null; then
  echo "✗ config.txt に先生のエコリング会員メール/パスワードを設定してください（データ収集用）"
  open -e config.txt 2>/dev/null; exit 1
fi

# 1) データ収集（先生のアカウントで）
echo "▶ エコリングにログインしてデータ収集中…"
"$PY" collector.py --pages 1

# 2) 認証サーバーを常時稼働（KeepAlive）
cat > "$LA/com.sedori.eco.auth.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.sedori.eco.auth</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/auth_server.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs_auth.txt</string>
  <key>StandardErrorPath</key><string>$DIR/logs_auth.txt</string>
</dict></plist>
PLIST
pkill -f "auth_server.py" 2>/dev/null
launchctl unload "$LA/com.sedori.eco.auth.plist" 2>/dev/null
launchctl load "$LA/com.sedori.eco.auth.plist"

# 3) データの定期収集（30分毎）
cat > "$LA/com.sedori.eco.collect.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.sedori.eco.collect</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/collector.py</string><string>--pages</string><string>1</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><false/><key>StartInterval</key><integer>1800</integer>
  <key>StandardOutPath</key><string>$DIR/logs_collect.txt</string>
  <key>StandardErrorPath</key><string>$DIR/logs_collect.txt</string>
</dict></plist>
PLIST
launchctl unload "$LA/com.sedori.eco.collect.plist" 2>/dev/null
launchctl load "$LA/com.sedori.eco.collect.plist"

# 4) Tailscale Funnel で公開
echo ""
echo "▶ 公開URLを発行します。初回はブラウザで Tailscale にログインしてください。"
"$TS" up 2>/dev/null
"$TS" funnel --bg 8782 2>&1
echo ""
echo "════════════════════════════════════════════"
echo "  公開URL（生徒に渡す固定URL）↓"
"$TS" funnel status 2>&1 | grep -o "https://[^ ]*" | head -1
echo ""
echo "  生徒はこのURLを開き、各自のエコリング会員ログインで閲覧します。"
echo "  （Macを起動したままにしてください）"
echo "════════════════════════════════════════════"
