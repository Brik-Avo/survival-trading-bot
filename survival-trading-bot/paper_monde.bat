@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (echo Lance d'abord install.bat & pause & exit /b 1)
set PYTHONIOENCODING=utf-8
call .venv\Scripts\activate.bat
python -m pip install -q yfinance vaderSentiment
python bot\tendance_monde.py paper
if "%PORT%"=="" set PORT=8000
start "Dashboard" cmd /k ".venv\Scripts\activate.bat && python dashboard\dashboard.py"
timeout /t 3 >nul
start "" http://localhost:%PORT%
pause
