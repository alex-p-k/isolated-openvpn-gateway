@echo off
setlocal DisableDelayedExpansion
set "PYTHONDONTWRITEBYTECODE=1"
where py.exe >nul 2>nul
if errorlevel 1 goto python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 goto python
py -3 "%~dp0install.py" --wizard %*
exit /b %errorlevel%
:python
where python.exe >nul 2>nul
if errorlevel 1 goto missing
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 goto missing
python "%~dp0install.py" --wizard %*
exit /b %errorlevel%
:missing
echo ERROR [PYTHON_REQUIRED]: Install Python 3.11 or newer for your Windows user.
echo Official installer: https://www.python.org/downloads/windows/
echo Enable the Python launcher or add Python to PATH, open a new terminal, then run .\setup.cmd again.
echo No private runtime from another application was searched or installed.
exit /b 1
