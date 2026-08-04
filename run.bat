@echo off
chcp 65001 > nul
set "PRJ_ROOT=%~dp0"
set "SAKURA_PRJ_ROOT=%PRJ_ROOT%"

REM 模型缓存统一放到项目目录，避免 Windows 清理 C 盘 Temp 目录时误删
set "HF_HOME=%PRJ_ROOT%\data\hf-cache"
set "HF_ENDPOINT=https://hf-mirror.com"
set "SENTENCE_TRANSFORMERS_HOME=%PRJ_ROOT%\data\hf-cache"
set "FASTEMBED_CACHE_PATH=%PRJ_ROOT%\data\cache\fastembed"
if not exist "%HF_HOME%" mkdir "%HF_HOME%"
if not exist "%FASTEMBED_CACHE_PATH%" mkdir "%FASTEMBED_CACHE_PATH%"

cd /d "%PRJ_ROOT%"
set "SAKURA_LAUNCHER_RESTART=1"

:run
".venv\Scripts\python.exe" main.py
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="73" (
    cls
    echo [Sakura] 正在重启...
    goto run
)
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [Sakura] 进程已退出，代码 %EXIT_CODE%
)
pause
