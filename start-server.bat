@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 慧根堂·数字财税助手 - 后端服务

echo ============================================================
echo   慧根堂 · 数字财税助手  ——  后端服务启动器
echo ============================================================
echo.

set LLM_BASE_URL=https://api.deepseek.com/v1
set LLM_MODEL=deepseek-chat
set ADMIN_TOKEN=admin-dev-2026

echo  [1] 大模型（可选）：粘贴 DeepSeek 的 API Key（sk- 开头）。
echo      直接回车 = 用"规则引擎"模式启动（也能正常问答，只是不调用大模型）。
set /p LLM_API_KEY=     请输入 Key（可留空）: 

echo.
echo  [2] 正在启动后端，请勿关闭本窗口...
echo      启动后浏览器访问： http://localhost:8080
echo      说明：本窗口保持打开 = 服务在运行；关闭窗口 = 服务停止。
echo.

set PYEXE=C:\Users\admin\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PYEXE%" set PYEXE=python

"%PYEXE%" server.py

echo.
echo  服务已停止。按任意键关闭窗口。
pause >nul
