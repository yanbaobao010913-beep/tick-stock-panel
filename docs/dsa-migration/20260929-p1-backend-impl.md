# DSA P1 后端实施报告（2026-09-29）

> 范围：迁移计划 §2（P1 数据契约）+ §3（P1 API 契约，含 §3.3 历史只读桥接）+ 18:00 定时分析。
> 执行环境：Windows + Git Bash，后端命令统一 `uv --directory backend run ...` 形态。
> 红线遵守情况：未修改 `backend/app/` 下任何核心文件；未修改任何前端文件（仅只读核实了前端消费方一行）；未访问 `D:\Documents\Zai\tick-stock-panel` 生产副本；DSA 旧库 `D:\Documents\daily_stock_analysis\data\stock_analysis.db` 全程仅以 sqlite URI `mode=ro` 只读访问，无任何写入；未执行 git commit/push，未删除文件。

## 1. 概述

P1 后端按迁移计划"三条已拍板决策"落地：

- **分析走扩展重做**：新增 `dsa_analysis.py` 扩展模块，输出 DSA 式实战报告（四点位 + 操作建议 + 盯盘条件），不动 TSP 核心"四维分析不出买卖建议"的设计；数据组装复用 `stock_analyzer` / `levels`，不平行实现第二套。
- **DSA 历史可看**：`dsa_bridge.py` 在保留原 health 探针（代码零改动）的基础上新增 §3.3 只读桥接端点，旧数据不搬迁、不写入。
- **一切走二开机制**：两个模块均为 `setup(registrar)` + `startup(context)` 扩展形态，经 loader 自动发现注册（有集成测试断言），`backend/app/` 核心目录零改动。

最终状态：`/api/ext/dsa/*` 共 8 个路径 10 条方法路由全部注册；两个定向测试文件 69 用例全部通过；DSA 相关文件 ruff 零告警。全量基线（全仓 pytest / ruff）存在**存量**失败（1 例 pytest、1577 条 ruff），经核实均与 DSA 文件无关（见 §5）。

## 2. 交付清单

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `backend/app/custom/dsa_analysis.py` | 新增 | P1 分析模块全量：DsaReportStore 报告存储（:107-196）、LLM 提示词（:206-249）、输出解析与"不许编"规范化（:295-409）、任务表 + 单工作线程（:416-512）、单票流水线（:527-573）、18:00 定时调度（:580-666）、HTTP 路由（:697-781）、startup 钩子（:784-790） |
| `backend/app/custom/dsa_bridge.py` | 增量 | 原 health 探针原样保留（:363-370，前端在用的契约未动）；新增 §3.3 只读桥接：列值映射（:122-140）、created_at 转 ISO（:106-119）、三时代 markdown 拼装纯函数 `build_legacy_markdown`（:210-347）、风控抑制注记（:192-207）、只读连接与降级（:354-357、:390-401、:409-424） |
| `backend/tests/test_dsa_analysis.py` | 新增 | 49 用例：存储（含原子写失败、损坏跳过、路径穿越 fail-closed）、解析（含 `test_parse_structured_never_fabricates_missing_values`）、任务 API、报告 API、schedule 校验、调度纯函数与 tick、loader 集成（断言 `/api/ext/dsa/health` 契约原样保留，:675-683）。LLM 与数据全 mock，目录 `tmp_path` 隔离 |
| `backend/tests/test_dsa_legacy_bridge.py` | 新增 | 20 用例：tmp_path 构造按侦查 17 列结构的 mini sqlite，monkeypatch 注入 `DSA_DB_PATH`（:210-219），**不碰真实旧库**；覆盖列表过滤/分页/坏行跳过、三时代 markdown、风控抑制注记、库缺失/损坏/EXCLUSIVE 锁定降级（锁的是临时副本）、health 不回归 |
| `docs/dsa-migration/20260929-migration-plan.md` | 仅追加一节 | §3 末尾追加"### 3.5 分析调度偏好(2026-09-29 补充)"（schedule 端点契约补充），其余内容零改动 |
| `docs/dsa-migration/20260929-p1-backend-impl.md` | 新增 | 本报告 |

依赖事实（复用而非重写）：`stock_analyzer.py:41 _load_kline` / `:55 _clean_rows` / `:79 _load_financials` / `:298 _KLINE_KEEP_COLS`；`levels.py:566 compute_levels` / `:595 summarize_levels`；AI 走 `app/services/ai_provider.py` 的 `generate_ai_text` / `ai_configured`；自选取 `app/services/watchlist.list_symbols`；交易日取 `app/services/trading_day.is_trading_day`；偏好走 `app/services/preferences`。

## 3. API 一览（全部在 `/api/ext/dsa` 前缀下）

本轮以 `python -c` 枚举 `app.main:app` 实测注册结果，与迁移计划 §3 期望清单逐条一致（8 路径 10 方法）：

| 方法 | 路径 | 用途 | 来源 |
| --- | --- | --- | --- |
| GET | `/api/ext/dsa/health` | 旧库可见性探针，返回 `{status, dsa_db_found, analysis_history_count}` | P0 已有，本期零改动 |
| POST | `/api/ext/dsa/analysis/tasks` | 创建批量分析任务；symbols 1~50 只、去重保序、mode `full\|brief`（默认 full），非法 400 | 契约 §3.1 |
| GET | `/api/ext/dsa/analysis/tasks/{task_id}` | 任务进度轮询（前端 2s 间隔），返回 `{task_id, status, total, done, items[]}`，item 含 `report_id`/`error` | 契约 §3.1 |
| GET | `/api/ext/dsa/analysis/reports` | 报告库列表：`symbol` 过滤 + `limit`（≤100）/`offset`，按 `created_at` 倒序，返回 ReportSummary（不含 markdown/phase_decision） | 契约 §3.2 |
| GET | `/api/ext/dsa/analysis/reports/{report_id}` | 报告详情 ReportDetail（含 `markdown` 与 `phase_decision`） | 契约 §3.2 |
| DELETE | `/api/ext/dsa/analysis/reports/{report_id}` | 删除报告，返回 `{ok: true}`，不存在 404 | 契约 §3.2 |
| GET | `/api/ext/dsa/analysis/schedule` | 读取定时偏好，无配置时返回默认 `{enabled: true, hour: 18, minute: 0}` | 本期补充，已记入计划 §3.5 |
| PUT | `/api/ext/dsa/analysis/schedule` | 写入定时偏好；`enabled/hour/minute` 类型与范围严格校验（`type()` 精确区分，`1`/`"18"` 一律拒绝），非法 400，合法 200 回显 | 本期补充，已记入计划 §3.5 |
| GET | `/api/ext/dsa/legacy/reports` | DSA 历史列表（只读）：`symbol` 过滤 + `limit`（≤100）/`offset`，`created_at` 倒序；旧库缺失/锁定/损坏降级为 `{total: 0, items: []}`，不抛 5xx | 契约 §3.3 |
| GET | `/api/ext/dsa/legacy/reports/{report_id}` | DSA 历史详情：LegacyReportSummary + `markdown`（三时代拼装）；旧库不可用 404 `{"detail": "旧库不可用"}` | 契约 §3.3 |

错误格式统一走契约 §3.4：非 2xx 时 body 为 `{"detail": "人类可读原因"}`。

## 4. 关键设计决策

### 4.1 报告存储：自建 DsaReportStore（每报告一个 JSON）

`data/user_data/dsa_reports/` 下每报告一个 JSON 文件（文件名即 id），临时文件 + `os.replace` 原子写（`dsa_analysis.py:84-92`），进程内锁串行化读写；单文件损坏跳过并记 warning，不拖垮列表（:147-166）；报告 id 限 `[A-Za-z0-9._-]+` 白名单，含路径字符一律 fail-closed 返回不存在（:100-104），杜绝路径穿越。

**未复用核心 `JsonReportStore`**（`json_report_store.py:29`）：其"单文件 + 条数上限裁剪"设计与报告归档语义冲突——报告库不设上限，且 P2 要按 symbol 读最新报告做盯盘对账；裁剪历史会破坏 P2 输入。自建存储保持与核心一致的原子写 + 锁语义，此取舍已在模块 docstring（`dsa_analysis.py:7-11`）说明。id 规则 `r_{yyyymmdd}_{symbol}_{三位序号}`，同日同 symbol 递增（:121-131）。

### 4.2 任务模型：内存任务表 + 模块级单工作线程串行队列

任务态仅在内存（重启丢任务，可接受），模块级单 daemon 工作线程串行消费 queue（:464-483），避免并发打爆 LLM 配额——这是与 DSA 原版一致的串行语义。单票失败只记 item 级 `error` 不落盘报告，不拖垮整任务（:502-506）；全部 item 终态后任务置 `done`，仅流水线自身崩溃才置 `failed`（:507-512）。

**契约偏差 #1（done 计数口径）**：契约示例（1 个 done item + 1 个 failed item 显示 `done: 1`）按"仅成功"计数；实现按**终态**计数（done+failed，:454）。取此口径的原因是前端实际消费方 `frontend/src/custom/dsa/pages/AnalysisCenterPage.tsx:146` 用 `task.done/task.total` 画进度百分比，终态计数才能在任务结束时走满 100%。已用测试 `test_task_item_error_does_not_kill_task`（`test_dsa_analysis.py:449-462`）固化该语义，docstring（`dsa_analysis.py:13-15`）有说明。**待前端方知悉确认。**

### 4.3 LLM 输出解析：解析容错 + 硬性"不许编"

取**最后一个** ```json 围栏块解析（:391-409）；解析成功时把该机器块从 markdown 正文剥离，结构化字段单独入库（**契约偏差 #2**：契约只写"markdown=完整报告"，四点位/操作建议/盯盘条件的自然语言版仍在正文，仅剥离已成功解析的机器块；解析失败场景保留原始全文并置空结构化字段）。字段级容错（:372-388）：`sentiment_score` 越界（如 150）不钳制直接置空，零分与字符串数字合法；四点位只接受有限正数（:321-324），空格串/负数/不可解析一律 `None`；`phase_decision` 各子字段逐项清洗，整体无有效内容则整段置 `None`（:338-369）。"不许编"有专门测试 `test_parse_structured_never_fabricates_missing_values`（`test_dsa_analysis.py:277-290`）。LLM 调用一次 `generate_ai_text`（工作线程内 `asyncio.run`，timeout 300s，`dsa_analysis.py:559-561`）。

### 4.4 定时调度：默认开 + 无 Key 跳过 + 纯函数判定

偏好键 `dsa-analysis-schedule`（契约 §2.2 建议键），默认 `{enabled: true, hour: 18, minute: 0}`（:580-581）；历史偏好缺字段/类型不对时回退默认（:586-599）。daemon 调度线程 30s 一拍（:660-666），判定逻辑抽为纯函数 `_decide_scheduled_run`（:602-629），顺序：enabled → HH:MM 匹配 → 当日未跑 → 交易日（`is_trading_day` 探测失败为 `None` 时退化为周一~五并记日志）→ `ai_configured` → 自选非空。**未配置 AI Key 时到点以 `no_ai_key` 跳过、不建任务、不烧配额**（有专门测试 :639-651）；触发时对全量自选创建 `mode=full` 任务，当日只跑一次（内存 last-run 日期）。PUT 端点严格校验（:762-779）：`type(x) is bool/int`，拒绝 `1`/`"18"` 等近型值，非法一律 400 而非 422。

### 4.5 历史桥接：mode=ro 只读 + 契约降级 + 列值唯一真源

每次请求新建 sqlite 连接，URI `file:...?mode=ro` + busy timeout 3s，`contextlib.closing` 用完即关（`dsa_bridge.py:354-357`），**绝不写入旧库**；`sqlite3.Error/OSError` 降级为列表 `{total: 0, items: []}` / 详情 404 `{"detail": "旧库不可用"}`（:394-396、:415-417），不抛 5xx。列值映射按侦查结论：id `str()` 化（库内 INTEGER、前端契约 string）、code 原样作 symbol、created_at 空格换 T 输出秒精度 naive ISO 北京墙钟（与 dsa_analysis 口径一致，:106-119）；id/code 缺失视为损坏行跳过且不计入 total。四点位以列值为唯一真源（侦查对全部 1418 行逐行比对 JSON 无一不一致）；理想买点被风控抑制时以 `suppressed_by_guardrail` + `ideal_buy_presuppression` 在 markdown 注记原值（:192-207）。`build_legacy_markdown` 覆盖三个时代（era B dashboard 结构 / market_review 用 JSON `news_summary` 兜底 `raw_response` / era A 与 raw_result NULL 行退化为列值），任何输入不返回空正文（:210-347）。

## 5. 验证结果

### 5.1 本轮实际执行（本报告撰写时逐条运行）

| # | 命令 | 结论 |
| --- | --- | --- |
| 1 | `uv --directory backend run --frozen pytest tests/test_dsa_analysis.py -q` | ✅ **49 passed** |
| 2 | `uv --directory backend run --frozen --extra dev pytest tests/test_dsa_analysis.py -q` | ✅ **49 passed in 4.18s** |
| 3 | `uv --directory backend run --frozen --extra dev pytest tests/test_dsa_legacy_bridge.py -q` | ✅ **20 passed in 5.13s** |
| 4 | `uv --directory backend run --frozen --extra dev ruff check app/custom/dsa_analysis.py app/custom/dsa_bridge.py tests/test_dsa_analysis.py tests/test_dsa_legacy_bridge.py` | ✅ **All checks passed!**（DSA 4 文件零告警） |
| 5 | `uv --directory backend run --frozen --extra dev ruff check app tests`（全量） | ⚠️ **Found 1577 errors, exit 1**——全为存量；对输出 grep `dsa` 零命中，**无一告警涉及 DSA 4 文件**（详见 5.3） |
| 6 | `uv --directory backend run --frozen --extra dev pytest -q -rf`（全量） | ⚠️ **1 failed, 2523 passed, 6 skipped in 81.25s**——唯一失败为存量（详见 5.3） |
| 7 | `uv --directory backend run --frozen python -c "...app.main import app..."`（枚举路由） | ✅ **8 路径 10 方法与契约清单逐条一致**（含 DELETE /analysis/reports/{id}；见 §3 表） |

命令形态说明（素材偏差的实测复核）：素材记录分析 ask 当初执行不带 `--extra dev` 的命令报 `error: Failed to spawn: pytest`（当时 .venv 刚重建、pytest/ruff 属 `pyproject.toml:66-71` 的 dev extra 未安装）。本轮实测同一裸命令**现在可通过**（#1，先前 `--extra dev` 运行已把 dev 依赖装入 `.venv`，`uv run` 默认同步不裁剪）。为保证全新环境可复现，本报告统一采用 `--extra dev` 形态，此为环境事实而非契约改动。

### 5.2 素材记录（前序实现/验证轮执行，本轮未复跑）

以下结果来自本工作流前序 ask 的实际执行记录，本报告未重复执行，按素材原样转述：

- **真实旧库只读冒烟**（桥接轮）：以 `mode=ro` 访问真实旧库，列表 `total=1418` 与侦查一致；id=1463/1425/915/1011 四时代（era B / market_review / era A / raw_result NULL）markdown 拼装与点位 None 语义全部正确；health 计数 1418。全程未写旧库。
- **服务级冒烟**（最终验证轮）：启动后端于 127.0.0.1:8913，`GET /api/ext/dsa/health` 200 `{"status":"ok","dsa_db_found":true,"analysis_history_count":1418}`；`GET /legacy/reports?limit=3` 200 total=1418 且 item 字段齐全；`GET /analysis/reports` 200 空列表；确认未配置 AI Key 后 POST 任务 `{"symbols":["000000.SH"]}` → 1 拍到终态，item `failed` 且 error 文案与 `dsa_analysis.py:533` 一致（"标的 000000.SH 暂无日K数据"），任务级 `done`（与 4.2 设计一致）；schedule GET→PUT 18:30→回读一致→恢复默认 18:00 回读确认，偏好未污染；服务已 taskkill 干净、端口释放、未留垃圾文件。

### 5.3 存量问题（非本次改动引入，红线禁改核心文件，仅记录待人工确认）

- **全量 pytest 1 例失败**：`tests/test_atomic_write_retry.py::test_minute_date_queries_do_not_pin_partition_handles`。本轮**单跑复现**（`uv --directory backend run --frozen --extra dev pytest tests/test_atomic_write_retry.py::test_minute_date_queries_do_not_pin_partition_handles -q` → 1 failed），报错为 psutil 在 Windows 枚举进程打开文件句柄时 `RuntimeError: SystemExtendedHandleInformation buffer too big`（psutil `_pswindows.py:978`）——环境相关的核心存量问题，与 DSA 模块无调用关系。
- **全量 ruff 1577 条告警**（ruff 0.15.14，`app tests` 全量）：SIM105/RUF001/RUF100 等规则命中核心文件（如 `app/__init__.py:12`、`app/api/alerts.py:92` 的中文全角标点）。对输出 grep `dsa` 零命中，DSA 4 文件干净（与 #4 定向检查一致）。疑似 ruff 版本演进导致的历史告警放大，**待人工确认基线版本**。

### 5.4 未覆盖（如实声明）

- **真实 LLM 全链路**：本机未配置 AI Key，测试中 `generate_ai_text` 全部 mock；提示词对真实模型输出的实际效果（围栏合规率、四点位给出率）未验证，待联调。
- **定时到点的真实触发**：18:00 墙钟触发路径以纯函数 + `_scheduler_tick` 单测覆盖（含触发/当日一次/非交易日/无 Key/空自选），未等待真实时刻验证。
- **前端联调**：前端由并行工作流开发，本后端工作未动前端，`pnpm build` 不在本次范围；前后端契约联调未做。
- **P2 消费链路**：`phase_decision` → watch-plan 对账未实现（P2 范围）。
- **多 worker 部署**：任务表/调度线程为单进程语义，未验证 uvicorn 多 worker 场景（当前 TSP 桌面/单进程部署形态下不构成问题，多 worker 属待确认项）。

## 6. 遗留风险与后续

### 6.1 前端联调点（供前端方核对）

1. **legacy 列表 `operation_advice` 约 50 行是 200-300 字长句**（SQLite VARCHAR(20) 不限长），卡片需截断，详情页展示全文。
2. **legacy 点位 NULL 是业务真实状态**（ideal_buy 42.2% / secondary_buy 45.9% NULL 为风控抑制；stop_loss/take_profit 约 10% NULL），按"无买点"渲染而非当错误；被抑制原值在详情 markdown 注记。
3. **market_review 行原样混在 legacy 列表**（`symbol="MARKET"`，无点位；契约未规定过滤，total 同口径），前端可自行按 symbol 识别展示。
4. **id 全部为 string**（含 legacy 整数 id 的 `str()` 化）；**created_at 为 naive ISO 秒精度**（北京墙钟口径，无时区后缀）。
5. **limit 上限 100**（legacy 与 analysis 报告列表一致），前端分页需按 `total` 翻页。
6. **任务内存态**：服务重启后 `GET /analysis/tasks/{id}` 404，轮询侧需容错；`done` 计数含失败 item（进度条语义，见 4.2 偏差 #1）。
7. `DELETE /api/ext/dsa/analysis/reports/{report_id}` 已实现（契约 §3.2 内），前端可选接入。

### 6.2 P2 对接点

- **`phase_decision` 是 P2 对账输入**：新报告的 `watch_conditions`/`risk_conditions`（含 price）已结构化入库（`dsa_analysis.py:338-369`），P2 按 symbol 读最新报告即可，无需再解析 markdown。
- **存储语义为 P2 预留**：报告库不设条数上限、支持 symbol 过滤倒序（4.1），支撑"每票最新报告"查询。
- monitor-rules 同步（计划 §4）：对账服务需给规则打 `source="dsa_report_sync"` 标、不碰人工规则——本期未触碰 monitor。

### 6.3 风险

- 单 worker 串行 + LLM 300s 超时上限：50 只满额任务理论最长 ~4 小时（防配额打爆的既定取舍），前端进度条可观测。
- 旧库处于 WAL 模式且 DSA 应用仍在运行：已用 `mode=ro` + busy_timeout 3s + 降级兜底，锁竞争窗口内列表会短暂返回空结果（前端按"旧库不可用"提示，符合契约 §3.3）。
- 调度 30s 一拍 + 整分钟匹配：分钟粒度生效，PUT 改偏好后最迟 30s 生效；当日 last-run 在内存，进程重启当天可能重跑一次（可接受，重启本身不常见于盘中）。
- 存量基线问题（5.3）在本仓现状下持续存在，不阻塞 P1 联调，但合并前需维护者对基线表态。
