"""DSA 历史只读桥接测试 (迁移计划 §3.3)。

tmp_path 建 mini sqlite (按侦查的真实 17 列表结构, 含一条 code 缺失的坏行、
一条 raw_result NULL 行、一条 JSON 损坏行、一条风控抑制行), 经 monkeypatch
注入 dsa_bridge.DSA_DB_PATH, 不碰真实旧库。覆盖: 列表过滤/排序/分页/limit
上限/total/坏行跳过不计入、详情三个时代 markdown 拼装与列值优先、风控抑制
注记、库缺失/锁定/文件损坏的契约降级 (列表空结果 / 详情 404 旧库不可用)、
health 端点不回归。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import dsa_bridge
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

LEGACY_URL = "/api/ext/dsa/legacy/reports"
HEALTH_URL = "/api/ext/dsa/health"

_SUMMARY_KEYS = {
    "id",
    "symbol",
    "created_at",
    "operation_advice",
    "sentiment_score",
    "ideal_buy",
    "secondary_buy",
    "stop_loss",
    "take_profit",
    "analysis_summary",
}


# ================================================================
# mini 旧库: 侦查得到的 17 列结构 (去掉 NOT NULL 以便注入坏行)
# ================================================================

_DDL = """
CREATE TABLE analysis_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id VARCHAR(64),
    code VARCHAR(10),
    name VARCHAR(50),
    report_type VARCHAR(16),
    sentiment_score INTEGER,
    operation_advice VARCHAR(20),
    trend_prediction VARCHAR(50),
    analysis_summary TEXT,
    raw_result TEXT,
    news_content TEXT,
    context_snapshot TEXT,
    ideal_buy FLOAT,
    secondary_buy FLOAT,
    stop_loss FLOAT,
    take_profit FLOAT,
    created_at DATETIME
)
"""

_ERA_B_DASHBOARD = {
    "core_conclusion": {
        "signal_type": "持有",
        "one_sentence": "多头结构完好, 持股待涨。",
        "time_sensitivity": "次日有效",
        "position_advice": {
            "no_position": "不追, 等回踩 7.7 附近",
            "has_position": "持有, 跌破 7.1 止损",
        },
    },
    "battle_plan": {
        "sniper_points": {
            "ideal_buy": 7.77,
            "secondary_buy": 7.5,
            "stop_loss": 7.1,
            "take_profit": 8.6,
        },
        "position_strategy": {
            "suggested_position": "3 成仓位试仓",
            "entry_plan": "回踩 7.7 分两笔建仓",
            "risk_control": "单笔亏损不超 2%",
        },
    },
    "phase_decision": {
        "immediate_action": "持股不动",
        "action_window": "突破 8.6 放量后",
        "watch_conditions": ["放量站上 8.6", "板块联动走强"],
        "next_check_time": "2026-09-30 10:30",
    },
}

_ERA_B_RAW = json.dumps(
    {
        "code": "002383",
        "current_price": 7.88,
        "change_pct": 1.02,
        "model_used": "deepseek-chat",
        "technical_analysis": "技术面: 均线多头排列, 量能温和放大。",
        "fundamental_analysis": "",
        "news_summary": "",
        "risk_warning": "大盘走弱时跟随回调。",
        "buy_reason": "",
        "trend_prediction": "JSON 趋势预判(应被列值覆盖)",
        "analysis_summary": "JSON 摘要(应被列值覆盖)",
        "dashboard": _ERA_B_DASHBOARD,
    },
    ensure_ascii=False,
)

_MARKET_RAW = json.dumps(
    {
        "dashboard": None,
        "news_summary": "## 2026-09-28 大盘复盘\n\n两市缩量整理, 科技板块领涨。\n",
        "raw_response": "[dsa-market-region]: # 备用全文",
    },
    ensure_ascii=False,
)

_ERA_A_RAW = json.dumps(
    {"analysis_summary": "JSON 旧摘要", "trend_prediction": "JSON 旧趋势", "dashboard": None},
    ensure_ascii=False,
)

_SUPPRESSED_RAW = json.dumps(
    {
        "current_price": 7.88,
        "change_pct": 1.02,
        "dashboard": {
            "battle_plan": {
                "sniper_points": {
                    "ideal_buy": None,
                    "secondary_buy": None,
                    "stop_loss": 7.1,
                    "take_profit": 8.6,
                    "suppressed_by_guardrail": True,
                    "ideal_buy_presuppression": 7.77,
                },
            },
        },
    },
    ensure_ascii=False,
)

# (id, code, name, report_type, sentiment_score, operation_advice,
#  trend_prediction, analysis_summary, raw_result,
#  ideal_buy, secondary_buy, stop_loss, take_profit, created_at)
_ROWS = [
    (1, "002383", "上海沿浦", "full", 62, "持有", "震荡偏多",
     "均线多头排列, 回踩关注支撑。", _ERA_B_RAW, 7.77, 7.5, 7.1, 8.6,
     "2026-09-28 21:00:29.719335"),
    (2, "MARKET", "大盘复盘", "market_review", 55, "查看复盘", None,
     "大盘情绪偏暖, 结构性行情延续。", _MARKET_RAW, None, None, None, None,
     "2026-09-28 18:00:00.000000"),
    (3, "600519", "贵州茅台", "simple", 58, "观望", "震荡上行",
     "白马股防御属性凸显。", _ERA_A_RAW, None, None, 1600.0, 1800.0,
     "2024-01-02 10:00:00.000000"),
    # 坏行: code 缺失 (列表应跳过且不计入 total), 且 created_at 最新以验证跳过
    (4, None, None, "simple", None, None, None, "rerun", None, None, None, None, None,
     "2026-09-29 10:00:00.000000"),
    # raw_result 为 NULL 的重跑合成行 (对应真实库 id=1011)
    (5, "000001", "平安银行", "full", 50, "观望", None, "重跑合成记录", None,
     None, None, None, None, "2024-01-01 00:00:00.000000"),
    # raw_result JSON 损坏行
    (6, "300750", "宁德时代", "full", 48, "观望", "高位震荡", "JSON 损坏行的摘要",
     '{"dashboard": {', None, None, None, None, "2024-01-01 09:00:00.000000"),
    # 理想买点被风控抑制 (对应真实库 id=1463)
    (7, "002383", "上海沿浦", "full", 70, "买入", "多头趋势", "强势股回踩即是机会。",
     _SUPPRESSED_RAW, None, None, 7.1, 8.6, "2026-09-27 21:00:00.000000"),
]

_INSERT_SQL = """
INSERT INTO analysis_history (
    id, code, name, report_type, sentiment_score, operation_advice,
    trend_prediction, analysis_summary, raw_result,
    ideal_buy, secondary_buy, stop_loss, take_profit, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# 好行按 created_at 倒序 (坏行 id=4 已剔除)
_DESC_IDS = ["1", "2", "7", "3", "6", "5"]


def _create_mini_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(_DDL)
        conn.executemany(_INSERT_SQL, _ROWS)
        conn.commit()
    finally:
        conn.close()


def _make_client() -> TestClient:
    app = FastAPI()
    registrar = BackendExtensionRegistrar(
        dsa_bridge.EXTENSION_ID, api_version=BACKEND_EXTENSION_API_VERSION,
    )
    dsa_bridge.setup(registrar)
    for router in registrar.routers:
        app.include_router(router)
    return TestClient(app)


@pytest.fixture
def legacy_db(tmp_path) -> Path:
    db_path = tmp_path / "stock_analysis.db"
    _create_mini_db(db_path)
    return db_path


@pytest.fixture
def client(legacy_db, monkeypatch) -> TestClient:
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    return _make_client()


# ================================================================
# 列表
# ================================================================

def test_list_orders_desc_maps_summary_and_skips_bad_row(client):
    resp = client.get(LEGACY_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 6  # 7 行中坏行 (code 缺失) 跳过不计入
    assert [item["id"] for item in body["items"]] == _DESC_IDS
    first = body["items"][0]
    assert set(first.keys()) == _SUMMARY_KEYS  # 契约字段, 不多不少
    assert first["id"] == "1"  # 库内 INTEGER, 契约 string
    assert first["symbol"] == "002383"
    assert first["created_at"] == "2026-09-28T21:00:29"  # 空格换 T, 秒精度
    assert first["operation_advice"] == "持有"
    assert first["sentiment_score"] == 62
    assert first["ideal_buy"] == 7.77
    assert first["secondary_buy"] == 7.5
    assert first["stop_loss"] == 7.1
    assert first["take_profit"] == 8.6
    assert first["analysis_summary"] == "均线多头排列, 回踩关注支撑。"
    # market_review 行原样返回, 点位为空
    market = body["items"][1]
    assert market["symbol"] == "MARKET"
    assert market["ideal_buy"] is None


def test_list_symbol_filter(client):
    resp = client.get(LEGACY_URL, params={"symbol": "002383"})
    body = resp.json()
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == ["1", "7"]

    assert client.get(LEGACY_URL, params={"symbol": "MARKET"}).json()["total"] == 1
    empty = client.get(LEGACY_URL, params={"symbol": "999999"}).json()
    assert empty == {"total": 0, "items": []}


def test_list_pagination(client):
    body = client.get(LEGACY_URL, params={"limit": 2, "offset": 2}).json()
    assert body["total"] == 6  # total 不随分页变化
    assert [item["id"] for item in body["items"]] == ["7", "3"]
    tail = client.get(LEGACY_URL, params={"limit": 20, "offset": 6}).json()
    assert tail["total"] == 6
    assert tail["items"] == []


def test_list_limit_capped_at_100(legacy_db, monkeypatch):
    conn = sqlite3.connect(legacy_db)
    try:
        for i in range(105):
            conn.execute(
                "INSERT INTO analysis_history (code, name, report_type, created_at)"
                " VALUES (?, ?, ?, ?)",
                (f"60{i % 100:04d}", f"填充{i}", "full", "2026-09-20 10:00:00.000000"),
            )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", legacy_db)
    client = _make_client()
    body = client.get(LEGACY_URL, params={"limit": 500}).json()
    assert body["total"] == 111  # 6 好行 + 105, 坏行不计入
    assert len(body["items"]) == 100  # limit 上限 100


def test_list_param_validation(client):
    assert client.get(LEGACY_URL, params={"limit": 0}).status_code == 400
    assert client.get(LEGACY_URL, params={"offset": -1}).status_code == 400


def test_list_db_missing_degrades_to_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", tmp_path / "missing.db")
    client = _make_client()
    resp = client.get(LEGACY_URL)
    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "items": []}


def test_list_corrupt_file_degrades_to_empty(tmp_path, monkeypatch):
    bogus = tmp_path / "not_a_db.db"
    bogus.write_text("this is not a sqlite database", encoding="utf-8")
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", bogus)
    client = _make_client()
    assert client.get(LEGACY_URL).json() == {"total": 0, "items": []}


def test_list_locked_db_degrades_then_recovers(client, legacy_db):
    blocker = sqlite3.connect(legacy_db)
    try:
        blocker.execute("BEGIN EXCLUSIVE")  # 独占锁: 只读桥接应等 3s 后降级
        resp = client.get(LEGACY_URL)
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}
    finally:
        blocker.rollback()
        blocker.close()
    assert client.get(LEGACY_URL).json()["total"] == 6  # 锁释放后恢复


# ================================================================
# 详情与 markdown 拼装
# ================================================================

def test_detail_era_b_full_markdown(client):
    resp = client.get(f"{LEGACY_URL}/1")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == _SUMMARY_KEYS | {"markdown"}
    md = body["markdown"]
    # 表头: 列值 + JSON 元数据
    assert "# 上海沿浦 · DSA 历史分析报告" in md
    assert "- 代码: 002383" in md
    assert "- 时间: 2026-09-28T21:00:29" in md
    assert "- 类型: full" in md
    assert "- 情绪评分: 62" in md
    assert "- 操作建议: 持有" in md
    assert "- 现价: 7.88" in md
    assert "- 涨跌幅: +1.02%" in md
    assert "- 模型: deepseek-chat" in md
    # dashboard 各节
    assert "## 核心结论" in md
    assert "- 一句话结论: 多头结构完好, 持股待涨。" in md
    assert "- 持仓建议: 持有, 跌破 7.1 止损" in md
    assert "## 狙击点位" in md
    assert "- 理想买点: 7.77" in md
    assert "- 止盈: 8.6" in md  # 8.60 → 8.6
    assert "## 仓位策略" in md
    assert "- 建议仓位: 3 成仓位试仓" in md
    assert "## 阶段决策" in md
    assert "- 行动窗口: 突破 8.6 放量后" in md
    assert "- 盯盘条件:" in md
    assert "  - 放量站上 8.6" in md
    assert "- 下次检查: 2026-09-30 10:30" in md
    # 列值优先于 JSON 顶层
    assert "## 趋势预判" in md
    assert "震荡偏多" in md
    assert "JSON 趋势预判(应被列值覆盖)" not in md
    assert "JSON 摘要(应被列值覆盖)" not in md
    assert "均线多头排列" in md
    # 顶层非空长段落逐段追加, 空段省略
    assert "### 技术面分析" in md
    assert "技术面: 均线多头排列, 量能温和放大。" in md
    assert "### 风险提示" in md
    assert "### 基本面分析" not in md


def test_detail_market_review_uses_news_summary(client):
    body = client.get(f"{LEGACY_URL}/2").json()
    assert body["symbol"] == "MARKET"
    assert body["ideal_buy"] is None
    md = body["markdown"]
    assert "## 2026-09-28 大盘复盘" in md
    assert "两市缩量整理" in md
    assert "[dsa-market-region]" not in md  # news_summary 优先于 raw_response


def test_detail_era_a_uses_column_values(client):
    body = client.get(f"{LEGACY_URL}/3").json()
    assert body["stop_loss"] == 1600.0
    md = body["markdown"]
    assert "震荡上行" in md
    assert "JSON 旧趋势" not in md  # 列值优先
    assert "JSON 旧摘要" not in md
    assert "- 止损: 1600" in md
    assert "## 核心结论" not in md  # era A 无 dashboard 结构


def test_detail_raw_result_null_falls_back_to_columns(client):
    resp = client.get(f"{LEGACY_URL}/5")
    assert resp.status_code == 200
    md = resp.json()["markdown"]
    assert md.startswith("# 平安银行 · DSA 历史分析报告")
    assert "重跑合成记录" in md


def test_detail_malformed_raw_result_falls_back_to_columns(client):
    resp = client.get(f"{LEGACY_URL}/6")
    assert resp.status_code == 200
    md = resp.json()["markdown"]
    assert "JSON 损坏行的摘要" in md
    assert "高位震荡" in md


def test_detail_suppressed_ideal_buy_annotated(client):
    body = client.get(f"{LEGACY_URL}/7").json()
    assert body["ideal_buy"] is None  # 列 NULL 是真实业务状态
    assert body["stop_loss"] == 7.1
    assert "风控抑制, 原值 7.77" in body["markdown"]


def test_detail_bad_row_404(client):
    resp = client.get(f"{LEGACY_URL}/4")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "报告不存在"}


def test_detail_missing_and_non_numeric_id(client):
    assert client.get(f"{LEGACY_URL}/9999").json() == {"detail": "报告不存在"}
    assert client.get(f"{LEGACY_URL}/abc").json() == {"detail": "报告不存在"}


def test_detail_db_missing_404_contract_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", tmp_path / "missing.db")
    client = _make_client()
    resp = client.get(f"{LEGACY_URL}/1")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "旧库不可用"}


def test_build_legacy_markdown_never_blank():
    assert dsa_bridge.build_legacy_markdown({}).startswith("#")
    assert dsa_bridge.build_legacy_markdown({
        "id": 1, "code": None, "name": None, "raw_result": None,
    }).startswith("#")


# ================================================================
# health 不回归
# ================================================================

def test_health_no_regression(client):
    resp = client.get(HEALTH_URL)
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "dsa_db_found": True,
        "analysis_history_count": 7,  # COUNT(*) 原始行数 (含坏行), 原有语义
    }


def test_health_missing_db_shape_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(dsa_bridge, "DSA_DB_PATH", tmp_path / "missing.db")
    client = _make_client()
    resp = client.get(HEALTH_URL)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "dsa_db_found": False, "analysis_history_count": None}
