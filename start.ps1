$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Host "Docker Desktop не найден. Установите его: https://www.docker.com/products/docker-desktop/" -ForegroundColor Red
  Read-Host "Нажмите Enter, чтобы закрыть окно"
  exit 1
}

docker compose up --build -d
Write-Host "LandPlotFinder запускается..."

for ($attempt = 0; $attempt -lt 60; $attempt++) {
  try {
    Invoke-WebRequest -Uri "http://127.0.0.1:8787/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
    Start-Process "http://127.0.0.1:8787"
    Write-Host "Готово: http://127.0.0.1:8787" -ForegroundColor Green
    exit 0
  } catch {
    Start-Sleep -Seconds 2
  }
}

Write-Host "Приложение не успело запуститься. Выполните: docker compose logs" -ForegroundColor Red
exit 1
