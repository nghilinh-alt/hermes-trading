@echo off
cd /d "%~dp0"
echo Starting Hermes Trading Dashboard...
start "" http://localhost:8888
python dashboard.py
pause
