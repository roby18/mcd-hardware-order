@echo off
chcp 65001 >nul
REM 麦门点餐台 + 饮食侦探 一键启动 (Windows)
REM 用法:
REM   1) 设置麦当劳 Token (用餐赛道)
REM      set MCD_MCP_TOKEN=你的Token
REM   2) 设置元宝搜索 Key (侦探赛道)
REM      set YUANBAO_API_KEY=sk-...
REM   3) 若要用小智语音, 再设置接入点
REM      set MCP_ENDPOINT=wss://api.xiaozhi.me/mcp/?token=你的接入点token
REM   4) start.bat

setlocal
cd /d "%~dp0"

set PY=C:\Users\roby6\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not exist "%PY%" set PY=python

echo ============================================
echo  麦门点餐台 + 饮食侦探
echo ============================================

if "%MCD_MCP_TOKEN%"=="" (
  echo [!] 未设置 MCD_MCP_TOKEN, 点餐台菜单将无法加载
  echo     申请入口: https://open.mcd.cn/mcp
  echo.
)
if "%YUANBAO_API_KEY%"=="" (
  echo [!] 未设置 YUANBAO_API_KEY, 侦探页搜索将不可用
  echo     申请文档: https://cloud.tencent.com/document/product/1806/130615
  echo.
)

echo [1/3] 启动桥接服务 http://127.0.0.1:8787 ...
start "bridge" /min "%PY%" bridge_server.py --port 8787

echo       等待菜单与图片缓存加载...
timeout /t 12 /nobreak >nul

echo [2/3] 启动小智 MCP 桥接器...
if "%MCP_ENDPOINT%"=="" (
  echo [!] 未设置 MCP_ENDPOINT, 跳过语音链路
  echo     获取方式: 小智控制台 -^> 配置角色 -^> 高级设置 -^> MCP设置 -^> 获取 MCP 接入点
) else (
  start "pipe" /min "%PY%" mcp_pipe.py --endpoint "%MCP_ENDPOINT%"
  echo       语音链路已启动
)

echo [3/3] 打开两个作品页面...
start "" http://127.0.0.1:8787
start "" http://127.0.0.1:8787/detective

echo.
echo 点餐台:   http://127.0.0.1:8787          (用餐/麦当劳赛道)
echo 侦探页:   http://127.0.0.1:8787/detective (侦探赛道)
echo 关闭方式: 关闭 bridge 与 pipe 两个窗口
echo.
pause
