@echo off
REM ============================================================
REM  小智语音桥 - 单独启动 (hardware-order)
REM  仅启动 mcp_pipe.py(连小智云端), 不起网页。
REM  前提: bridge_server.py 已在 http://127.0.0.1:8787 运行(否则先跑 start.bat)。
REM  凭证自动从 ..\..\.workbuddy\credentials-*.env 读取 MCP_ENDPOINT。
REM ============================================================
setlocal
cd /d "%~dp0"

set "PY=C:\Users\roby6\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

call :loadcred

if not defined MCP_ENDPOINT (
  echo [!] 未找到 MCP_ENDPOINT(小智 MCP 接入点)。
  echo     请到 小智控制台 -^> 配置角色 -^> 高级设置 -^> MCP设置 -^> 获取 MCP 接入点,
  echo     写入 ..\..\.workbuddy\credentials-xiaozhi.env 的 MCP_ENDPOINT= 后重试。
  echo.
  pause
  exit /b 1
)

echo 启动小智语音桥(窗口名: voice)...
start "voice" "%PY%" mcp_pipe.py --endpoint "%MCP_ENDPOINT%"
echo.
echo 已启动。对着小智设备说话即可操作点餐台(配单/加菜/查餐盘/联网搜索)。
echo 关闭方式: 关闭名为 voice 的窗口。
echo.
pause
exit /b

:loadcred
set "CREDDIR=%~dp0..\..\.workbuddy"
for %%F in ("%CREDDIR%\credentials-*.env") do (
  for /f "usebackq tokens=1,* delims==" %%A in ("%%F") do (
    if /i "%%A"=="MCP_ENDPOINT" set "MCP_ENDPOINT=%%B"
    if /i "%%A"=="YUANBAO_API_KEY" set "YUANBAO_API_KEY=%%B"
  )
)
exit /b