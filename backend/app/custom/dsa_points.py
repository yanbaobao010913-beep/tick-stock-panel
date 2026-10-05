"""DSA 点位回测策略的信号物化器 (迁移计划 §4.7-2b)。

把区间内的 DSA 分析报告 (dsa_analysis 报告库) 物化成核心回测引擎的信号行,
供 backtest/strategy.py 的 matrix_native 分支在 dsa_points 策略上特判接入:

  - operation_advice 含 卖出/减仓/清仓 → 离场信号行 (报告日, 引擎 exit_delay
    次日开盘成交); 含 买入/加仓 → 入场候选; 观望/持有 → 不动 (DSA §4.6-1 映射,
    卖出类关键词优先 —— 同文矛盾时取保守侧, 与 dsa_paper_bridge 一致)
  - 入场行放在报告日次一交易日 (严格大于报告日的第一根K), entry_delay=0
    (成交日即信号日, 由 backtest/strategy.py 钉死口径), 逐格注入 ideal_buy
    作为介入价: 触及 (low <= ideal_buy) 才成交, 成交价 min(当日 open,
    ideal_buy), 未触及不建仓 —— 触及/成交语义由 MatcherConfig.entry_touch 开启
  - 报告 stop_loss / take_profit 注入为跟随入场格的逐仓位绝对风控线; 缺点位
    的报告照常入场 (线回退配置级/百分比口径), 缺 ideal_buy 的买入建议不建仓
    (DSA 口径: 不许编)

同一 (票, 入场日) 多报告同日时取更晚创建的报告 (list_reports 倒序反转后后写
生效); 同票同日多条卖出建议按布尔并集。分数恒 0 (仓位模拟等权配置)。
"""
from __future__ import annotations

import bisect
import logging
from datetime import date

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from app.custom import dsa_analysis, dsa_paper_bridge
from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
)

logger = logging.getLogger(__name__)

# app/custom/*.py 由扩展 loader 自动发现, 统一要求 EXTENSION_ID + setup:
# 本模块是被 backtest/strategy.py 特判调用的物化器, 无路由无 startup。
EXTENSION_ID = "dsa.points"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

_ENTRY_ADVICE_KEYWORDS = ("买入", "加仓")   # DSA §4.6-1 入场映射 (卖出类见 bridge)


def _positive_price(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if f > 0 and np.isfinite(f) else None


def _is_entry_advice(advice: str) -> bool:
    return any(keyword in advice for keyword in _ENTRY_ADVICE_KEYWORDS)


def materialize_dsa_signal_rows(
    market: MarketDataMatrix,
    start: date,
    end: date,
) -> SignalMatrix:
    """区间内 DSA 报告 → 信号矩阵 (entry/exit + 逐格介入/止损/止盈价)。

    market 提供 K 线轴 (timestamp_labels/symbols); 报告 symbol 与轴 symbol 均
    为后缀点分式 (dsa_analysis 物化时已归一), 直接字符串匹配, 匹配不到的票
    (区间/资产类型外) 跳过。任何单票异常只跳过该报告并记日志, 不拖垮整体。
    """
    shape = market.shape
    symbol_index = {str(sym): i for i, sym in enumerate(market.symbols)}
    day_index: dict[str, int] = {}
    for t, label in enumerate(market.timestamp_labels):
        day_index.setdefault(str(label)[:10], t)
    sorted_days = sorted(day_index)

    entry = np.zeros(shape, dtype=np.uint8)
    exit_signal = np.zeros(shape, dtype=np.uint8)
    score = np.zeros(shape, dtype=np.float32)
    entry_price = np.full(shape, np.nan, dtype=np.float32)
    stop_price = np.full(shape, np.nan, dtype=np.float32)
    take_profit_price = np.full(shape, np.nan, dtype=np.float32)

    start_text, end_text = start.isoformat(), end.isoformat()
    reports = dsa_analysis._STORE.list_reports(None)
    materialized = 0
    # list_reports 按 created_at 倒序 → 反转成升序, 同 (票,入场日) 更晚报告后写生效
    for report in reversed(reports):
        day = str(report.get("created_at") or "")[:10]
        if not (start_text <= day <= end_text):
            continue
        asset_id = symbol_index.get(str(report.get("symbol") or ""))
        if asset_id is None:
            continue
        advice = str(report.get("operation_advice") or "")
        points = report.get("points") if isinstance(report.get("points"), dict) else {}
        try:
            if dsa_paper_bridge._is_sell_advice(advice):
                t = day_index.get(day)
                if t is not None:
                    exit_signal[t, asset_id] = 1   # 引擎 exit_delay=1 → 次日开盘离场
                continue
            if not _is_entry_advice(advice):
                continue   # 观望/持有/空 → 不动
            ideal_buy = _positive_price(points.get("ideal_buy"))
            if ideal_buy is None:
                continue   # 买入类但无介入价: 不建仓 (不许编)
            next_day = None
            pos = bisect.bisect_right(sorted_days, day)
            if pos < len(sorted_days):
                next_day = sorted_days[pos]
            if next_day is None:
                continue   # 报告日之后无 K 线, 无次日可成交
            t = day_index[next_day]
            entry[t, asset_id] = 1
            entry_price[t, asset_id] = ideal_buy
            stop = _positive_price(points.get("stop_loss"))
            take = _positive_price(points.get("take_profit"))
            if stop is not None:
                stop_price[t, asset_id] = stop
            if take is not None:
                take_profit_price[t, asset_id] = take
            materialized += 1
        except Exception as e:  # 单报告脏数据跳过, 不拖垮整体
            logger.warning("dsa points: 报告 %s 物化跳过: %s", report.get("id"), e)

    logger.info(
        "dsa points: 物化 %d 条入场信号 (报告窗口 %s~%s, 市场 %s)",
        materialized, start, end, shape,
    )
    return make_signal_matrix(
        shape,
        entry=entry,
        exit=exit_signal,
        score=score,
        entry_price=entry_price,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
    )


def setup(registrar: BackendExtensionRegistrar) -> None:
    """无路由: 物化器由回测路径同步调用 (loader 仅要求 setup 存在)。"""
    _ = registrar
