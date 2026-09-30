"""DSA 持仓账 (迁移计划 §4.4 P3): TSP 自有流水账 + FIFO 重放持仓 + DSA 旧库一次性导入。

定位: 自有持仓账 (手工记流水, 读时 FIFO 重放, 不存快照), 替代 DSA 桥成为 P2
watch-plan 的持仓来源; 切换点在 dsa_watch.load_positions (§4.4.3, 本模块不感知
watch)。DSA 旧库只用于一次性导入 (POST /portfolio/import-from-dsa)。

存储: data/user_data/dsa_portfolio/trades.jsonl, 一行一个 Trade JSON, 追加写;
删除/导入走全量原子重写 (临时文件 + os.replace, 与 dsa_analysis._atomic_write_json
同款模式); 进程内锁串行化 (POST 的重放校验+追加、DELETE 的过滤+校验+重写各自
在单次锁内完成, 校验与落盘不分离, 防并发线程交错丢流水); JSON 解析损坏行跳过
记 warning。账本被手工改出字段级脏数据时: 非法行重放跳过记 warning, 若因此
超卖则读路径抛 OversellError → 500 (fail-closed, 大声故障优于静默空仓)。

费用口径: fee 只记流水不资本化——FIFO 重放的批次成本只含 price*quantity,
avg_cost/total_cost 均不含费用, 持仓成本与市值、盈亏同口径; 导入 DSA 旧库时
fee = 旧库 fee + tax (总费用落一条流水)。

时间口径: traded_at 为北京墙钟 naive ISO 秒精度 (与 dsa_analysis._now_iso 同
口径); 入参带时区时换算到北京墙钟 (确定性的单位换算, 非编造), 纯日期视为当日
零点; 导入行用旧库 trade_date (仅日期)。

id 口径: 手动记账 tr_{yyyymmdd}_{HHMMSS}_{6位hex}; 导入行 dsa_{旧库id} 留溯源。

import 方向: 本模块只 import app.custom.dsa_analysis (§4.4.3 防成环; dsa_watch
单向 import 本模块), 故访问 DSA 旧库的只读连接自建 (与 dsa_bridge 同款 URI
mode=ro + busy timeout, 绝不写入), 不复用 dsa_bridge。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Body, HTTPException, Request

from app.custom import dsa_analysis
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.market_time import CN_TZ, cn_now

logger = logging.getLogger(__name__)

EXTENSION_ID = "dsa.portfolio"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

_DOTTED_SYMBOL_RE = re.compile(r"\d{6}\.(?:SH|SZ|BJ)")
_QTY_EPS = 1e-6  # 数量噪声容限 (A 股最小单位 1 股, 浮点残差远小于此)


class OversellError(ValueError):
    """FIFO 重放超卖 (中文消息; POST/DELETE 映射 422, 读路径视为账本损坏)。"""


# ================================================================
# FIFO 重放 (纯函数)
# ================================================================

def replay_positions(trades: list[dict]) -> dict[str, dict]:
    """流水 → FIFO 持仓 (纯函数, 不碰 IO)。

    按 (traded_at, 入账顺序) 稳定排序: 买入入队 (price, quantity), 卖出消费最老
    批次; 卖出超过当前持仓抛 OversellError (中文 ValueError)。字段非法的行
    (非有限正数 quantity/price、side 非法、symbol 缺失) 跳过记 warning——正常
    写入路径已校验, 该分支只兜手工改坏的账本。

    费用不资本化: 批次成本只含 price*quantity, fee 仅流水留痕, 持仓成本口径与
    市值一致。输出 {symbol: {"quantity", "avg_cost", "total_cost"}} (剩余批次
    按数量加权均价; total_cost=Σ剩余 qty*price), quantity>0 即持仓。
    """
    lots: dict[str, list[list[float]]] = {}  # symbol → [[price, qty], ...] 最老在前
    ordered = sorted(
        enumerate(trades),
        key=lambda pair: (str(pair[1].get("traded_at") or ""), pair[0]),
    )
    for _idx, trade in ordered:
        symbol = str(trade.get("symbol") or "").strip()
        side = trade.get("side")
        quantity = _finite_number(trade.get("quantity"))
        price = _finite_number(trade.get("price"))
        if (
            not symbol or side not in ("buy", "sell")
            or quantity is None or price is None or quantity <= 0 or price <= 0
        ):
            logger.warning("dsa portfolio: 流水行字段非法, 重放跳过: %r", trade)
            continue
        if side == "buy":
            lots.setdefault(symbol, []).append([price, quantity])
            continue
        remaining = quantity
        queue = lots.setdefault(symbol, [])
        while remaining > _QTY_EPS and queue:
            lot = queue[0]
            take = min(lot[1], remaining)
            lot[1] -= take
            remaining -= take
            if lot[1] <= _QTY_EPS:
                queue.pop(0)
        if remaining > _QTY_EPS:
            raise OversellError(
                f"{symbol} 卖出 {quantity} 股超过当前持仓 {quantity - remaining} 股, "
                "流水 FIFO 重放失败 (超卖)",
            )
    positions: dict[str, dict] = {}
    for symbol, queue in lots.items():
        qty_left = sum(lot[1] for lot in queue)
        if qty_left <= _QTY_EPS:
            continue  # quantity>0 即持仓, 清仓不出现
        cost = sum(lot[0] * lot[1] for lot in queue)
        positions[symbol] = {
            "quantity": round(qty_left, 6),
            "avg_cost": round(cost / qty_left, 6),
            "total_cost": round(cost, 6),
        }
    return positions


def _finite_number(value: Any) -> float | None:
    """有限数值才有效 (bool/字符串/NaN/Inf 一律 None); 重放内部容错用。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# ================================================================
# 流水存储: jsonl 追加写 + 全量原子重写, 进程内锁
# ================================================================

class TradeStore:
    """jsonl 流水存储 (一行一个 Trade JSON); 写路径均为「锁内校验+落盘」原子语义。"""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root  # 测试注入; None → settings.data_dir (懒解析, 测试可 monkeypatch)
        self._lock = threading.Lock()

    def _dir(self) -> Path:
        if self._root is not None:
            return Path(self._root)
        from app.config import settings

        return Path(settings.data_dir) / "user_data" / "dsa_portfolio"

    def _path(self) -> Path:
        return self._dir() / "trades.jsonl"

    def _read_all_unlocked(self) -> list[dict]:
        """文件 → Trade 列表 (文件序=入账顺序); 缺文件=空账; 损坏行跳过记 warning。"""
        try:
            text = self._path().read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        trades: list[dict] = []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("dsa portfolio: 流水第 %d 行损坏, 跳过: %s", lineno, e)
                continue
            if isinstance(data, dict):
                trades.append(data)
            else:
                logger.warning("dsa portfolio: 流水第 %d 行不是 JSON 对象, 跳过", lineno)
        return trades

    def list_trades(self) -> list[dict]:
        with self._lock:
            return self._read_all_unlocked()

    def _append_unlocked(self, trade: dict) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(trade, ensure_ascii=False) + "\n")

    def _rewrite_unlocked(self, trades: list[dict]) -> None:
        """全量原子重写 (临时文件 + os.replace); 失败清理临时文件后重抛。"""
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(
                "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in trades),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def append_checked(self, trade: dict) -> dict:
        """锁内读→含本笔 FIFO 重放校验→追加; 超卖抛 OversellError, 零落盘。"""
        with self._lock:
            replay_positions([*self._read_all_unlocked(), trade])
            self._append_unlocked(trade)
        return trade

    def delete(self, trade_id: str) -> tuple[bool, list[dict] | None]:
        """锁内读→按 id 过滤→剩余流水重放校验→原子重写。

        返回 (False, None)=id 不存在; 剩余重放超卖抛 OversellError (零落盘,
        防止删买入把账删坏); 成功返回 (True, 剩余流水)。
        """
        with self._lock:
            trades = self._read_all_unlocked()
            remaining = [t for t in trades if t.get("id") != trade_id]
            if len(remaining) == len(trades):
                return False, None
            replay_positions(remaining)
            self._rewrite_unlocked(remaining)
            return True, remaining

    def import_all(self, trades: list[dict]) -> int | None:
        """导入事务: 锁内复核账本为空后单次原子写入全部行。

        单次重写而非逐行 append: 中途崩溃不留半截账 (半截账会因幂等 409 永远
        无法重导)。账本非空返回 None (并发兜底, 调用方 409); 空列表不落盘。
        """
        with self._lock:
            if self._read_all_unlocked():
                return None
            if trades:
                self._rewrite_unlocked(trades)
            return len(trades)


_STORE = TradeStore()


# ================================================================
# 手动记账校验 (POST /portfolio/trades)
# ================================================================

def _make_manual_id() -> str:
    now = cn_now()
    return f"tr_{now:%Y%m%d}_{now:%H%M%S}_{uuid4().hex[:6]}"


def _positive_number(value: Any) -> float | None:
    """quantity/price: 仅接受 JSON 数值 (bool 拒绝), 有限且 > 0。"""
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def _nonnegative_number(value: Any) -> float | None:
    """fee: 数值且有限且 >= 0; 其余 (含字符串) 视为未提供。"""
    number = _finite_number(value)
    return number if number is not None and number >= 0 else None


def _parse_traded_at(value: Any) -> str | None:
    """traded_at → 北京墙钟 naive ISO 秒精度; 省略=现在, 纯日期=当日零点,
    带时区换算北京墙钟 (确定性换算); 解析不了返回 None (调用方 400)。"""
    if value is None:
        return dsa_analysis._now_iso()
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(CN_TZ).replace(tzinfo=None)
    return parsed.isoformat(timespec="seconds")


def _validated_trade(repo, payload: dict) -> dict:
    """POST body → 合法 Trade (字段契约同 §4.4.1); 不合法抛 400 (中文 detail)。"""
    raw_symbol = payload.get("symbol")
    if not isinstance(raw_symbol, str):
        raise HTTPException(status_code=400, detail="代码格式不对")
    symbol = dsa_analysis._canonical_symbol(repo, raw_symbol)
    if not _DOTTED_SYMBOL_RE.fullmatch(symbol):
        raise HTTPException(status_code=400, detail="代码格式不对")
    side = payload.get("side")
    side = side.strip().lower() if isinstance(side, str) else None
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side 仅支持 buy|sell")
    quantity = _positive_number(payload.get("quantity"))
    if quantity is None:
        raise HTTPException(status_code=400, detail="quantity 必须是有限正数")
    price = _positive_number(payload.get("price"))
    if price is None:
        raise HTTPException(status_code=400, detail="price 必须是有限正数")
    fee = payload.get("fee")
    if fee is not None:
        fee = _nonnegative_number(fee)
        if fee is None:
            raise HTTPException(status_code=400, detail="fee 必须是非负数")
    traded_at = _parse_traded_at(payload.get("traded_at"))
    if traded_at is None:
        raise HTTPException(status_code=400, detail="traded_at 无法解析 (ISO 日期或日期时间)")
    note = payload.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise HTTPException(status_code=400, detail="note 必须是字符串")
        note = note.strip() or None
    return {
        "id": _make_manual_id(),
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "fee": fee,
        "traded_at": traded_at,
        "note": note,
    }


# ================================================================
# DSA 旧库一次性导入 (只读; 连接自建, import 方向约束见模块 docstring)
# ================================================================

DSA_DB_PATH = Path(r"D:\Documents\daily_stock_analysis\data\stock_analysis.db")
_LEGACY_DB_TIMEOUT_SECONDS = 3.0
_LEGACY_TRADE_COLUMNS = (
    "id", "account_id", "symbol", "trade_date", "side", "quantity", "price", "fee", "tax", "note",
)


def _connect_legacy_ro() -> sqlite3.Connection:
    """DSA 旧库只读连接 (URI mode=ro + busy timeout), 用完即关, 绝不写入。"""
    uri = f"file:{DSA_DB_PATH.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=_LEGACY_DB_TIMEOUT_SECONDS)


def _fetch_legacy_trade_rows() -> list[dict]:
    """portfolio_trades 全量按 (trade_date, id) 升序 (即重放入账顺序); 库异常上抛。"""
    sql = f"SELECT {', '.join(_LEGACY_TRADE_COLUMNS)} FROM portfolio_trades ORDER BY trade_date, id"
    with closing(_connect_legacy_ro()) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql).fetchall()]


def _legacy_float(value: Any) -> float | None:
    """旧库数值列容错 (SQLite 无列类型约束, TEXT 数字也接受); 解析不了为 None。"""
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


def _legacy_date_to_iso(value: Any) -> str | None:
    """trade_date (DATE/TEXT) → naive ISO 秒精度; 纯日期=当日零点; 解析不了 None。"""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=None).isoformat(timespec="seconds")


def _map_legacy_trade_row(repo, row: dict) -> dict | None:
    """portfolio_trades 行 → Trade; 不可映射返回 None (调用方 skipped++)。

    跳过判据 (契约只把 side/quantity/price 定为硬性): side 非 buy/sell、
    quantity/price 非有限正数、symbol 规范化后仍非点分式、trade_date 解析不了、
    id 缺失。fee = 旧库 fee + tax (总费用口径); 两列全缺/不可解析 → null。
    """
    legacy_id = row.get("id")
    raw_symbol = row.get("symbol")
    if legacy_id is None or not isinstance(raw_symbol, str) or not raw_symbol.strip():
        return None
    side = str(row.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        return None
    quantity = _legacy_float(row.get("quantity"))
    price = _legacy_float(row.get("price"))
    if quantity is None or price is None or quantity <= 0 or price <= 0:
        return None
    symbol = dsa_analysis._canonical_symbol(repo, raw_symbol.strip())
    if not _DOTTED_SYMBOL_RE.fullmatch(symbol):
        return None
    traded_at = _legacy_date_to_iso(row.get("trade_date"))
    if traded_at is None:
        return None
    fee = _legacy_float(row.get("fee"))
    tax = _legacy_float(row.get("tax"))
    total_fee = None if fee is None and tax is None else (fee or 0.0) + (tax or 0.0)
    note = row.get("note")
    return {
        "id": f"dsa_{legacy_id}",
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "fee": total_fee,
        "traded_at": traded_at,
        "note": str(note).strip() or None if note is not None else None,
    }


# ================================================================
# 持仓视图 (GET /portfolio/positions)
# ================================================================

def _safe_name_map(repo, symbols: list[str]) -> dict[str, str]:
    if repo is None or not symbols:
        return {}
    try:
        return repo.get_name_map(symbols) or {}
    except Exception as e:
        logger.warning("dsa portfolio: 名称读取失败: %s", e)
        return {}


def _last_close(repo, symbol: str) -> float | None:
    """最近收盘价: 近 14 个自然日窗口 (今天-14天, 今天) 最后一行, 优先 raw_close
    (不复权, 与成本同口径, CONTRIBUTING §3.2); 缺列/缺值退化 close (前复权,
    除权日与历史成本比较存在口径误差, 接受并在此声明)。无数据/读取失败 →
    null (不编造)。"""
    if repo is None:
        return None
    end = cn_now().date()
    start = end - timedelta(days=14)
    try:
        asset_type = repo.resolve_asset_type(symbol)
    except Exception as e:
        logger.warning("dsa portfolio: resolve_asset_type(%s) 失败, 回退 stock: %s", symbol, e)
        asset_type = "stock"
    try:
        df = repo.get_daily_asset(
            asset_type, symbol, start, end, columns=["date", "raw_close", "close"],
        )
    except Exception as e:
        logger.warning("dsa portfolio: %s 日K读取失败: %s", symbol, e)
        return None
    if df.is_empty():
        return None
    row = df.tail(1).to_dicts()[0]
    for key in ("raw_close", "close"):
        value = row.get(key)
        number = _finite_number(value)
        if number is not None and number > 0:
            return number
    return None


def _build_positions(repo) -> dict:
    """重放 → 契约 {items, totals}。last_price 为 null 的持仓其市值/浮盈/浮盈率
    全 null (不编造); totals.market_value/unrealized_pnl 只要任一持仓缺价即 null
    (部分和不冒充总数); total_cost 恒可加 (与行情无关)。"""
    replayed = replay_positions(_STORE.list_trades())
    symbols = sorted(replayed)  # items 按 symbol 升序
    names = _safe_name_map(repo, symbols)
    items: list[dict] = []
    for symbol in symbols:
        pos = replayed[symbol]
        last_price = _last_close(repo, symbol)
        market_value = pos["quantity"] * last_price if last_price is not None else None
        pnl = market_value - pos["total_cost"] if market_value is not None else None
        pct = (
            pnl / pos["total_cost"] * 100
            if pnl is not None and pos["total_cost"] else None
        )
        items.append({
            "symbol": symbol,
            "name": names.get(symbol),
            "quantity": pos["quantity"],
            "avg_cost": pos["avg_cost"],
            "total_cost": pos["total_cost"],
            "last_price": last_price,
            "market_value": market_value,
            "unrealized_pnl": pnl,
            "unrealized_pnl_pct": pct,
        })
    price_missing = any(item["market_value"] is None for item in items)
    totals = {
        "total_cost": sum(item["total_cost"] for item in items),
        "market_value": (
            None if price_missing
            else sum(item["market_value"] for item in items)
        ),
        "unrealized_pnl": (
            None if price_missing
            else sum(item["unrealized_pnl"] for item in items)
        ),
    }
    return {"items": items, "totals": totals}


# ================================================================
# HTTP 路由 (契约 §4.4.2; prefix=/api/ext/dsa, 五条新路径零冲突)
# ================================================================

def setup(registrar: BackendExtensionRegistrar) -> None:
    router = APIRouter(prefix="/api/ext/dsa", tags=["dsa-portfolio"])

    @router.get("/portfolio/positions")
    def get_positions(request: Request) -> dict:
        repo = getattr(request.app.state, "repo", None)
        return _build_positions(repo)

    @router.get("/portfolio/trades")
    def list_trades(request: Request, symbol: str | None = None, limit: int = 100) -> dict:
        if limit < 1:
            raise HTTPException(status_code=400, detail="limit 须 >= 1")
        repo = getattr(request.app.state, "repo", None)
        trades = _STORE.list_trades()
        if symbol:
            # 查询参数先规范化: 裸 600519 能命中存储为 600519.SH 的流水
            wanted = dsa_analysis._canonical_symbol(repo, symbol)
            trades = [t for t in trades if t.get("symbol") == wanted]
        ordered = sorted(
            enumerate(trades),
            key=lambda pair: (str(pair[1].get("traded_at") or ""), pair[0]),
        )
        # traded_at 倒序; 同刻按入账序倒序 (后记的在前), 顺序确定
        items = [trade for _idx, trade in reversed(ordered)][:limit]
        return {"items": items}

    @router.post("/portfolio/trades")
    def add_trade(request: Request, payload: Annotated[dict, Body()]) -> dict:
        repo = getattr(request.app.state, "repo", None)
        trade = _validated_trade(repo, payload)
        try:
            return _STORE.append_checked(trade)
        except OversellError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None

    @router.delete("/portfolio/trades/{trade_id}")
    def delete_trade(trade_id: str) -> dict:
        try:
            found, _remaining = _STORE.delete(trade_id)
        except OversellError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        if not found:
            raise HTTPException(status_code=404, detail="流水不存在")
        return {"ok": True}

    @router.post("/portfolio/import-from-dsa")
    def import_from_dsa(request: Request) -> dict:
        repo = getattr(request.app.state, "repo", None)
        if _STORE.list_trades():
            raise HTTPException(status_code=409, detail="本账已有流水, 拒绝重复导入")
        try:
            rows = _fetch_legacy_trade_rows()
        except (sqlite3.Error, OSError) as e:
            logger.warning("dsa portfolio import: DSA 旧库不可用: %s", e)
            raise HTTPException(status_code=400, detail="DSA 旧库不可用") from None
        staged: list[dict] = []
        skipped = 0
        for row in rows:
            trade = _map_legacy_trade_row(repo, row)
            if trade is None:
                skipped += 1
                continue
            try:
                replay_positions([*staged, trade])
            except OversellError:
                logger.warning(
                    "dsa portfolio import: 旧库行 id=%s 会造成超卖, 跳过", row.get("id"),
                )
                skipped += 1
                continue
            staged.append(trade)
        imported = _STORE.import_all(staged)
        if imported is None:
            # 并发兜底: 校验后另一线程先落了流水 → 维持幂等拒绝
            raise HTTPException(status_code=409, detail="本账已有流水, 拒绝重复导入")
        return {"imported": imported, "skipped": skipped}

    registrar.include_router(router)
