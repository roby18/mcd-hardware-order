# 麦门点餐台 · 硬件联动版

> 一块 ESP32 键盘开发板 + 小智语音 + 麦当劳 MCP，做一个能真的下单的点餐台。
> 8 个键是快捷加菜，旋钮是切换选择，按住旋钮说话让 AI 帮你配一单。

## 一分钟看懂

```
输入(菜单照片)           中间(点餐台)              输出(订单)
--------------------------------------------------------------------------
麦当劳 MCP 实时菜单   ->  唱片中心大图环形切换  ->  按键加菜/旋钮选餐
(89 款, 带真实图片)      邻近条 + 菜单卡缩略图      实时算价 + 凑单提示
```

```
用户动作                系统反应
----------------------------------------------------------
拖动黑胶旋转       ->  中心大图 + 邻近条 + 菜单卡三层同步切到新餐品
短按黑胶/麦克风    ->  语音点餐（演示模式自动跑一轮"两人六十块"）
按住黑胶/麦克风    ->  PTT 真语音模式，对接小智硬件
按 1-7 号键        ->  加入键帽上标注的餐品（悬停键帽出现 ⚙ 可单独改绑）
按 8 号键          ->  确认这一单，实时算价
让 AI 配一单       ->  本地算法 / 大模型双引擎（设置里切换）
```

网页实时显示：餐盘、应付金额、已省多少、**还差多少钱能解锁下一档优惠**。

## 唱片中心 = 菜品照片

旋钮旋转时，黑胶中心（150px 金色描边圆窗）切换成对应餐品的**真实照片**，
底部浮层叠加菜名与实时价格。照片三级回退，保证现场一定能出图：

1. **本地缓存** `web/img/<商品码>.png` —— 启动时自动从麦当劳 CDN 预下载全部
   89 张（`prefetch_images`），现场断网/网络差也能演示，加载 <15ms
2. 远程 CDN `menu-img.mcd.cn` —— 本地缓存缺失时回退
3. 首字兜底 —— 图片全失败时显示餐名首字（红色渐变圆底）

邻近条 7 个 chip 和菜单卡同样带缩略图（34/44px 圆图、52px 圆角图），
三层照片同步切换，就是「输入菜单 → 旋钮选餐 → 输出订单」的完整视觉链路。

## 唱机旋钮怎么玩

网页中央是一台唱片机，就是硬件旋钮的镜像：

- **拖动黑胶**：指针绕圆心转动即旋转唱片，每转过 42° 切一首"歌"（换一个餐品），
  唱片持续旋转、**中心照片翻转换成新餐品**，伴随物理棘轮的"嗒"声（WebAudio 合成，无音频文件）。
- **松手**：保留角速度惯性滑行，转速衰减到零才停，期间继续切歌——和真旋钮一样。
- **快速转动时**：黑胶纹路加动态模糊，唱臂从待机位摆到播放位。
- **下方邻近条**：实时显示前后各 3 个餐品，按距离做景深衰减
  （scale 1.18/0.92/0.74/0.58 + 透明度/模糊/亮度递减），点任意一个直接跳转。
- **短按（<340ms）**：进入语音演示，浮层显示"我在听"→ 自动执行"帮我配一桌两人六十块的"。
- **长按（>340ms）**：PTT 真语音模式，日志记录"旋钮按下/松开"。
- 键盘 `←` `→` 等价旋转，`回车` 把当前选中加入餐盘，`1`-`8` 等价按键。

旋钮交互移植自 [stylecam-palette](https://github.com/roby18/stylecam-palette)
（第七期 AI 硬件训练营作业，同一块 EasyInput 键盘的网页复刻版）：

| stylecam 技法 | 本作落地 |
|---|---|
| 环形最短距离算法 `ringOff` | 邻近条按选中项取环上最短偏移 |
| 距离分层 scale/opacity/zIndex | 原样保留（1.3→0.64 改横向 1.18→0.58） |
| 景深衰减 `blur+亮度` | `blur(d*.55px) brightness(1-d*.08)` |
| 果冻弹性曲线 `cubic-bezier(.34,1.42,.48,1)` | 全局过渡曲线（chip 位移、标签翻转、唱臂） |
| 旋钮白点 `45+idx*36` | LIVE INPUT 面板红钮白点跟随真实唱片角度 |
| LIVE INPUT 硬件镜像（S1-S8 高亮） | 按键灯与网络解耦，260ms 松灯 |

比原版多的：黑胶是**真物理拖拽**（指针角度→角速度→惯性衰减），
原版只有键盘 ←→ 步进；切歌有棘轮声和动态模糊。

## 视觉风格

麦当劳品牌体系：官方红 `#DA291C` + 金拱门黄 `#FFC72C`，暖白底、圆角卡片、
金色 M 标（SVG 手绘）、红色实体键帽（带 3D 按压行程）。单文件零依赖、无 CDN，
断网环境可演示。

## 三个入口

| 入口 | 用途 |
|------|------|
| `http://127.0.0.1:8787` | 麦门点餐台（实时餐盘、按键、旋钮、AI 配单） |
| `http://127.0.0.1:8787/detective` | 侦探赛道作品「吃腻了麦当劳，然后呢？」· 元宝多信源分层取证（官方/学术/新闻 × 时效） |
| `POST /api/key {"key":1}` | 硬件按键入口（ESP32 或任何能发 HTTP 的设备） |
| MCP over stdio | 小智语音调用入口（经 `mcp_pipe.py` 接到云端） |

> 两条赛道是两件独立作品（规则：每件作品只参加一条赛道），共用同一套桥接底座。
> 点餐台投用餐/麦当劳赛道，detective 页投侦探赛道，各自可独立打开、独立演示。

**侦探赛道 · 元宝搜索特有能力**：detective 页对每个问题发起 5 轮递进搜索，每轮调用元宝
SearchPro 不同的垂直能力——① 场景定义（Query）② 官方口径（`Industry=gov`）③ 研究佐证
（`Industry=acad`）④ 最新动态（`Industry=news` + `Freshness=d30`）⑤ 交叉验证。证据链展示
信源类型徽标、发布时间、相关度与配图（`pics`），结论卡给出「权威信源命中 N/3」。按领域 /
时效过滤取证是通用搜索做不到的能力，也是本作的核心差异点。

## 快速开始

### 1. 装依赖

只需要一个：

```bash
pip install websockets
```

其余全部用 Python 标准库。

### 2. 配置凭证

```bash
set MCD_MCP_TOKEN=你的麦当劳MCP Token        # https://open.mcd.cn/mcp
set YUANBAO_API_KEY=sk-...                   # 元宝搜索(腾讯云), 侦探赛道用
set MCP_ENDPOINT=wss://api.xiaozhi.me/mcp/?token=...   # 可选, 语音链路才需要
```

元宝搜索接入文档：https://cloud.tencent.com/document/product/1806/130615
（`POST https://api.wsa.cloud.tencent.com/SearchPro`，`Authorization: Bearer <KEY>`）

小智 MCP 接入点获取：控制台 → 配置角色 → 高级设置 → MCP设置 → 获取 MCP 接入点。

### 3. 启动

**推荐：最外层总脚本**（一个脚本拉起全部服务，不用到处找）

```bat
D:\AI\Workbuddy\xiaozhiAI\start-all.bat    # 点餐台 bridge + 小智 MCP 链路(hub 监督器) + 打开页面
D:\AI\Workbuddy\xiaozhiAI\stop-all.bat     # 一键停止全部服务
```

总脚本会自动：

1. 从 `.workbuddy/credentials-*.env` 读取 `MCD_MCP_TOKEN` / `MCP_ENDPOINT` / `YUANBAO_API_KEY`（不回显值）
2. 启动 `bridge_server.py`（端口 8787，已占用则复用）
3. 用 `xiaozhi-mcp-hub\.venv\Scripts\python.exe` 启动 `supervise.py`，看护 mcp_pipe（小智接入点）与 hub（麦当劳 MCP），崩溃自动重启
4. 打开点餐台 `/` 与侦探页 `/detective`

**仓库内脚本**（功能相同，适合只拷了本目录的情况）：

```bat
start.bat                      # 一键启动：网页点餐台 + 小智 MCP 链路(自动找 hub 虚拟环境)
start_voice.bat                # 只起语音桥：点餐台已在运行时，单独起/重启小智语音链路
```

或手动三步：

```bash
python bridge_server.py --port 8787      # 桥接服务 + 网页
python mcp_pipe.py --endpoint wss://...  # 语音链路（可选）
```

> 注意：小智 hub 必须用 `xiaozhi-mcp-hub\.venv` 里的 Python 跑，系统 Python 缺 `mcp/websockets/httpx` 会直接起不来。

### 4. 日志与链路体检（判断小智是否连上）

所有服务输出统一落在最外层 `logs/` 目录，文件名固定，可直接 `tail -f`：

| 文件 | 内容 |
|------|------|
| `logs\bridge.log` | 点餐台：启动、菜单加载、算价、加菜/删菜、下单、大模型调用（带时间戳） |
| `logs\xiaozhi-hub.log` | 小智链路：supervise 看护状态 + mcp_pipe 连接日志 + hub 的 MCP 调用明细 |

```bat
tail-log.bat     # 实时跟踪全部日志（Ctrl+C 退出，不影响服务）
check-link.bat   # 一键体检：bridge 端口/API、链路进程、接入点凭证、连接状态，直接给结论
```

命令行方式（Git Bash）：

```bash
tail -f logs/bridge.log logs/xiaozhi-hub.log
```

**判断小智连接成功的标志**（在 `logs\xiaozhi-hub.log` 里）：

```text
[hub.py] Connecting to WebSocket server...          ← 正在连
[hub.py] Successfully connected to WebSocket server  ← 连上了（关键行）
[hub.py] Started server process: ...hub.py           ← 本地 MCP 服务已拉起
```

出现 `Connection closed` / `连接失败` / supervise 的 `退出了` 说明掉线，supervise 会自动退避重连；
持续掉线常见原因：接入点 token 失效或被顶号、网络不通、多个 supervise 抢同一接入点（先跑 `stop-all.bat` 再启动）。

## 目录结构

```
hardware-order/
├── bridge_server.py    餐盘状态 + HTTP API + SSE 推送 + 麦当劳调用 + 图片预下载
├── mcp_server.py       stdio MCP 服务（7 个工具）
├── mcp_pipe.py         接小智 MCP 接入点的 WebSocket 桥接器
├── start.bat           一键启动（bridge + 小智 MCP 链路）
├── start_voice.bat     单独起/重启小智语音链路
├── web/index.html      点餐台网页（单文件，无构建：唱机旋钮 + 照片窗 + 邻近条 + 硬件镜像折叠钮 + 可改按键帽 + 设置抽屉 + 菜单 + 餐盘）
├── web/detective.html  侦探赛道作品页（三轮递进搜索 + 证据链）
├── web/img/            菜品照片本地缓存（启动时自动下载，89 张）
├── config/             运行期配置（llm.json 含 Key 已 gitignore；keymap.json 键位映射）
└── README.md
```

## MCP 工具清单

| 工具 | 说明 |
|------|------|
| `mcd_plan` | 按人数/预算/忌口配一单，返回真实优惠金额 |
| `mcd_cart` | 查看餐盘与算价 |
| `mcd_add` | 按名称加入餐盘 |
| `mcd_remove` | 减少或移除 |
| `mcd_clear` | 清空餐盘 |
| `mcd_key` | 模拟按下硬件 1-8 号键 |
| `mcd_menu` | 查看当前在售菜单 |

## HTTP API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/state` | 当前餐盘与算价快照 |
| GET | `/api/events` | SSE 实时推送 |
| GET | `/api/menu` | 菜单与可领券（含 `image` 远程图与 `localImg` 本地路径） |
| GET | `/img/<商品码>.png` | 菜品照片本地缓存 |
| POST | `/api/key` | `{"key": 1-8}` 按键 |
| POST | `/api/dial` | `{"delta": ±1}` 旋钮 |
| POST | `/api/voice` | `{"state":"down"/"up"}` 旋钮按下（语音 PTT 状态） |
| POST | `/api/add` | `{"code": "...", "qty": 1}` 加菜 |
| POST | `/api/remove` | 减菜 |
| POST | `/api/clear` | 清空 |
| POST | `/api/plan` | `{"people":2,"budget":60,"avoid":"","engine":"local"/"llm"}` AI 配单 |
| POST | `/api/reload` | `{"city":"广州","keyword":"中山大学"}` 重新定位门店 |
| GET | `/api/config` | 配置下发：大模型（key 脱敏）+ 按键映射 |
| POST | `/api/llm-config` | 保存大模型配置；`{"test":true}` 测连通 |
| POST | `/api/keymap` | 保存 8 键映射 `{"keymap":{"1":{"type":"item","code":"5022"}}}` |

## 硬件接线（ESP32 侧，待接入）

当前网页已提供虚拟按键与旋钮，硬件不是演示的必要条件。
接入时把按键事件 POST 到 `/api/key` 即可：

```
S1-S7 -> POST /api/key {"key": 1..7}
S8    -> POST /api/key {"key": 8}
旋钮A/B 旋转 -> POST /api/dial {"delta": +1 / -1}
旋钮按下     -> POST /api/voice {"state":"down"}（松开时 up）
```

网页上 1-7 键帽显示的真实餐品名，是按当前菜单前 7 个主食自动推导的，
和硬件按键映射完全一致——菜单随时段变化，标签自动跟着变。

## 关键设计

**1. 连接是本机主动出站**

`mcp_pipe.py` 主动连 `wss://api.xiaozhi.me/mcp/`，所以本地服务**不需要公网 IP**，
也不需要内网穿透。评委看演示走录屏即可。

**2. 每一分钱都来自官方接口**

价格、优惠、门槛差额全部由麦当劳 MCP 的 `calculate-price` 实时算出，
本地不做任何估算。菜单按时段变化，也不缓存。

**3. 网页用 SSE 而不是 WebSocket**

SSE 走标准 HTTP，零依赖、穿透性好，单向推送正好满足"实时餐盘"的需求。
浏览器到服务器用普通 POST，不需要额外库。

**4. 双引擎：小智语音 + 大模型 API 互为备选**

语音链路走小智（MCP over WebSocket + ESP32 硬件）；网页配单可切**大模型 API**
（OpenAI 兼容，默认 `deepseek-v4.1-flash`，设置面板里填 Base URL / Key / 模型，
可先"测试连接"再启用）。两者独立：语音坏了不影响配单，大模型没配也能用本地算法。
**大模型只负责"选什么"，价格一律回店用 MCP 真实计算**——不会出现模型编造的价格，
调用失败自动回退本地算法并在界面上注明。

**5. 8 键全部可改绑**

键位存 `config/keymap.json`。网页上悬停任意键帽出现 ⚙，可绑任意菜单餐品或
系统动作（确认/清空），也可"恢复默认"（1-7 = 当前菜单前 7 主食）。
设置面板里能看到全部 8 键的当前绑定。硬件镜像面板默认收起成左下角小图标，
点击展开——不占主界面，演示时一键拉起向评委证明"网页和硬件是一套系统"。

## 边界

- 只算价，不下单、不自动领券。
- 价格与优惠以麦当劳官方实时结果为准。
- 参赛作品，非麦当劳官方产品。
