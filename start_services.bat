@echo off
cd /d "%~dp0"
for %%S in (created exited dead) do (
    for /f %%C in ('docker ps -aq --filter "name=elemer-postgres" --filter "status=%%S"') do docker rm %%C >nul 2>nul
    for /f %%C in ('docker ps -aq --filter "name=elemer-grafana" --filter "status=%%S"') do docker rm %%C >nul 2>nul
)
docker compose up -d
pause
