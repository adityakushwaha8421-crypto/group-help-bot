#!/bin/zsh
# Start / check the bot and its Postgres. Both run under launchd (start at login, restart on crash) from the
# plists in ./launchd - ~/Library/LaunchAgents only holds symlinks to them. Safe to run any time.
cd "$(dirname "$0")/.."
P="$(pwd)"; U=$(id -u)
for svc in postgres bot; do
  L=com.betix.verifier.$svc
  [ -L ~/Library/LaunchAgents/$L.plist ] || { ln -sfn "$P/launchd/$L.plist" ~/Library/LaunchAgents/$L.plist && echo "🔗 linked $L"; }
  if ! launchctl print gui/$U/$L >/dev/null 2>&1; then
    launchctl bootstrap gui/$U ~/Library/LaunchAgents/$L.plist && echo "📦 $svc agent loaded"
  fi
  launchctl kickstart gui/$U/$L 2>/dev/null
done
sleep 3
PG=$(lsof -nP -iTCP:5433 -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $2}')
BOT=$(pgrep -f "[a]pp.main" | head -1)
[ -n "$PG" ]  && echo "🐘 Postgres: running (PID $PG)"   || echo "🐘 Postgres: NOT running"
[ -n "$BOT" ] && echo "🤖 Bot:      running (PID $BOT)"  || echo "🤖 Bot:      NOT running"
curl -s -m 3 http://127.0.0.1:8080/health && echo || echo "health: no response yet (bot may still be starting)"
