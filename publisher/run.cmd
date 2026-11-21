@echo off
rem Keep the publisher running in your own terminal, outside any Claude session.
rem Double-click this file or run it from a terminal. Ctrl+C stops it.
rem Only one publisher can run at a time: a second one exits with "already running".
cd /d "%~dp0.."
set PYTHONUNBUFFERED=1
"publisher\.venv\Scripts\python.exe" -m publisher run
pause
