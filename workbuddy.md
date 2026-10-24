# workbuddy.md — 麦门点餐台 & 饮食健身侦探 开发对话上下文

> 本文件记录本项目在 WorkBuddy 中的开发过程，用于核验 WorkBuddy 专项奖励条件，
> 以及同步参加麦当劳程序员创意开发大赛的对话记录核验。
> 凭证信息已按参赛声明要求全部移除，仅保留环境变量占位符。

---

## 一、需求来源

**用户**：我要参加 WaytoAGI 1024 切磋大会（广州&佛山会场），选用餐赛道和侦探赛道，两条赛道各投一件作品。

**助手动作**：抓取赛事官网首页、参赛规则页、奖项页、用餐赛道页、侦探赛道页与麦当劳套餐说明页，
产出《1024 切磋大会参赛手册》，并提取关键约束。

**关键约束提取**：
- 用餐赛道前三必须用 WorkBuddy 开发（工具是评奖资格线，不是加分项），作品须真实调用麦当劳 MCP
- 侦探赛道搜索是核心，去掉元宝搜索作品就转不起来；须提交 3 次真实查询的录屏/截图（每次有结果 + 点得开的来源）
- 侦探赛道获奖硬门槛：须公开分享到小红书或视频号，带 4 个指定话题
- 参赛材料中不得包含真实 Token，配置文件仅用环境变量占位符
- 两条赛道是两件独立作品（每件作品只参加一条赛道），可共用同一套工程底座

---

## 二、技术侦察（使用 WorkBuddy 执行）

### 2.1 拉取官方文档
- 麦当劳 MCP 集成指南、参赛声明 `CONTEST_DECLARATION.md`、`activityGuidelines.md`
- 腾讯云元宝搜索（SearchPro）接入文档

**关键发现**：
1. 麦当劳 MCP 接入地址为 `https://mcp.mcd.cn`（streamablehttp）
2. 核心能力 `calculate-price` 返回的 `enjoyable.balance`（门槛差额）回答了"再加多少钱能解锁下一档优惠"——
   这正是用户在凑单时真正想知道、但麦当劳官方界面上没有任何入口能看到的信息。没有 MCP，这个功能不可能存在。
3. 元宝 SearchPro 支持按 `Industry`（gov / acad / news）与 `Freshness`（如 d30）做垂直/时效检索，
   这是通用搜索做不到的取证能力。

### 2.2 实机验证 MCP 连通性（用户注入 Token 后用环境变量方式验证）
```
[1] initialize            -> 200 OK, serverInfo: mcd-mcp v1.0.0
[2] tools/list            -> 35 个 Tool
[3] query-nearby-stores   -> 返回门店列表（含最近距离）
[4] query-meals           -> 11 个分类 / 89 款在售餐品（name/image/currentPrice/originalPrice/discountType）
[5] available-coupons     -> 可领券列表
[6] calculate-price       -> 返回 productPrice / discount / price（应付）/ enjoyed / enjoyable.balance（门槛差额）
```
结论：价格、优惠、门槛差额全部由官方接口实时算出，本地不做任何估算。

---

## 三、开发过程

### 3.1 工程结构（仓库 `contest-1024/hardware-order/`）
```
bridge_server.py   餐盘状态 + HTTP API + SSE 推送 + 麦当劳调用 + 图片预下载
mcp_server.py      stdio MCP 服务（7 个工具）
mcp_pipe.py        接小智 MCP 接入点的 WebSocket 桥接器
web/index.html     点餐台网页（单文件：唱机旋钮 + 照片窗 + 邻近条 + 硬件镜像 + 设置抽屉）
web/detective.html 侦探赛道作品页（5 轮递进搜索 + 证据链）
config/            运行期配置（llm.json 含 Key 已 gitignore；keymap.json 键位映射）
```

### 3.2 点餐台核心模块
| 模块 | 职责 |
|------|------|
| 餐盘状态机 | 持有当前餐盘、算价快照，SSE 实时推送 |
| 麦当劳适配层 | 调用 MCP 的 query-meals / calculate-price，仅读不写 |
| 旋钮/按键镜像 | POST /api/dial、/api/key，网页复刻 ESP32 硬件 |
| AI 配单双引擎 | 大模型选餐（OpenAI 兼容，默认 deepseek-v4.1-flash）+ 本地算法兜底，失败回退并透出原因 |
| 8 键可改绑 | keymap.json 存储；网页悬停键帽出现 ⚙ 改绑 |

### 3.3 侦探页核心模块
| 模块 | 职责 |
|------|------|
| 问题拆解 | 把口语问题拆成可检索的条件组合 |
| 5 轮递进 | ①场景定义（全网）②官方口径(gov)③研究佐证(acad)④最新动态(news, d30)⑤交叉验证 |
| 证据链渲染 | 信源类型徽标(gov红/acad蓝/news橙)、发布时间、相关度、配图(pics) |
| 结论卡 | "权威信源命中 N/3" + 未找到部分明确标注 |

### 3.4 踩坑与修复
- **大模型推理 JSON 解析失败**：推理模型返回带思考过程的混合文本，原解析正则贪婪匹配失败 →
  改为扫描花括号平衡块、取最后一个可解析且含目标字段的对象。
- **预算门禁缺失导致低价场景空解**：加预算门禁，低于阈值转本地枚举。
- **本地算法同款×2 凑数**：提示词要求"份量要够所有人吃饱"，驱动大模型给两个口味而非同款×2。
- **SSE 比 WebSocket 更适合**：单向推送满足"实时餐盘"，零依赖、穿透性好。
- **连接本机主动出站**：mcp_pipe 主动连 wss，本地服务不需要公网 IP，评委看录屏即可。

### 3.5 实跑结果（用餐赛道，2 人 / 60 元）
- 大模型给出两个不同口味三件套（约 54-57 元），`engine: llm`
- 价格卡显示 原价 / 现价 / 优惠 / **凑单差额**，全部来自 MCP
- 大模型 Key 故意改错一位 → 约 2 秒回退本地算法，顶部提示"大模型不可用，已回退本地算法（HTTP 错误 401）"

---

## 四、安全处理

按 `CONTEST_DECLARATION.md` 的「信息安全声明」要求：
1. MCP Token / 元宝 API Key 保存在仓库外的 `.workbuddy/credentials-*.env`，并加入 `.gitignore`
2. 代码中不存在任何 Token 字面量，统一读取环境变量 `MCD_MCP_TOKEN` / `YUANBAO_API_KEY`
3. `.gitignore` 排除 `.env` / `config/llm.json` / `*.log` / `web/img/`（13M 可再生缓存图，运行时重新拉取）
4. 本文件（workbuddy.md）在写入前已移除所有凭证字面量
5. 脚本只调用只读类 Tool，不调用会产生副作用的下单/领券工具

---

## 五、交付文件

| 文件 | 说明 |
|------|------|
| `README.md` | 项目介绍、安装、使用、技术细节（权威） |
| `workbuddy.md` | 本文件 |
| `bridge_server.py` / `mcp_server.py` / `mcp_pipe.py` | 桥接服务三件套 |
| `web/index.html` | 点餐台网页 |
| `web/detective.html` | 侦探页 |
| `路演演示案例.md` | 8 分钟双赛道路演脚本 |

---

## 六、待办
- [ ] 侦探赛道：跑 3 次真实查询并截图/录屏上传（第 3 条选查不到的，展示"敢说不知道"）
- [ ] 侦探赛道：小红书/视频号发布（四话题齐全），链接回填提交页（赛道之星/优秀奖硬门槛）
- [ ] 推送前全量扫描 Token 泄露（含 git 历史）
