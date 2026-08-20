$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
docker compose stop
Write-Host "LandPlotFinder остановлен. Данные сохранены." -ForegroundColor Green
