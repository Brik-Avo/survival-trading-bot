@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (echo Lance d'abord install.bat & pause & exit /b 1)
set PYTHONIOENCODING=utf-8
rem Charge les variables du fichier .env (lignes CLE=valeur, commentaires ignores)
if exist .env for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do if not "%%b"=="" set "%%a=%%b"
if "%PORT%"=="" set PORT=8000
start "Trading Bot" cmd /k ".venv\Scripts\activate.bat && python bot\trading_bot.py"
timeout /t 3 >nul
start "Dashboard" cmd /k ".venv\Scripts\activate.bat && python dashboard\dashboard.py"
timeout /t 3 >nul
start "" http://localhost:%PORT%
echo Bot et dashboard lances. Ferme les deux fenetres (ou Ctrl+C) pour arreter.
