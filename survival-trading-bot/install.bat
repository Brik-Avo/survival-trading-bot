@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Installation du Survival Trading Bot ===

rem Choisit une version de Python compatible (3.12 > 3.11 > 3.13). 3.14 n'a pas encore toutes les dependances precompilees.
set PY=
for %%v in (3.12 3.11 3.13) do (
  if not defined PY (
    py -%%v --version >nul 2>nul && set "PY=py -%%v"
  )
)
if not defined PY (
  echo [ERREUR] Aucune version compatible de Python trouvee ^(3.11, 3.12 ou 3.13^).
  echo Installe Python 3.12 depuis https://www.python.org/downloads/release/python-31210/
  echo ^(Windows installer 64-bit, coche "Add python.exe to PATH"^), puis relance install.bat.
  pause & exit /b 1
)
echo Python utilise :
%PY% --version

rem Recree le venv s'il a ete fait avec une autre version
if exist .venv (
  echo Suppression de l'ancien environnement...
  rmdir /s /q .venv
)
%PY% -m venv .venv || (echo [ERREUR] creation du venv & pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install --prefer-binary -r requirements.txt || (echo [ERREUR] installation des dependances & pause & exit /b 1)
if not exist .env copy .env.example .env >nul
echo.
echo === Installation terminee. Lance start.bat ===
pause
