@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (echo Lance d'abord install.bat & pause & exit /b 1)
set PYTHONIOENCODING=utf-8
rem Arrete les anciennes versions et efface l'ancien etat pour ne jamais afficher de vieilles positions
taskkill /fi "WINDOWTITLE eq Trading Bot*" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq Scalper*" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq Dashboard*" /f >nul 2>&1
del /q bot_state_v2.json* >nul 2>&1
if "%PORT%"=="" set PORT=8000
start "Scalper (simulation)" cmd /k ".venv\Scripts\activate.bat && python bot\scalper.py"
timeout /t 3 >nul
start "Dashboard" cmd /k ".venv\Scripts\activate.bat && python dashboard\dashboard.py"
timeout /t 3 >nul
start "" http://localhost:%PORT%

echo SCALPER v7 lance : 100 positions, levier max du marche. Ferme les fenetres pour arreter.
