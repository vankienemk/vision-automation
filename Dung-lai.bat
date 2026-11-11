@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Dung Vision Automation
cd /d "%~dp0"

set "PORT=8080"
if exist "config.yaml" (
  for /f "usebackq tokens=1,2 delims=: " %%a in (`findstr /R /C:"^ *port: *[0-9]" config.yaml`) do (
    if /i "%%a"=="port" set "PORT=%%b"
  )
)

echo Dang yeu cau ung dung dung lai (cong !PORT!)...
powershell -NoProfile -Command "try { Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:!PORT!/api/system/shutdown' -TimeoutSec 5 | Out-Null; Write-Host '  -> Da gui yeu cau dung (an toan: luu config, bao MQTT offline).' } catch { Write-Host '  -> Khong goi duoc API (app co the dang tat, hoac bat xac thuc).' }"

timeout /t 4 /nobreak >nul

rem Neu API khong tat duoc (vd: da bat dang nhap), tat tien trinh python cua app.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match '-m app' } | ForEach-Object { Write-Host ('  -> Tat tien trinh PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

echo Xong.
timeout /t 3 /nobreak >nul
