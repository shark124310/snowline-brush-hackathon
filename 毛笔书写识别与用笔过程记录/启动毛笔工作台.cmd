@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" run.py
) else if exist "D:\conda3_envs\zzss\python.exe" (
  "D:\conda3_envs\zzss\python.exe" run.py
) else (
  python run.py
)
pause
