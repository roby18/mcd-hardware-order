@echo off
chcp 65001 >nul
REM ============================================================
REM  麦门点餐台 + 饮食侦探 · 一键本地启动 (Windows)
REM  凭证自动从 ..\..\.workbuddy\credentials-*.env 读取(服务端加载)
REM  无需手动设置环境变量, 双击即用。
REM ============================================================
setlocal
cd /d "%~dp0"

set PY=C:\Users\roby6\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not exist "%PY%" set PY=python

echo ============================================
echo   麦门点餐台 + 饮食侦探  (本地调试)
echo ============================================

REM 端口 8787 已被占用则复用, 否则启动桥接服务
netstat -ano | findstr ":8787 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo [i] 检测到 8787 已有服务在运行, 直接复用
) else (
  echo [1/2] 启动桥接服务 http://127.0.0.1:8787 ...
  start "bridge" /min "%PY%" bridge_server.py --port 8787
  echo       等待菜单与图片缓存加载...
  timeout /t 12 /nobreak >nul
)

echo [2/2] 打开页面...
start "" http://127.0.0.1:8787
start "" http://127.0.0.1:8787/detective

echo.
echo 点餐台:   http://127.0.0.1:8787          用餐/麦当劳赛道
echo 侦探页:   http://127.0.0.1:8787/detective 侦探赛道
echo.
echo 调试提示: 加几样菜到餐盘 -^> 点「官方膳食佐证」看元宝官方信源
echo 关闭方式: 关闭名为 bridge 的黑色窗口(或任务栏最小化窗口)
echo.
pause
