#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
docker compose stop
echo "LandPlotFinder остановлен. Данные сохранены."
