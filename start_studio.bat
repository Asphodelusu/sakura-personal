@echo off
chcp 65001 > nul
set "PRJ_ROOT=%~dp0"
set "SAKURA_PRJ_ROOT=%PRJ_ROOT%"

powershell -NoProfile -Command "$path = $env:SAKURA_PRJ_ROOT; if ($path -match '[^\x20-\x7E]') { exit 1 } else { exit 0 }" > nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Write-Host '[ERROR] Non-ASCII path detected. Please move the project to a pure ASCII path such as D:\sakura'; Write-Host ('Current path: ' + $env:SAKURA_PRJ_ROOT)"
    pause
    exit /b 1
)

if exist "%PRJ_ROOT%runtime\python.exe" (
    set "PYTHON_EXE=%PRJ_ROOT%runtime\python.exe"
) else if exist "%PRJ_ROOT%.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%PRJ_ROOT%.venv\Scripts\python.exe"
) else (
    echo [ERROR] python.exe not found in runtime\ or .venv\. Please set up a Python environment first.
    pause
    exit /b 1
)

cd /d "%PRJ_ROOT%"
"%PYTHON_EXE%" -m tools.studio_tauri.main 2> "%PRJ_ROOT%studio_error.log"
if errorlevel 1 (
    echo.
    echo [ERROR] Sakura Character Studio exited with error:
    echo --------------------------------------------------------
    type "%PRJ_ROOT%studio_error.log"
    echo --------------------------------------------------------
    echo.
    echo Build Tauri studio first: cd tools\studio-tauri\src-tauri ^&^& cargo build --release
    echo Or set SAKURA_TAURI_STUDIO_BIN to sakura-studio.exe
)
pause
