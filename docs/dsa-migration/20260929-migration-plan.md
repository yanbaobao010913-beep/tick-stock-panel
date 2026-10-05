# DSA → TSP 迁移总计划与前后端契约（P1-P3 执行层）

> 定稿：2026-09-29。前端：Kimi（本仓 `frontend/src/custom/dsa/`）；后端：GLM（本仓 `backend/app/custom/dsa_*` 扩展模块）。
> **上位文档（权威，先读）**：`daily_stock_analysis/docs/tasks/tsp-migration-plan-20260929.md`（B 方案 8 项补齐清单 + 阶段 0-5 + 止损点）与 `docs/research/tsp-migration-assessment-20260929.md`。本文是其中 #2/#3 项的前后端契约细化，与上位文档冲突时以上位文档为准并回来改本文。
> 仓库已从 `D:\Documents\_scout\tick-stock-panel` 挪到 `D:\Documents\tick-stock-panel`（2026-09-29 用户拍板；backend/.venv 已删，GLM 首次 `uv sync` 重建）。
> **红线：绝不动 `D:\Documents\Zai\tick-stock-panel` 生产副本（:3018 在用），阶段 5 切换前所有工作只在本仓。**
> 与上位 8 项清单的对应：本文 P1 = 清单 #3（LLM 决策报告管线）；P2 = #3 衍生的盯盘闭环 + #2 的规则对账；P3 = #2（告警分级/阶梯/回放审计）；P4+ = #4-#7。清单 #1（免费数据链插件）是后端独立战线，GLM 自行排期，P1 分析流水线依赖数据可用（用户在用实例已有数据源配置，工作副本联调时需同配）。

## 0. 三条已拍板决策

1. **分析走扩展重做 DSA 式报告**：不动 TSP 核心"四维分析不出买卖建议"的设计；DSA 式报告（四点位+操作建议+盯盘条件）全部走 `/api/ext/dsa/*` 扩展 API + `frontend/src/custom/dsa/` 扩展页面。
2. **DSA 历史报告要可看**：`dsa_bridge` 只读 DSA sqlite（`D:\Documents\daily_stock_analysis\data\stock_analysis.db`），前端做只读历史浏览。旧数据不搬迁、不写入。
3. **一切走二开机制，不改核心文件**：后端挂 `backend/app/custom/`（`setup(registrar)` 模式，参照 `dsa_bridge.py`），前端挂 `frontend/src/custom/dsa/extension.tsx`（路由+侧边栏菜单自动发现）。

## 1. 优先级与阶段

用户原话：先保证 **① 分析功能有效 ② 次日盯盘 ③ 报警**，其他慢慢做。

| 阶段 | 目标 | 后端（GLM） | 前端（Kimi） | 验收 |
|---|---|---|---|---|
| **P1 分析** | 批量/定时出 DSA 式报告，可看可归档 | 分析流水线 + 任务 API + 报告存储 + 18:00 定时 | 分析中心页：批量触发+进度、报告库、详情 | 选 3 只自选股批量分析，出含四点位+操作建议+盯盘条件的报告，刷新后可查 |
| **P2 盯盘** | 盘后点位 → 次日盯盘清单 + 自动挂告警线 | 盯盘清单 API + 点位→monitor-rules 对账服务（小时级）+ 盘前自检 | 次日盯盘面板：清单（持仓/空仓分栏）+ 挂线状态对照 | 盘后跑分析，次日清单出现点位，monitor 页能看到同步规则并正确触发 |
| **P3 持仓**（2026-09-29 用户拍板提前，原 P4 之一） | 手工记账 + FIFO 重放 + 浮盈；P2 持仓来源从 DSA 桥切到本账，脱离 DSA | 记账 API + FIFO 重放 + 持仓视图 + 一次性从 DSA 导入 | 持仓页：持仓表（浮盈）+ 流水 + 记一笔 + 导入 | 记两买一卖，数量/成本/浮盈正确；从 DSA 导入后与旧库持仓一致 |
| **P4 报警** | 对齐 DSA 告警体验 | ATR 接近/逼近两档预警、Windows toast 渠道、回放审计 | 触发历史增强、同步规则来源标注、告警→报告跳转 | 盘中价格逼近止损线先出"接近"预警，触线出 critical，toast 弹出 |
| **P5+ 慢慢做** | 决策信号命中率、复盘对齐、ETF 轮动等 | 另立项 | 另立项 | — |

## 2. P1 数据契约（报告）

### 2.1 结构化报告（`ReportDetail`）

```jsonc
{
  "id": "r_20260929_600519_001",       // 字符串主键
  "symbol": "600519",
  "name": "贵州茅台",                    // 可空
  "created_at": "2026-09-29T18:00:12+08:00",
  "mode": "full",                       // full | brief
  "sentiment_score": 65,                // 0-100, 可空
  "operation_advice": "持有",           // 买入/加仓/持有/减仓/清仓/观望 等自由文本, 可空
  "trend_prediction": "震荡偏多",        // 可空
  "analysis_summary": "……",             // 一段摘要, 可空
  "points": {                           // 四点位, 单位元, 均可空(空=该票本次没给)
    "ideal_buy": 1680.0,
    "secondary_buy": 1720.0,
    "stop_loss": 1650.0,
    "take_profit": 1850.0
  },
  "phase_decision": {                   // 盯盘条件, P2 的对账输入; 整体可空
    "action_window": "突破1750放量后",
    "immediate_action": "不追，等回踩",
    "next_check_time": "2026-09-30 10:30",
    "watch_conditions": ["放量站上1750", "板块联动走强"],
    "risk_conditions": [
      { "kind": "reduce", "text": "跌破1700减半仓", "price": 1700.0 }
    ]
  },
  "markdown": "# ……完整 markdown 报告"
}
```

`ReportSummary` = 上表去掉 `markdown` 和 `phase_decision.watch_conditions/risk_conditions`（列表页够用即可，字段裁剪后端自定，但必须含 id/symbol/name/created_at/operation_advice/sentiment_score/points）。

**硬要求**：报告必须含四点位与操作建议（这是与 TSP 核心分析的本质区别）；某票 LLM 没给出点位时字段置空，**不许编**。

### 2.2 分析流水线要求（后端自由实现，约束如下）

- LLM 配置复用 TSP 现有 `ai_provider`（设置页 `/api/settings/ai`），不另搞一套 key。
- 行情/基本面数据用 TSP 自己的数据管道（kline/intraday/financials），不依赖 DSA 库（DSA 库只用于历史浏览）。
- 批量上限 50 只/任务；单票失败不拖垮整任务（item 级 error）。
- 定时任务：A 股交易日 18:00 自动对全量自选股跑分析（可开关，偏好存 settings preferences，key 建议 `dsa-analysis-schedule`）。
- 报告存储：后端自定（建议 `data/user_data/dsa_reports/` 或 duckdb/sqlite），但读写只能经本节 API。

## 3. P1 API 契约（全部挂在 `/api/ext/dsa` 前缀下）

已有：`GET /api/ext/dsa/health`（dsa_bridge）。

### 3.1 分析任务

```
POST /api/ext/dsa/analysis/tasks
  body:  { "symbols": ["600519","000001"], "mode": "full" }   // symbols 1-50, mode 可省默认 full
  resp:  { "task_id": "t_20260929_180012" }

GET  /api/ext/dsa/analysis/tasks/{task_id}
  resp:  {
    "task_id": "…", "status": "running|done|failed",
    "total": 3, "done": 1,
    "items": [
      { "symbol": "600519", "status": "done", "report_id": "r_…" },
      { "symbol": "000001", "status": "running" },
      { "symbol": "300750", "status": "failed", "error": "LLM 超时" }
    ]
  }
```

前端轮询此接口（2s 间隔），不要求 SSE；后端若想直接给 SSE 也可以，但轮询契约必须先有。

**符号规范化注记（2026-09-29 补充，P1 bug 修复）**：仓库 parquet 与 instruments 的 symbol 均为 `代码.交易所` 后缀式（如 `600519.SH`），而本节示例与前端手动输入框是裸 6 位代码，裸代码直传会匹配不到日K而误报"暂无日K数据"。后端各入口（创建任务 / 报告列表 `symbol` 过滤 / 单票分析）统一做符号规范化：接受裸 6 位代码、交易所前缀式（`SZ000636`）与点分式（`600519.SH`，大小写不敏感），统一转为后缀点分式后存储与返回——任务 items 与报告的 `symbol` 字段因此是后缀式。裸代码优先经维表反查交易所，未命中按代码段规则兜底（6/9 开头→SH、0/2/3 开头→SZ、4/8 开头与 92 段→BJ），仍无法判定的原样透传并自然报"暂无日K"。

### 3.2 报告库

```
GET    /api/ext/dsa/analysis/reports?symbol=600519&limit=20&offset=0
       → { "total": 35, "items": [ReportSummary] }   // symbol 可省=全部, 按 created_at 倒序
       // date=YYYY-MM-DD 精确当日; before=YYYY-MM-DD 严格早于该日的往期 (与 symbol 可组合)
GET    /api/ext/dsa/analysis/reports/{report_id}     → ReportDetail
DELETE /api/ext/dsa/analysis/reports/{report_id}     → { "ok": true }
```

### 3.3 DSA 历史（只读桥接）

```
GET /api/ext/dsa/legacy/reports?symbol=&limit=20&offset=0
    → { "total": N, "items": [LegacyReportSummary] }
    // LegacyReportSummary: { id, symbol, created_at, operation_advice, sentiment_score,
    //                        ideal_buy, secondary_buy, stop_loss, take_profit, analysis_summary }
GET /api/ext/dsa/legacy/reports/{id} → LegacyReportSummary + { "markdown": "…" }
    // markdown 由 raw_result/report 字段在后端拼成可读 markdown 返回
```

DSA 库不可用（文件缺失/锁定）时返回 `{ "total": 0, "items": [] }` 而不是 5xx，前端据此显示"旧库不可用"。

### 3.4 通用错误格式

走 TSP 现有 `ApiError` 约定：非 2xx 时 body `{"detail": "人类可读原因"}`。前端统一 toast。

### 3.5 分析调度偏好(2026-09-29 补充)

P1 落地补充（详见 `20260929-p1-backend-impl.md`）：§2.2 的定时开关落地为独立偏好端点，偏好存 settings preferences，键 `dsa-analysis-schedule`。

```
GET /api/ext/dsa/analysis/schedule → {"enabled": bool, "hour": 0-23, "minute": 0-59}   // 无配置时返回默认 {"enabled": true, "hour": 18, "minute": 0}
PUT /api/ext/dsa/analysis/schedule body 同上 → 200 回显写入值; enabled/hour/minute 类型与范围严格校验, 非法一律 400 ({"detail": ...})
```

调度线程消费同一键：`enabled=false` 时不触发；到点但无 AI Key / 自选为空时跳过不建任务。前端设置项直接用这对端点。

## 4. P2 契约：次日盯盘（2026-09-29 定稿，用户"进p2"）

**持仓来源（过渡期拍板）**：`dsa_bridge` 只读 DSA 库 `portfolio_positions`（`quantity > 0`；多账户同票合并：quantity 求和、avg_cost 按数量加权）。TSP 内不建持仓账（P4 再议）；DSA 桥不可用时 `holding=false` 全量按空仓处理，不许 5xx。

### 4.1 API

```
GET /api/ext/dsa/watch-plan
→ {
  "generated_at": "2026-09-29T21:30:00+08:00",
  "positions_source": "dsa_bridge",
  "items": [{
    "symbol": "600519", "name": "贵州茅台"|null,
    "holding": true,
    "quantity": 100.0, "avg_cost": 1680.0,        // 非持仓为 null
    "report": { "id", "created_at", "operation_advice", "sentiment_score",
                "points": {...}, "phase_decision": {...} } | null,   // 该票最新 P1 报告
    "rules": [{ "rule_id": "…", "kind": "stop_loss|take_profit|add|reduce|entry",
                "price": 1650.0, "severity": "critical", "enabled": true }],
    "sync_state": "synced|stale|no_points|no_report"
    // synced=规则与最新报告点位一致; stale=报告已更新规则未对账; no_points=报告无点位; no_report=无报告
  }]   // 持仓股在前，其余按自选顺序
}

POST /api/ext/dsa/watch-sync/run
→ { "created": 2, "updated": 1, "removed": 0, "skipped": 3 }

GET /api/ext/dsa/premarket/status
→ { "date": "2026-09-30", "ran_at": iso|null,
    "checks": [{ "name": "数据源", "status": "ok|warn|fail", "detail": "…" }] }
    // 未跑过: ran_at=null, checks=[]。检查项后端自定, 建议: 数据源可用 / 自选K线覆盖 / 同步规则启用状态 / DSA桥可用
```

### 4.2 对账语义（后端硬要求）

- 同步规则 = TSP monitor-rule：`type="price"`、`scope="symbols"`、`symbols=[code]`、`cooldown_seconds=86400`、`name` 前缀 **`DSA·`**（命名 `DSA·{symbol}·{kind}`）。**只增删改 `DSA·` 前缀的规则，人工规则一律不碰**（TSP 规则无 source 字段，以前缀为标记）。
- 条件用现价字段（field 名以 `GET /api/monitor-rules/options` 返回为准）。
- 持仓股：`stop_loss` → `price <= X` critical；`take_profit` → `price >= X` critical；`secondary_buy` → `price <= X` warn（kind=add）；`risk_conditions` 中 `kind=reduce` 且带 `price` → `price <= X` warn（kind=reduce）。
- 空仓：`ideal_buy` → `price <= X` info（kind=entry）。
- 点位为空的项不建规则；票移出自选或清仓 → 删其全部同步规则；报告更新 → 对账改价。
- 触发时机：每次分析任务完成后自动对账一次（挂在 dsa_analysis 任务收尾）+ 调度线程周期兜底。

### 4.3 P2 实施拍板（2026-09-30 补充）

P2 后端落地（详见 `20260929-p2-backend-impl.md`）对 §4 未明说处的拍板记录，与正文冲突处以本节为准：

1. **删除触发器收敛（2026-09-30 修订）**：删规则的两个票级事件按盯盘集合语义落位——**未持仓票移出自选** → 删其全部 DSA· 规则；**清仓**（在集合内、不在持仓）→ 删其全部持仓规则并按空仓语义重建 entry（"清仓→删全部"与"空仓→ideal_buy 建 entry"的字面张力取 diff 自洽解）；**持仓票移出自选但未清仓** → 规则保留（票仍在盯盘集合，见第 6 条，§4.2"移出自选→删全部"据此收敛为"未持仓票移出自选"）。持仓票无报告或报告点位全空（含 LLM 围栏解析失败→points 全 None）时跳过该票点位派生 diff、**票级保留既有规则**（sync_state 如实报 no_points/no_report），防 LLM 闪失当晚清掉用户止损线。**部分缺点位 → 槽位级保留（2026-09-30 再修订）**：报告缺哪个点位列（持仓侧 stop_loss/take_profit/secondary_buy、空仓侧 ideal_buy/entry），该槽位的既有规则保留不删；报告给出的点位照常对账改价；reduce 属列表语义（报告更新即整列替换）不在保留范围；sync_state 对保留槽位不参与 synced 比较（否则部分缺点位票永远显示待对账）。主会话按推荐默认落地（票级全保为一行翻转的备选），固化测试 5 例（`test_reconcile_partial_points_preserves_missing_slot_rules` / `test_reconcile_flat_missing_ideal_buy_keeps_entry_rule` / `test_diff_rules_partial_points_preserves_missing_slot_rules` / `test_judge_sync_state_ignores_preserved_kinds` / `test_empty_point_kinds_slots`）。
2. **watch-sync 错误语义**：桥不可用/自选读失败/规则存储读失败 → 零写入中止 + 400 `{"detail"}`（§4 头降级条款只约束 watch-plan 的 200 全空仓；同步静默回 0 计数比报错更误导）。
3. **skipped 语义**：因点位为空未建的期望槽位数（与 created/updated/removed 同单位=规则条数），另含 id 占用跳过数。
4. **多条 reduce**：name 加序号 `DSA·{symbol}·reduce·{n}`（契约命名模板的最小扩展），按 price 升序定 rank 并同价去重。
5. **持仓合并（2026-09-30 修订）**：旧库 UNIQUE(account_id, symbol, market, currency, cost_method) 使同持仓 avg/fifo 各一行，混读会重复计数——实现取**成本方法整库二选一，绝不混读**：任一行带 fifo 则整库只取 fifo 行（实测真实旧库 fifo 11 行是 avg 的超集、账户 3/4 仅 fifo 行，只取 avg 会丢持仓），全库无 fifo 行才取 avg 行；再按 (票,账户) 去重 → 跨账户 quantity 求和、avg_cost 按数量加权。**不按 portfolio_accounts.is_active 过滤**（实测 12/17 行挂非活跃账户）；行级脏数据（TEXT quantity/NULL 成本）跳过并 warning，绝不让 watch-plan 变 5xx。
6. **items 域 = DSA 持仓 + TSP 自选（2026-09-30 修订）**：盯盘集合为两者并集——持仓在前（sym6 字典序，输出确定性）、其余按自选顺序、按 sym6 去重；各票 symbol 经 `dsa_analysis._canonical_symbol` 统一输出**后缀点分式**（`代码.交易所`，§3.1 符号规范化注记同款，DSA 库内裸 6 位/SZ 前缀式一并归一，无法判定交易所的原样透传）。自选外的持仓股照样有条目并参与建规则（§4.1"持仓股在前"的落实）；`resolve_asset_type=index` 的集合项跳过建规则（000001.SH/SZ 撞码防护）。前端仅按 symbol 展示渲染，类型兼容（WatchPlanPage 只消费不解析）。
7. **premarket**：每交易日 09:10-11:30 窗口内由调度线程跑一次（非交易日不跑），结果原子落盘 `data/user_data/dsa_watch/premarket.json`；`GET /premarket/status` 纯读，未跑/非当日/损坏 → `{date:今天, ran_at:null, checks:[]}`，永不 5xx、永不内联触发。检查项四项（数据源/自选K线覆盖/同步规则启用/DSA桥可用），各自独立容错。
8. **sync_state**：纯集合比较（点位集合相等即 synced），不建 last_synced 状态文件；enabled 不参与比较（手动停用仍 synced）；规则读失败降级为 stale（有点位时）。
9. **触发链（2026-09-30 补 pending flush）**：任务收尾钩子仅终态 done 任务触发（流水线崩溃的 failed 任务不触发，30min 兜底覆盖）；兜底对账复用 dsa_analysis 调度循环 30min 门控；对账三入口经模块锁串行；落盘后镜像 API 层 `_sync_engine` 重载引擎，engine 引用在请求 handler 捕获。**engine 未捕获时的补生效**：本轮确有规则变更则置 `_ENGINE_FLUSH_PENDING`，任一 dsa_watch/分析路由随后捕获到 engine 即刻补 flush（成功清位、失败保留重试）；无变更时仅落盘记 warning。
10. **P1 §6.2 作废**：对账规则打 `source=` 标的建议作废——TSP 规则无 source 字段，name 前缀 `DSA·` 是唯一托管标记；外来 DSA· 命名（解析不出 sym6+五 kind）不托管不比对不删。
11. **持仓来源软切换（2026-09-30 补，P3 提前落地）**：watch-plan/对账/盘前自检的持仓统一经 `dsa_watch.load_positions()`——本账（dsa_portfolio 流水）非空即整账 FIFO 重放（`positions_source="dsa_portfolio"`），为空回落 DSA 桥（`"dsa_bridge"`）；本账读取/重放故障不回落桥（导入后桥是过时旧账），按桥不可用走既有降级。**触发条件是"记第一笔账"，早于 §4.4.3 原"P3 验收"时点（该节已加修订注）**；切换后桥内其余持仓立即失持，下次对账按清仓语义删其 DSA· 规则——开始手工记账前必须先跑 `POST /portfolio/import-from-dsa` 全量导入（详风险链见 §4.4.3 修订注与 impl report §4.8/§6）。

## 4.4 P3 契约：持仓账（2026-09-29 定稿，用户拍板提前到报警之前）

**定位**：TSP 自有持仓账（手工记流水，FIFO 重放），替代 DSA 桥成为 P2 watch-plan 的持仓来源。Lots 页（手动批次提醒）不动但菜单已隐藏（用户拍板，`Layout.tsx` 注释留痕）。

### 4.4.1 数据模型

- 流水 `Trade`：`{ id, symbol, side: "buy"|"sell", quantity, price, fee?, traded_at (ISO), note? }`。存储后端自定（建议 `data/user_data/dsa_portfolio/trades.jsonl` 追加写 + 原子重写）。
- 持仓 = 流水 FIFO 重放（读时重放，不存快照）：买入入队，卖出消费最老批次；**卖出超过持仓 → 422 拒绝**；quantity>0 即视为持仓。
- `avg_cost` = 剩余批次加权成本；`last_price` 取 repo 最新收盘价（无数据返回 null，前端显示 —，不许编）。

### 4.4.2 API

```
GET    /api/ext/dsa/portfolio/positions
       → { "items": [{ "symbol", "name"|null, "quantity", "avg_cost", "total_cost",
                        "last_price"|null, "market_value"|null,
                        "unrealized_pnl"|null, "unrealized_pnl_pct"|null }],
           "totals": { "total_cost", "market_value"|null, "unrealized_pnl"|null } }
GET    /api/ext/dsa/portfolio/trades?symbol=&limit=100
       → { "items": [Trade] }   // traded_at 倒序
POST   /api/ext/dsa/portfolio/trades
       body: { "symbol", "side", "quantity", "price", "fee"?, "traded_at"?, "note"? }
       → Trade   // traded_at 省略=现在; 卖超 → 422
DELETE /api/ext/dsa/portfolio/trades/{id} → { "ok": true }
POST   /api/ext/dsa/portfolio/import-from-dsa
       → { "imported": n, "skipped": n }
       // 一次性从 dsa_bridge 只读 DSA portfolio_trades 全量灌入; 幂等: 已有流水时拒绝(409)
```

### 4.4.3 与 P2 的切换点

- P3 验收前：watch-plan 持仓来源保持 `dsa_bridge`（现状）。
- P3 验收后：watch-plan 的 holding/quantity/avg_cost 改读**本扩展** positions；DSA 桥只留历史报告浏览。切换由 GLM 在后端完成，前端 watch-plan 契约不变（`positions_source` 字段值变为 `dsa_portfolio`，前端仅展示该值）。

### 4.4.4 修正（2026-09-30 实盘污染事件）

`import-from-dsa` **必须只导入 DSA `portfolio_accounts.is_active=1` 的账户流水**（DSA 库里有 Demo 测试账户，全量导入会把测试流水灌成假持仓——已实际翻车一次，Kimi 手工清洗：删 46 条→只灌实盘 39 条）；同时 symbol 需归一化（DSA 存在 `SZ000636` 前缀格式）。GLM 修 import 实现时一并补这两条 + 对应测试。

> **2026-09-30 修订（软切换提前生效，取代上面两行的时点描述）**：实现未按"P3 验收"分界硬切换，而是**按本账流水是否非空软切换**（`dsa_watch.load_positions`，dsa_watch.py:227-252）——本账（dsa_portfolio 流水账）**非空即整账切换**：holding/quantity/avg_cost 完全以本账 FIFO 重放为准，`positions_source` 如实上报 `"dsa_portfolio"`；本账为空 → 回落 dsa_bridge 只读旧库，`positions_source="dsa_bridge"`。**用户记第一笔账即触发切换**，无需等 P3 验收、也无需先跑 import-from-dsa。风险与正确路径：本账非空后，桥内其余真实持仓立即失持 → 下次对账（任务收尾/30min 兜底/手动）按 §4.3-1"清仓"语义删其全部 DSA· 止损/止盈/加减仓规则（报告含 ideal_buy 还会建 entry）——**半截记账/漏导入即触发**。因此开始手工记账前应先 `POST /portfolio/import-from-dsa`（§4.4.2，幂等：已有流水时 409 拒绝，故导入必须是第一笔动作）把桥内持仓全量灌入本账，切换才是无损的；该风险已在 impl report §6 遗留风险固化。`positions_source` 字段自本修订起为动态值（`"dsa_bridge" | "dsa_portfolio"`），前端仅展示该值，类型不受影响。

## 4.6 P5 契约：点位模拟 + 报告回测（2026-09-30 定稿，用户拍板方案 A：扩展内自建，不动 TSP 核心撮合/引擎）

> 语义权威 = DSA 实现（`daily_stock_analysis/src/core/trailing_stop.py`、`src/services/paper_trading_service.py`、`src/services/decision_signal_outcome_service.py`）。本节是移植契约，数字口径与 DSA 逐项对齐，保证与 DSA 历史可比。

### 4.6-1 信号源与入场

- 信号源 = 本扩展 P1 报告库（`dsa_analysis` 的每报告一文件存储），每份报告含 `points`（ideal_buy/secondary_buy/stop_loss/take_profit）+ `operation_advice` + `created_at`
- **入场只用 `ideal_buy`**（`secondary_buy` 永不成交，仅展示——DSA 口径）：报告日**次日**限价挂 `ideal_buy`——开盘价更低按开盘价成交（更优），盘中最低价触及按 `ideal_buy` 成交，全天未触及不建仓（当根作废不顺延挂单）；开盘即破止损价放弃建仓（防假盈利）；一字板（low==high）不成交
- action 映射：`operation_advice` 文本含 买入/加仓 → 入场候选；含 卖出/减仓/清仓 → 次日开盘平仓（exit_reason=`sell_signal`）；含 观望/持有 → 不动。同一票同时只允许一个敞口 episode；持仓期间的新买入信号忽略，卖出信号平仓
- 交易日判定走 `trading_day.is_trading_day`；下一交易日 = repo 日K序列里报告日之后的下一根

### 4.6-2 出场状态机（逐 bar，T+1：建仓当根不检查）

按序判定（每根 K 线，从建仓根的**下一根**起）：
1. **未启动**且最低价 ≤ `stop_loss` → `stop_loss` 出场；跳空低开破线按 `min(开盘价, stop_loss)`…实为 `开盘价`（开盘已破按开盘价，DSA 口径 `min(止损价, 开盘价)`）
2. 最高价 ≥ `take_profit` → **启动移动止盈**（take_profit 是启动价不是卖出价），`peak = max(启动价, 当日最高)`，本根不出场
3. **已启动**：出场线 `line = max(启动价, peak × (1 − trail%))`，`trail% = clamp(1.0 × ATR20%, 2%, 8%)`（ATR 取建仓锚点前 21 根现算，读不到回落 5%）； peak 每根刷新（下一根生效，无未来函数）；最低价 ≤ line → `trail_hit`（跳空按开盘价）；**启动后止损失效**
4. 卖出类信号 → 次日开盘 `sell_signal`
5. 持有满 10 根日K → 当日收盘 `window_expired`
6. 一字板当天触发卖不出 → 状态原样顺延下一根
7. 数据末尾仍持仓 → `open`（浮动按最新收盘 MTM）

### 4.6-3 模拟盘口径（前向账户）

- 初始资金 10 万、10 槽位、每槽等额；**份额允许零碎股**（slot_capital/entry_price，不整手取整——与 DSA 一致，保证可比）；**零费用零滑点**（DSA 口径，跟 TSP 核心 paper 的精细费用刻意不同，为的是和 DSA 历史对照）
- 槽位按入场日先到先得，满槽跳过（`skipped/no_slot`）；行情缺失 `skipped/no_bars`（不伪造成交）
- 净值：每日 cash + 持仓按当日收盘 MTM；基准 = 沪深300 归一到初始资金
- 重放语义：每次运行从 inception 整段重放、结果整表覆写（无增量状态，改口径自动纠正全历史）；交易日 15:05 后自动跑 + 手动触发
- 存储：`data/user_data/dsa_paper/`（state.json + trades.json + equity.json，原子写）

### 4.6-4 报告回测 / 命中率（ outcomes ）

- 每份报告 × horizon（1/3/5/10 根日K）一条 outcome：锚定 = 报告日收盘，forward bars 严格晚于报告日
- 方向映射：买入/加仓类 → up（走出场状态机判定收益）；持有 → not_down；卖出/减仓/规避 → not_up（纯窗口收益）；观望/无点位 → `unable`（**不进命中率分母**）
- 判定：±2% neutral 带 → hit/miss/neutral；exit_reason ∈ stop_loss/trail_hit/window_end
- 聚合口径：命中率分母 = hit+miss（neutral/unable/无方向全部排除——硬规则）；样本 <30 的维度不下结论（前端灰显）

### 4.6-5 API（全挂 `/api/ext/dsa`）

```
POST /paper/run            → { "ok": true, "episodes": n, "ran_at": iso }   # 手动整段重放
GET  /paper/overview       → { "state": { "inception_date", "initial_capital", "max_slots" },
                               "summary": { "total_return_pct", "max_drawdown_pct", "win_rate",
                                            "closed", "open", "skipped" },
                               "equity": [{ "date", "equity", "benchmark" }] }
GET  /paper/trades         → { "items": [{ "symbol", "name", "entry_date", "entry_price", "shares",
                                            "exit_date"|null, "exit_price"|null,
                                            "exit_reason"|null, "return_pct"|null, "status" }] }
GET  /paper/outcomes       → { "items": [{ "report_id", "symbol", "created_at", "direction",
                                            "horizon", "outcome": "hit|miss|neutral|unable",
                                            "exit_reason"|null, "return_pct"|null }],
                               "stats": { "hit_rate", "n", "by_horizon": { "1": {...}, ... } } }
```

### 4.6-6 前端（Kimi）

- 新扩展页"点位模拟" `/dsa/paper`：KPI 磁贴（总收益/最大回撤/胜率/持仓中）、净值曲线 vs 沪深300（echarts）、持仓中/已平仓交易表（exit_reason 中文徽章）、命中率区（总体 + 分 horizon，n<30 灰显标注）
- 侧边栏菜单加一项；TSP 核心 `/paper` 页不动（两套并存，用户不用那边）

## 4.5 P4 契约：报警（2026-09-30 定稿，Kimi 实施）

### 4.5-1 接近/逼近阶梯（ATR 自适应）

- 每条主规则（`stop_loss`/`take_profit`/`add`/`reduce`；**entry 不配阶梯**——介入线预警噪音大于价值）配两条同向预警：
  - **接近（near）**：带宽 `b = clamp(0.5 × ATR20%, 0.3%, 3%)`，ATR 读不到回落 1.0%；`op<=` → 价 = `X×(1+b)`，`op>=` → `X×(1-b)`；severity=**info**
  - **逼近（mid）**：带宽 `max(b/2, 0.15%)`，同向同理；severity=**warn**
  - 价格四舍五入到分；cooldown 与主规则同 86400——接近/逼近/到价每日各响一次（= DSA 三段阶梯语义，治"接近一次就哑"）
- ATR20% 在对账时从 repo 日K现算（`mean(TR[-20:]) / 最新收盘`），同一 `_build_specs` 路径供对账与 sync_state 判定，口径永远一致
- 命名：规则名 `DSA·{sym6}·near_{kind}[·rank]` / `mid_{kind}`（kind 用英文原词）；id `dsa_{sym}_near_{kind}[_{rank}]`；与主规则同生同灭同对账
- 槽位级保留按 base kind 生效：报告缺 `stop_loss` 时其 near/mid 阶梯一并保留
- 消息文案：`DSA 接近止损提醒: 收盘价 ≤ 1.55`（kind label 前缀 接近/逼近）

### 4.5-2 ~~critical 开声~~（2026-09-30 用户否决，不做）

### 4.5-3 回放审计

- 交易日 15:30（收盘后）跑：当天全部 `DSA·` 触发记录逐条对当日收盘（触发价 vs 收盘价，方向按 kind 判定"触线后是否继续走弱/走强"），产出 markdown 落 `data/user_data/dsa_alert_audit/YYYY-MM-DD.md`
- `GET /api/ext/dsa/alert-audit` → `{ "items": [{ "date", "total", "path" }] }`（倒序）；`GET /api/ext/dsa/alert-audit/{date}` → `{ "date", "markdown" }`
- 前端：次日盯盘页加"告警复盘"区块读此接口（核心监控中心页不动）

## 4.7 P5' 契约：点位进 TSP 核心模拟盘/回测（2026-09-30 中午用户改拍方案 B，取代 §4.6 的"扩展内自建"为扩展并存但非主线）

> 用户拍板：模拟盘/回测在 TSP 基础上改（方案 B），接受核心文件改动与上游冲突面。§4.6 的扩展版点位模拟（dsa_paper）已建成可用，保留作 DSA 口径对照，若嫌冗余可删。
> 核心改动必须带测试 + 遵循 TSP CONTRIBUTING（ruff/pytest 基线不破）。

### 4.7-1 B1 模拟盘点价位单（先做这个）

- **核心 `app/strategy/paper.py`**：订单加条件触发能力——`order_type` 新增 `"conditional"`，字段 `trigger_price: float`、`trigger_op: "<="|">="`、`expire: "day"|"gtc"`。
  - 盘中 `evaluate_intraday`（paper.py:774 附近）：快照价满足触发条件才 `_fill_order`，成交价 = 现价
  - 盘后 `settle_day`（paper.py:909 附近）：用当日 low/high 判定穿越；成交价为 `min(open, trigger)`（`<=` 向）/ `max(open, trigger)`（`>=` 向）——跳空按开盘价，DSA 口径
  - `expire=day` 当日未触发作废（入场单语义）；`expire=gtc` 挂单直到成交/撤单（保护单语义）
  - 复用 `_fill_order` 全部校验（费用/滑点/T+1/涨跌停/资金）不动
- **核心 `app/api/paper.py`**：下单模型放行新字段。
- **桥（扩展侧，新模块或并入 dsa_watch）**：分析任务完成后——空仓票最新报告有 `ideal_buy` → 建次日 `conditional` 买单（`trigger_op="<=", expire="day"`，金额 = 权益/槽位数）；模拟盘持仓票最新报告 `stop_loss` 变化 → 重建 GTC 止损卖单（先撤旧的）。**take_profit 是移动止盈启动价，B1 不出单**（TSP 模拟盘无回撤跟踪引擎，如实标注）；卖出类建议 → `next_open` 卖单。
- **2026-10-02 补注（GLM 实现已定）**：桥为 `dsa_paper_bridge.py` 纯后台 60s 轮询对账（无手动端点，替代契约原写的 POST /paper-bridge/run，已接受）；订单 source 标 `dsa:{report_id}[:stop|:exit]` 幂等去重；报告缺 stop_loss 时既有止损单保留（对齐 §4.3-1 槽位级保留）。
- 前端：Paper 页订单/成交列表能认出 conditional 单（核心 Paper.tsx 小改：类型徽章+触发价列）；其余不动。

### 4.7-2 B2 回测绝对点位（2026-10-02 细化定稿，GLM 后端 / Kimi 前端）

- **核心 `app/backtest/engine.py`**：
  - `MatcherConfig` 加三个绝对价字段：`entry_price: float | None`、`stop_price: float | None`、`take_profit_price: float | None`（默认 None = 行为不变，存量测试不破）。
  - **先把三处同构 `_risk_exit`（portfolio :961 / legacy :1379 / 独立候选 :2017 附近）抽成一个共享函数再改**（DSA 教训：修共享函数一次，不逐 caller 补丁）：绝对 `stop_price` 存在时止损线直接用它（取代 `entry_price×(1−pct)`），`take_profit_price` 同理；穿越判定与成交价口径（开盘已破按 open、否则按线价）原样复用。
  - 入场：`entry_price` 存在时，入场信号日成交需满足当日 `low ≤ entry_price`（触及判定），成交价 `min(open, entry_price)`（开盘更低按开盘，与 DSA 一致）；未触及 → 该信号不建仓（不留挂单）。矩阵路径用现成 `entry_price_override` 钩子实现，`_can_buy` 加触及检查。
- **核心 `app/api/backtest.py` / 策略 overrides 链路**：放行这三个键到 MatcherConfig。
- **扩展侧端点**（挂在 `/api/ext/dsa`）：`POST /backtest-report { "report_id": "…" }` ——取该报告的 symbol/点位/日期，构造报告日的入场信号，调核心引擎（绝对点位 + 费用滑点按默认）跑 episode，返回 `{ "symbol", "entry": { "date", "price" }|null, "exit": { "date", "price", "reason" }|null, "return_pct"|null, "holding_bars", "note" }`；未触及建仓/数据不足走 `note` 说明不报错。
- **前端（Kimi）**：报告详情弹窗加"点位回测"按钮 → 调该端点 → 结果卡（建仓/出场/收益/原因/持有根数）。回测页本身不动。

### 4.7-2b 回测页"DSA 点位"策略（2026-10-02 晚用户拍板："利用回测的功能实现 DSA 的效果"——否掉独立 tab 方案）

- **形态**：后端把"DSA 点位"注册成一个策略定义，自动出现在回测页策略列表（`/api/strategies`），用户选中 → 仓位模拟/全量模拟 → **结果页零改动**。前端零改动（列表自动带出）。
- **后端（GLM）**：
  1. **报告物化成信号**：回测运行时把区间内扩展报告转为引擎信号行——`operation_advice` 文本映射方向（DSA §4.6-1 口径：买入/加仓→入场候选；卖出/减仓/清仓→次日开盘离场信号；观望/持有→不动）；入场信号日=报告日次日，`entry_price_override` 矩阵注入该报告 `ideal_buy`（触及判定 `low ≤ ideal_buy`，成交 `min(open, ideal_buy)`，未触及不建仓）。
  2. **逐仓位绝对风控线**：B2 的 `stop_price`/`take_profit_price` 从 MatcherConfig 配置级扩为**逐仓位级**（pos 级字段，入场时从该信号行的报告点位注入）——同一策略下各票的线各自不同，这才是 DSA 的效果。配置级字段保留（单票/报告回测继续用）。
  3. **策略定义**：id 建议 `dsa_points`，名称"DSA 点位"，出现在"全部"分组；参数面板可空（用报告点位）或暴露 trail/费用覆盖（可选，v1 不做）。出场语义：绝对止损线 + 绝对止盈线 + 卖出类建议次日开盘（TSP 口径：固定线成交，非移动止盈——与 DSA 的 trailing 差异在 docstring/报告里如实标注，用户要看 trailing 效果用扩展版 dsa_paper）。
  4. 实现位置：优先走扩展/自定义策略机制；若策略系统不支持注入信号矩阵，最小核心改动点在 `backtest/strategy.py` 的矩阵构建处特判 `dsa_points`。
- **验收**：选"DSA 点位"跑近 3 个月仓位模拟 → 交易明细里的入场价=报告 ideal_buy（或更优开盘）、出场价/原因对得上报告止损止盈线；与扩展版 dsa_paper 同区间结果方向一致（口径差异=费用滑点+固定止盈线，已声明）。

### 4.7-2b 补：实施注记（2026-10-03 GLM 落地记录）

- **实现组装**（契约第 4 条"优先扩展机制，最小特判"的落位）：
  - **内建策略文件** `app/strategy/builtin/dsa_points.py`（id `dsa_points`，名称"DSA 点位"，`asset_types=["stock","etf"]`，`params=[]`，`MAX_HOLD_DAYS=10`）——出现在 `/api/strategies`（source=builtin，前端列表自动带出零改动）。META 打 `dsa_signal_rows: True` 标记；`compute_signals` 返回合法空信号（选股器/实时路径安全空跑，回测被特判接管）。
  - **扩展物化器** `app/custom/dsa_points.py`（新模块，EXTENSION_ID `dsa.points`，未动其他 dsa_* 模块）：`materialize_dsa_signal_rows(market, start, end)` 把区间内报告物化成 SignalMatrix。卖出类关键词复用 `dsa_paper_bridge._is_sell_advice`（同文矛盾取保守侧）；入场行放报告日次一交易日（严格大于报告日的第一根K），逐格注入 ideal_buy/stop_loss/take_profit（float32，NaN=无）；缺 ideal_buy 的买入建议不建仓；同（票，入场日）多报告取更晚创建；窗口外/市场外标的跳过；单报告脏数据跳过不拖垮整体。
  - **核心特判**（`backtest/strategy.py` matrix_native 分支，契约许可的落点）：① matcher_config 对 dsa_signal_rows 策略钉死口径——`entry_fill="close_t"`（信号行已预放在成交日，entry_delay=0）、`exit_fill="open_t+1"`（离场行在报告日，delay 次日开盘）、`entry_touch=True`、配置级 `entry_price` 关闭（避免全矩阵 min(open, 配置价) 覆盖逐格 ideal_buy；stop_price/take_profit_price 配置级保留作报告缺点位时的回退线）；② 信号计算换成物化器（不走过滤/评分管线）；③ 入场时间掩码豁免（报告日期界已在物化器内完成，入场格允许落在区间末根后一根；离场行照常受掩码）。请求 matching 不影响口径。
- **引擎/matrix 协议扩展**（为逐仓位线与逐格触及做的通用能力，分钟路径不受影响）：
  - `SignalMatrix`/`MarketMatrix` 新增可选逐格数组 `entry_price`（SignalMatrix 侧新增；MarketMatrix 侧已有）/`stop_price`/`take_profit_price`，贯穿 `_finalize_signal_matrix`/`make_signal_matrix`/`slice_signal_matrix`/`apply_time_masks`/`build_market_matrix_from_signals`（显式 `entry_price_override` kwarg 仍优先——分钟路径不变）；validate 对价位数组要求 float32 只读、NaN=无、有限值须为正。**fail-closed 守卫**：逐格介入价 + `entry_delay_bars=1` 会错位，直接拒绝。
  - `MatcherConfig.entry_touch: bool`：开启后逐格介入价带触及语义（`_can_buy` 按格内介入价判 `low <= 价`，`_resolve_entry_prices` 成交 `min(当日 open, 格内价)`）；关闭时逐格价 = 精确成交价（分钟口径原样）。
  - **逐仓位绝对风控线**：两个 matrix 撮合器建仓时把格内 stop/take 注入 `pos["stop_price"]/["take_profit_price"]`，`_risk_exit_decision` 新增 `pos_stop_price/pos_take_profit_price` 参数，优先级 逐仓位 > 配置级绝对 > 百分比。panel legacy 撮合器无逐格机制，不接入（dsa_points 只走 matrix 路径）。
- **环境过滤（regime_filter）不作用于 DSA 信号行**（入场掩码豁免的副作用，v1 如实声明）。
- **测试**：`tests/backtest/test_dsa_points_strategy.py` 11 例（物化器 5 + loader 装载 1 + 策略级 e2e 5：仓位模拟双票逐仓位止损线各自生效/跳空按开盘/止盈线/未触及不建仓/卖出建议次日开盘 signal 离场/全量模拟 10 根持有窗）。存量锁定测试随第 27 个内建策略更新计数（test_matrix_strategy 26→27 ×2、test_screener_etf 25→26）。全仓 2834 过，唯一失败为存量 cnfree（此前会话已用 HEAD 复现验证）。改动文件 ruff 零新增（engine 净减 2；三个新文件零告警）。
- **验收第一句**（选策略跑近 3 个月仓位模拟看交易明细）依赖实机数据联调；与 dsa_paper 的同号互证口径差异同 B2（费用滑点 + 固定止盈线），已声明。

### 4.7-2 补：B2 实施注记（2026-10-02 GLM 落地记录）

- **核心 engine.py**：三字段已加（`entry_price`/`stop_price`/`take_profit_price`，默认 None 行为不变，存量测试全过）。实现拍板补充：
  - **同构抽取实际为四处**：计划列的三处（matrix 独立 :961 / legacy 独立 :1379 / portfolio matrix 内联 :2017）之外，`simulate_portfolio_legacy` 的 `_process_risk_exits` 是第四份逐字同构副本——按「修共享函数一次」的教训一并抽入模块级纯函数 `_risk_exit_decision`，四处 caller 只保留各自的前置守卫（pending/当日建仓跳过）。抽取后既有回测套件全过（含行为锁定用例），engine ruff 告警净减。
  - **绝对线语义**：`stop_price` 存在时**取代**百分比止损线（两者同设百分比不生效），`take_profit_price` 同理；移动止损/回撤止盈线照常共存，多线取最高有效线（先到先触发）；穿越与成交价口径（开盘已破按 open、盘中触及按线价）原样复用。
  - **入场触及**：四个撮合器的 `_can_buy` 统一加 `low <= entry_price` 触及检查（low 无效 fail-closed 按未触及），新拒单计数 `buy_entry_not_touched`；成交价 `min(当日 open, entry_price)`——matrix 路径在 `_resolve_entry_prices`（`entry_price_override` 钩子的消费函数）里全矩阵实现，panel legacy 路径在买入点用 `_absolute_entry_base` 调整基准。分钟策略逐格 override 路径不设 `entry_price`，互不干扰。
- **overrides 链路**：`app/api/backtest.py` 的 `overrides` 本就是裸 dict 透传（缓存键已含 overrides JSON），无需改动；放行点在 `app/backtest/strategy.py`——新增 `_normalize_abs_price`（有限正数才有效，空/非法/非正 = 不启用，对齐 `_normalize_pct` 宽松先例），三键经主路径与分钟路径两处 MatcherConfig 构造传入（优化器/步进优化共享同一入口）。既有分钟 e2e 已在跑该调用链（接线错误会 TypeError）。
- **扩展端点**：`backend/app/custom/dsa_backtest.py`（新模块，EXTENSION_ID `dsa.backtest`，未动其他 dsa_* 模块）。`POST /api/ext/dsa/backtest-report` 实现拍板补充：
  - **口径**：信号日 = 报告 created_at 前 10 位；`matching="open_t+1"`（次一交易日成交，触及判定/成交价都在成交日）+ `exit_fill="close_t"`（无卖出信号，max_hold/end 按当日收盘 = DSA 窗口到期口径）；持有窗 `max_hold_days=10`（DSA §4.6-2）；费用滑点按引擎默认（fees 万2 双边 + 滑点 5bps；DSA dsa_paper 是零费用口径，刻意不同——互证只看同号）；一字板拒单不在口径内（原始 enriched 无 limit 信号列，如实缺席）；卖出类建议不出场（核心引擎无 sell_signal，如实缺席）。
  - **结果语义**：建仓/出场等数据性结果 200 + note（未触及建仓 → note「未触及介入价」、数据不足 → note 不报错）；报告不存在 404、报告缺 ideal_buy 400（调用方错误）；`holding_bars` = 引擎原生 `duration`（成交日之后的持有根数，与 DSA 逐根计数一致）；`return_pct` 为百分数（含费用），与 dsa_paper trades 同单位可互证。
  - 面板窗口：报告日 -15/+45 自然日；引擎走核心单例（镜像 `api/backtest._get_engine`，PanelCache 复用）。
- **测试**：`tests/backtest/test_absolute_points.py`（16 例：共享决策纯函数口径 + matrix/portfolio/legacy 三撮合器的触及/跳空/绝对线/取代百分比/移动止损共存）+ `tests/test_dsa_backtest.py`（9 例：端点契约形状、全流程/跳空/止盈/未触及 note/数据不足/404/400/持有窗/loader 注册）。验证：回测 + dsa 套件全过；全仓 2822 过（仅存量 cnfree 失败与 worker spawn 偶发，均与本次无关）；改动文件 ruff 无新增告警（engine/strategy 有历史告警按 CONTRIBUTING 不顺手清理，新增文件零告警）。
- **前端（Kimi）**：报告详情弹窗"点位回测"按钮由 Kimi 并行实施（工作区已有对应改动），后端契约如上；回测页本身不动。
- **B2 验收**（同一报告与 dsa_paper episode 结果同号互证）：依赖实机数据联调确认；核心口径与 dsa_paper 的入场（次日触及 min(open, 介入价)）/止损/持有窗已对齐，费用与移动止盈为两处已知口径差异（见上）。

### 4.7-1 补：B1 实施注记（2026-10-02 GLM 落地记录）

- **核心**：`app/strategy/paper.py` + `app/api/paper.py` 已落 conditional 单（字段/撮合口径按本节契约），测试 `tests/test_paper_conditional.py`（盘中现价成交、跳空按开盘价、穿越按触发价、day 作废、gtc 持续、费用/T+1/涨跌停不被绕过、API 透传）。实现拍板补充：
  - **day 语义 = 首个可评估交易日**：18:00 后创建的单次日结算评估（未触发即作废）；盘中（开盘后）创建的单当日结算跳过（`_placed_after_price_time` 对 conditional 取 09:30 门控，开盘后区间不可回填），次日评估。盘中即时触发不受创建时刻限制。
  - **排队重试保持触发语义**：`queue_limit_orders` 开启时 conditional 单遇涨跌停拒单**不转 next_open**（否则保护性止损单会被静默改成次日无条件市价卖），保持原触发条件次日重判，计顺延超限过期。
  - `read_daily_bar` 扩展返回全 OHLC（含 null 安全）；缺 low/high 视同缺行情顺延。
- **桥**：`backend/app/custom/dsa_paper_bridge.py`（新扩展模块，未动其他 dsa_* 模块——dsa_analysis 的任务收尾钩子固定调 dsa_watch，桥自起 daemon 线程 60s 轮询报告库对账，不动钩子链）。实现拍板补充：
  - **账户 = default**；槽位 = 10（对齐 §4.6-3 口径）；买单金额 = 权益/10 按触发价折整百，资金校验按账户现金（现金不足的槽位 skipped 留痕）。
  - **幂等按订单 source**（`dsa:{report_id}` 入场 / `:stop` 止损 / `:exit` 建议卖）：同 source 订单任何状态下存在即不重建 → day 作废不重挂、报告未更新不重建；对账为纯 diff，pending 的 dsa 单不在保留集即撤。
  - **卖出类建议 = 次日开盘全量可卖整百 next_open 卖单**，意图优先于止损线（本拍撤该票全部止损单）；空仓票的卖出类建议不建入场单。
  - **保护性保留**：持仓票最新报告缺 `stop_loss` 或报告库已无该票报告时，既有 pending 止损单保留不撤（对齐 §4.3-1 槽位级保留语义）；空仓票残留卖出单一律撤。
  - **已知边界**：止损单触发遇跌停拒单后默认（queue 关）过期不自动重挂——账户开启 `queue_limit_orders` 可保持触发条件次日重试；应用整体跨日宕机时 day 单可能晚一日成交（作废的权威路径是盘后结算，宕机日无结算）。
  - 测试 `tests/test_dsa_paper_bridge.py`（入场生成/幂等/作废不重挂/换报告撤旧建新/止损 GTC/止损变化重建/缺止损保留/报告全删保留/exit 单/take_profit 不出单/无账户待命/loader 契约）。
- **前端**：Paper 页 conditional 类型徽章+触发价列未做（本任务后端范围；订单列表目前原样透出 `order_type`/`trigger_price` 字段，功能可辨）。
- **B1 验收第一句**（真实链路：空仓票出报告 → 模拟盘出现 conditional 单 → 盘中触发成交）依赖运行实例跑分析任务 + 盘中行情，待用户实机联调确认。

### 4.7-3 验收

- B1：测试覆盖 触发成交/跳空口径/未触发作废/GTC 持续/费用 T+1 校验不被绕过；真实链路：给一只空仓票出报告 → 模拟盘账户出现 conditional 单 → 盘中触发成交
- B2：同一报告用绝对点位回测结果与 dsa_paper 扩展的 episode 结果同号（口径互证）

## 5. 前端落地（Kimi 已做/在做的部分）

- **2026-09-29 用户拍板（第二次澄清）：分析中心并入核心"个股分析"页**——核心 `pages/StockAnalysis.tsx` 有 2 行受控改动（1 行 import + 1 行 `<DsaAnalysisSection symbol={symbol} />`，上游冲突重贴即可），功能主体在 `custom/dsa/components/DsaAnalysisSection.tsx`（分析自选/新建分析/任务进度/报告库/DSA 历史/详情弹窗）。独立"分析中心"页与侧边栏入口已撤。
- `frontend/src/custom/dsa/extension.tsx`：注册路由 `/dsa/watch`（次日盯盘）+ 侧边栏菜单一项。
- `frontend/src/custom/dsa/api.ts`：本契约的 typed client（fetch 封装与 `lib/api.ts` 同款：超时、toast、`detail` 解析）。
- 组件复用核心件：`PageHeader`/`EmptyState`/`Modal`/`Toast`/`MarkdownRenderer`；自选股从现有 `api`（`/api/watchlist`）取，后端不需要再提供 targets 接口。
- 次日盯盘页（P2）：持仓/空仓分栏清单（最新报告点位+盯盘条件+挂线状态+sync_state 徽标）、立即对账按钮、盘前自检卡；点"看报告"复用报告详情弹窗。

## 6. 并行守则

- GLM 只动 `backend/app/custom/`（可新增 `dsa_analysis.py` 等模块，别改 `dsa_bridge.py` 的 health 契约，前端在用）；改核心文件前先到对话里提。
- Kimi 只动 `frontend/src/custom/dsa/`。
- 契约变更必须改本文档并标注日期；前端按本文档 mock 开发，后端按本文档实现，**联调以 `GET /api/ext/dsa/health` + `/openapi.json` 枚举路径为准**（401 不能证明路由不存在——先认证再枚举）。
- 验证基线：后端 `uv run pytest` + `ruff check`；前端 `pnpm build`。验证用的服务跑完即杀，不留常驻进程。
