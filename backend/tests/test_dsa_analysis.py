"""DSA 式分析流水线扩展测试 (迁移计划 P1 §2/§3.1/§3.2 + 定时任务)。

覆盖: 报告存储(存取/过滤分页/删除/原子写/损坏跳过/路径穿越 fail-closed)、
LLM 输出解析(正常 json 块/无围栏/字符串数字/坏 json/取最后一块/不许编造)、
符号规范化(裸 6 位/前缀式/点分式/维表优先/规则兜底/非法透传)、
任务 API(创建校验/规范化去重保序/轮询终态/item 级 error 不拖垮/404)、
报告 API(列表分页/裸代码查后缀式报告/详情/删除/404)、schedule GET/PUT 校验与持久化、
调度判定纯函数与 tick 胶水(触发/当日一次/非交易日/无 Key/空自选)。

LLM 与数据访问全部 mock (StubRepo + monkeypatch generate_ai_text),
数据目录用 tmp_path 隔离, 不碰真实 data/。
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as app_config
from app.custom import dsa_analysis, dsa_bridge, dsa_watch
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.extensions.loader import configure_backend_extensions

TASKS_URL = "/api/ext/dsa/analysis/tasks"
REPORTS_URL = "/api/ext/dsa/analysis/reports"
SCHEDULE_URL = "/api/ext/dsa/analysis/schedule"


# ================================================================
# fixtures 与桩
# ================================================================

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """settings.data_dir 指向 tmp (报告/偏好全部落在隔离目录)。"""
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_runtime_state(tmp_path, monkeypatch):
    """任务表 / last-run / 运行时仓库引用逐测试复位; P2 收尾对账隔离 (stub 掉
    dsa_watch.on_task_done + 桥路径指向 tmp 不存在文件, 既有用例绝不真实读
    DSA 旧库, 也杜绝 worker 线程跨用例读到下一用例的 settings.data_dir)。"""
    with dsa_analysis._task_lock:
        dsa_analysis._tasks.clear()
    dsa_analysis._last_run_date = None
    dsa_analysis._RUNTIME["repo"] = None
    dsa_analysis._RUNTIME["data_dir"] = None
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", tmp_path / "no-such-legacy.db")
    monkeypatch.setattr(dsa_watch, "on_task_done", lambda task: None)
    dsa_watch._RUNTIME.update({"repo": None, "data_dir": None, "engine": None})
    dsa_watch._last_watch_sync = 0.0
    dsa_watch._last_premarket_date = None
    yield
    dsa_watch._RUNTIME.update({"repo": None, "data_dir": None, "engine": None})
    dsa_watch._last_watch_sync = 0.0
    dsa_watch._last_premarket_date = None
    with dsa_analysis._task_lock:
        dsa_analysis._tasks.clear()
    dsa_analysis._last_run_date = None
    dsa_analysis._RUNTIME["repo"] = None
    dsa_analysis._RUNTIME["data_dir"] = None


class StubRepo:
    """最小 repo 桩: 默认全部标的都有日K; 传入受限集合时其余返回空 (触发"暂无日K数据")。"""

    def __init__(self, kline_symbols: tuple | None = None):
        self.kline_symbols = None if kline_symbols is None else set(kline_symbols)

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_daily_asset(self, asset_type, symbol, start, end) -> pl.DataFrame:
        if self.kline_symbols is not None and symbol not in self.kline_symbols:
            return pl.DataFrame()
        return _kline_df()

    def get_name_map(self, symbols=None) -> dict[str, str]:
        return {s: f"股票{s}" for s in (symbols or [])}


def _kline_df(rows: int = 30, base: float = 10.0) -> pl.DataFrame:
    return pl.DataFrame({
        "date": [f"2026-08-{i + 1:02d}" for i in range(rows)],
        "open": [base + i * 0.05 for i in range(rows)],
        "high": [base + i * 0.1 + 0.2 for i in range(rows)],
        "low": [base + i * 0.1 - 0.2 for i in range(rows)],
        "close": [base + i * 0.1 for i in range(rows)],
        "volume": [1_000_000.0 + i for i in range(rows)],
        "turnover_rate": [1.0 + i * 0.01 for i in range(rows)],
    })


def _make_client(repo=None) -> TestClient:
    app = FastAPI()
    registrar = BackendExtensionRegistrar(
        "dsa.analysis", api_version=BACKEND_EXTENSION_API_VERSION,
    )
    dsa_analysis.setup(registrar)
    for router in registrar.routers:
        app.include_router(router)
    if repo is not None:
        app.state.repo = repo
    return TestClient(app)


@pytest.fixture
def client(data_dir):
    return _make_client(StubRepo())


_STRUCTURED = {
    "operation_advice": "持有",
    "sentiment_score": 65,
    "trend_prediction": "震荡偏多",
    "analysis_summary": "均线多头排列, 回踩关注支撑。",
    "points": {
        "ideal_buy": "1680.5",   # 字符串数字
        "secondary_buy": 1720,
        "stop_loss": "1650",
        "take_profit": 1850.0,
    },
    "phase_decision": {
        "action_window": "突破 1750 放量后",
        "immediate_action": "不追, 等回踩",
        "next_check_time": "2026-09-30 10:30",
        "watch_conditions": ["放量站上 1750", "   "],
        "risk_conditions": [
            {"kind": "reduce", "text": "跌破 1700 减半仓", "price": "1700"},
            "非对象条目应被跳过",
        ],
    },
}


def _llm_text(markdown: str = "# 报告\n\n正文", structured: dict | None = _STRUCTURED,
              fence_raw: str | None = None) -> str:
    parts = [markdown]
    if fence_raw is not None:
        parts.append(f"```json\n{fence_raw}\n```")
    elif structured is not None:
        parts.append(f"```json\n{json.dumps(structured, ensure_ascii=False)}\n```")
    return "\n\n".join(parts)


def _mock_llm(text: str, captured: list | None = None):
    async def fake(messages, **kwargs):
        if captured is not None:
            captured.append(messages)
        return text
    return fake


def _poll_task(client: TestClient, task_id: str, timeout_s: float = 10.0) -> dict:
    """轮询任务终态; 时限按任务规模动态放大 (50-item 满额任务在全仓并发负载下
    默认 10s 曾不够而 flaky), 首个响应即知 total; 显式 timeout_s 仍是下限。"""
    body = client.get(f"{TASKS_URL}/{task_id}").json()
    deadline = time.monotonic() + max(timeout_s, float(body.get("total", 0)) * 0.5 + 5.0)
    while True:
        if body["status"] in ("done", "failed"):
            return body
        if time.monotonic() >= deadline:
            raise AssertionError(f"task {task_id} did not reach terminal state in time")
        time.sleep(0.05)
        body = client.get(f"{TASKS_URL}/{task_id}").json()


# ================================================================
# 报告存储
# ================================================================

def test_store_save_assigns_id_and_beijing_wall_clock_created_at(data_dir):
    report = dsa_analysis._STORE.save({"symbol": "600519", "markdown": "A"})
    day = report["created_at"][:10].replace("-", "")
    assert report["id"] == f"r_{day}_600519_001"
    assert len(report["created_at"]) == 19 and report["created_at"][10] == "T"


def test_store_id_sequence_increments_per_day_and_symbol(data_dir):
    store = dsa_analysis._STORE
    first = store.save({"symbol": "600519", "created_at": "2026-09-29T18:00:01"})
    second = store.save({"symbol": "600519", "created_at": "2026-09-29T18:00:02"})
    other = store.save({"symbol": "000001", "created_at": "2026-09-29T18:00:03"})
    assert first["id"] == "r_20260929_600519_001"
    assert second["id"] == "r_20260929_600519_002"
    assert other["id"] == "r_20260929_000001_001"


def test_store_list_orders_desc_and_filters_by_symbol(data_dir):
    store = dsa_analysis._STORE
    a = store.save({"symbol": "600519", "created_at": "2026-09-28T18:00:00"})
    b = store.save({"symbol": "000001", "created_at": "2026-09-29T18:00:00"})
    assert [r["id"] for r in store.list_reports()] == [b["id"], a["id"]]
    assert [r["id"] for r in store.list_reports("600519")] == [a["id"]]


def test_store_get_and_delete_roundtrip(data_dir):
    store = dsa_analysis._STORE
    saved = store.save({"symbol": "600519", "created_at": "2026-09-29T18:00:00", "markdown": "A"})
    assert store.get(saved["id"])["markdown"] == "A"
    assert store.get("r_missing") is None
    assert store.delete(saved["id"]) is True
    assert store.get(saved["id"]) is None
    assert store.delete(saved["id"]) is False


def test_store_rejects_path_traversal_ids(data_dir):
    store = dsa_analysis._STORE
    assert store.get("../../secrets") is None
    assert store.get("..\\evil") is None
    assert store.delete("../../secrets") is False


def test_store_skips_corrupt_file_without_killing_list(data_dir):
    store = dsa_analysis._STORE
    good = store.save({"symbol": "600519", "created_at": "2026-09-29T18:00:00"})
    d = data_dir / "user_data" / "dsa_reports"
    (d / "r_20260929_000001_001.json").write_text("{not json", encoding="utf-8")
    assert [r["id"] for r in store.list_reports()] == [good["id"]]
    assert store.get("r_20260929_000001_001.json") is None


def test_store_failed_write_keeps_existing_reports(data_dir, monkeypatch):
    """os.replace 失败 (如磁盘满) 不得破坏已落盘报告, 且不残留 .tmp。"""
    store = dsa_analysis._STORE
    saved = store.save({"symbol": "600519", "created_at": "2026-09-29T18:00:00"})

    real_replace = dsa_analysis.os.replace

    def boom(src, dst):
        if str(dst).endswith(".json"):
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(dsa_analysis.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.save({"symbol": "600519", "created_at": "2026-09-29T18:01:00"})
    assert [r["id"] for r in store.list_reports()] == [saved["id"]]
    d = data_dir / "user_data" / "dsa_reports"
    assert not list(d.glob("*.tmp"))


# ================================================================
# LLM 输出解析
# ================================================================

def test_parse_extracts_structured_and_strips_fence():
    markdown, out = dsa_analysis._parse_llm_output(_llm_text())
    assert markdown == "# 报告\n\n正文"
    assert out["operation_advice"] == "持有"
    assert out["sentiment_score"] == 65
    assert out["trend_prediction"] == "震荡偏多"
    assert out["points"] == {
        "ideal_buy": 1680.5, "secondary_buy": 1720.0,
        "stop_loss": 1650.0, "take_profit": 1850.0,
    }
    assert out["phase_decision"]["watch_conditions"] == ["放量站上 1750"]
    assert out["phase_decision"]["risk_conditions"] == [
        {"kind": "reduce", "text": "跌破 1700 减半仓", "price": 1700.0},
    ]


def test_parse_without_json_fence_keeps_markdown_structured_empty():
    markdown, out = dsa_analysis._parse_llm_output("# 报告\n没有围栏")
    assert "没有围栏" in markdown
    assert out == dsa_analysis._empty_structured()


def test_parse_bad_json_keeps_raw_markdown_structured_empty():
    text = _llm_text(fence_raw="{broken json")
    markdown, out = dsa_analysis._parse_llm_output(text)
    assert "{broken json" in markdown
    assert out == dsa_analysis._empty_structured()


def test_parse_takes_last_json_fence():
    text = (
        "```json\n{\"operation_advice\": \"旧\"}\n```\n\n中间正文\n\n"
        "```json\n{\"operation_advice\": \"新\"}\n```"
    )
    _, out = dsa_analysis._parse_llm_output(text)
    assert out["operation_advice"] == "新"


def test_parse_structured_never_fabricates_missing_values():
    out = dsa_analysis._normalize_structured({
        "points": {"ideal_buy": "abc", "stop_loss": -3},
        "sentiment_score": 150,
        "phase_decision": "not a dict",
    })
    assert out["points"] == {
        "ideal_buy": None, "secondary_buy": None, "stop_loss": None, "take_profit": None,
    }
    assert out["sentiment_score"] is None  # 越界分数不钳制、不编造
    assert out["operation_advice"] is None
    assert out["trend_prediction"] is None
    assert out["analysis_summary"] is None
    assert out["phase_decision"] is None


def test_parse_structured_accepts_zero_score_and_empty_phase_is_none():
    assert dsa_analysis._normalize_structured({"sentiment_score": 0})["sentiment_score"] == 0
    assert dsa_analysis._normalize_structured({"sentiment_score": "82"})["sentiment_score"] == 82
    assert dsa_analysis._normalize_phase({"watch_conditions": [], "risk_conditions": []}) is None
    assert dsa_analysis._normalize_structured("not a dict") == dsa_analysis._empty_structured()


# ================================================================
# 单票流水线
# ================================================================

def test_analyze_one_builds_prompt_and_saves_report(data_dir, monkeypatch):
    captured: list = []
    monkeypatch.setattr(
        dsa_analysis, "generate_ai_text",
        _mock_llm(_llm_text(), captured),
    )
    monkeypatch.setattr(
        dsa_analysis, "_load_financials",
        lambda _data_dir, _symbol: {"metrics": [{"roe": 30.0}], "income": []},
    )
    report = dsa_analysis._analyze_one(StubRepo(), data_dir, "600519", "full")

    # 裸代码入口在流水线开头规范化为后缀式 (StubRepo 维表为空 → 代码段规则兜底)
    assert report["symbol"] == "600519.SH"
    assert report["name"] == "股票600519.SH"
    assert report["mode"] == "full"
    assert report["operation_advice"] == "持有"
    assert report["points"]["ideal_buy"] == 1680.5
    assert report["markdown"].startswith("# 报告")
    assert "```json" not in report["markdown"]  # json 围栏已剥离, 结构化单独存
    assert set(report) == {
        "id", "symbol", "name", "created_at", "mode", "sentiment_score",
        "operation_advice", "trend_prediction", "analysis_summary",
        "points", "phase_decision", "markdown",
    }
    # 落盘后可读回
    assert dsa_analysis._STORE.get(report["id"])["id"] == report["id"]

    system, user = captured[0][0]["content"], captured[0][1]["content"]
    assert '"close"' in user          # 日K JSON 注入
    assert "roe" in user              # 财务 JSON 注入
    assert "当前价" in user           # 价位摘要注入
    assert "暂无财务数据" not in user
    assert "800" not in system        # full 模式无精简要求


def test_analyze_one_brief_mode_asks_for_short_report(data_dir, monkeypatch):
    captured: list = []
    monkeypatch.setattr(dsa_analysis, "generate_ai_text", _mock_llm(_llm_text(), captured))
    dsa_analysis._analyze_one(StubRepo(), data_dir, "600519", "brief")
    assert "800" in captured[0][0]["content"]
    assert captured[0][1]["content"].startswith("标的标准代码: 600519.SH")


def test_analyze_one_without_financials_notes_missing(data_dir, monkeypatch):
    captured: list = []
    monkeypatch.setattr(dsa_analysis, "generate_ai_text", _mock_llm(_llm_text(), captured))
    dsa_analysis._analyze_one(StubRepo(), data_dir, "600519", "full")
    assert "暂无财务数据" in captured[0][1]["content"]


def test_analyze_one_no_kline_raises(data_dir):
    with pytest.raises(RuntimeError, match="暂无日K数据"):
        dsa_analysis._analyze_one(StubRepo(kline_symbols=()), data_dir, "600519", "full")


def test_analyze_one_propagates_llm_error(data_dir, monkeypatch):
    async def boom(messages, **kwargs):
        raise RuntimeError("AI API Key 未配置, 请在设置页配置")
    monkeypatch.setattr(dsa_analysis, "generate_ai_text", boom)
    with pytest.raises(RuntimeError, match="AI API Key 未配置"):
        dsa_analysis._analyze_one(StubRepo(), data_dir, "600519", "full")


def test_analyze_one_empty_markdown_fails(data_dir, monkeypatch):
    """连 markdown 都为空 → item failed, 不落盘。"""
    monkeypatch.setattr(dsa_analysis, "generate_ai_text", _mock_llm(_llm_text(markdown="")))
    with pytest.raises(RuntimeError, match="AI 未返回报告正文"):
        dsa_analysis._analyze_one(StubRepo(), data_dir, "600519", "full")
    assert dsa_analysis._STORE.list_reports() == []


def test_analyze_one_without_json_fence_still_saves_report(data_dir, monkeypatch):
    """json 块整体失败 → 仍保存报告 (markdown 有值, 结构化字段全空)。"""
    monkeypatch.setattr(dsa_analysis, "generate_ai_text", _mock_llm("# 报告\n无围栏正文"))
    report = dsa_analysis._analyze_one(StubRepo(), data_dir, "600519", "full")
    assert report["markdown"] == "# 报告\n无围栏正文"
    assert report["operation_advice"] is None
    assert report["points"] == {k: None for k in dsa_analysis._POINT_KEYS}
    assert report["phase_decision"] is None
    assert report["sentiment_score"] is None


# ================================================================
# 符号规范化 (_canonical_symbol, 迁移计划 §3.1 注记 2026-09-29)
# ================================================================

class DimensionRepo:
    """固定维表桩: get_name_map keys 为后缀式 symbol (与真实 instruments 一致)。"""

    def get_name_map(self, symbols=None) -> dict[str, str]:
        return {"600519.SH": "贵州茅台", "000001.SZ": "平安银行", "510300.SH": "沪深300ETF"}


class BrokenRepo:
    """维表查询抛异常的桩 (异常时须按代码段规则兜底, 不冒泡)。"""

    def get_name_map(self, symbols=None) -> dict[str, str]:
        raise RuntimeError("维表不可用")


def test_canonical_symbol_bare_code_prefers_dimension_table():
    # 裸 6 位代码: 维表反查优先
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "600519") == "600519.SH"
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "000001") == "000001.SZ"
    # 5 开头 ETF 规则未覆盖, 只能靠维表判定
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "510300") == "510300.SH"


def test_canonical_symbol_dimension_miss_falls_back_to_code_rules():
    # 维表未命中 → 代码段规则兜底; 92 北交所段先于 9 开头判定
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "300750") == "300750.SZ"
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "688981") == "688981.SH"
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "900901") == "900901.SH"
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "920002") == "920002.BJ"
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "430047") == "430047.BJ"
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "833171") == "833171.BJ"


def test_canonical_symbol_rule_miss_returns_original_not_fabricated():
    # 规则未覆盖 (5/1/7 开头) 且维表没有 → 原样透传, 让 _load_kline 自然报暂无日K
    assert dsa_analysis._canonical_symbol(StubRepo(), "511990") == "511990"
    assert dsa_analysis._canonical_symbol(StubRepo(), "113050") == "113050"


def test_canonical_symbol_suffixed_prefixed_and_idempotent():
    # 点分式: 大小写归一, 幂等
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "600519.SH") == "600519.SH"
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "600519.sh") == "600519.SH"
    # 前缀式 → 点分式
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "SZ000636") == "000636.SZ"
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "sh600519") == "600519.SH"
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "bj430047") == "430047.BJ"
    # 幂等: 规范化结果再规范化不变
    once = dsa_analysis._canonical_symbol(DimensionRepo(), "600519")
    assert dsa_analysis._canonical_symbol(DimensionRepo(), once) == once


def test_canonical_symbol_unrecognized_input_passthrough():
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "AAPL") == "AAPL"
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "60051") == "60051"
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "600519.XX") == "600519.XX"
    assert dsa_analysis._canonical_symbol(DimensionRepo(), "") == ""
    assert dsa_analysis._canonical_symbol(DimensionRepo(), " 600519 ") == "600519.SH"


def test_canonical_symbol_repo_broken_or_none_falls_back_to_rules():
    assert dsa_analysis._canonical_symbol(BrokenRepo(), "600519") == "600519.SH"
    assert dsa_analysis._canonical_symbol(None, "000001") == "000001.SZ"


# ================================================================
# 任务 API
# ================================================================

def test_task_create_rejects_invalid_payloads(client):
    assert client.post(TASKS_URL, json={"symbols": []}).status_code == 400
    assert client.post(TASKS_URL, json={"symbols": "600519"}).status_code == 400
    assert client.post(TASKS_URL, json={"symbols": [1, 2]}).status_code == 400
    assert client.post(
        TASKS_URL, json={"symbols": [f"{i:06d}" for i in range(51)]},
    ).status_code == 400
    assert client.post(
        TASKS_URL, json={"symbols": ["600519"], "mode": "deep"},
    ).status_code == 400
    # 去重在数量校验前: 60 个重复 → 1 只 → 合法
    ok = client.post(TASKS_URL, json={"symbols": ["600519"] * 60})
    assert ok.status_code == 200


def test_task_create_accepts_fifty_symbols_boundary(client, monkeypatch):
    monkeypatch.setattr(dsa_analysis, "generate_ai_text", _mock_llm(_llm_text()))
    symbols = [f"{i:06d}" for i in range(50)]
    r = client.post(TASKS_URL, json={"symbols": symbols})
    assert r.status_code == 200
    body = _poll_task(client, r.json()["task_id"])
    assert body["status"] == "done" and body["total"] == body["done"] == 50


def test_task_dedup_preserves_order_and_defaults_full_mode(client, monkeypatch):
    monkeypatch.setattr(dsa_analysis, "generate_ai_text", _mock_llm(_llm_text()))
    r = client.post(TASKS_URL, json={"symbols": ["000001", "600519", "000001", "600519.SH"]})
    body = _poll_task(client, r.json()["task_id"])
    # 规范化后去重保序: 裸 600519 与 600519.SH 是同一标的
    assert [i["symbol"] for i in body["items"]] == ["000001.SZ", "600519.SH"]
    assert all(i["status"] == "done" for i in body["items"])
    detail = client.get(f"{REPORTS_URL}/{body['items'][0]['report_id']}").json()
    assert detail["mode"] == "full"


def test_task_lifecycle_runs_items_saves_reports(client, monkeypatch):
    monkeypatch.setattr(dsa_analysis, "generate_ai_text", _mock_llm(_llm_text()))
    r = client.post(TASKS_URL, json={"symbols": ["600519", "000001"]})
    task_id = r.json()["task_id"]
    body = _poll_task(client, task_id)
    assert body == {
        "task_id": task_id,
        "status": "done",
        "total": 2,
        "done": 2,
        "items": [
            {"symbol": "600519.SH", "status": "done", "report_id": body["items"][0].get("report_id")},
            {"symbol": "000001.SZ", "status": "done", "report_id": body["items"][1].get("report_id")},
        ],
    }
    for item in body["items"]:
        assert item["report_id"].startswith("r_")
        detail = client.get(f"{REPORTS_URL}/{item['report_id']}")
        assert detail.status_code == 200
        detail = detail.json()
        assert detail["sentiment_score"] == 65
        assert detail["phase_decision"]["immediate_action"] == "不追, 等回踩"
    assert client.get(REPORTS_URL).json()["total"] == 2


def test_task_bare_symbol_resolves_to_suffixed_and_produces_report(monkeypatch):
    """复现 P1 bug: 仓库 parquet/instruments 的 symbol 均为后缀式 (600519.SH),
    而前端手动输入与契约 §3.1 示例是裸代码; 修复前裸 600519 直传 _load_kline
    匹配不到 → "标的 600519 暂无日K数据"。修复后入口规范化命中维表格式。
    StubRepo 只对 '600519.SH' 返回日K (模拟真实仓库分区格式)。"""
    monkeypatch.setattr(dsa_analysis, "generate_ai_text", _mock_llm(_llm_text()))
    scoped = _make_client(StubRepo(kline_symbols=("600519.SH",)))
    r = scoped.post(TASKS_URL, json={"symbols": ["600519"]})
    body = _poll_task(scoped, r.json()["task_id"])
    item = body["items"][0]
    assert item["symbol"] == "600519.SH"
    assert item["status"] == "done" and item["report_id"]
    detail = scoped.get(f"{REPORTS_URL}/{item['report_id']}").json()
    assert detail["symbol"] == "600519.SH"
    assert detail["operation_advice"] == "持有"
    # 裸代码与后缀式是同一标的: 混合提交去重为一条 (幂等)
    r2 = scoped.post(TASKS_URL, json={"symbols": ["600519", "600519.SH"]})
    body2 = _poll_task(scoped, r2.json()["task_id"])
    assert [i["symbol"] for i in body2["items"]] == ["600519.SH"]


def test_task_item_error_does_not_kill_task(client, monkeypatch):
    monkeypatch.setattr(dsa_analysis, "generate_ai_text", _mock_llm(_llm_text()))
    scoped = _make_client(StubRepo(kline_symbols=("600519.SH",)))
    r = scoped.post(TASKS_URL, json={"symbols": ["600519", "000001"]})
    body = _poll_task(scoped, r.json()["task_id"])
    # 有失败 item 也算 done, done 计数含失败项 (进度条可走满)
    assert body["status"] == "done"
    assert body["done"] == body["total"] == 2
    by_symbol = {i["symbol"]: i for i in body["items"]}
    assert by_symbol["600519.SH"]["status"] == "done"
    assert by_symbol["000001.SZ"]["status"] == "failed"
    assert "暂无日K数据" in by_symbol["000001.SZ"]["error"]
    # 失败 item 不产生报告
    assert scoped.get(REPORTS_URL).json()["total"] == 1


def test_task_items_expose_only_contract_fields(client, monkeypatch):
    monkeypatch.setattr(dsa_analysis, "generate_ai_text", _mock_llm(_llm_text()))
    r = client.post(TASKS_URL, json={"symbols": ["600519"]})
    body = _poll_task(client, r.json()["task_id"])
    for item in body["items"]:
        assert set(item) <= {"symbol", "status", "report_id", "error"}


def test_task_get_unknown_returns_404(client):
    assert client.get(f"{TASKS_URL}/t_missing").status_code == 404


# ================================================================
# 报告 API
# ================================================================

def _seed_reports() -> list[dict]:
    store = dsa_analysis._STORE
    # 报告经流水线落盘的 symbol 已是后缀式 (§3.1 规范化注记)
    seeds = [
        {"symbol": "600519.SH", "created_at": "2026-09-28T18:00:00", "markdown": "m1"},
        {"symbol": "600519.SH", "created_at": "2026-09-29T18:00:00", "markdown": "m2",
         "operation_advice": "持有", "sentiment_score": 65},
        {"symbol": "000001.SZ", "created_at": "2026-09-27T18:00:00", "markdown": "m3"},
    ]
    return [store.save(dict(s, points={"ideal_buy": 1.0, "secondary_buy": None,
                                       "stop_loss": None, "take_profit": None}))
            for s in seeds]


def test_reports_list_pagination_filter_and_summary_shape(client):
    saved = _seed_reports()
    body = client.get(REPORTS_URL).json()
    assert body["total"] == 3
    assert [i["id"] for i in body["items"]] == [
        saved[1]["id"], saved[0]["id"], saved[2]["id"],
    ]
    summary = body["items"][0]
    assert "markdown" not in summary and "phase_decision" not in summary
    for key in ("id", "symbol", "name", "created_at", "operation_advice",
                "sentiment_score", "points"):
        assert key in summary

    # 裸 600519 查询参数规范化为 600519.SH 后命中后缀式落盘报告 (§3.1 注记回归)
    filtered = client.get(f"{REPORTS_URL}?symbol=600519").json()
    assert filtered["total"] == 2
    assert all(i["symbol"] == "600519.SH" for i in filtered["items"])

    paged = client.get(f"{REPORTS_URL}?limit=1&offset=1").json()
    assert paged["total"] == 3 and len(paged["items"]) == 1
    assert paged["items"][0]["id"] == saved[0]["id"]

    assert client.get(f"{REPORTS_URL}?limit=500").status_code == 200
    assert client.get(f"{REPORTS_URL}?limit=0").status_code == 400
    assert client.get(f"{REPORTS_URL}?offset=-1").status_code == 400


def test_reports_filter_by_date_and_before(client):
    """date 精确命中当日, before 为不含当日的往期; 与 symbol 可组合, 非法格式 400。"""
    _seed_reports()  # 000001.SZ@09-27, 600519.SH@09-28, 600519.SH@09-29

    day = client.get(REPORTS_URL, params={"date": "2026-09-28"}).json()
    assert day["total"] == 1 and day["items"][0]["symbol"] == "600519.SH"

    past = client.get(REPORTS_URL, params={"before": "2026-09-29"}).json()
    assert past["total"] == 2
    assert all(i["created_at"][:10] < "2026-09-29" for i in past["items"])

    combo = client.get(REPORTS_URL, params={"symbol": "600519", "before": "2026-09-29"}).json()
    assert combo["total"] == 1 and combo["items"][0]["created_at"][:10] == "2026-09-28"

    no_hit = client.get(REPORTS_URL, params={"symbol": "600519", "date": "2026-09-27"}).json()
    assert no_hit["total"] == 0

    assert client.get(REPORTS_URL, params={"date": "20260928"}).status_code == 400
    assert client.get(REPORTS_URL, params={"before": "abc"}).status_code == 400


def test_reports_filter_bare_symbol_hits_suffixed_report(client):
    """报告 symbol 落盘为后缀式; 查询参数裸 600519 经规范化仍命中, 其他票不误伤。"""
    dsa_analysis._STORE.save({
        "symbol": "600519.SH", "created_at": "2026-09-29T18:00:00", "markdown": "m",
    })
    for query in ("600519", "600519.SH", "600519.sh"):
        body = client.get(REPORTS_URL, params={"symbol": query}).json()
        assert body["total"] == 1
        assert body["items"][0]["symbol"] == "600519.SH"
    assert client.get(REPORTS_URL, params={"symbol": "000001"}).json()["total"] == 0


def test_report_detail_delete_and_404(client):
    saved = dsa_analysis._STORE.save({
        "symbol": "600519", "created_at": "2026-09-29T18:00:00",
        "markdown": "# 报告", "operation_advice": "持有",
        "phase_decision": {"watch_conditions": ["放量站上 1750"]},
        "points": {"ideal_buy": 1.0, "secondary_buy": None,
                   "stop_loss": None, "take_profit": None},
    })
    body = client.get(f"{REPORTS_URL}/{saved['id']}").json()
    assert set(body) == {
        "id", "symbol", "name", "created_at", "mode", "sentiment_score",
        "operation_advice", "trend_prediction", "analysis_summary",
        "points", "phase_decision", "markdown",
    }
    assert body["markdown"] == "# 报告"
    assert body["phase_decision"] == {"watch_conditions": ["放量站上 1750"]}

    assert client.delete(f"{REPORTS_URL}/{saved['id']}").json() == {"ok": True}
    assert client.get(f"{REPORTS_URL}/{saved['id']}").status_code == 404
    assert client.delete(f"{REPORTS_URL}/{saved['id']}").status_code == 404
    assert client.get(f"{REPORTS_URL}/r_missing").status_code == 404


# ================================================================
# schedule API
# ================================================================

def test_schedule_default_and_put_roundtrip(client):
    assert client.get(SCHEDULE_URL).json() == {"enabled": True, "hour": 18, "minute": 0}

    value = {"enabled": False, "hour": 19, "minute": 30}
    r = client.put(SCHEDULE_URL, json=value)
    assert r.status_code == 200 and r.json() == value
    assert client.get(SCHEDULE_URL).json() == value
    # 经 preferences 持久化 (键固定 dsa-analysis-schedule)
    from app.services import preferences
    assert preferences.load()["dsa-analysis-schedule"] == value


@pytest.mark.parametrize("payload", [
    {"enabled": True, "hour": 24, "minute": 0},
    {"enabled": True, "hour": -1, "minute": 0},
    {"enabled": True, "hour": 18, "minute": 60},
    {"enabled": True, "hour": 18, "minute": -1},
    {"enabled": "yes", "hour": 18, "minute": 0},
    {"enabled": 1, "hour": 18, "minute": 0},
    {"enabled": True, "hour": "18", "minute": 0},
    {"enabled": True, "minute": 0},
])
def test_schedule_put_rejects_invalid(client, payload):
    assert client.put(SCHEDULE_URL, json=payload).status_code == 400


# ================================================================
# 调度判定与定时任务
# ================================================================

def _decide(**overrides) -> tuple[bool, str]:
    now = overrides.pop("now", datetime(2026, 9, 29, 18, 0))  # 周二
    params: dict = {
        "prefs": {"enabled": True, "hour": 18, "minute": 0},
        "last_run_date": None,
        "trading_day": True,
        "ai_ready": True,
        "symbols": ["600519"],
    }
    params.update(overrides)
    return dsa_analysis._decide_scheduled_run(now=now, **params)


def test_decide_triggers_at_preferred_minute():
    assert _decide() == (True, "trigger")


def test_decide_skips_outside_trigger_minute():
    assert _decide(now=datetime(2026, 9, 29, 18, 1)) == (False, "not_time")


def test_decide_runs_once_per_day():
    assert _decide(last_run_date=datetime(2026, 9, 29).date()) == (False, "already_ran")


def test_decide_skips_non_trading_day_and_falls_back_on_unknown():
    assert _decide(trading_day=False) == (False, "not_trading_day")
    # None (探测链失败) → 退化为周一~五: 周二触发
    assert _decide(trading_day=None)[0] is True
    # None + 周六 → 跳过
    assert _decide(now=datetime(2026, 10, 3, 18, 0), trading_day=None) == (False, "not_trading_day")


def test_decide_skips_without_ai_key_or_watchlist():
    assert _decide(ai_ready=False) == (False, "no_ai_key")
    assert _decide(symbols=[]) == (False, "watchlist_empty")


def test_decide_skips_when_disabled():
    assert _decide(prefs={"enabled": False, "hour": 18, "minute": 0}) == (False, "disabled")


def test_scheduler_tick_creates_full_task_once_per_day(monkeypatch, data_dir):
    monkeypatch.setattr(dsa_analysis, "_load_schedule",
                        lambda: {"enabled": True, "hour": 18, "minute": 0})
    monkeypatch.setattr(dsa_analysis, "is_trading_day", lambda now=None: True)
    monkeypatch.setattr(dsa_analysis, "ai_configured", lambda: True)
    monkeypatch.setattr(dsa_analysis, "list_symbols",
                        lambda: [{"symbol": "600519 "}, {"symbol": "000001"}])
    created: list = []
    monkeypatch.setattr(
        dsa_analysis, "_create_task",
        lambda symbols, mode, **kw: created.append((symbols, mode)) or {"task_id": "t_x"},
    )
    now = datetime(2026, 9, 29, 18, 0)
    assert dsa_analysis._scheduler_tick(now=now) == (True, "trigger")
    assert created == [(["600519", "000001"], "full")]
    # 同日第二拍不重跑
    assert dsa_analysis._scheduler_tick(now=now.replace(second=31)) == (False, "already_ran")
    assert len(created) == 1


def test_scheduler_tick_skips_without_ai_key(monkeypatch, data_dir):
    monkeypatch.setattr(dsa_analysis, "_load_schedule",
                        lambda: {"enabled": True, "hour": 18, "minute": 0})
    monkeypatch.setattr(dsa_analysis, "is_trading_day", lambda now=None: True)
    monkeypatch.setattr(dsa_analysis, "ai_configured", lambda: False)
    monkeypatch.setattr(dsa_analysis, "list_symbols", lambda: [{"symbol": "600519"}])
    created: list = []
    monkeypatch.setattr(
        dsa_analysis, "_create_task",
        lambda symbols, mode, **kw: created.append((symbols, mode)) or {"task_id": "t_x"},
    )
    assert dsa_analysis._scheduler_tick(now=datetime(2026, 9, 29, 18, 0)) == (False, "no_ai_key")
    assert created == []


def test_scheduler_tick_skips_when_watchlist_empty(monkeypatch, data_dir):
    monkeypatch.setattr(dsa_analysis, "_load_schedule",
                        lambda: {"enabled": True, "hour": 18, "minute": 0})
    monkeypatch.setattr(dsa_analysis, "is_trading_day", lambda now=None: True)
    monkeypatch.setattr(dsa_analysis, "ai_configured", lambda: True)
    monkeypatch.setattr(dsa_analysis, "list_symbols", lambda: [])
    assert dsa_analysis._scheduler_tick(now=datetime(2026, 9, 29, 18, 0)) == (
        False, "watchlist_empty",
    )


def test_scheduler_tick_respects_disabled_pref(monkeypatch, data_dir):
    monkeypatch.setattr(dsa_analysis, "_load_schedule",
                        lambda: {"enabled": False, "hour": 18, "minute": 0})
    assert dsa_analysis._scheduler_tick(now=datetime(2026, 9, 29, 18, 0)) == (False, "disabled")


# ================================================================
# 扩展注册 (loader 自动发现 + 不破坏 dsa_bridge health 契约)
# ================================================================

def test_extension_registers_via_loader_and_keeps_bridge_health():
    app = FastAPI()
    registry, errors = configure_backend_extensions(app)
    assert "dsa.analysis" in registry.extension_ids()
    assert errors == ()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/ext/dsa/analysis/tasks" in paths
    assert "/api/ext/dsa/analysis/schedule" in paths
    assert "/api/ext/dsa/health" in paths  # dsa_bridge 契约原样保留
