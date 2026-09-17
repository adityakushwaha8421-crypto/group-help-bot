#!/bin/zsh
# Stop the bot and its Postgres; they stay stopped until ./scripts/run.sh or the next login.
U=$(id -u)
launchctl bootout gui/$U/com.betix.verifier.bot 2>/dev/null && echo "🤖 bot stopped"
launchctl bootout gui/$U/com.betix.verifier.postgres 2>/dev/null && echo "🐘 postgres stopped"
