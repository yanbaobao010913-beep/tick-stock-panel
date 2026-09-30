"""DSA 报警扩展 (迁移计划 P4 §4.5-3 回放审计)。

回放审计: 交易日 15:30 (收盘后) 把当天 DSA· 触发记录逐条对当日收盘,
markdown 落 data/user_data/dsa_alert_audit/YYYY-MM-DD.md;
POST /alert-audit/run 可手动补跑当天 (盘后联调用)。

(§4.5-2 critical 开声 2026-09-30 用户否决, 不做。)
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)
from app.market_time import cn_now
from app.services import alert_store
from app.services.trading_day import is_trading_day

logger = logging.getLogger(__name__)

EXTENSION_ID = "dsa.alerts"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

_AUDIT_DIR_NAME = "dsa_alert_audit"
_AUDIT_TIME = (15, 30)          # 交易日 15:30 自动跑
_SCHED_TICK_SECONDS = 60.0

_RUNTIME: dict[str, Any] = {"repo": None, "data_dir": None}
_AUDIT_DONE_DATE: str | None = None  # 本进程已跑审计的日期 (防重复; 文件存在性另判)


# ================================================================
# 回放审计
# ================================================================

def _audit_dir() -> Path:
    return Path(_RUNTIME["data_dir"]) / _AUDIT_DIR_NAME


def _audit_path(day: str) -> Path:
    return _audit_dir() / f"{day}.md"


def _rule_op_price(ev: dict) -> tuple[str | None, float | None]:
    conds = ev.get("conditions")
    if isinstance(conds, list) and conds and isinstance(conds[0], dict):
        op = conds[0].get("op")
        value = conds[0].get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return (str(op) if op else None), float(value)
    return None, None


def _day_close(symbol: str, day: str) -> float | None:
    """repo 取该票当日收盘价; 失败回落 None (审计宁缺不编)。"""
    repo = _RUNTIME.get("repo")
    if repo is None:
        return None
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
        asset_type = repo.resolve_asset_type(symbol)
        df = repo.get_daily_asset(asset_type, symbol, d, d, columns=["date", "close"])
        if df is None or df.is_empty():
            return None
        return float(df["close"][0])
    except Exception as e:  # noqa: BLE001
        logger.info("审计取收盘价失败 %s %s: %s", symbol, day, e)
        return None


def _verdict(op: str | None, threshold: float | None, close: float | None) -> str:
    """触线后收盘落点判定: <= 线看是否收回线上, >= 线看是否站稳。"""
    if op is None or threshold is None or close is None:
        return "—"
    if op == "<=":
        return "收盘仍在线下" if close <= threshold else "收盘收回线上"
    if op == ">=":
        return "收盘站稳线上" if close >= threshold else "收盘回落线下"
    return "—"


def run_audit(day: str | None = None) -> dict:
    """跑一天审计并落盘。返回 {date, total, path}。非交易日且无触发也照出报告 (空报告)。"""
    day = day or cn_now().date().isoformat()
    events = alert_store.list_recent(Path(_RUNTIME["data_dir"]), days=2, limit=2000)
    day_events = [
        ev for ev in events
        if datetime.fromtimestamp(float(ev.get("ts", 0)) / 1000).date().isoformat() == day
        and (str(ev.get("name") or "").startswith("DSA·") or str(ev.get("rule_id") or "").startswith("dsa_"))
    ]
    day_events.sort(key=lambda ev: float(ev.get("ts", 0)))

    lines = [
        f"# DSA 告警回放审计 {day}",
        "",
        f"共 {len(day_events)} 条 DSA· 触发记录。",
        "",
        "| 时间 | 代码 | 规则 | 触发价 | 规则阈值 | 当日收盘/最新 | 判定 |",
        "|---|---|---|---|---|---|---|",
    ]
    for ev in day_events:
        ts = datetime.fromtimestamp(float(ev.get("ts", 0)) / 1000).strftime("%H:%M:%S")
        symbol = str(ev.get("symbol") or "—")
        rule_name = str(ev.get("name") or ev.get("rule_id") or "—")
        trigger_price = ev.get("price")
        op, threshold = _rule_op_price(ev)
        close = _day_close(symbol, day)
        verdict = _verdict(op, threshold, close)
        tp = f"{trigger_price:.2f}" if isinstance(trigger_price, (int, float)) else "—"
        th = f"{threshold:.2f}" if threshold is not None else "—"
        cl = f"{close:.2f}" if close is not None else "—"
        lines.append(f"| {ts} | {symbol} | {rule_name} | {tp} | {th} | {cl} | {verdict} |")

    path = _audit_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    logger.info("DSA 告警审计已生成: %s (%d 条)", path, len(day_events))
    return {"date": day, "total": len(day_events), "path": str(path)}


def _scheduler_loop() -> None:
    global _AUDIT_DONE_DATE
    while True:
        try:
            now = cn_now()
            day = now.date().isoformat()
            if (
                is_trading_day(now)
                and (now.hour, now.minute) >= _AUDIT_TIME
                and _AUDIT_DONE_DATE != day
                and not _audit_path(day).exists()
            ):
                run_audit(day)
                _AUDIT_DONE_DATE = day
        except Exception as e:  # noqa: BLE001
            logger.warning("告警审计调度异常: %s", e)
        time.sleep(_SCHED_TICK_SECONDS)


# ================================================================
# 扩展挂载
# ================================================================

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def setup(registrar: BackendExtensionRegistrar) -> None:
    router = APIRouter(prefix="/api/ext/dsa", tags=["dsa-alerts"])

    @router.get("/alert-audit")
    def list_audits() -> dict:
        d = _audit_dir()
        items = []
        if d.exists():
            for p in sorted(d.glob("????-??-??.md"), reverse=True):
                day = p.stem
                if not _DATE_RE.match(day):
                    continue
                try:
                    head = p.read_text(encoding="utf-8").splitlines()
                    m = re.search(r"共 (\d+) 条", "\n".join(head[:4]))
                    items.append({"date": day, "total": int(m.group(1)) if m else 0, "path": str(p)})
                except OSError:
                    continue
        return {"items": items}

    @router.post("/alert-audit/run")
    def run_audit_now() -> dict:
        return run_audit()

    @router.get("/alert-audit/{day}")
    def get_audit(day: str) -> dict:
        if not _DATE_RE.match(day):
            return {"date": day, "markdown": ""}
        p = _audit_path(day)
        if not p.exists():
            return {"date": day, "markdown": ""}
        return {"date": day, "markdown": p.read_text(encoding="utf-8")}

    registrar.include_router(router)


def startup(context: ExtensionContext) -> None:
    _RUNTIME["repo"] = context.repository
    _RUNTIME["data_dir"] = Path(context.data_dir)
    threading.Thread(target=_scheduler_loop, name="dsa-alert-audit", daemon=True).start()
