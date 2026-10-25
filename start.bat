@echo off
chcp 65001 >nul
setlocal
REM ============================================================
REM  麦门点餐台 + 饮食侦探 一键本地启动 Windows
REM   [1] 网页点餐台/侦探页 -> bridge_server.py  必选
REM   [2] 小智语音链路      -> xiaozhi-mcp-hub\supervise.py 自动看护
REM  凭证自动从 ..\..\.workbuddy\credentials-*.env 读取, 无需手动设环境变量。
REM  提示: 外层目录 D:\AI\Workbuddy\xiaozhiAI\start-all.bat 是同一套服务的总入口。
REM ============================================================
cd /d "%~dp0"
set "ORDER=%~dp0"
set "ROOT=%ORDER%..\..\"
set "HUB=%ROOT%xiaozhi-mcp-hub"

REM ---- Python 解释器: 优先 managed venv 自带 websockets, 否则系统 python ----
set "PY=C:\Users\roby6\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "HUBPY=%HUB%\.venv\Scripts\python.exe"

REM ---- 从仓库外凭证文件抽取所需变量, 只取所需, 不显示值 ----
call :loadcred
if defined MCD_MCP_TOKEN (echo [i] 已载入麦当劳 MCP Token) else (echo [!] 未找到 MCD_MCP_TOKEN)
if defined MCP_ENDPOINT (echo [i] 已载入小智 MCP 接入点, 语音链路将启动) else (echo [!] 未找到 MCP_ENDPOINT, 本次跳过语音桥)

echo ============================================
echo   麦门点餐台 + 饮食侦探  一键启动
echo ============================================

REM ---- [1] 端口 8787 已占用则复用, 否则启动桥接服务 ----
if not exist "%ORDER%bridge_server.py" (
  echo [FAIL] 找不到 bridge_server.py, 请从 hardware-order 目录运行
  pause
  exit /b 1
)
netstat -ano | findstr ":8787 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo [1/3] 检测到 8787 已有服务在运行, 直接复用
) else (
  echo [1/3] 启动桥接服务 http://127.0.0.1:8787 ...
  start "麦门点餐台-bridge" "%PY%" "%ORDER%bridge_server.py" --port 8787
  echo       等待菜单与图片缓存加载...
  ping -n 11 127.0.0.1 >nul
)

REM ---- [2] 小智语音链路: 用 hub 自带虚拟环境跑监督器 ----
if not exist "%HUB%\supervise.py" (
  echo [2/3] 跳过语音桥, 未找到 xiaozhi-mcp-hub
) else if not exist "%HUBPY%" (
  echo [2/3] [WARN] 未找到 hub 虚拟环境 %HUB%\.venv\Scripts\python.exe
  echo       小智 hub 需要 mcp/websockets/httpx, 请先在 xiaozhi-mcp-hub 目录执行:
  echo         python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
) else (
  echo [2/3] 启动小智 MCP 链路, 窗口名 小智MCP-hub ...
  start "小智MCP-hub" /min "%HUBPY%" "%HUB%\supervise.py"
)

REM ---- [3] 打开页面 ----
echo [3/3] 打开页面...
start "" http://127.0.0.1:8787
start "" http://127.0.0.1:8787/detective

echo.
echo 点餐台:   http://127.0.0.1:8787          用餐 / 麦当劳赛道
echo 侦探页:   http://127.0.0.1:8787/detective 侦探赛道
echo 语音桥:   状态看 小智MCP-hub 窗口日志, 对 ESP32 说话即可点餐
echo 停止:     关闭 bridge 与 小智MCP-hub 窗口, 或运行 ..\..\stop-all.bat
echo.
pause
exit /b 0

:loadcred
REM 扫描 ..\..\.workbuddy\credentials-*.env, 抽取所需变量, 不显示值
for %%F in ("%ROOT%.workbuddy\credentials-*.env") do (
  for /f "usebackq tokens=1,* delims==" %%A in ("%%F") do (
    if /i "%%A"=="MCD_MCP_TOKEN" set "MCD_MCP_TOKEN=%%B"
    if /i "%%A"=="MCP_ENDPOINT" set "MCP_ENDPOINT=%%B"
    if /i "%%A"=="YUANBAO_API_KEY" set "YUANBAO_API_KEY=%%B"
    if /i "%%A"=="TENCENTCLOUD_WSA_APIKEY" set "TENCENTCLOUD_WSA_APIKEY=%%B"
  )
)
exit /b 0
