@echo off
setlocal
rem The venv lives in a short path: PySide6's DLLs fail to load from very long folders.
set "VENV=%LOCALAPPDATA%\Upstream\venv"
if not exist "%VENV%\Scripts\pythonw.exe" (
  echo First run: setting up Python environment...
  py -3.12 -m venv "%VENV%" 2>nul || python -m venv "%VENV%" || goto :fail
  "%VENV%\Scripts\python.exe" -m pip install -q -r "%~dp0requirements.txt" || goto :fail
)
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0main.py"
exit /b 0

:fail
echo Setup failed. Python 3.11+ must be installed.
pause
exit /b 1
