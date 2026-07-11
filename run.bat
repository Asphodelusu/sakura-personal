@echo off
chcp 65001 > nul
set "PRJ_ROOT=%~dp0"
set "SAKURA_PRJ_ROOT=%PRJ_ROOT%"

REM sentence-transformers 模型缓存放到项目目录
set "HF_HOME=%PRJ_ROOT%\data\hf-cache"
set "HF_ENDPOINT=https://hf-mirror.com"
set "SENTENCE_TRANSFORMERS_HOME=%PRJ_ROOT%\data\hf-cache"
if not exist "%HF_HOME%" mkdir "%HF_HOME%"

cd /d "%PRJ_ROOT%"
".venv\Scripts\python.exe" main.py
pause
