#!/bin/bash
TS="/usr/local/bin/tailscale"
# Tailが停止していたら起動
if ! "$TS" status >/dev/null 2>&1; then
  "$TS" up >/dev/null 2>&1
fi
# Funnelが張られていなければ張る
if ! "$TS" funnel status 2>/dev/null | grep -q "8782"; then
  "$TS" funnel --bg 8782 >/dev/null 2>&1
fi
