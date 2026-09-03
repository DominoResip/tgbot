@echo off
cd /d "%~dp0"
title SPT Schedule Bot
echo Starting SPT bot...
".\.venv\Scripts\python.exe" bot.py
pause
