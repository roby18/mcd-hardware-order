@echo off
chcp 65001 >nul
REM ============================================================
REM  麦门点餐台 + 饮食侦探 · 一键本地启动 (Windows)
REM    [1] 网页点餐台/侦探页  -> bridge_server.py   (必选)
REM    [2] 小智语音链路       -> mcp_pipe.py        (检测到 MCP_ENDPOINT 才起)
REM  凭证自动从 ..\..\.workbuddy\credentials-*.env 读取, 无需手动设环境变量。
REM  只用 hardware-order 这一套即可, 不需要 xiaozhi-mcp-hub。
REM ============================================================
setlocal
cd /d "%~dp0"

REM ---- Python 解释器: 优先 managed venv(自带 websockets), 否则系统 python ----
set "PY=C:\Users\roby6\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM ---- 从仓库外凭证文件抽取语音链路所需变量(只取所需, 不显示值) ----
call :loadcred
if defined MCP_ENDPOINT (echo [i] 已载入小智 MCP 接入点 - 语音链路将自动启动) else (echo [!] 未找到 MCP_ENDPOINT, 本次跳过语音桥)
if not defined YUANBAO_API_KEY if not defined TENCENTCLOUD_WSA_APIKEY echo [!] 未找到元宝 API KEY, 语音里的 yb_search 可能不可用

echo ============================================
echo   麦门点餐台 + 饮食侦探  (一键启动)
echo ============================================

REM ---- [1] 端口 8787 已占用则复用, 否则启动桥接服务 ----
netstat -ano | findstr ":8787 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo [1/3] 检测到 8787 已有服务在运行, 直接复用
) else (
  echo [1/3] 启动桥接服务 http://127.0.0.1:8787 ...
  start "bridge" /min "%PY%" bridge_server.py --port 8787
  echo       等待菜单与图片缓存加载...
  timeout /t 12 /nobreak >nul
)

REM ---- [2] 小智语音链路: 使用 xiaozhi-mcp-hub 的监督器(推荐) ----
set "HUB=%~dp0..\..\xiaozhi-mcp-hub"
set "HUBPY=%HUB%\.venv\Scripts\python.exe"
if exist "%HUBPY%" if defined MCP_ENDPOINT (
  echo [2/3] 启动小智 MCP Hub 监督器 (窗口名: voice) ...
  start "voice" /min /d "%HUB%" "%HUBPY%" supervise.py
) else if defined MCP_ENDPOINT (
  echo [2/3] 启动小智语音桥 mcp_pipe.py (窗口名: voice) ...
  start "voice" "%PY%" mcp_pipe.py --endpoint "%MCP_ENDPOINT%"
) else (
  echo [2/3] 跳过语音桥(未配置 MCP_ENDPOINT)
)

REM ---- [3] 打开页面 ----
echo [3/3] 打开页面...
start "" http://127.0.0.1:8787
start "" http://127.0.0.1:8787/detective

echo.
echo 点餐台:   http://127.0.0.1:8787          用餐/麦当劳赛道
echo 侦探页:   http://127.0.0.1:8787/detective 侦探赛道
if defined MCP_ENDPOINT echo 语音桥:   已在 "voice" 窗口连接小智云端, 对着设备说话即可点餐
echo.
echo 关闭方式: 关闭名为 bridge / voice 的窗口
echo.
pause
exit /b

:loadcred
REM 扫描 ..\..\.workbuddy\credentials-*.env, 抽取语音链路所需变量(不显示值)
set "CREDDIR=%~dp0..\..\.workbuddy"
for %%F in ("%CREDDIR%\credentials-*.env") do (
  for /f "usebackq tokens=1,* delims==" %%A in ("%%F") do (
    if /i "%%A"=="MCP_ENDPOINT" set "MCP_ENDPOINT=%%B"
    if /i "%%A"=="YUANBAO_API_KEY" set "YUANBAO_API_KEY=%%B"
  )
)
exit /b
