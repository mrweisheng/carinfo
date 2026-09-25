# carinfo 检索服务接入文档

> **给谁看**:要调用本服务的前端/程序开发者(HTTP API),以及要在 AI 客户端
> (Claude Desktop / Cursor / Cline 等)里接入车源检索的同学(MCP)。
> 运维部署请看 [DEPLOY.md](DEPLOY.md),本文不涉及。
>
> **最后验证**:2026-09-25,全部端点与工具实测通过。

---

## 0. 两扇门,选哪扇

HTTP API 和 MCP 是**同一个检索内核的两个入口**,搜索结果、打分、比价口径完全一致,
不存在"两边数据不一样"的问题。

| 你的场景 | 用 |
|---|---|
| 自己写程序/前端/小程序,明确知道要查什么 | **HTTP API** |
| 让 AI 助手(或你构建的 Agent)替用户查车、组织回答 | **MCP** |
| 快速人工验证服务是否正常 | HTTP API 的 `/health` |

---

## 1. 通用约定

### 1.1 服务地址

| 入口 | 地址 |
|---|---|
| HTTP API | `https://searchcar.eazycar.top` |
| MCP(streamable-http) | `https://searchcar.eazycar.top/mcp` |

### 1.2 鉴权(必读)

所有请求必须带 `X-API-Key` 请求头(43 位共享密钥,向管理员索取):

```bash
curl -H "X-API-Key: <你的43位key>" https://searchcar.eazycar.top/health
```

- **没有带 / 带错 → 401**。所有路由都要鉴权,包括 `/docs`、`/health`,没有例外。
- **服务端自身没配好 key → 503**(这是部署侧的 fail-closed 设计,不是你的问题,联系管理员)。
- key 走 HTTPS 传输,不要写进代码仓库或前端明文。

### 1.3 错误码一览

| 状态码 | 含义 | 调用方应该 |
|---|---|---|
| 200 | 成功 | — |
| 400 | `/search/spec` 参数不合法;或查询条件太宽(候选超过 30 万条被拒) | 修参数/加约束 |
| 401 | 缺少或错误的 `X-API-Key` | 检查 key |
| 404 | `/vehicle/{id}` 车源不存在或已下架 | 正常业务情况 |
| 422 | 参数校验失败(`q` 为空、`limit` 越界等),响应里有具体字段说明 | 修参数 |
| 503 | 数据库不可用 / 连接池占满(突发并发太高) | 稍后重试(建议指数退避) |
| 502 / 504 | 网关层:后端挂了 / 网关 60 秒读超时 | 可安全重试(查询接口幂等) |

### 1.4 超时与并发建议

- **客户端超时建议 60 秒**:自然语言查询(`/search`、MCP `search_cars`)默认走大模型
  解析,通常几秒返回,极端情况(LLM 慢)会被网关 60s 超时切成 504,重试即可。
  想要稳定低延迟,加参数 `use_llm=false` 走规则解析(毫秒级,语义理解弱一些)。
- **并发建议 ≤ 5**:服务端数据库连接池为 10,突发打满会返回 503。
- **返回条数**:默认 5 条,上限 50。给多了用户也选不动。

### 1.5 数据口径

- 价格一律**港币(HKD)**;`price_text` 是按香港习惯格式化好的("HK$12.9 萬")。
- 默认只搜**私家车**(vehicle_type=1)——库里其他车型是历史遗留,要放开显式传 null。
- 默认剔除疑似问题车(价格低于同款行情 50% 的异常标价,`is_anomaly`)。
- 车源数据每日爬取更新;搜索依赖的行情/特征表由运维重算,行情类字段可能滞后数日。
- 图片是 28car CDN 的直链,**没有热链防盗**(无 Referer 也能访问)。在售车源的图基本
  可用;车下架久了原图可能被 CDN 清理(404),前端/客户端要做好图片挂掉的兜底。

---

## 2. HTTP API

### 2.1 快速开始

```bash
KEY=<你的43位key>

# 自然语言检索:五十万以内的阿尔法,返回 3 条
curl -s -H "X-API-Key: $KEY" \
  "https://searchcar.eazycar.top/search?q=%E4%BA%94%E5%8D%81%E8%90%AC%E4%BB%A5%E5%85%A7%E7%9A%84%E9%98%BF%E5%B0%94%E6%B3%95&limit=3"
```

> URL 里的中文需要 percent-encode(上面就是「五十萬以內的阿尔法」)。
> 用 requests / axios 等库时传 `params` 会自动编码。

### 2.2 `GET /search` — 自然语言检索(主力入口)

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `q` | string | ✓ | 自然语言查询,如「五十萬以內的阿尔法」 |
| `limit` | int 1-50 | | 返回条数,默认 5 |
| `use_llm` | bool | | `false` 强制走规则解析(快、免费、语义弱);留空用服务端配置 |

**响应结构**(字段说明见 §4 术语表):

```json
{
  "summary": "在 2030 台「ALPHARD」里筛出 3 台,按综合分排序。其中报价最低 HK$12.8 萬…",
  "items": [ { "vehicle_id": "s2689574", "car_model": "ALPHARD SA VELLFIRE", … } ],
  "total_matched": 1475,
  "parse_source": "llm",
  "spec": { "base_model": "ALPHARD", "price_max": 500000, … },
  "notes": [],
  "source_query": "五十萬以內的阿尔法"
}
```

- `parse_source`:`llm`(模型解析)/ `fallback`(规则降级,`notes` 里会写原因)。
- `spec`:这次查询实际生效的检索条件,**建议在 UI 上回显它**,让用户知道系统理解成了什么。
- `total_matched`:满足硬过滤的候选总数(不是返回条数)。
- **`image_url`**:每条候选的**首图(封面)**;要全部图片(每车 ≤5 张)用 `/vehicle/{id}` 的 `images`。
- **`contact_display`**:每条候选的**卖家联系方式**(如 `Chan · 98524136`),拿到检索结果即可直接联系车主,无需再查详情。检索只返回**在售且带联系方式**的车源(电话或邮箱);仅留邮箱的卖家联系方式慢,统一排在有电话的车源之后,并打「仅邮箱联系」标签。

**它能听懂什么**(中文/粤语/英文混合):

| 你说 | 系统理解 |
|---|---|
| 阿尔法 / 埃尔法 / ALPHARD / 丰田 埃尔法 | 车系 ALPHARD |
| 平治 / 宝马 / TOYOTA | 按品牌筛 |
| 三十万以下 / 五十萬以內 / 不要超过八十万 | 价格上限(硬过滤) |
| **五十万左右 / 大约三十萬** | **软偏好**:按贴近度排序,不会删掉 39 万/62 万的车 |
| 2015年打後 / 以後 / 以內 / 左右 | 年份下限/上限/软偏好 |
| 七座 | 座位数 |
| 一手车 / 零手 / 3手 | 手数上限 |
| 五万公里以内 | 里程上限 |
| 行货 / 水货 | 进口类型 |
| 中港牌 / 没有中港 | 筛选/排除中港牌车 |
| 3.5 | 排量(只影响排序,不硬筛) |
| 最便宜 / 最平 / 最新 / 最貴 | 排序方式 |
| 捡漏 / 超值 / 笋盘 / 性价比 | 只要比同款行情便宜的(打八折/九折档) |

### 2.3 `POST /search/spec` — 结构化检索(程序调用推荐)

不经过自然语言解析,条件自己拼,行为完全确定。Body 就是检索条件 JSON:

```bash
curl -s -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"base_model":"ALPHARD","price_max":500000,"year_min":2015,"hand_max":1,"sort":"price_asc","limit":10}' \
  https://searchcar.eazycar.top/search/spec
```

**字段全表**(全部可选;`*_near` 是排序偏好,**不参与过滤**):

| 字段 | 类型 | 语义 |
|---|---|---|
| `base_model` / `brand` | string | 车系键 / 品牌键(大写英文,如 `ALPHARD` / `TOYOTA`;不知道就别传,用 `/search`) |
| `model_keyword` | string | 原始关键词,对 car_model 做模糊匹配(归一失败时的兜底) |
| `displacement` | string | 排量偏好如 `"3.5"`,只加分不过滤 |
| `year_min` / `year_max` | int | 年份硬过滤 |
| `price_min` / `price_max` | number | 价格硬过滤(港币) |
| `price_near` / `year_near` | number | 「50万左右」软锚点:参与贴合度排序,**不删候选** |
| `seats` | int | 座位数(库里写法脏,服务端已兼容) |
| `vehicle_type` | int | 1私家车 2客货车 3货车 4电单车 5经典车;**默认 1**,传 `null` 放开 |
| `transmission` / `fuel_type` | string | 如 `自動`/`手動`;`汽油`/`柴油`/`混能`/`電動`(包含匹配) |
| `import_type` | string | `行貨` / `水貨` |
| `hand_max` | int | 手数上限(一手车=1) |
| `mileage_max` | int | 里程上限(公里) |
| `china_plate` | bool | 中港牌(兩地牌):`true` 只要有中港牌的 / `false` 排除 / 不传=不筛(库里约 400 台有) |
| `swap` | bool | 换车帖:`true` 只要换车帖(**收购线索**:卖家想换车=好谈价)/ `false` 排除 / 不传=不筛 |
| `max_price_ratio` | number | 只要更便宜的:0.9 = 比同款行情便宜 10% 以上 |
| `exclude_anomaly` | bool | 剔除疑似问题车,默认 `true` |
| `sort` | string | `score`(默认综合)/ `price_asc` / `price_desc` / `newest` |
| `limit` | int 1-50 | 返回条数,默认 5 |

**容错语义**:传进来的值类型不对(如 `"seats": "七座"`)不会报错,该条件被**静默
收敛为 null(不限)**——宁可查宽,不因脏值 500。区间写反自动交换。

### 2.4 `GET /vehicle/{vehicle_id}` — 单车详情

```bash
curl -s -H "X-API-Key: $KEY" https://searchcar.eazycar.top/vehicle/s2689574
```

返回车辆全量字段(含原始 `description`、`extra_fields`)+ 行情比价数据
(`price_ratio` / `market_p25` / `market_median` / `market_p75` / `market_level` /
`market_ref_n`)+ 格式化好的 `price_text` / `price_verdict`("比同款行情低 12%")
+ **全部图片 `images`**(URL 数组,按原页顺序,每车 ≤5 张)
+ **卖家联系方式**:`contact_name` / `phone_number`(8 位手机号) /
`contact_email`(仅留邮箱的卖家,约 7%) / `contact_info`(原始文本) /
`contact_display`(一行式,如 `陳生 · 62037222` 或 `趙生 · 電郵 xxx@yahoo.com.hk`)。
不存在或已下架 → 404。

### 2.5 `GET /models` — 库内车系榜

```bash
curl -s -H "X-API-Key: $KEY" "https://searchcar.eazycar.top/models?limit=20"
```

`limit` 1-500 默认 50。返回 `{"models": [{"base_model": "ALPHARD", "count": 2030, "median_price": 338000.0}, …]}`
(在售 ≥5 台的车系,按台数降序)。**做车型下拉框/热门入口用这个**,别自己猜车系名。

### 2.6 `GET /health` — 健康检查

```bash
curl -s -H "X-API-Key: $KEY" https://searchcar.eazycar.top/health
```

返回 `ok`、特征表行数、`llm_configured`(模型解析是否可用)、连接池状态。
数据库不可用时仍返回 JSON(`"ok": false`)+ 503,适合监控探活。

---

## 3. MCP 接入(AI 客户端 / Agent)

### 3.1 接入参数

| 字段 | 值 |
|---|---|
| URL | `https://searchcar.eazycar.top/mcp` |
| Transport | `streamable-http` |
| Header | `X-API-Key: <你的43位key>` |

### 3.2 Claude Desktop / Cursor / Cline 配置

在 MCP 配置里加一段(配完**重启客户端**才生效):

```json
{
  "mcpServers": {
    "carinfo": {
      "url": "https://searchcar.eazycar.top/mcp",
      "transport": "streamable-http",
      "headers": {
        "X-API-Key": "<你的43位key>"
      }
    }
  }
}
```

配好后客户端应显示 4 个工具(见 §3.4)。然后直接对话即可,例如:

> 帮我搵台五十万以内嘅阿尔法,最好一手

### 3.3 MCP Inspector / 原生 curl 验证

Inspector 类工具:填 URL + Header,`tools/list` 应出现 4 个工具。

原生 curl 要走完整握手(initialize → 拿 session id → 调用):

```bash
KEY=<你的43位key>
BASE=https://searchcar.eazycar.top/mcp

# 1) 握手,从响应头拿 Mcp-Session-Id
SESSION=$(curl -sS -i -X POST \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}' \
  $BASE | awk -F': ' 'tolower($1)=="mcp-session-id"{gsub(/\r/,"",$2);print $2}')

# 2) 列出工具
curl -sS -X POST -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION" \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' $BASE

# 3) 真实调用
curl -sS -X POST -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION" \
  --data '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_hot_models","arguments":{"limit":5}}}' $BASE
```

协议注意事项(自己写客户端时):

- **`Accept: application/json, text/event-stream` 必须带**,否则握手会被拒。
- 响应是 **SSE 流**(`event: message` + `data: {JSON}`),从 `data:` 行取 JSON。
  解析时按 `\n` 分行并强制 UTF-8,别用宽松的分行函数(多字节中文可能被误切断)。
- 会话 id 从 initialize 的**响应头** `Mcp-Session-Id` 获取,后续每个请求都要回传。
- key 错误时握手返回 401。

### 3.4 四个工具

#### `search_cars(query, limit=5)` — 自然语言检索(主力)

和 HTTP `GET /search` 同一内核,含大模型解析(约数秒)。`query` 支持的写法见 §2.2 的表。
返回摘要 + 候选列表(每条带 `why` 比价解释、`market_basis` 行情依据、`url`、首图 `image_url`、
`contact` 卖家联系方式——拿到结果即可联系车主,无需再查详情)。

#### `get_car_detail(vehicle_id)` — 单车详情

和 HTTP `GET /vehicle/{id}` 同数据(含全部图片 `images`、卖家联系方式
`contact_name` / `phone_number` / `contact_email` / `contact_display`)。
车源不存在时**不抛错**,返回
`{"error": "车源不存在或已下架", "vehicle_id": "..."}`——模型能优雅转述。

#### `list_hot_models(limit=30)` — 库内车系榜(上限 200)

**建议模型先调这个**再 `search_cars`:先知道库里有什么,避免拿不存在的车系瞎搜。

#### `search_by_spec(...)` — 结构化检索

参数即条件:`base_model / brand / price_min / price_max / year_min / year_max /
seats / hand_max / mileage_max / max_price_ratio / china_plate / swap / sort(默认 score) / limit(默认 5)`。
`china_plate`/`swap` 三态:`None` 不筛;`true` 只要(中港牌车 / 换车帖);`false` 排除。
换车帖(`swap=true`)对收购场景是线索:卖家想换车=好谈价。
调用方(或模型)已明确知道条件时用,跳过自然语言解析。

---

## 4. 响应字段术语表(两个入口通用)

| 字段 | 含义 |
|---|---|
| `price_ratio` | 报价 ÷ 同款同年段中位价。0.85 = 比行情便宜 15% |
| `market_level` | 比价基准可靠度:`bucket`(同款同 5 年段,最准)/ `near`(邻近年段)/ `model`(全年代中位,**可信度低,展示时要带上 `market_basis` 的说明**) |
| `market_ref_n` | 行情样本数,太小(个位数)说明比价仅供参考 |
| `labels` | 给 UI 的短标签数组:「划算 18%」「刚挂牌」「一手车」「5.2 萬公里」等 |
| `explain` | 一句话"为什么是它",每个数字都可回溯到字段 |
| `market_basis` | 比价依据的人话版(基准是哪一段、几台样本) |
| `score` | 综合分(0-1),六维加权:车型匹配 / 性价比 / 贴合度 / 挂牌时效 / 车况 / 关注热度 |
| `scores` / `score_breakdown` | 各维得分明细 |
| `is_anomaly` | 疑似问题车/标错价(默认已剔除) |
| `license_until` | 牌費到期(原文片段,如「26年12月」;剩余牌費可退,香港买家高度关心) |
| `china_plate` / `is_swap` | 中港牌 / 换车帖(可作检索条件,见 spec 字段表) |
| `age_days` | 挂牌天数 |
| `image_url` | 搜索候选的**首图(封面)**URL,28car CDN 直链(见 §1.5 的失效说明) |
| `images` | 详情接口返回的该车**全部图片** URL 数组(按原页顺序,≤5 张) |
| `contact_display` / MCP 列表的 `contact` | 卖家联系方式一行式:电话型 `Chan · 98524136`;仅邮箱型 `趙生 · 電郵 xxx@yahoo.com.hk`。检索结果只含在售且带联系方式的车源,仅邮箱的排在有电话的之后 |
| `contact_name` / `phone_number` / `contact_email` / `contact_info` | 详情接口的结构化联系人:姓名 / 8 位电话 / 邮箱(约 7% 卖家只留邮箱) / 原始文本 |

---

## 5. 常见问题

**Q: 401 但我确定 key 没错?**
确认请求头名字是 `X-API-Key`(不是 `Authorization`),值是 43 位完整复制(前后无空格)。

**Q: 查询返回 0 条?**
看响应里的 `spec`——多半是条件太严(如某车系库里只有 1 台还要求一手)。
用 `/models` 确认车系在库里的存量;放宽条件或去掉 `hand_max` 一类覆盖率低的过滤。

**Q: 「五十万左右」为什么返回了 62 万的车?**
这是设计行为:「左右」是**排序偏好不是过滤**——62 万的车不会被藏起来,只是排在更贴近
50 万的车后面。`summary` 里会如实说明。要硬边界请用「五十万以內」。

**Q: LLM 解析和我想的不一样?**
`spec` 字段能看到系统实际理解成的条件;`notes` 里有降级原因。精确场景请用
`/search/spec`(结构化)或 MCP 的 `search_by_spec`。

**Q: 想本地直连不走网关?**
HTTP:`uv run python -m carinfo.search.api --host 127.0.0.1 --port 8088`;
MCP(stdio,本地 Agent 直连,无需 key):`uv run python -m carinfo.search.mcp_server`。
详见 DEPLOY.md。
