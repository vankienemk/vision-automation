@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Kiem tra moi truong - Vision Automation
cd /d "%~dp0"

echo ============================================================
echo   KIEM TRA MOI TRUONG - VISION AUTOMATION
echo ============================================================
echo.

if exist ".venv\Scripts\python.exe" (
  set "PYCMD=.venv\Scripts\python.exe"
) else (
  echo [CANH BAO] Chua co .venv - hay chay Khoi-dong.bat truoc (no se tu cai thu vien).
  echo.
  set "PYCMD=python"
)

!PYCMD! run.py --doctor
set "CODE=!errorlevel!"

echo.
echo ============================================================
if not "!CODE!"=="0" (
  echo   CON LOI CAN XU LY - xem danh sach "CAN XU LY" o tren.
) else (
  echo   MOI THU OK - san sang chay Khoi-dong.bat
)
echo ============================================================
echo.
pause
