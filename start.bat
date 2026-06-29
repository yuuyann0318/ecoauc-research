@echo off
chcp 65001 >nul
cd /d %~dp0
echo ===== エコリング仕入れリサーチ（入札受付中・ローカル） =====
echo 自分のPC内専用なのでログイン画面は出ず、そのまま一覧が開きます。
echo 閉じるにはこの画面で Ctrl+C を押してください。
echo.
start "" http://localhost:8781/
python server.py
if errorlevel 1 (
  echo python が見つからない場合は py で再試行します...
  py server.py
)
pause
