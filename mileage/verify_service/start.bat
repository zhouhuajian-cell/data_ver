@echo off
REM 参数验证服务启动脚本
setlocal
cd /d "%~dp0.."
if "%VERIFY_PORT%"=="" set VERIFY_PORT=8801
if "%VERIFY_HOST%"=="" set VERIFY_HOST=127.0.0.1
echo Starting verify service on http://%VERIFY_HOST%:%VERIFY_PORT%
python verify_service\app.py
