# MCP 集成说明

> 本文件说明「麦门点餐台 · 智能硬件联动版」如何接入并使用麦当劳中国 MCP。
> 所有金额以麦当劳官方接口实时返回结果为准，本地不做任何估算。

## 使用的 MCP Server

| 项目 | 值 |
|------|-----|
| Server 名称 | `mcd-mcp` |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 认证方式 | 请求头 `Authorization: Bearer ${MCD_MCP_TOKEN}` |
| Server 版本 | 1.0.0（实测 initialize 返回） |
| 可用 Tool 数 | 35（实测 tools/list 返回） |
| 限流 | 600 次/分钟，超限返回 429 |
| 官方文档 | https://github.com/M-China/mcd-mcp-server |

Token 申请：https://open.mcd.cn/mcp （手机号登录 → 控制台 → 激活）

## 实际调用的 Tool

本作品在完整链路中共使用 10 个 Tool：

| Tool | 用途 | 在本作品中的角色 |
|------|------|-----------------|
| `query-nearby-stores` | 按位置查询可点餐门店 | 定位门店，取得 `storeCode` |
| `query-meals` | 查询门店当前可售餐品 | 菜单分类、餐品编码、现价/原价、图片 |
| `available-coupons` | 查询可领取的麦麦省券 | 展示"还有券没领" |
| `query-my-coupons` | 查询卡包内已有券 | 算价时纳入可用券 |
| `auto-bind-coupons` | 一键领取可用券 | 用户显式确认后才调用 |
| `calculate-price` | 计算金额与优惠 | **核心**：真实应付、优惠、门槛差额 |
| `delivery-query-addresses` | 查询外送地址簿 | 外送下单时选择收货地址 |
| `delivery-query-stores` | 查询可外送门店 | 取营业中的外送门店 |
| `create-order` | 创建订单 | 用户确认下单时调用（到店自取 / 外送） |
| `query-order` | 查询订单状态 | 下单后回查订单 |

未调用但已确认可用的相邻能力：`list-nutrition-foods`（营养数据）、`campaign-calendar`（活动日历）、
`cancel-order`（取消订单）。本作品只在用户显式确认后才调用会产生副作用的 Tool（领券 / 下单）。

## 调用流程

```
[1] query-nearby-stores  {beType:1, searchType:2, city, keyword}
        -> data[].storeCode / storeName / address / distance
        取 distance 最小者

[2] query-meals  {storeCode, orderType:1, beType:1}
        -> data.categories[].{name, meals[].code}
        -> data.meals.{code}.{name, currentPrice, originalPrice, image}
        实测样例: 广州某门店返回 11 个分类 / 89 款在售餐品（菜单按时段变化，不缓存）

[3] available-coupons / query-my-coupons
        -> 可领取券 + 卡包已有券

[4] 组合生成（本地 / 大模型双引擎）
        大模型只负责"选什么"，不负责定价

[5] calculate-price  {storeCode, orderType, beType, items:[{productCode, quantity}]}
        -> data.originalPrice         原价(分)
        -> data.price                 应付(分)
        -> data.discount              优惠金额(分)
        -> data.enjoyable.balance     门槛差额(分)      <-- 关键字段
        -> data.enjoyable.realDiscount / amountType   下一档优惠力度与类型

[6] 实时展示: 应付 / 已省 / 还差多少解锁下一档

[7] 下单（用户确认后）
        create-order -> 返回订单号
        query-order  -> 回查状态
```

### 外送与自取的参数差异

| 场景 | orderType | beType | 说明 |
|------|-----------|--------|------|
| 到店自取 | 1 | 1 | 不传 `beCode` |
| 麦乐送（外送） | 2 | 2 | 需 `delivery-query-addresses` 取 addressId、`delivery-query-stores` 取门店 |

⚠️ 实测要点：**外送与自取的商品码不同**，外送需按名称重新换码后再算价，直接复用自取商品码会算价失败。
外送金额还包含 `deliveryPrice`（配送费）与 `packingPrice`（打包费）。

## 业务价值：为什么必须用 MCP

本作品能成立，是因为 MCP 提供了三种**菜单上看不到**的数据：

**1. 真实应付价，而不是标价相加**

`calculate-price` 返回的是麦当劳官方算出来的应付金额，已计入套餐优惠、券、时段活动。
自己按菜单价目表加总是算不准的。

**2. 门槛差额 `enjoyable.balance`（立足点）**

它回答"再加多少钱能解锁下一档优惠"——正是顾客凑单时真正想知道、
但麦当劳官方界面上没有任何入口能看到的信息。没有 MCP，这个功能根本无法存在。

**3. 当前时段的真实可售菜单**

`query-meals` 返回的是"此刻这家店能买到什么"。麦当劳早/午/夜市菜单不同，
静态价目表会给出错误答案。本作品菜单按时段刷新，不做长期缓存。

反过来说：**去掉 MCP，这个作品就退化成一份过期的菜单价目表**。

## 硬件联动与 MCP 的分工

```
硬件(ESP32 8键+旋钮)  ->  POST /api/key、/api/dial  ->  桥接服务(餐盘状态)
                                                          |
                                                    调用麦当劳 MCP 算价
                                                          |
                                                    SSE 推送 -> 网页/硬件实时刷新
```

关键点：**硬件只负责"选了什么"，价格一律由 MCP 真实计算**。
即使大模型不可用，本地算法 + MCP 算价仍可完成点餐。

## 语音 + 大模型的分工

语音输入经大模型解析成「人数 / 预算 / 忌口」等结构化条件，
大模型只输出**商品选择**，随后这些商品仍回传 MCP `calculate-price` 真实算价。

**因此不存在"模型编造价格"的可能**：大模型可能选错菜，但说不出一个假价格。
大模型调用失败时自动回退本地算法，并在界面上注明回退原因。

## 数据处理说明

- 所有金额字段单位为「分」，统一转换为元后展示。
- MCP 返回体是「字段说明 + 原始 JSON」的混合文本，
  本项目实现余额扫描解析，取最后一个可解析且含 `data` 的 JSON 块。
- 菜品照片启动时从麦当劳 CDN 预下载到本地缓存（断网也能演示），缺失时回退远程 CDN，
  再失败则显示餐名首字兜底。

## 安全与合规

- 代码中**不存在任何 Token、密钥或账号凭证**，统一通过环境变量 `MCD_MCP_TOKEN` 读取。
- `.gitignore` 已排除 `.env`、`config/llm.json`、`*token*`、`credentials*`。
- 领券、下单类 Tool 只在用户显式确认后调用，不自动执行。
- 下单结果的返回值**不含完整支付链接**，只回订单号与状态，避免语音播报长链接。
- 不使用、不存储任何用户个人信息。
