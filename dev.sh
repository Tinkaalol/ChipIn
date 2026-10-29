#!/usr/bin/env bash
# Запуск бота с автоперезапуском при изменении .py файлов в bot/ и migrations/
set -euo pipefail
cd "$(dirname "$0")"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python"
exec "$PY" -m watchfiles --filter python "$PY -m bot.main" bot migrations
