# DSA P2+P3 后端实施报告（次日盯盘 + 自有持仓账，2026-09-29~30）

> 范围：迁移计划 §4（P2 次日盯盘契约）+ §4.4（P3 持仓账契约），并落档 2026-09-30 评审轮结论。
> 报告定位：P2 阶段已有专报 [`20260929-p2-backend-impl.md`](20260929-p2-backend-impl.md)（并行会话撰写，含 P2 契约逐条对照与开发过程），本文是其撮要 + P3 全量实施记录 + 评审遗留落档的合并报告（评审 #4 建议"在 §4.4.3 补日期标注并补一份 P3 实施记录"，后者即本文由来）。**对 P2 的验收判断须两份文件并用**——本文 P2 部分仅是撮要，不含逐条契约对照；与本文冲突处以代码为准。
> 执行环境：Windows，后端命令统一 `uv --directory backend run --frozen --extra dev ...` 形态。
> 红线遵守情况：未修改 `backend/app/` 下任何核心文件；未修改任何前端文件；未访问 `D:\Documents\Zai\tick-stock-panel` 生产副本；DSA 旧库 `D:\Documents\daily_stock_analysis\data\stock_analysis.db` 全程仅以 sqlite URI `mode=ro` 只读访问（含 P3 侦查与流水线冒烟），测试一律 tmp_path mini 库 + monkeypatch `DSA_DB_PATH`，无任何写入；未执行 git commit/push，未删除文件。

## 1. 概述

- **P2 次日盯盘**：单扩展模块 `dsa_watch.py`（`EXTENSION_ID="dsa.watch"`）落地契约 §4 三端点——`GET /watch-plan`（盯盘集合=持仓∪自选 × 每票最新 P1 报告六字段投影 × 现存 `DSA·` 规则 + sync_state 四态）、`POST /watch-sync/run`（点位→monitor-rules 对账，只碰 `DSA·` 前缀规则）、`GET /premarket/status`（盘前自检纯读）。对账三入口（任务收尾钩子/调度 30min 兜底/手动）经 `_SYNC_LOCK` 串行；落盘后镜像 `_sync_engine` 重载引擎，engine 未捕获时置 `_ENGINE_FLUSH_PENDING` 待捕获补 flush。
- **实现来源披露（如实记录）**：dsa_watch.py 主体（接管时约 1030 行，素材记录；最终 1155 行）与 dsa_analysis 挂接点①②由另一并行会话先行落盘，P2 工作流经 escalate 决议（等其静默 ≥5 分钟）接管并对齐契约，**保留其架构**：规则 id/name 用其式样 `dsa_{symkey}_{kind}` / `DSA·{sym6}·{kind}`（非原指令草稿的 `dsa_{token}_{codeslug}`），premarket 用其"文件态 + 09:10-11:30 调度窗"式样（非内存现算）——均为决议"设计自由处从其设计"。P2 对齐改动：成本方法整库 fifo 优先/否则 avg 两法绝不混读、盯盘集合改持仓∪自选（持仓在前）、全部 symbol 经 `_canonical_symbol` 归一为后缀点分式、新增 `_ENGINE_FLUSH_PENDING` 补 flush、单票落盘异常容忍、在 `dsa_analysis._resolve_runtime` 加挂接点③。接管停笔时该并行会话已静默 >40 分钟（P2 会话观察，素材记录，无法事后复验）；若其恢复活动可能与盘上版本冲突——本报告撰写时实测 `dsa_watch.py` mtime 为 2026-09-30 00:48:40、`dsa_portfolio.py` 为 00:56:30，即 P3 完成后无后续写入，冲突未发生。
- **P3 自有持仓账**：新增 `dsa_portfolio.py`（`EXTENSION_ID="dsa.portfolio"`）——jsonl 流水 TradeStore + FIFO 重放纯函数 + 5 条 `/api/ext/dsa/portfolio` 方法路由 + DSA 旧库只读一次性导入；并把 `dsa_watch.load_positions` 持仓来源切到本账（本账非空→FIFO 重放 `positions_source="dsa_portfolio"`，空→回落桥 `"dsa_bridge"`，如实上报，见 §4.6-3）。P3 全程 P2 三个测试文件零改动、全通过。

最终状态（**指测试与静态验证层面**，功能链路的真实端到端运行未发生，见 §5.4）：`/api/ext/dsa/*` 共 **15 路径 18 方法路由**（P1 累计 8/10，P2 本期新增 3/3，P3 本期新增 4/5）全部注册，loader 冲突校验通过（`errors == ()` 有测试断言）；四个 DSA 测试文件 **185 用例全部通过**（portfolio 47 + watch 61 + analysis 57 + bridge 20，本报告撰写会话复跑，见 §5.1）；DSA 8 文件 ruff 零告警（复跑）。全仓 pytest/ruff 存量基线（1 例 psutil 环境红 / 1577 条存量告警）按任务口径未跑；与 DSA 无关的依据是 P1 报告 §5.3 的核实（单跑复现唯一失败为 psutil 环境问题、对全量 ruff 输出 grep `dsa` 零命中，均与本仓 DSA 文件无调用关系）。

## 2. 交付清单

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `backend/app/custom/dsa_watch.py` | 新增（1155 行） | P2 全量 + P3 切换：symbol 工具（`_norm6` :116-128）、持仓读取与合并（行级容错 :155-178、fifo/avg 整库二选一 :181-203、来源切换 `load_positions` :227-252）、期望规则推导与 diff 纯函数（:307-460）、对账服务 `run_reconcile`（:596-684，`_SYNC_LOCK` 串行、id 占用防护、单条落盘 OSError 容忍）、watch-plan 聚合（:722-793）、盘前自检（:800-984）、任务收尾/调度兜底/引擎捕获钩子（:1000-1096）、三条路由 + startup（:1122-1155） |
| `backend/app/custom/dsa_portfolio.py` | 新增（582 行） | P3 全量：FIFO 重放纯函数 `replay_positions`（:66-122，超卖抛 `OversellError`）、TradeStore（:137-234，追加写 + 删除/导入全量原子重写 + 进程内锁"锁内校验+落盘"）、手动记账校验（:277-317）、旧库一次性导入（:337-409、:550-580）、持仓视图 `_build_positions`（:458-498）、五条路由（:505-582） |
| `backend/app/custom/dsa_analysis.py` | 增量（3 处挂接点，均函数级导入防循环 + 全吞异常） | ① `_process_task` 任务置 done 后调 `dsa_watch.on_task_done(task)`（:563-572）；② `_schedule_loop` 循环体追加 `dsa_watch.scheduler_tick()`（:738）；③ 引擎捕获两处——`_resolve_runtime`（:747-755，覆盖全部 dsa_watch 路由与分析路由）与 `create_analysis_task` handler（:807-814）。P1 阶段另含符号规范化 `_canonical_symbol` 修复（详见 P1 报告） |
| `backend/app/custom/dsa_bridge.py` | **零改动** | 仅被 import 复用（`dsa_watch` 复用 `_connect_legacy_ro()`；`GET /health` 契约一字未动，loader 集成测试回归） |
| `backend/tests/test_dsa_watch.py` | 新增 | 61 用例，分组 A 纯函数 / B 对账服务 / C 持仓 / D 端点 / E premarket / F 挂接与并发 / G loader 集成 |
| `backend/tests/test_dsa_portfolio.py` | 新增 | 47 用例：重放（超卖/坏行/费用口径）、存储事务（并发/损坏行/原子重写）、校验 400 矩阵、导入（409 幂等/坏行 skip/超卖行 skip）、端点、与 `dsa_watch.load_positions` 切换契约；portfolio 与 bridge 两模块各自 autouse 隔离真实旧库 |
| `backend/tests/test_dsa_analysis.py` | 增量（P2 评审轮） | autouse fixture 补 P2 隔离：stub `dsa_watch.on_task_done` + monkeypatch `dsa_bridge.DSA_DB_PATH`→tmp 不存在路径 + 复位 `dsa_watch` 模块态（:56-63），既有用例从此不真实读旧库、无跨用例竞态 |
| `backend/tests/test_dsa_legacy_bridge.py` | 零改动 | 20 用例持续回归 |
| `docs/dsa-migration/20260929-migration-plan.md` | 仅追加 §4.3 | P2 实施拍板（迁移计划 :179-192）；P3/评审的契约口径记录于本文 §4.6（§4.4.3 日期标注待补，见 §6.1-4） |
| `docs/dsa-migration/20260929-p2-backend-impl.md` / 本文件 | 新增 | P2 专报（并行会话）/ 本合并报告 |

依赖事实（复用而非重写）：`dsa_bridge._connect_legacy_ro`（mode=ro + busy timeout 3s）；`dsa_analysis._canonical_symbol`（§3.1 符号规范化注记，migration-plan :98）与 `_now_iso`（北京墙钟 naive ISO）；`monitor_rules.load_all/load_one/save_one/delete_one/normalize/validate`（save_one 裸写，调用方自调 normalize+validate）；引擎重载镜像 `app/api/monitor_rules.py` `_sync_engine`（含私有 `_reconcile_index_asset_type` 复用）；`watchlist.list_symbols`、`trading_day.is_trading_day`、`fs_utils.atomic_write_text`、`market_time.cn_now/CN_TZ`。

## 3. API 一览（15 路径 18 方法，全部在 `/api/ext/dsa` 前缀下）

本报告撰写会话以 `python -c` 枚举 `app.main:app` 实测（枚举命令与结论见 §5.1 #4，逐条清单即下表）：

| 方法 | 路径 | 用途 | 来源 |
| --- | --- | --- | --- |
| GET | `/api/ext/dsa/watch-plan` | 盯盘清单：`{generated_at, positions_source, items[]}`；items=持仓∪自选（持仓在前 sym6 字典序、其余按自选序、按 sym6 去重，symbol 统一后缀点分式）；每 item 含 name/holding/quantity/avg_cost（非持仓 null）/report 六字段投影（无报告 null）/rules[]/sync_state 四态。持仓读取降级 → 200 全 `holding=false`（不 5xx）；自选读失败 → 退化持仓条目仍 200；规则读失败 → rules=[] + sync_state 保守 stale | P2 新增，§4.1 |
| POST | `/api/ext/dsa/watch-sync/run` | 对账：`{created, updated, removed, skipped}` 同单位（规则条数）。前置读取失败（持仓/自选/规则存储）→ 零写入中止，400 `{"detail"}`（非 5xx） | P2 新增，§4.1 |
| GET | `/api/ext/dsa/premarket/status` | 盘前自检纯读：`{date, ran_at|null, checks[{name,status,detail}]}`；未跑/非当日/损坏 → 今日空态；永不 5xx、永不内联触发 | P2 新增，§4.1 |
| GET | `/api/ext/dsa/portfolio/positions` | 持仓视图：FIFO 重放 → `{items[{symbol,name,quantity,avg_cost,total_cost,last_price,market_value,unrealized_pnl,unrealized_pnl_pct}], totals{...}}`；`last_price` 无数据 null 不编造 | P3 新增，§4.4.2 |
| GET | `/api/ext/dsa/portfolio/trades` | 流水列表：`symbol` 过滤（查询参数先规范化，裸 600519 能命中存储为 600519.SH 的流水）+ `limit`（<1 400），traded_at 倒序、同刻按入账序倒序 | P3 新增，§4.4.2 |
| POST | `/api/ext/dsa/portfolio/trades` | 记一笔：字段严格校验（中文 detail 400）；锁内含本笔重放校验后追加，超卖 422 零落盘 | P3 新增，§4.4.2 |
| DELETE | `/api/ext/dsa/portfolio/trades/{trade_id}` | 删一笔：id 不存在 404；剩余流水重放校验防删坏账，超卖 422 | P3 新增，§4.4.2 |
| POST | `/api/ext/dsa/portfolio/import-from-dsa` | 一次性从 DSA 旧库导入：已有流水 409 幂等拒绝；旧库不可用 400；坏行/超卖行 skip 计数 → `{imported, skipped}` | P3 新增，§4.4.2 |
| GET/POST | `/analysis/tasks`、`GET /analysis/tasks/{id}`、`GET/DELETE /analysis/reports[/{id}]`、`GET/PUT /analysis/schedule`、`GET /health`、`GET /legacy/reports[/{id}]` | P1 既有 8 路径 10 方法，本期零改动（详表见 P1 报告 §3） | P1 |

关键字段口径（便于对照实现，均经本会话读码核实）：

- **sync_state 四态**（`_judge_sync_state` :703-719）：`synced`=现存托管规则集与最新报告派生的期望规则集一致（内容含 op/price/severity，价格 round 6 位比较；enabled 不参与——手动停用仍 synced）；`stale`=报告已更新而规则未对账（含规则读失败时的保守降级）；`no_points`=有报告但无可适用点位；`no_report`=该票无最新报告。
- **watch-plan report 六字段投影**（`_project_report` :691-700）：`id` / `created_at` / `operation_advice` / `sentiment_score` / `points` / `phase_decision`（后两者原样透传，空值由前端隐藏）。
- **五种规则 kind**（`_RULE_OP_BY_KIND` :85-87）：`stop_loss`（close≤X，critical）、`take_profit`（close≥X，critical）、`add`（close≤X，warn）、`reduce`（close≤X，warn，可多条带 rank）、`entry`（close≤X，info）。派生来源（契约 §4.2 推导表，`derive_expected_rules` :307-339）：持仓票由报告 points 的 stop_loss→stop_loss、take_profit→take_profit、secondary_buy→add，及 phase_decision.risk_conditions 中 kind=reduce 且带价→reduce；空仓票由 ideal_buy→entry。

## 4. 关键设计决策与契约口径

### 4.1 对账语义（P2 撮要）

- **托管标记**：name 前缀 `DSA·` 是唯一托管标记（TSP 规则无 source 字段）；人工规则不进任何写路径；外来 `DSA·` 命名（解析不出 sym6+五 kind）不托管不比对不删（`parse_dsa_rule` :354-390）。
- **删除触发器**：未持仓票移出自选 → 删其全部 `DSA·` 规则；清仓票（在集合内不在持仓）→ 删全部持仓规则并按空仓语义重建 entry；持仓票移出自选但未清仓 → 规则保留（集合=持仓∪自选的语义推论，§4.3-1 已拍板）。持仓票**无报告或报告无可适用点位**（含 LLM 围栏解析失败→points 全 None）时跳过该票点位派生 diff、保留既有规则（`_build_specs` :553-556），防 LLM 闪失当晚清掉用户止损线。**评审张力（非阻断）**：判据是票级 all-or-nothing——只要派生出任意一条规则就不 skip，缺位的 key 仍落入 removes（评审实测探针：持仓 600460 + 既有 `dsa_600460sh_stop_loss` + 报告 points={stop_loss: None, take_profit: 38.0} → skip_diff=False，removes 含止损规则）；迁移计划 §4.3-1"报告缺点位"按自然读法含部分缺失，与实现"全部缺才保"存在字面张力。~~合并前待拍板（§6.1-1）~~ **已闭环（2026-09-30）**：主会话按推荐默认落地槽位级保留——缺哪个点位列保哪个槽位，给出的照常对账；143 passed / ruff 零告警，契约 §4.3-1 已加修订注。
- **幂等与防护**：确定性 id `dsa_{symkey}_{kind}[_{rank}]` 天然 upsert；create 前查 id 占用（被挤出托管集不静默覆写，:631-635）；更新只改 conditions+severity，保留 id/name/symbols/asset_type/cooldown/created_at/enabled（手动停用不被复活）；reduce 多条按 price 升序定 rank 并同价去重（round 到分）；指数票跳过建规则（000001.SH/SZ 撞码防护，:548-552）。

### 4.2 持仓读取：成本方法整库二选一 + P3 来源切换

- **桥路径（回落时才走）**：`quantity > 0` 原始行 → 行级清洗（TEXT/NULL 脏行跳过并 warning，绝不打穿降级，:155-178）→ **fifo/avg 整库二选一绝不混读**（:189）：任一行带 fifo 只取 fifo 行，全库无 fifo 才取 avg 行；跨账户 quantity 求和、avg_cost 按数量加权；**不按 `portfolio_accounts.is_active` 过滤**（实测 12/17 行挂非活跃账户）。P2 侦查依据（mode=ro 只读）：quantity>0 共 17 行，账户2 六持仓 avg+fifo 双记（数值同），账户3/4 仅 5 条 fifo 行（601318/515880/588200/SZ000636/SZ002383），avg-only 0 行——fifo 是 avg 的超集，只取 avg 会丢 5 持仓。
- **P3 切换（`load_positions` :227-252）**：本账（`dsa_portfolio` 流水）非空 → FIFO 重放，`positions_source="dsa_portfolio"`；本账为空 → 回落桥，`"dsa_bridge"`（用户未导入/记账前持仓视图不回归）；**本账读取/重放故障不回落桥**（导入后桥是过时旧账，按旧账对账会误删规则），抛 `BridgeUnavailableError` 走既有降级；两来源都不可用 → watch-plan 降级态保持 P2 契约值 `"dsa_bridge"`（:734-735 注释：来源链末端，无任何来源成功时无从如实上报）。
- **评审 #2（非阻断）**：本账按 sym6 归并的实现是字典覆盖（:242-247）——评审实测：流水含 600519.SH buy 100@10 与 600519.SZ buy 200@20 → 返回 `{'600519': {200.0, 20.0}}`，.SH 一侧静默丢弃；docstring :233-234 声称"同码跨市场撞码并票与桥口径一致"与实现不符（桥路径 `merge_position_rows` 才是求和+加权）。现实 A 股可交易代码跨所不撞码（需手工误录才触发）；docstring 与实现待改一致（§6.1-2）。

### 4.3 引擎生效链：捕获 + pending flush

写盘 ≠ 生效（引擎无周期重载），对账落盘后镜像 `_sync_engine`。engine 引用捕获链：挂接点③（分析路由 `_resolve_runtime` :747-755 / `create_analysis_task` :807-814）+ 三个 dsa_watch handler 首行 `_capture_runtime`。**engine 未捕获时的补生效**：本轮确有规则变更则置 `_ENGINE_FLUSH_PENDING`（:571-578），任一 dsa_watch/分析路由随后捕获到 engine 即刻补 flush（成功清位、失败保留重试，`capture_engine` :1067-1080）；无变更时仅落盘记 warning。**残余缺口（如实声明）**：`get_analysis_task` 轮询路由（:817-823）不经 `_resolve_runtime`、捕获不到 engine——若 18:00 定时分析全程无其他页面访问，规则已落盘但引擎内存要等下一次 dsa_watch/monitor/分析路由请求（含报告列表 :834-837，任务轮询不算）才生效。

### 4.4 premarket（沿用并行会话式样）

每交易日 09:10-11:30 窗口内由调度线程跑一次（`_decide_premarket_run` 纯函数：trading_day 未知退化周一~五），结果原子落盘 `data/user_data/dsa_watch/premarket.json`；`GET /premarket/status` 纯读，永不 5xx、永不内联触发。四检查项独立容错（异常→该项 fail，detail 截断 120 字符）：数据源（首只自选 `_load_kline` 探针，落后上一交易日 warn；用 K 线探针而非 `app.state.capabilities`）、自选K线覆盖（逐票窄区间）、同步规则启用（复用 watch-plan 判据 + "持仓不在自选 N 只"提示）、DSA桥可用（直连桥持仓读取）。后两处检查实现与 P3 后的实际持仓来源（`load_positions` 本账优先）存在口径差，评审 #6 指出 fail 文案在"本账非空"时失真（§6.1-6）。

### 4.5 P3 持仓账设计

- **存储**：`data/user_data/dsa_portfolio/trades.jsonl` 一行一个 Trade JSON；POST 追加写、DELETE/导入全量原子重写（临时文件 + `os.replace`，失败清理后重抛）；进程内锁"锁内校验+落盘"不分立（POST 的重放校验+追加 :201-206、DELETE 的过滤+剩余重放校验+重写 :208-221——删买入导致超卖同样 422 零落盘，防把账删坏）；损坏行跳过记 warning。
- **导入事务**：`import_all` 锁内复核账本为空后**单次原子重写**全部行（:223-234）——逐行 append 中途崩溃会留半截账，半截账因幂等 409 永远无法重导；并发兜底再 409（:576-579）。
- **导入语义**：`portfolio_trades` 全量按 (trade_date, id) 升序（即重放入账顺序，:337-342）；行映射判据：side 非 buy/sell、quantity/price 非有限正数、symbol 经 `_canonical_symbol` 后仍非点分式（无法定交易所，如 5/1/7 开头且维表未命中）、trade_date 解析不了、id 缺失 → skip 计数；会造成超卖的行也 skip（:567-574）；`fee = 旧库 fee + tax` 合并一条（:396-398）；导入行 id `dsa_{旧库id}` 留溯源；**多账户流水并成单账 FIFO 流**（同票跨账户按时间序重放，与 TSP 单账本设计一致）。
- **读路径 fail-closed**：手工改坏的超卖账本 → `OversellError` → GET positions 500（大声故障优于静默空仓，模块 docstring :11-12 声明，有测试固化）；正常写入/删除路径均已校验，该分支仅兜手工改账。
- **import 方向**：本模块只 import `dsa_analysis`，`dsa_watch` 单向 import 本模块（模块 docstring :24-26）。

### 4.6 契约偏差与口径拍板（汇总）

| # | 项 | 契约/指令原文 | 实现口径 | 依据 |
| --- | --- | --- | --- | --- |
| 1 | **done 计数口径** | §3.1 示例按"仅成功"计数 | **终态计数**（done+failed，`dsa_analysis.py:510`）；P2 挂接点①即挂在终态 done 之后（:563-572），failed 任务（流水线崩溃）不触发收尾对账、由 30min 兜底覆盖 | P1 报告 §4.2 偏差 #1 延续；前端用 `done/total` 画进度条 |
| 2 | **skipped 计数口径** | 原指令草稿："有票无报告或期望规则为空" | 沿并行会话口径（§4.3-3 拍板）：缺点位期望槽位（持仓票 stop_loss/take_profit/secondary_buy 各 1 槽 + reduce 无价每条 1 槽；空仓票 ideal_buy 1 槽仅在有报告时计，`_count_empty_point_slots` :292-304）+ id 占用/校验失败/落盘失败数（:631-648 计入 `id_conflicts`），`skipped = diff.skipped + id_conflicts`（:680），与 created/updated/removed 同单位（规则条数） | 与"无报告票"语义不同：无报告持仓票走 skip_diff 保留规则、不计 skipped |
| 3 | **positions_source 过渡策略** | §4.4.3（migration-plan :223-226）二态表述："P3 验收后改读本扩展；DSA 桥只留历史报告浏览" | 动态回落：本账非空→`"dsa_portfolio"`；空→回落 `"dsa_bridge"`；降级保持 `"dsa_bridge"`；本账故障不回落 | 过渡期连续性设计（字面硬切会把未导入用户的真持仓判空仓、对账误删止损线）；`positions_source` 如实上报（`test_load_positions_local_first_then_bridge_fallback`、`test_watch_plan_switches_bridge_to_local_ledger` 断言两种来源）。评审 #4 认定属契约文档漂移而非行为缺陷；§4.4.3 日期标注待补（§6.1-4） |
| 4 | **费用不资本化** | §4.4.1 未明说费用是否进成本 | fee 只记流水不资本化：FIFO 批次成本只含 price×quantity，`avg_cost`/`total_cost` 均不含费用——持仓成本与市值、盈亏同口径；导入时 `fee = 旧库 fee + tax` 合并一条流水 | 模块 docstring :14-16 与 `replay_positions` docstring :74-76 拍板；有测试固化 |
| 5 | **raw_close 退化** | §4.4.1 "last_price 取 repo 最新收盘价（无数据返回 null，不许编）" | `_last_close`（:426-455）：近 14 个自然日窗口最后一行，**优先 raw_close（不复权，与成本同口径）→ 缺列/缺值退化 close（前复权，除权日与历史成本比较存在口径误差，接受并声明）→ 无数据/读取失败 null 不编造** | 契约未指定复权口径；`_last_close` docstring :427-430 已声明 |
| 6 | totals 部分缺价 | §4.4.2 totals 值可 null | 任一持仓缺 `last_price` → `totals.market_value/unrealized_pnl` 整体 null（部分和不冒充总数）；`total_cost` 恒可加（与行情无关） | `_build_positions` docstring :459-461；评审未异议 |
| 7 | 时间口径 | §4.4.1 traded_at 为 ISO | 北京墙钟 naive ISO 秒精度（与 `dsa_analysis._now_iso` 同口径）；入参带时区换算北京墙钟（确定性单位换算）、纯日期视为当日零点；导入行用旧库 trade_date（仅日期→当日零点） | 模块 docstring :18-20 |
| 8 | 规则 id/name 式样 | 原指令草稿 `dsa_{token}_{codeslug}` | `dsa_{symkey}_{kind}[_{rank}]` / `DSA·{sym6}·{kind}[·{rank}]`（:136-144） | escalate 决议"设计自由处从其设计"；与迁移计划 §4.2 命名模板"（命名 `DSA·{symbol}·{kind}`，migration-plan:172）"及 §4.3-4"多条 reduce name 加序号（migration-plan:186）"拍板一致 |

## 5. 验证结果

### 5.1 本报告撰写会话逐条运行

| # | 命令 | 结论 |
| --- | --- | --- |
| 1 | `uv --directory backend run --frozen --extra dev pytest tests/test_dsa_portfolio.py tests/test_dsa_watch.py tests/test_dsa_analysis.py tests/test_dsa_legacy_bridge.py -q` | ✅ **185 passed in 10.44s**（63 warnings 全为 `app/services/watchlist.py:216` 存量 `datetime.utcnow()` DeprecationWarning，非本次引入） |
| 2 | 同上四文件 `pytest --collect-only -q` | ✅ 用例数 47 / 61 / 57 / 20，与 P3/P2/P1 各阶段记载一致，合计 185 |
| 3 | `uv --directory backend run --frozen --extra dev ruff check app/custom/dsa_portfolio.py app/custom/dsa_watch.py app/custom/dsa_bridge.py app/custom/dsa_analysis.py tests/test_dsa_portfolio.py tests/test_dsa_watch.py tests/test_dsa_analysis.py tests/test_dsa_legacy_bridge.py` | ✅ **All checks passed!**（DSA 8 文件零告警） |
| 4 | `uv --directory backend run --frozen python -c "...app.main import app..."`（枚举 `/api/ext/dsa` 路由） | ✅ 输出 `paths 15 methods 18`，逐条与 §3 表一致（P1 累计 8/10 + P2 新增 3/3 + P3 新增 4/5） |
| 5 | `git diff --check` + `git status --porcelain` | ✅ diff --check 干净（exit=0，唯一 warning 为 `frontend/vite.config.d.ts` 存量 CRLF 提示，非 DSA 改动）；DSA 相关为 4 模块 + 4 测试 + `docs/dsa-migration/`；工作区另有 `?? nul` 残留（评审 #7，§6.1-7）与他人前端未提交改动（未触碰） |

### 5.2 各阶段会话执行记录（素材转述，本会话未复跑）

- **P2（接管对齐会话）**：实现前 sqlite `mode=ro` 侦查真实旧库持仓（17 行分布见 §4.2）；`pytest tests/test_dsa_watch.py -q` 首跑 1 failed（对 add_batch 后插前排的顺序预期写反，属测试预期错误）→ 修正后 **61 passed**；`pytest tests/test_dsa_analysis.py tests/test_dsa_legacy_bridge.py -q` → **77 passed**（P1 规范化与并行会话两处挂钩共存无回归）；ruff 曾报 9 处 RUF002（自引入的 `∪` 字符）→ 全部替换为 `+` 后 All checks passed!；路由枚举 13 条方法 = P1 的 10 条 + 新增 3 条，零冲突；终跑 `pytest` 三文件 + `git diff --check` → **138 passed、diff 干净**。（并行会话自身开发过程记录见 P2 专报 §5。）
- **P3**：侦查期 `mode=ro` 只读核实真实旧库 `portfolio_trades` 15 列结构与样例行（46 行，side buy 29/sell 17，symbol 裸码/后缀式混存），未写入；`pytest tests/test_dsa_portfolio.py -q` → **47 passed, 1 warning**（存量 DeprecationWarning）；四文件 → **185 passed**（P2 三个测试文件零改动）；ruff 八文件曾暴露 RUF002（全角标点）4 处与 F401（未用导入）1 处，已修复后全绿；路由枚举 15/18；`git status --porcelain` 仅 3 个授权文件 + 既有未跟踪文件。
- **P1（背景）**：符号规范化修复（`_canonical_symbol`）三处接入、57 用例全绿（详见 P1 报告）。

### 5.3 流水线终验冒烟（转述：流水线执行、只读真实数据，本会话未复跑）

- **冒烟 1（P1 规范化假设）**：`instruments 600519 -> 600519.SH`；`enriched 最新分区含 600519.SH: True`——裸码→后缀式反查与数据侧假设成立。
- **冒烟 2（P3 FIFO 重放正确性）**：以 `dsa_portfolio.replay_positions` 重放真实旧库 `portfolio_trades` 全部流水（只读），与旧库 `portfolio_positions` fifo 口径持仓逐票对照——**10 只票（000636/002383/002837/512400/515880/588200/600105/600460/601318/603986）数量完全一致，差异 `{}`，跳过超卖 0**。为"导入→重放→持仓"链路的正确性提供了真实数据只读证据；经 API 导入**落盘**的完整写路径仍未执行（红线禁写真实 `data/`，用户实际导入时才发生，见 §5.4）。

### 5.4 未覆盖（如实声明）

- **导入端到端写入**：导入逻辑仅对 tmp_path mini sqlite（按真实旧库 mode=ro 侦查的 schema 1:1 建表）验证 + 冒烟 2 只读对照；`POST /portfolio/import-from-dsa` 对真实 46 行旧库的写入未执行。
- **服务级端到端冒烟**：P2/P3 会话均未起真实服务冒烟（P1 阶段曾做）；前端真机联调未做——响应形状已逐字段对照 `frontend/src/custom/dsa/api.ts` 的 `PortfolioPositions`/`PortfolioTrade` 等类型，由测试断言兜底。
- **真实 LLM 全链路**：本机无 AI Key、测试全 mock（P1 §5.4 已声明）；no_points 保留语义即兜底。
- **全仓 pytest/ruff**：按任务口径未跑；存量基线（1 例 psutil 环境红 / 1577 条存量告警）见 P1 报告 §5.3，与 DSA 无关。

## 6. 遗留风险与后续

### 6.1 评审遗留七项（2026-09-30 评审：未发现阻断问题；七项全非阻断，逐条落档）

> **"非阻断"语义与未决状态（须先读）**：评审判定七项均不阻塞开发继续，但其中 **#1/#3/#4 是评审明确标注"合并前"的动作项，#6.3 基线表态是评审既有的合并前置**——其余四项中 **#1 已于 2026-09-30 落地闭环**（见下），#3/#4/#6.3 截至本文撰写未处理。即：当前状态下可提测、可评审，**不可点验收**；验收决定须在剩余三项完成（或用户显式拍板接受现状）之后作出：
>
> | 前置项 | 内容 | 待谁处理 |
> | --- | --- | --- |
> | ~~部分缺点位删除行为（§6.1-1）~~ | **已闭环（2026-09-30，主会话落地槽位级保留）**：用户未及应答，主会话按推荐默认拍板"缺位 slot 保留"——报告缺哪个点位列，该槽位既有规则保留不删，给出的点位照常对账；`ExpectedSpec.preserve_kinds` + `diff_rules`/`_judge_sync_state` 过滤，新增 5 例测试，DSA 套件 143 passed / ruff 零告警；契约 §4.3-1 已加修订注（翻回票级全保为一行改动）。若用户偏好票级全保，改 `diff_rules` 的 preserve 判据为票级即可 | ~~已处理~~ |
> | 硬编码绝对路径（§6.1-3） | 收敛为复用 `dsa_bridge._connect_legacy_ro`（含 docstring 修正） | 后端改码（纯机械，无行为变化） |
> | §4.4.3 契约标注（§6.1-4） | 补日期标注 + 动态回落语义 | 文档修订 |
> | 全仓存量基线（§6.3） | 对 1 例 psutil 红 / 1577 条 ruff 告警表态 | 维护者 |
>
> 其余三项（#2/#5/#6）为瑕疵修正与文案修正，不构成验收前置；#7 是工作区卫生项。

| # | 位置 | 内容 | 处置建议 |
| --- | --- | --- | --- |
| 1 | `dsa_watch.py:553-556` | 持仓票报告**部分缺点位**（如缺 stop_loss 但给 take_profit）时既有止损规则仍会被当晚对账删除——§4.3-1 保护只覆盖 points 全 None 形态（票级 all-or-nothing 判据；评审探针证据见 §4.1）；迁移计划 :183"报告缺点位"自然读法含部分缺失；测试只固化全 None 与无报告两形态。"LLM 对数据不足点位按提示词应落 null（null=本次不适用，删旧线可辩）"是评审的非阻断理由之一，但**该 LLM 行为属未经真实模型验证的假设**（本机无 AI Key、测试全 mock，§5.4）——与"防 LLM 闪失当晚清掉用户止损线"的设计动机（§4.1）并存时，此风险不能以该假设为由豁免，拍板时应按"LLM 真实行为未知"对待 | **合并前拍板**：要么按票级全保，要么对持仓票缺位 slot 保留既有规则；现状是"缺位即删" |
| 2 | `dsa_watch.py:241-247`（docstring :233-234） | 本账同码跨市场撞码是**覆盖而非合并**（docstring 声称与桥口径一致，实现不符；桥路径才是求和+加权）——撞码需手工误录才触发 | docstring 与实现改一致（§4.2 已核实） |
| 3 | `dsa_portfolio.py:324` | 硬编码机器绝对路径复制自 `dsa_bridge.py:32`，违反 CONTRIBUTING.md:80/:219；"防成环"的复用排除理由与事实不符（已核实 dsa_bridge.py 无 `app.custom` import，复用 `_connect_legacy_ro` 不成环，dsa_watch 即如此复用且测试 monkeypatch 生效）。缓解因素：迁移计划 §0 决策 2（migration-plan:12）正文层面已写入该旧库路径字面——"dsa_bridge 只读 DSA sqlite（D:\Documents\daily_stock_analysis\data\stock_analysis.db）"，即契约本身钉死了该路径（个人机单用户工具），且 P1 已有同款先例 | **合并前收敛**为复用 `dsa_bridge._connect_legacy_ro` 并同步修正 dsa_portfolio docstring 第 24-26 行的"防成环"表述（纯机械改动，无行为变化） |
| 4 | `dsa_watch.py:30-37, 248-252` 对照 migration-plan §4.4.3（:223-226） | P2→P3 切换的动态回落与 §4.4.3 二态字面不一致，且契约文档未按 §6（:244）"契约变更必须改本文档并标注日期"修订；行为本身有依据且 `positions_source` 如实上报——属契约文档漂移而非行为缺陷。评审 #4 的建议有两部分：补 §4.4.3 日期标注 + 补一份 P3 实施记录（**后者即本文，已交付**；前者待做，见前置清单） | **合并前在 migration-plan §4.4.3 补日期标注**与动态回落语义 |
| 5 | `dsa_watch.py:715-719` 对照 :393-403 | sync_state 按 round(…,6) 判 synced、对账按 1e-9 容差判改价——重叠区间内"显示已同步但下轮对账被改回"（评审探针：31.0000004 vs 31.0） | 纯展示一致性问题；统一容差即可，非阻断 |
| 6 | `dsa_watch.py:954-958` | premarket "DSA桥可用" fail 文案"旧库不可用, 持仓按空仓处理"是 P2 时代口径；P3 本账非空时实际持仓走 `load_positions()`（:933-936）并不按空仓处理，文案失真 | 随 §4.4.3 修订一并更新文案 |
| 7 | 仓库根 `?? nul` | 工作区残留 Windows 保留名文件（命令重定向产物），非 DSA 交付物 | 删除并加 .gitignore |

### 6.2 风险与运行注意

- **engine 残余缺口**：全新重启后若既不开盯盘页也不手动建分析任务、只等 18:00 定时，当次收尾对账"落盘但引擎未重载"（ExtensionContext 不带 engine，contracts 红线禁改核心）；规则现由 pending 位标记，延至下一次任一 dsa_watch/分析 API 请求（任务轮询路由不捕获，§4.3）或重启生效。**运行缓解（基于代码事实）**：次日开盘前访问一次次日盯盘页即可——任一 dsa_watch/分析路由 handler 首行 `_capture_runtime` → `capture_engine` 检测到 pending 位即立即 `set_rules` 补 flush（`dsa_watch.py:1067-1080`）；手动 `POST /watch-sync/run` 同效。若不执行该动作，新规则要到下次请求或重启才进引擎内存，期间不触发。
- **首次导入与账本故障恢复（写路径首次执行的操作指引）**：`POST /portfolio/import-from-dsa` 对真实旧库的写入此前从未执行，用户首次导入即该写路径首次运行。基于代码事实的操作要点：
  - 导入前置：`GET /portfolio/trades` 确认本账为空（非空 409）；导入前可备份 `data/user_data/dsa_portfolio/trades.jsonl`（若已存在）。
  - 失败形态：导入在锁内复核空账后**单次原子重写**（`import_all` :223-234，临时文件 + `os.replace`，失败清理后重抛）——要么全部落盘、要么账本保持原状，**不会留半截账**；中途崩溃后重试是安全的。
  - 导入成功后想重导：因幂等 409 无法重导，恢复手段为停服后删除/清空 `trades.jsonl`（恢复空账）或逐笔 `DELETE /portfolio/trades/{id}`；核对 `imported/skipped` 与 `GET /portfolio/positions` 后再投入对账。
  - 账本被手工改坏（`GET /portfolio/positions` 500 `OversellError`）：此时 `DELETE` 也可能 422（删除路径同样先重放校验剩余流水），可靠恢复是停服后手工修复 jsonl 对应行或清空重建；jsonl 一行一个 Trade JSON，可直接编辑。
- **触发验收前置（验收前操作项）**：本仓 preferences 实测 `realtime_quotes_enabled=false`（P2 侦查）；"规则正确触发"验收前须先在设置页开启实时行情 + 全市场拉取源，且评估只在交易日 9:30-11:30/13:00-15:00——18:00 同步的规则次日开盘才被首次评估属预期。
- **并行会话冲突窗口**：P2 接管的 dsa_watch.py 若原并行会话恢复活动可能冲突（停笔时其已静默 >40 分钟；P3 会话核实 mtime 稳定、未触发静默协议）。
- **降级语义**：watch-plan 桥/本账都不可用 → 200 全空仓（不 5xx，契约硬要求）；watch-sync 前置失败 → 零写入中止 400（异常≠空值：空自选是合法用户态，异常是故障防误删）；单条规则落盘/删除 OSError 只跳过该条计 skipped，不拖垮整轮。
- **watchlist 无 symbol 格式校验**（services 层存量）：同 6 位码不同后缀在展示层并票；指数票已跳过建规则，当前自选无指数码未触发；根治需核心校验，超出红线。

### 6.3 存量基线

全仓 pytest 1 例失败（psutil Windows 句柄枚举）与 1577 条 ruff 存量告警均非本次引入，与 DSA 文件无调用关系（P1 报告 §5.3 单跑复现与 grep 核实），合并前需维护者对基线表态。
