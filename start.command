#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker Desktop не найден. Установите его с https://www.docker.com/products/docker-desktop/"
  read -r -p "Нажмите Enter, чтобы закрыть окно..."
  exit 1
fi

docker compose up --build -d

echo "LandPlotFinder запускается..."
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8787/health >/dev/null 2>&1; then
    if command -v open >/dev/null 2>&1; then
      open http://127.0.0.1:8787
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open http://127.0.0.1:8787 >/dev/null 2>&1 || true
    fi
    echo "Готово: http://127.0.0.1:8787"
    exit 0
  fi
  sleep 2
done

echo "Приложение не успело запуститься. Посмотрите журнал: docker compose logs"
exit 1
