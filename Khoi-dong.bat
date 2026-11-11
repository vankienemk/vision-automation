@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Vision Automation
cd /d "%~dp0"

echo ============================================================
echo   VISION AUTOMATION - khoi dong tren Windows
echo ============================================================
echo.

rem --- 1) chon trinh thong dich Python ----------------------------------------
set "PYCMD="
if exist ".venv\Scripts\python.exe" set "PYCMD=.venv\Scripts\python.exe"
if not defined PYCMD (
  py -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PYCMD=py -3"
)
if not defined PYCMD (
  python -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
  echo [LOI] Khong tim thay Python tren may nay.
  echo       1. Tai Python 3.12 tai https://www.python.org/downloads/windows/
  echo       2. Khi cai nho TICH o "Add python.exe to PATH".
  echo       3. Mo lai file nay.
  echo.
  pause
  exit /b 1
)

rem --- 2) lan dau: tao moi truong rieng + cai thu vien ------------------------
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Dang tao moi truong Python rieng ".venv" ...
  %PYCMD% -m venv .venv
  if errorlevel 1 (
    echo [LOI] Khong tao duoc .venv ^(thieu quyen ghi hoac Python loi^).
    echo       Thu chuyen thu muc chuong trinh ve vi du C:\vision-automation
    echo       va chay lai file nay bang quyen Administrator.
    pause
    exit /b 1
  )
  set "PYCMD=.venv\Scripts\python.exe"
  echo [2/3] Dang cai thu vien ^(lan dau mat 5-15 phut, can Internet^)...
  !PYCMD! -m pip install --upgrade pip
  rem Ban torch CPU nho hon ~10 lan so voi ban CUDA (may chi chay CPU la du).
  !PYCMD! -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
  !PYCMD! -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [LOI] Cai thu vien that bai. Kiem tra ket noi mang roi chay lai file nay.
    pause
    exit /b 1
  )
)

rem --- 3) chay chuong trinh --------------------------------------------------
if not exist "config.yaml" echo [3/3] Lan dau chay: se tao config.yaml va mo trinh duyet.

rem Mo trinh duyet sau 6 giay (app can vai giay de nap model AI).
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 6; Start-Process 'http://127.0.0.1:8080'"

echo.
echo   Dang khoi dong... trinh duyet se tu mo http://127.0.0.1:8080
echo   TAT chuong trinh: chay Dung-lai.bat, hoac bam Ctrl+C roi dong cua so nay.
echo.
!PYCMD! run.py --config config.yaml --with-webcam %*

set "EXITCODE=!errorlevel!"
echo.
if not "!EXITCODE!"=="0" (
  echo [CANH BAO] Chuong trinh ket thuc voi ma loi !EXITCODE!.
  echo            Xem chi tiet trong thu muc logs\ .
) else (
  echo Chuong trinh da dung binh thuong.
)
echo.
pause
