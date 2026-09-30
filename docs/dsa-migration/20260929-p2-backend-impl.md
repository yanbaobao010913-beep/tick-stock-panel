# DSA P2 后端实施报告（次日盯盘，2026-09-30）

> 范围：迁移计划 §4（P2 契约）——watch-plan 聚合 + 点位→monitor-rules 对账 + 盘前自检 + 对 dsa_analysis 的三处挂接。评审期间 P3 持仓账（`dsa_portfolio.py`）由并行工作流落地并与本模块衔接（持仓来源软切换），相关语义一并记录。
> 执行环境：Windows + Git Bash，后端命令统一 `uv --directory backend run --frozen --extra dev ...` 形态。
> 红线遵守情况：未修改 `backend/app/` 下任何核心文件；未修改任何前端文件（仅只读核实 `frontend/src/custom/dsa/api.ts` P2 类型段与 WatchPlanPage 消费面）；未访问 `D:\Documents\Zai\tick-stock-panel` 生产副本；DSA 旧库全程仅 sqlite URI `mode=ro` 只读（dsa_bridge.py:32 硬编码路径，仅桥只读查询），测试一律 tmp_path mini 库 + monkeypatch `DSA_DB_PATH`，零写入；未 git commit/push；冒烟用临时目录与隔离 DATA_DIR 已清理（见 §5.2）。
> 行号基准：本报告 file:line 以评审终轮代码为准（dsa_watch.py 1155 行）。

## 1. 概述

P2 后端按契约 §4 落地为单扩展模块 `dsa_watch.py`（`EXTENSION_ID="dsa.watch"`）：

- **盯盘清单** `GET /watch-plan`：盯盘集合 = 持仓（来源见下）+ TSP 自选并集（持仓在前 sym6 序、其余按自选序、按 sym6 去重；symbol 统一经 `dsa_analysis._canonical_symbol` 输出后缀点分式）× 每票最新 P1 报告（六字段投影，**字段明列见 §3**）× 现存 `DSA·` 同步规则 + 纯集合比较的 `sync_state` 四态。
- **自动挂线** `POST /watch-sync/run`：报告点位对账成 `DSA·` 前缀 price 规则（只碰前缀规则）；自动触发两路——分析任务收尾钩子 + 调度线程 30min 兜底。
- **盘前自检** `GET /premarket/status`：每交易日 09:10-11:30 窗口内调度线程跑一次四项检查并原子落盘；GET 纯读，永不 5xx、永不内联触发。
- **持仓来源**：经 `load_positions()` 软切换——本账（dsa_portfolio 流水）非空 → FIFO 重放（`positions_source="dsa_portfolio"`）；为空 → 回落 dsa_bridge 只读旧库（`"dsa_bridge"`）。触发条件、风险链与正确路径见 §4.4/§6。

最终状态：P2 交付时点门禁三文件 **133 passed**、DSA 文件 ruff 零告警、`/api/ext/dsa/*` 11 条路由全部注册（工作流门禁记录，§5.1）；评审期间并行 P3 落地后复测为 **15 路径 18 方法**、四 DSA 测试文件 **185 passed**（§5.1 第 3 轮）。**遗留 1 项 medium 评审发现待用户拍板**（持仓票"部分缺点位"语义，§5.3-F3-1），7 项 low 沿袭记录在案。

## 2. 交付清单

| 文件 | 状态 | 职责（file:line） |
| --- | --- | --- |
| `backend/app/custom/dsa_watch.py` | 新增（1155 行） | symbol 工具（`_norm6` :116-128、`_symkey/_rule_id/_rule_name` :131-139）；持仓读取与合并（行级容错 `_clean_position_row` :150-178、fifo/avg 整库二选一 `merge_position_rows` :181-203、桥只读 `_fetch_position_rows`/`load_merged_positions` :206-224、来源软切换 `load_positions` :227-252）；期望规则推导与 diff 纯函数（`derive_expected_rules` :307、`parse_dsa_rule` :354、`diff_rules` :415）；规则构建 `_build_rule` :467（save_one 裸写前置 normalize+validate）；盯盘集合 `_watch_symbols` :503、期望 spec `_build_specs` :531（skip 判据 :553-556）；引擎重载 `_reload_engine` :565（`_ENGINE_FLUSH_PENDING` :96）；对账服务 `run_reconcile` :596-684（`_SYNC_LOCK` 串行、前置读取零写入中止、id 占用防护、单条落盘 OSError 容忍）；sync_state 判定 :703；watch-plan 聚合 `build_watch_plan` :722-793；盘前自检（四检查项 `run_premarket_checks` :914、窗口判定 `_decide_premarket_run` :967、状态文件读写 :798-822）；挂接钩子（`on_task_done` :1000、`_watch_fallback_tick` :1014、`_premarket_tick` :1028、`scheduler_tick` :1054、`capture_engine` :1067、`_capture_runtime` :1083）；三条路由 `setup` :1122-1146 + `startup` :1149-1155（只填运行时，不自起线程） |
| `backend/app/custom/dsa_analysis.py` | 增量（3 处） | 挂接点① `_process_task` 任务置 done 后调 `dsa_watch.on_task_done`（:565-573，函数级导入防循环 + 全吞异常）；挂接点② `_schedule_loop` 循环体追加 `dsa_watch.scheduler_tick`（:733-740）；挂接点③ `create_analysis_task` return 前 `dsa_watch.capture_engine`（:808-815）。（并行会话同期加符号规范化 `_SYMBOL_FORM_RE/_exchange_suffix/_canonical_symbol` :212-260，属 P1 §3.1 注记范畴，与本模块正交） |
| `backend/app/custom/dsa_bridge.py` | **零改动** | 仅 import 复用 `_connect_legacy_ro`（模块属性引用，monkeypatch `DSA_DB_PATH` 可生效）；`GET /api/ext/dsa/health`（:363-371）契约一字未动（loader 集成测试回归断言） |
| `backend/tests/test_dsa_watch.py` | 新增（61 用例） | 分组：A 纯函数（归一/合并/diff/parse/sync_state/窗口门控）、B 对账服务（happy path 全字段断言+engine、幂等、改价保 enabled/created_at、清仓换 entry、移出自选三情形、自选异常零写入、桥不可用中止、no_points/无报告保留、id 占用跳过、指数跳过）、C 持仓桥（端到端/缺库/无表/零写入逐字节/脏行）、D 端点（全形状/桥不可用 200 全空仓/规则读失败降级/sync 计数与 400/多账户合并/自选失败退化）、E premarket（四项/独立故障/fail 文案/stale warn/覆盖 warn/状态回读）、F 挂接（收尾、异常吞、`_process_task` 集成、30min 门控、premarket 当日一次、10 线程并发守恒、capture_engine(None) 不覆盖）、G loader 集成（三新路径 + health 原样 + EXTENSION_ID 合法） |
| `backend/tests/test_dsa_analysis.py` | 增量 | ① `_poll_task` 时限按任务规模动态放大（首响应读 total，`max(timeout_s, total*0.5+5)`，:161-171）；② autouse fixture P2 隔离（stub `dsa_watch.on_task_done` + monkeypatch `DSA_DB_PATH`→tmp 不存在路径 + 复位 dsa_watch 模块态，:46-72） |
| `backend/tests/test_dsa_portfolio.py` | P3 并行新增（36 用例） | 含 P2 切换固化：`test_load_positions_local_first_then_bridge_fallback`、`test_watch_plan_switches_bridge_to_local_ledger`（"桥里的 600460 不再视为持仓"断言，:687-724） |
| `docs/dsa-migration/20260929-migration-plan.md` | 追加 | §4.3「P2 实施拍板（2026-09-30）」11 条（:179-192）+ §4.4.3 修订注（软切换时点，:228） |
| `docs/dsa-migration/20260929-p2-backend-impl.md` | 新增 | 本报告（P2/P3 交界事项与 `20260929-p2p3-backend-impl.md` 互引） |

依赖事实（复用而非重写）：`dsa_bridge._connect_legacy_ro`（mode=ro + busy timeout 3s）；`strategy/monitor_rules.load_all/load_one/save_one/delete_one/normalize/validate`（save_one 裸写，调用方自调 normalize+validate）；引擎重载镜像 `api/monitor_rules.py:46-55 _sync_engine`（含私有 `_reconcile_index_asset_type` 复用）；`fs_utils.atomic_write_text`；`watchlist.list_symbols`；`trading_day.is_trading_day`；`dsa_portfolio._STORE.list_trades/replay_positions`（P3）。

## 3. API 一览（P2 新增 3 路径 3 方法，`/api/ext/dsa` 前缀）

| 方法 | 路径 | 用途与响应语义 | 契约 |
| --- | --- | --- | --- |
| GET | `/api/ext/dsa/watch-plan` | `{generated_at, positions_source, items[]}`。`positions_source` 动态值 `"dsa_bridge"\|"dsa_portfolio"`（软切换如实上报，§4.4）。items=持仓+自选并集（**symbol 通常为后缀点分式**如 `600460.SZ`，**例外：ETF 裸码透传**如 `512400`——`_canonical_symbol` 维表未命中不编造；持仓在前 sym6 序、其余按自选序、sym6 去重）；每 item 八字段 symbol/name(可空)/holding/quantity/avg_cost（**非持仓两项均为 null**）/report（**六字段投影，无报告 null**，字段明列见下）/rules[](rule_id/kind/price/severity/enabled)/sync_state。**report 六字段**（`_project_report` :691-700）：`id`、`created_at`、`operation_advice`、`sentiment_score`、`points`（四键 ideal_buy/secondary_buy/stop_loss/take_profit，均可空）、`phase_decision`（含 action_window/immediate_action/next_check_time/watch_conditions[]/risk_conditions[{kind,text,price}]，原样透传）。**持仓源不可用降级形状**（:731-735，本账/桥均失败时）：`positions={}`、`positions_source="dsa_bridge"`（来源链末端是桥）、items=**自选条目（非空数组）**，每条 `holding=false`、`quantity=null`、`avg_cost=null`，report/rules/sync_state 照常计算（规则照常读盘、sync_state 按空仓语义判定）——整体 200 不 5xx（契约硬要求；该路径未冒烟实测，形状以代码为准）。自选读失败→集合退化为持仓条目仍 200；规则读失败→rules=[]+sync_state 保守 stale | §4.1 |
| POST | `/api/ext/dsa/watch-sync/run` | **请求体无参数**：handler 不读 body（:1132-1136），body 可整体省略、传 `{}` 或任意内容均被忽略；无按 symbol 过滤等可选参数。响应 `{created, updated, removed, skipped}` 四计数同单位（规则条数）。前置读取失败（持仓源不可用/自选读失败/规则存储读失败）→ 零写入中止 + 400 `{"detail"}`（非 5xx） | §4.1 |
| GET | `/api/ext/dsa/premarket/status` | `{date, ran_at\|null, checks[{name,status,detail}]}`；未跑/非当日/损坏 → 今日空态；永不 5xx、永不内联触发 | §4.1 |

既有 10 条方法路由（health/analysis×7/legacy×2）零变化；评审期间 P3 并行新增 portfolio 4 路径 5 方法（positions/trades GET+POST/trades/{id} DELETE/import-from-dsa）。P2 交付时点实测注册 **11 路径 13 方法**，终轮复测 **15 路径 18 方法**（§5.1）。错误格式统一契约 §3.4：非 2xx body `{"detail": "人类可读原因"}`。

## 4. 关键设计决策（拍板取舍与理由）

### 4.1 删除触发器与"缺点位保留"语义

- 三个票级事件按盯盘集合语义落位：**未持仓票移出自选**→删其全部 DSA· 规则；**清仓**（在集合内、不在持仓）→删全部持仓规则并按空仓语义重建 entry（"清仓→删全部"与"空仓→ideal_buy 建 entry"的字面张力取 diff 自洽解）；**持仓票移出自选但未清仓**→规则保留（集合=持仓∪自选的语义推论）。契约 §4.2"移出自选→删其全部"据此收敛，迁移计划 §4.3-1 已标注修订。
- **持仓票无报告或报告完全派生不出规则**时跳过该票点位派生 diff、保留既有规则（`_build_specs` :553-556，防 LLM 围栏解析失败当晚清掉止损线）；sync_state 如实报 no_points/no_report。
- **未决项（medium，待用户拍板）**：skip 判据是票级 all-or-nothing——报告只要派生出任意一条规则就不 skip，部分缺位的 key 仍落入 removes。评审在当前代码实测复现：持仓 600460 + 报告 points={stop_loss:None, take_profit:38.0} → 既有 dsa_600460sh_stop_loss 被删。§4.3-1"报告缺点位保留"按自然读法含部分缺失，与实现"全部缺才保"存在字面张力。**拍板材料（两方案影响面，合并前二选一后改码+补测试）**：

  | | 方案 A：票级全保 | 方案 B：缺位 slot 保留（键级） |
  | --- | --- | --- |
  | 语义 | 持仓票报告存在且**任一**槽位缺失 → 整票 skip_diff（其余键的改价更新也一并暂停） | 仅把"缺位槽位对应的现存键"（stop_loss/take_profit/add → (kind,1)；reduce 按现有 rank 序）从 removes 中豁免；其余键照常 diff 改价 |
  | 改码点 | `_build_specs` :553-556 判据加 `or empty_slots > 0`（一行级改动） | `ExpectedSpec` 加 `preserve_keys` 字段（:268-277）+ `diff_rules` removes 过滤（:415-465）+ `_judge_sync_state` :703-720 比较集扣除 preserved 键 |
  | 补测试 | "部分缺失保留全部既有规则"+"该票改价不生效"两例；调整现有改价用例前置（需点位齐全的报告） | "部分缺失仅保留缺位键、其余键改价生效"+"preserved 键不影响 synced 判定"两例；现有用例基本不动 |
  | 前后端可感知差异 | 部分缺失票 watch-plan 恒 stale（expected≠actual 恒真）；报告改价对该票失效（需点位补齐后的新报告才恢复） | removed 计数不再含缺位键；缺位票其余键正常 updated；sync_state 更准确（其余键同步即 synced）；watch-plan rules[] 展示不变（既有线本就投影） |
  | 代价 | 实现 1 行，但"报告更新→对账改价"契约在部分缺失票上失效，用户须知情 | 实现 3 处（~20 行）+sync_state 语义细化，行为最贴近契约逐条读法 |

  冒烟 §5.2-b 验证的"删报告后 removed=0"是"无报告"侧（两方案下行为一致），不覆盖"部分缺失"侧。原记录见 `20260929-p2p3-backend-impl.md` §6.1-1（该文件 :60/:136）。

### 4.2 持仓合并：fifo/avg 整库二选一（桥路径）

旧库 `UNIQUE(account_id, symbol, market, currency, cost_method)` 使同持仓 avg/fifo 各一行、混读重复计数——实现整库二选一绝不混读：任一行带 fifo 只取 fifo 行，全库无 fifo 才取 avg（:181-203）。依据：P2 侦查 + 评审 mode=ro 探针独立实测（17 行/11 键/6 对双记且双记数值相同/0 仅 avg/5 仅 fifo——fifo 是 avg 的超集，账户 3/4 仅 fifo 行，只取 avg 会丢 5 持仓）。行级容错：TEXT quantity/NULL cost 脏行跳过+warning（:150-178），绝不打穿"桥不可用 200 不 5xx"。不按 `portfolio_accounts.is_active` 过滤（实测 12/17 行挂非活跃账户）。跨账户 quantity 求和、avg_cost 按数量加权。

### 4.3 盯盘集合与 symbol 口径

集合 = 持仓 ∪ 自选（`_watch_symbols` :503-528）：持仓在前（sym6 字典序，输出确定性）、其余按自选序、按 sym6 去重。symbol 经 `dsa_analysis._canonical_symbol` 统一输出后缀点分式（§3.1 符号规范化注记同款）；ETF 裸码（512400/515880/588200 型）维表未命中时文档化透传不编造（dsa_analysis.py:229-235）。自选外持仓照样有条目并建规则（§4.1"持仓股在前"的落实）。`resolve_asset_type=index` 的集合项跳过建规则（000001.SH/SZ 撞码防护）。前端仅展示渲染 symbol（WatchPlanPage :129），类型兼容。

### 4.4 持仓来源软切换（P3 提前落地的过渡策略）

`load_positions()`（:227-252）：本账流水非空 → 整账 FIFO 重放（`"dsa_portfolio"`，桥内其余持仓立即失持）；为空 → 回落桥（`"dsa_bridge"`）；本账读取/重放故障不回落桥（导入后桥是过时旧账），按桥不可用走既有降级。**触发条件是"记第一笔账"，早于 §4.4.3 原"P3 验收"时点**——迁移计划 §4.4.3 已加修订注（2026-09-30）、§4.3 新增条目 11。风险链与正确路径：**开始手工记账前必须先 `POST /portfolio/import-from-dsa` 全量导入**（幂等设计为已有流水 409 拒绝，故导入必须是第一笔动作），否则下次对账按清仓语义删掉桥内其余持仓的 DSA· 规则——详见 §6 遗留风险 4。

### 4.5 skipped 口径（含已记录的口径漂移）

skipped = 缺点位期望槽位数（持仓票 stop_loss/take_profit/secondary_buy 各 1 槽 + reduce 无价每条 1 槽；空仓票 ideal_buy 1 槽仅在有报告时计）+ id 占用/validate 失败/OSError 落盘失败数（:634-664 计入 `id_conflicts`），与 created/updated/removed 同单位（规则条数）。**评审沿袭 low 项**：后三类失败计入 skipped 与 §4.3-3"缺点位槽位"口径有漂移（代码未变，如实记录，未处置）。

### 4.6 premarket 时机拍板

每交易日 **09:10-11:30 窗口内一次**（`_decide_premarket_run` 纯函数：trading_day 未知退化周一~五，镜像 18:00 调度），结果原子落盘 `data/user_data/dsa_watch/premarket.json`；**GET 纯读**，未跑/非当日/损坏 → 今日空态，永不 5xx、永不内联触发。理由：(a) "盘前自检"语义上 ran_at 应是固定盘前时点而非用户首次打开页面的时刻；(b) GET 无 IO 探测开销、无副作用；(c) 与 18:00 调度同款机制（30s tick + 当日一次 + 纯函数判定）实现与测试成本最低；(d) 11:30 后才启动则当日缺检，GET 空态（前端不渲染卡片），可接受。四检查项独立容错（异常→该项 fail，detail 截 120 字）：数据源（首只自选 K 线探针，落后上一交易日 warn）/ 自选K线覆盖（逐票窄区间）/ 同步规则启用（复用 watch-plan 判据，stale→warn，附"持仓不在自选 N 只"）/ DSA桥可用（fail 文案"旧库不可用, 持仓按空仓处理"）。**已记录 low 项**：软切换后该 fail 文案在"本账非空"时失真（故障可能来自本账，:954-958；p2p3 报告 §6.1-6 挂起待改）。

### 4.7 并发与失败语义

对账三入口（任务收尾/调度兜底/手动 POST）经模块级 `_SYNC_LOCK` 串行（monitor_rules 的 save/delete 本身无锁）；前置读取顺序固定（持仓→自选→报告→规则），**任一失败零写入中止**（异常≠空值：空自选是合法用户态=删未持仓票规则，异常是故障=中止防误删）；应用阶段单条规则 validate 失败/落盘 OSError 只跳过该条不拖垮整轮；10 线程并发计数守恒有测试固化。人工规则不进任何写路径（冒烟 cmp 逐字节验证）；外来 DSA· 命名（解析不出 sym6+五 kind）不托管不比对不删；create 前 load_one 查 id 占用（用户改名挤出的规则跳过+warning，不静默覆写、enabled=false 不被复活）。

### 4.8 调度兜底周期与挂接点

- **30min 门控兜底**：`_watch_fallback_tick` 以 `time.monotonic()` 距 `_last_watch_sync` ≥1800s 才跑，跑完/尝试完都刷新时钟；**`_last_watch_sync` 初值 `0.0` 为 falsy，不拦首拍——冷启动后首个 +30s 拍必真实执行一次兜底对账**（engine 未捕获时打 §4.8 所述 warning，属预期非异常）。复用 dsa_analysis 调度循环（30s 一拍）不自起线程（单线程单睡眠；loader 按 sorted 次序 startup，dsa_watch 排后、首拍前运行时已就绪）。周期取 30min：对账幂等，兜底只需明显快于"隔日失效"又不至于空转。
- **任务收尾钩子**：仅终态 done 任务触发（放在 `task["status"]="done"` 之后、外层 try 之内；on_task_done 内部再全吞异常双保险）；任务级 failed（流水线崩溃）不触发，30min 兜底覆盖，属设计取舍。对账在 worker 线程内联，成本为估算值（glob + 几十个小 JSON + 一次 mode=ro 查询 + 少量文件写，百毫秒量级；桥不可用最坏 +3s busy timeout）。
- **引擎生效链**：写盘≠生效（引擎无周期重载），对账落盘后镜像 `_sync_engine` 调 `engine.set_rules`（0 变更也执行治愈陈旧引擎）；engine 引用在请求 handler 捕获（挂接点③ + 三 handler 首行 `_capture_runtime`）；有变更而未捕获时置 `_ENGINE_FLUSH_PENDING`（:96, :565-583），任一 dsa_watch/分析路由捕获到 engine 即刻补 flush——后台写盘的规则不再依赖 ≤30min 兜底窗口。

### 4.9 对 P1 报告 §6.2 建议的作废声明

P1 报告 §6.2 建议"对账规则打 `source="dsa_report_sync"` 标"作废：TSP 规则无 source 字段，name 前缀 `DSA·` 是唯一托管标记。后任勿再引入。

## 5. 验证结果（只写实际执行过的命令与真实输出）

### 5.1 门禁各轮（时间序）

| 轮次 | 命令 | 结论 | 出处 |
| --- | --- | --- | --- |
| ① P2 交付门禁 | `pytest tests/test_dsa_watch.py tests/test_dsa_analysis.py tests/test_dsa_legacy_bridge.py -q`（--frozen --extra dev） | ✅ **133 passed** | 工作流门禁记录 |
| ① 同轮 | `ruff check app/custom/dsa_watch.py app/custom/dsa_bridge.py app/custom/dsa_analysis.py tests/test_dsa_watch.py tests/test_dsa_analysis.py tests/test_dsa_legacy_bridge.py` | ✅ DSA 文件**零告警**（All checks passed） | 工作流门禁记录 |
| ① 同轮 | `python -c` 枚举 `app.main:app` 的 `/api/ext/dsa` 路由 | ✅ **11 条路由全部注册**（11 路径 13 方法：旧 8/10 + watch-plan、watch-sync/run、premarket/status 3/3） | 工作流门禁记录 + 实现方同轮枚举一致 |
| ② 评审第 1 轮复跑（文档同步后） | 同 ① 形态 pytest 三文件 / ruff 六文件 | ✅ **138 passed**（收集项 61+57+20；62 warnings 均为 watchlist.py:216 `datetime.utcnow()` 存量 DeprecationWarning）/ ✅ All checks passed | 实现方评审期间实跑 |
| ③ 评审第 2 轮复跑（P3 并行落地后，扩口径） | 同形态 `pytest` **四文件**（+test_dsa_portfolio.py）/ `ruff` **八文件**（+dsa_portfolio.py、test_dsa_portfolio.py） | ✅ **185 passed**（收集项 **61+57+20+47**；portfolio 文件 36 个测试函数含 2 处 `parametrize` 展开为 **47 项**——36+11，此处即 174→185 的 11 例来源；63 warnings 同上）/ ✅ **All checks passed!**（收集数于本报告撰写时以 `pytest --collect-only -q` 四文件逐一实测：61/57/20/47） | 实现方评审期间实跑 |
| ③ 同轮 | 同 ① 形态路由枚举 | ✅ **15 路径 18 方法**（+portfolio 4 路径 5 方法） | 实现方评审期间实跑 |
| ③ 同轮 | 旧库只读探针（temp 脚本，sqlite URI `file:...?mode=ro`）：`SELECT MIN/MAX(updated_at),COUNT(*) FROM portfolio_positions WHERE quantity>0` | min=2026-08-19 23:29:29，max=**2026-09-29 17:06:58**，rows=17（旧库持仓已被 DSA 应用恢复维护） | 实现方评审期间实跑 |
| — | 全仓 `pytest -q` / `ruff check app tests` | ⏸️ **未运行**——任务口径明示不跑（存量红与 DSA 无关）；基线以 P1 报告 §5.3 为准（1 例 psutil 红 + 1577 存量告警，均与 DSA 无调用关系） | — |

开发过程如实记录：首跑 test_dsa_watch.py 曾 12 failed，其中 **1 个真实实现 bug**（`diff_rules` 现存规则索引 3 元组建表却用 2 元组查询→既有规则永不匹配、每轮重复 create，幂等/改价用例抓住）已修复并有防回归用例；其余为断言与桩行为对齐。评审期间实现经并行改版（fifo 整库二选一、盯盘集合=持仓+自选、pending flush、P3 软切换），测试同步改写扩充至 61 例。

### 5.2 冒烟（工作流侧记录，本轮如实引用；本报告撰写者未复跑）

【启动】按 P1 报告 §5.2 先例起 uvicorn 于 127.0.0.1:8914。默认 DATA_DIR 首次启动失败：main.py:399 lifespan 报 MiningProcessLockError（存量 ：8000 开发服务 `--reload` 持有 `data/.mining_process.lock`，非本任务进程、不触碰）。改用隔离 `DATA_DIR=.../data-smoke-8914`（从真库拷入 watchlist.parquet 41 只、preferences.json、dsa_reports 60 份、capabilities.json；不含 secrets.json）重启成功。首个后台实例 ~3 分钟被宿主回收（exit 127、无 Python 异常），经 Start-Process 脱离重启后全部检查在该实例复跑通过。

【必测三端点】
- `GET /api/ext/dsa/watch-plan` → 200：`{generated_at:"2026-09-30T02:2x", positions_source:"dsa_bridge", items:41}`；10 只持仓票在前 sym6 序（000636.SZ 100@58.5、002383.SZ 1400@8.307、002837.SZ 200@58.126——avg/fifo 双记去重未翻倍、512400 1500@2.063、515880 15500@0.6798、588200 12000@1.1958 跨账户 4000+8000 加权，与 mode=ro 直查旧库 17 行一致、600105.SH 300@42.034、600460.SH 300@34.648、601318.SH 500@54.938、603986.SH 100@435.352）；item 八字段齐全，无报告票 sync_state=no_report、rules=[]。ETF 裸码（512400/515880/588200）系 `_canonical_symbol` 文档化透传（不编造），契约 §4.3-6 明文允许。
- `POST /api/ext/dsa/watch-sync/run` body `{}` → 200 `{"created":0,"updated":0,"removed":0,"skipped":0}`（盯盘集内无报告无规则，零变更符合预期）。
- `GET /api/ext/dsa/premarket/status` → 200 `{"date":"2026-09-30","ran_at":null,"checks":[]}`（02:2x 在 09:10-11:30 窗口外；调度日志 30s 一拍仅记 is_trading_day 退化 INFO、未写盘——符合 §4.3-7 永不内联触发）。

【P1 回归】`GET /analysis/reports` → 200 total=60（与拷入一致）；`GET /legacy/reports?limit=1` → 200 total=1418（首条 id=1463 era B，与 P1 口径一致）；`GET /analysis/schedule` → 200 `{"enabled":true,"hour":18,"minute":0}`。

【端到端写路径（隔离 DATA_DIR 内）】先经核心 POST /api/monitor-rules 建人工规则 manual_smoke_001（非 DSA· 前缀）并逐字节快照。a) 持仓票路径：落假报告 r_20260930_600105.SH_901（四点位+reduce 带价 41.5）→ sync → `{"created":4}`；存储文件 + 核心 GET /api/monitor-rules + watch-plan 三方核对：DSA·600105·{stop_loss|take_profit|add|reduce·1}，conditions 全为 field=close、<=39.1 critical / >=46.8 critical / <=40.2 warn / <=41.5 warn、cooldown_seconds=86400、symbols=["600105.SH"]、enabled=true、asset_type=stock；watch-plan synced 且 rules 四条投影正确；幂等复跑 0/0/0/0；人工规则逐字节不变（cmp 通过）。b) 删该假报告再 sync → removed=0：持仓票无报告按 §4.3-1 设计保留既有线（skip_diff），watch-plan 如实报 no_report——"removed>0 且规则清零"在持仓票路径不成立，属既定拍板语义（有固化测试），非缺陷。c) 未持仓票路径（成立路径）：落假报告 r_20260930_600584.SH_902（600584.SH 在自选、非持仓）→ sync `{"created":1}`（DSA·600584·entry：close<=39.8、info、86400、symbols=[600584.SH]）→ watch-plan synced → 删假报告 → sync `{"removed":1}` 且该票 DSA· 规则清零。d) 收尾经核心 DELETE 清掉 600105 四条，终态 monitor_rules 仅剩人工规则（逐字节不变）。【引擎生效链】服务日志：启动 +30s 兜底 tick 在无请求时打 warning 落盘。**机理核实（本报告撰写时读码，dsa_watch.py:1014-1026）**：`_last_watch_sync` 初值 `0.0` 为 falsy，`if _last_watch_sync and ...` 门控不拦首拍——**冷启动首个 +30s 拍必真实执行一次兜底对账**（非跳过）；该时点冒烟库盯盘集内无 DSA· 规则、无匹配报告 → diff 全 0 → `changed=False` → `_reload_engine` 走 else 分支打 warning「规则已落盘, monitor 引擎未重载 (engine 引用未捕获, 将在下次 dsa_watch/monitor API 请求或重启后生效)」（:576-580）。**该 warning 属预期输出**（0 变更也提醒引擎未重载），非异常信号；若当轮确有变更则会改打置 pending 的另一条 warning（:570-575）。冒烟后续手动 POST 经 handler 捕获 engine 后无重载告警，created=4 即时进引擎。【清理】taskkill 进程树、netstat 确认 8914 释放、无本任务残留；data-smoke-8914 整目录删除；真库核对 monitor_rules 0 文件、dsa_reports 60 份、无 dsa_watch/dsa_portfolio 目录（与开工前一致）；/tmp 临时文件已删；git 工作区 18 条与开工时一致；存量 ：8000 服务 health 200 未受影响；旧库全程 mode=ro 零写入。

【"全量 no_report / sync created=0"为何符合预期（零交集探针核实）】冒烟从真库拷入的 dsa_reports 60 份报告与 watchlist 41 只自选零交集——本报告撰写时以只读探针复测：60 份报告 distinct symbol 仅 **1 个**（`600519.SH`），而真库 watchlist.parquet 41 只的 sym6 集不含 600519，交集为 **EMPTY**。故"全量 no_report、sync created=0"是数据现状的必然结果（报告-票匹配按 `_norm6` 归一 join，逻辑本身由 matched 场景单测与 §5.2 端到端 a/c 两条写路径覆盖），非匹配缺陷；也意味着该次冒烟未覆盖"报告命中自选票"的 watch-plan 展示与对账联动（该面由单测与端到端假报告路径覆盖）。

### 5.3 代码评审发现与处置

> 行号归属约定：凡外部文件引用一律显式标注文件名与行号；未带文件名的行号属本文件。外部锚点如下——"迁移计划"= `20260929-migration-plan.md`（§4.3 十一条位于该文件 :179-196、§4.4.3 修订注位于 :228）；"p2p3 报告"= `20260929-p2p3-backend-impl.md`；P1 报告 = `20260929-p1-backend-impl.md`（全仓基线在其 §5.3，该文件 :98-101）。**这些外部锚点未随本文档复制，按"只读本文档"口径只能采信其存在，已给出定位行号供核验。**

| 轮次 | 发现 | 处置 |
| --- | --- | --- |
| 第 1 轮（medium，已闭环） | 契约文档落后于并行改版：fifo 整库二选一、盯盘集合=持仓+自选并集/后缀式 symbol、自选读失败退化持仓条目、updated_at 已到 09-29 | ✅ 迁移计划 §4.3 条目 1/5/6/9 + §4.4.3 修订注 + 本报告同步改到与代码一致并标注日期（外部锚点见上，可按行号核验）；targeted pytest 138 passed + ruff 绿 |
| 第 2 轮（medium，已闭环） | P3 持仓来源软切换未落契约文档、与迁移计划 §4.4.3 原"P3 验收前保持 dsa_bridge"时点冲突 | ✅ 采文档化修法：迁移计划 §4.4.3 修订注（:228）+ §4.3 条目 11（:192）+ 本报告 §4.4/§4.8/§6 风险 4；代码方向经评审核实成立（fifo 超集实测、前端仅展示渲染），保留原实现与固化测试 |
| 第 3 轮（当前，1 medium 未决 + 7 low） | **F3-1（medium，待用户拍板）**：持仓票"部分缺点位"时缺位规则仍被删（skip 判据 all-or-nothing，评审实测复现：points={stop_loss:None, take_profit:38.0} → 既有止损规则入 removes）；§4.3-1 自然读法含部分缺失——拍板材料已内联本报告 §4.1。**F3-2（low）**：skipped 口径漂移（OSError/validate 失败计入 skipped，dsa_watch.py:634-664）。**F3-3（low）**：手改规则文件可使 rules[].severity 输出 null（api.ts:127 非可空；dsa_watch.py:388）。**F3-4（low）**：`_latest_reports()` 目录级 IO 异常残口未包 try/except（dsa_watch.py:485；契约点名三类失败均已降级）。**F3-5（low）**：load_positions 本账 sym6 归并是字典覆盖非加权（dsa_watch.py:241-247；跨所撞码需手工误录才触发，流水无损；docstring :233-234 与实现不符）。**F3-6（low）**：premarket"DSA桥可用"fail 文案在软切换后失真（dsa_watch.py:954-958）。**F3-7（low）**：旧库只读连接双份实现（dsa_portfolio.py:324-334 自带，待收敛复用 dsa_bridge._connect_legacy_ro）。**F3-8（low）**：仓库根未跟踪杂物文件 `nul`（Windows 重定向产物，非本次实现产生，红线禁删） | ❌ **均未改码**：F3-1 待拍板（拍板材料见本报告 §4.1；原记录在 p2p3 报告 §6.1-1，该文件 :60/:136）；F3-2/3/4/5/6/7 已如实记录于本报告 §4.5/§6/§7 与 p2p3 报告 §6.1（该文件 :67/:75/:108/:137/:150——外部锚点，未随附），归属后任；F3-8 红线约束不可删 |

### 5.4 未覆盖（如实声明）

冒烟任务声明的五项：① 持仓票"清仓/移出自选→删全部规则"实时触发未实测（桥只读不可模拟清仓、无本账可清；语义有固化单测但冒烟任务不跑套件）；② 桥不可用降级路径未实测（需改 DSA_DB_PATH 或移走旧库）——其响应形状已在 §3 按 `dsa_watch.py:731-735` 明列（items=自选条目全 holding=false、positions_source="dsa_bridge"），但未经运行验证；③ premarket 四检查项真实执行未发生（02:2x 窗口外，仅验空态与永不内联触发）；④ 规则进引擎后的告警实际评估未测（非交易时段）；⑤ pytest/ruff 套件不在冒烟任务范围。另有全仓门禁未跑（§5.1 尾行）。本报告为文档修订：本轮实际执行的核实命令仅为只读操作——`pytest --collect-only -q`（四文件逐一，得 61/57/20/47）、真库只读探针两则（updated_at 区间；报告 symbol ∩ 自选 sym6 = EMPTY）、代码 grep/sed 读码，未改动任何代码、未跑完整测试套件。

## 6. 契约偏差与理由

1. **删除触发器收敛**（§4.3-1 拍板）：仅未持仓移出自选/清仓两票级事件删规则；no_points/no_report 保留既有规则。理由：契约删除触发器原文不含"报告缺点位"，防 LLM 闪失清线。**残留张力见 §4.1 未决项（部分缺失场景）**。
2. **items 域 = 持仓∪自选、symbol 后缀点分式**（§4.3-6 修订）：自选外持仓照样有条目建规则。理由：§4.1"持仓股在前"的落实 + 前后端 symbol 已统一规范化口径（§3.1 注记）；前端仅展示渲染，类型兼容。
3. **watch-sync 桥不可用 400 detail**（§4.3-2）：§4 头降级条款只约束 watch-plan 不 5xx；同步静默回 0 计数比报错更误导。
4. **skipped 语义**（§4.3-3）：缺点位期望槽位数 + id 占用/校验/落盘失败数，同单位规则条数（口径漂移已记录，§4.5）。
5. **多条 reduce**：name 加序号 `DSA·{symbol}·reduce·{n}`、按 price 升序 rank、同价 round 到分去重（契约命名模板最小扩展）。
6. **持仓合并整库二选一**（§4.3-5 修订）：fifo 优先于"avg 优先"初版拍板——fifo 是实测超集，只取 avg 会丢账户 3/4 的 5 持仓。
7. **持仓来源软切换**（§4.3-11 + §4.4.3 修订注）：本账非空即切换，早于 §4.4.3 原"P3 验收"时点；positions_source 动态值。理由与风险链见 §4.4/§6-4。
8. **premarket 时机/存储**（§4.3-7）：09:10-11:30 窗口调度跑一次 + GET 纯读 + `data/user_data/dsa_watch/premarket.json` 机器态（不进 preferences.json，避免污染用户偏好键空间）。
9. **sync_state 纯集合比较**（§4.3-8）：不建 last_synced 状态文件（点位集合相等即 synced，状态文件会成第二事实源）；enabled 不参与（手动停用仍 synced）；规则读失败降级 stale。
10. **P1 §6.2 作废**（§4.3-10）：`source=` 标建议作废，前缀 `DSA·` 唯一托管标记。
11. **smoke 任务书偏差澄清**：任务书期望"持仓票删报告后 sync removed>0 且规则清零"，实测 removed=0——持仓票无报告保留既有线是 §4.3-1 既定拍板（有固化测试），非缺陷；未持仓票路径 removed=1 成立（§5.2-c）。

## 7. 遗留风险

1. **未决 medium**：持仓票"部分缺点位"清线（§4.1/§5.3-F3-1）——合并前需用户拍板二选一（票级全保 / 缺位 slot 保留）后改码+补测试。
2. **软切换清线风险**：本账非空后桥内其余真实持仓立即失持，下次对账删其全部 DSA· 规则——半截记账/漏导入即触发；缓解=先 import-from-dsa 全量导入（409 幂等守护使其必须是第一笔动作）；排障入口=查 watch-plan 的 `positions_source`。
3. **engine 捕获残余缺口**：全新重启后既不开盯盘页也不手动建任务、只等 18:00 定时 → 当次收尾对账落盘但引擎未重载；pending 位使其延至下一次任一 dsa_watch/分析 API 请求（捕获即补 flush）或重启（机制边界：ExtensionContext 不带 engine，contracts.py:18-22；红线禁改核心）。
4. **触发验收前置**：本仓 preferences 实测 `realtime_quotes_enabled=false`（侦查A）——"规则正确触发"验收前须开启实时行情 + 全市场拉取源；评估仅在交易日 9:30-11:30/13:00-15:00，18:00 同步的规则次日开盘才被首次评估属预期。
5. **评审 7 项 low**（§5.3-F3-2~8）：skipped 口径漂移、severity 手改文件可 null、`_latest_reports` IO 残口、本账撞码字典覆盖+docstring 失真、premarket fail 文案失真、旧库只读连接双份、仓库根 `nul` 杂物——均已记录归属后任（p2p3 报告 §6.1 多处挂起）。
6. **watchlist 无 symbol 格式校验**：同 6 位码异后缀在 items 层按 sym6 并票展示（规则层指数跳过防护）；根治需核心校验，超出红线。

## 8. 前端联调点（供前端方核对）

1. **symbol 现为后缀点分式**（如 `600460.SZ`、ETF 裸码透传如 `512400`）：React key 仍唯一（sym6 去重保证）；展示原样即可，勿自行截断。
2. **`positions_source` 为动态值** `"dsa_bridge" | "dsa_portfolio"`：原样展示，勿按常量 "dsa_bridge" 硬编码判断。
3. `rules[].kind` 必须恰为五字面量（stop_loss/take_profit/add/reduce/entry）；`price` 恒为非 null number；`severity` 封闭三态 **`info` / `warn` / `critical`**（monitor_rules.py:37 SEVERITIES）——**手改规则文件边缘场景可能输出 null**（评审 F3-3，前端可做 `?? 'info'` 兜底）；`enabled` 驱动置灰。
4. `sync_state` 四态语义：synced=点位集合相等（enabled 不参与，手动停用仍 synced，前端置灰展示如实）；stale=集合不等或规则状态未知（含规则读失败降级）；no_points=报告存在但无可适用点位；no_report=无报告。
5. `report` 为 null 时无"看报告"按钮；id 恒为报告库真实 id。
6. `generated_at` 为 naive 北京墙钟秒精度 ISO（`new Date()` 本地时区解析，用户机 +08:00 正确）。
7. premarket：`checks=[]` 不渲染卡片；`ran_at` 类型有但页面未消费；`status` 封闭三态 **`ok` / `warn` / `fail`**；检查项 `name` 为后端写死的固定中文字符串，恰为 **`"数据源"`、`"自选K线覆盖"`、`"同步规则启用"`、`"DSA桥可用"`** 四者（dsa_watch.py:845-958 一带硬编码；前端按 name 定制图标/文案以此为准，自选读取失败时"数据源/自选K线覆盖"两项合并为同文案 fail）；非 ok 项展开 detail（≤120 字符）。
8. watch-sync 成功后 invalidate watch-plan；四计数 toast 语义自洽（skipped 含缺点位槽位与跳过数）。
9. watch-plan 查询 retry:1 非 quiet——后端各降级路径均 200（持仓源不可用=items 为自选条目全 `holding=false` 且 `positions_source` 降为 `"dsa_bridge"`、自选失败=集合退化为持仓条目、规则失败=rules 空降级，详见 §3 降级形状），整页 isError 仅应出现在扩展未注册场景。
