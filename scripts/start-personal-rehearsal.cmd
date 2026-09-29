@echo off
setlocal
cd /d "%~dp0.."
"runtime\python.exe" -I -X utf8 "tools\personal_desktop_rehearsal.py" %*
if errorlevel 1 pause
endlocal
