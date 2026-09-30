"""DSA 迁移可行性探针与历史只读桥接: 以只读方式访问 DSA 的 sqlite 分析库。

health 探针: 报告旧库可见性与 analysis_history 行数 (前端迁移探针在用, 契约不变)。
§3.3 历史桥接: /legacy/reports 列表与 /legacy/reports/{id} 详情, 每次请求新建
URI mode=ro 只读连接 (busy timeout 3s, 用完即关), 绝不写入旧库; 旧库缺失/
锁定/查询失败时按契约降级 (列表 {total: 0, items: []} / 详情 404), 不抛 5xx。
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)

logger = logging.getLogger(__name__)

DSA_DB_PATH = Path(r"D:\Documents\daily_stock_analysis\data\stock_analysis.db")

EXTENSION_ID = "dsa.bridge"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

_HEARTBEAT_INTERVAL_SECONDS = 5.0
_LEGACY_DB_TIMEOUT_SECONDS = 3.0
_LEGACY_MAX_LIMIT = 100
_LEGACY_POINT_KEYS = ("ideal_buy", "secondary_buy", "stop_loss", "take_profit")
_LEGACY_LIST_COLUMNS = (
    "id",
    "code",
    "created_at",
    "operation_advice",
    "sentiment_score",
    *_LEGACY_POINT_KEYS,
    "analysis_summary",
)


def _read_analysis_history_count() -> tuple[bool, int | None]:
    """只读连接 DSA 库; 文件缺失或查询失败时返回 (False, None), 绝不抛给路由."""
    if not DSA_DB_PATH.exists():
        return False, None
    try:
        uri = f"file:{DSA_DB_PATH.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute("SELECT COUNT(*) FROM analysis_history").fetchone()
        return True, (int(row[0]) if row else None)
    except (sqlite3.Error, OSError):
        return False, None


# ================================================================
# DSA 历史只读桥接 (迁移计划 §3.3): 列值映射与 markdown 拼装
# ================================================================

def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_float(value: Any) -> float | None:
    """数值字段容忍字符串数字; 解析不了置空 (不许编)。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _coerce_int(value: Any) -> int | None:
    number = _coerce_float(value)
    if number is None or number != int(number):
        return None
    return int(number)


def _fmt_price(value: Any) -> str | None:
    number = _coerce_float(value)
    if number is None:
        return None
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _legacy_iso_created_at(raw: Any) -> str | None:
    """DSA created_at ('YYYY-MM-DD HH:MM:SS.ffffff' 北京墙钟) → ISO 秒精度。

    库内无时区标记, 按 DSA 本机即北京时区的口径原样转 naive ISO (与
    dsa_analysis 的 created_at 口径一致); 解析不了的畸形值原样返回, 不编不丢。
    """
    text = _clean_text(raw)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        return text
    return parsed.replace(tzinfo=None).isoformat(timespec="seconds")


def _legacy_summary_from_row(row: Mapping[str, Any]) -> dict | None:
    """analysis_history 行 → LegacyReportSummary (契约 §3.3)。

    id (库内 INTEGER, 前端契约 string) 与 code 是身份字段, 缺失视为损坏行
    返回 None 由调用方跳过; 其余字段取不到置空, 不许编。
    """
    rec_id = row.get("id")
    symbol = _clean_text(row.get("code"))
    if rec_id is None or symbol is None:
        return None
    return {
        "id": str(rec_id),
        "symbol": symbol,
        "created_at": _legacy_iso_created_at(row.get("created_at")),
        "operation_advice": _clean_text(row.get("operation_advice")),
        "sentiment_score": _coerce_int(row.get("sentiment_score")),
        **{key: _coerce_float(row.get(key)) for key in _LEGACY_POINT_KEYS},
        "analysis_summary": _clean_text(row.get("analysis_summary")),
    }


def _json_dict(raw: Any) -> dict | None:
    """raw_result TEXT → dict; 缺失/损坏/非对象一律 None (调用方走列值兜底)。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _dig(data: Any, *keys: str) -> Any:
    """沿嵌套 dict 逐层取值; 任一层缺失或非 dict 返回 None。"""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _md_section(lines: list[str], title: str, body: list[str]) -> None:
    """body 有实际内容时输出二级标题小节; 全空则整节省略。"""
    kept = [line for line in body if line]
    if kept:
        lines.append(f"## {title}")
        lines.extend(kept)
        lines.append("")


_LEGACY_POINT_LABELS = (
    ("ideal_buy", "理想买点"),
    ("secondary_buy", "次级买点"),
    ("stop_loss", "止损"),
    ("take_profit", "止盈"),
)

_MARKET_REVIEW_TYPE = "market_review"
_MARKET_REVIEW_CODE = "MARKET"
# era B 顶层长段落白名单 (侦查: 其余顶层键多为空串或元数据, 不做全量倾倒)
_LEGACY_LONG_SECTIONS = (
    ("technical_analysis", "技术面分析"),
    ("fundamental_analysis", "基本面分析"),
    ("news_summary", "消息面摘要"),
    ("risk_warning", "风险提示"),
    ("buy_reason", "买入理由"),
)


def _sniper_lines(row: Mapping[str, Any], dashboard: dict | None) -> list[str]:
    """四点位列表 (列是唯一真源); 理想买点被风控抑制时用 JSON 原值注记。"""
    lines: list[str] = []
    has_ideal = False
    for key, label in _LEGACY_POINT_LABELS:
        text = _fmt_price(row.get(key))
        if text:
            lines.append(f"- {label}: {text}")
            has_ideal = has_ideal or key == "ideal_buy"
    if not has_ideal:
        sniper = _dig(dashboard, "battle_plan", "sniper_points")
        if isinstance(sniper, dict) and sniper.get("suppressed_by_guardrail"):
            original = _fmt_price(sniper.get("ideal_buy_presuppression"))
            suffix = f"(风控抑制, 原值 {original})" if original else "(被风控抑制)"
            lines.append(f"- 理想买点: 暂无{suffix}")
    return lines


def build_legacy_markdown(row: Mapping[str, Any]) -> str:
    """把 analysis_history 单行拼成可读 markdown (纯函数, 便于测试)。

    兼容三个时代 (侦查实测): era B (dashboard 为 dict) 拼 元信息 + 核心结论/
    狙击点位/仓位策略/阶段决策 + 趋势预判/分析摘要 (列值优先) + 顶层非空长段落;
    market_review 正文直接用 JSON news_summary (兜底 raw_response / 摘要列);
    era A 旧 simple 与 raw_result 缺失/损坏的行退化为列值 (趋势预判+摘要+点位)。
    任何输入都至少返回标题与元信息, 详情接口不会拿到空正文。
    """
    data = _json_dict(row.get("raw_result"))
    dashboard = data.get("dashboard") if isinstance(data, dict) else None
    if not isinstance(dashboard, dict):
        dashboard = None

    code = _clean_text(row.get("code"))
    name = _clean_text(row.get("name")) or code or "未知标的"
    created_at = (
        _legacy_iso_created_at(row.get("created_at"))
        or _clean_text(row.get("created_at"))
        or "未知时间"
    )
    report_type = _clean_text(row.get("report_type"))

    lines = [f"# {name} · DSA 历史分析报告", ""]
    meta = [f"- 代码: {code or '未知'}", f"- 时间: {created_at}"]
    if report_type:
        meta.append(f"- 类型: {report_type}")
    score = _coerce_int(row.get("sentiment_score"))
    if score is not None:
        meta.append(f"- 情绪评分: {score}")
    advice = _clean_text(row.get("operation_advice"))
    if advice:
        meta.append(f"- 操作建议: {advice}")
    if isinstance(data, dict):
        price = _fmt_price(data.get("current_price"))
        if price:
            meta.append(f"- 现价: {price}")
        change = _coerce_float(data.get("change_pct"))  # DSA 口径: 涨跌幅(%)
        if change is not None:
            meta.append(f"- 涨跌幅: {change:+.2f}%")
        model = _clean_text(data.get("model_used"))
        if model:
            meta.append(f"- 模型: {model}")
    lines.extend(meta)
    lines.append("")

    if dashboard is None:
        is_market_review = report_type == _MARKET_REVIEW_TYPE or (
            code or ""
        ).upper() == _MARKET_REVIEW_CODE
        if is_market_review:
            body = (
                _clean_text(_dig(data, "news_summary"))
                or _clean_text(_dig(data, "raw_response"))
                or _clean_text(row.get("analysis_summary"))
            )
            if body:
                lines.extend([body, ""])
            return "\n".join(lines).strip()
        trend = _clean_text(row.get("trend_prediction")) or _clean_text(
            _dig(data, "trend_prediction"),
        )
        _md_section(lines, "趋势预判", [f"- {trend}" if trend else ""])
        summary = _clean_text(row.get("analysis_summary")) or _clean_text(
            _dig(data, "analysis_summary"),
        )
        _md_section(lines, "分析摘要", [summary or ""])
        _md_section(lines, "狙击点位", _sniper_lines(row, None))
        return "\n".join(lines).strip()

    core = _dig(dashboard, "core_conclusion")
    core_body: list[str] = []
    if isinstance(core, dict):
        for key, label in (
            ("signal_type", "信号类型"),
            ("one_sentence", "一句话结论"),
            ("time_sensitivity", "时效性"),
        ):
            text = _clean_text(core.get(key))
            if text:
                core_body.append(f"- {label}: {text}")
        position_advice = core.get("position_advice")
        if isinstance(position_advice, dict):
            for key, label in (("no_position", "空仓建议"), ("has_position", "持仓建议")):
                text = _clean_text(position_advice.get(key))
                if text:
                    core_body.append(f"- {label}: {text}")
    _md_section(lines, "核心结论", core_body)

    _md_section(lines, "狙击点位", _sniper_lines(row, dashboard))

    strategy_body: list[str] = []
    strategy = _dig(dashboard, "battle_plan", "position_strategy")
    if isinstance(strategy, dict):
        for key, label in (
            ("suggested_position", "建议仓位"),
            ("entry_plan", "进场计划"),
            ("risk_control", "风险控制"),
        ):
            text = _clean_text(strategy.get(key))
            if text:
                strategy_body.append(f"- {label}: {text}")
    _md_section(lines, "仓位策略", strategy_body)

    phase_body: list[str] = []
    phase = _dig(dashboard, "phase_decision")
    if isinstance(phase, dict):
        for key, label in (
            ("immediate_action", "当前动作"),
            ("action_window", "行动窗口"),
            ("next_check_time", "下次检查"),
        ):
            text = _clean_text(phase.get(key))
            if text:
                phase_body.append(f"- {label}: {text}")
        watch = phase.get("watch_conditions")
        if isinstance(watch, list):
            conditions = [t for t in (_clean_text(item) for item in watch) if t]
            if conditions:
                phase_body.append("- 盯盘条件:")
                phase_body.extend(f"  - {c}" for c in conditions)
    _md_section(lines, "阶段决策", phase_body)

    trend = _clean_text(row.get("trend_prediction")) or _clean_text(
        _dig(data, "trend_prediction"),
    )
    _md_section(lines, "趋势预判", [f"- {trend}" if trend else ""])
    summary = _clean_text(row.get("analysis_summary")) or _clean_text(
        _dig(data, "analysis_summary"),
    )
    _md_section(lines, "分析摘要", [summary or ""])

    for key, title in _LEGACY_LONG_SECTIONS:
        text = _clean_text(_dig(data, key))
        if text:
            lines.extend([f"### {title}", text, ""])

    return "\n".join(lines).strip()


# ================================================================
# DSA 历史只读桥接 (迁移计划 §3.3): sqlite 访问与路由
# ================================================================

def _connect_legacy_ro() -> sqlite3.Connection:
    """每次请求新建只读连接 (URI mode=ro + busy timeout), 用完即关, 绝不写入。"""
    uri = f"file:{DSA_DB_PATH.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=_LEGACY_DB_TIMEOUT_SECONDS)


def setup(registrar: BackendExtensionRegistrar) -> None:
    router = APIRouter(prefix="/api/ext/dsa", tags=["dsa-bridge"])

    @router.get("/health")
    def health() -> dict:
        found, count = _read_analysis_history_count()
        return {
            "status": "ok",
            "dsa_db_found": found,
            "analysis_history_count": count,
        }

    @router.get("/legacy/reports")
    def list_legacy_reports(
        symbol: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        if limit < 1:
            raise HTTPException(status_code=400, detail="limit 须 >= 1")
        if offset < 0:
            raise HTTPException(status_code=400, detail="offset 须 >= 0")
        limit = min(limit, _LEGACY_MAX_LIMIT)
        wanted = (symbol or "").strip() or None
        sql = f"SELECT {', '.join(_LEGACY_LIST_COLUMNS)} FROM analysis_history"
        params: tuple[Any, ...] = ()
        if wanted is not None:
            sql += " WHERE code = ?"
            params = (wanted,)
        sql += " ORDER BY created_at DESC, id DESC"
        try:
            with closing(_connect_legacy_ro()) as conn:
                conn.row_factory = sqlite3.Row
                rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        except (sqlite3.Error, OSError) as e:
            logger.warning("dsa legacy list degraded, old db unavailable: %s", e)
            return {"total": 0, "items": []}
        summaries = [
            item for item in (_legacy_summary_from_row(r) for r in rows)
            if item is not None
        ]
        return {"total": len(summaries), "items": summaries[offset:offset + limit]}

    @router.get("/legacy/reports/{report_id}")
    def get_legacy_report(report_id: str) -> dict:
        try:
            rec_id = int(report_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="报告不存在") from None
        try:
            with closing(_connect_legacy_ro()) as conn:
                conn.row_factory = sqlite3.Row
                raw = conn.execute(
                    "SELECT * FROM analysis_history WHERE id = ?", (rec_id,),
                ).fetchone()
        except (sqlite3.Error, OSError) as e:
            logger.warning("dsa legacy detail degraded, old db unavailable: %s", e)
            raise HTTPException(status_code=404, detail="旧库不可用") from None
        if raw is None:
            raise HTTPException(status_code=404, detail="报告不存在")
        row = dict(raw)
        summary = _legacy_summary_from_row(row)
        if summary is None:
            raise HTTPException(status_code=404, detail="报告不存在")
        return {**summary, "markdown": build_legacy_markdown(row)}

    registrar.include_router(router)


def _heartbeat_loop(log_path: Path) -> None:
    while True:
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now(UTC).isoformat()}\n")
        except OSError:
            pass
        with suppress(Exception):
            time.sleep(_HEARTBEAT_INTERVAL_SECONDS)


def startup(context: ExtensionContext) -> None:
    # daemon 线程每 5s 追加一行心跳; 钩子立即返回, 不阻塞启动, 线程不阻止进程退出.
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(context.data_dir / "dsa_bridge_heartbeat.log",),
        name="dsa-bridge-heartbeat",
        daemon=True,
    )
    thread.start()
