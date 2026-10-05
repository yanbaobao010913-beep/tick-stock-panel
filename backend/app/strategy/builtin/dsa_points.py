"""DSA 点位 — 用核心回测引擎重放 DSA 分析报告的四点位 (迁移计划 §4.7-2b)。

回测运行时由 app/custom/dsa_points.materialize_dsa_signal_rows 把区间内的
DSA 分析报告物化成信号行 (本文件的 compute_signals 不参与回测撮合, 仅保证
选股器/实时矩阵等普通策略路径安全空跑 —— 报告不是可前瞻的选股数据):

  - operation_advice 含 买入/加仓 → 入场候选; 含 卖出/减仓/清仓 → 次日开盘
    离场信号; 观望/持有 → 不动 (DSA §4.6-1 映射)
  - 入场信号行放在报告日次一交易日, 逐格注入 ideal_buy: 触及 (low <= ideal_buy)
    才成交, 成交价 min(当日 open, ideal_buy), 未触及不建仓
  - 报告 stop_loss / take_profit 注入为逐仓位绝对风控线 (固定线成交, 非移动
    止盈 —— 与 DSA trailing 的口径差异如实声明; 要看 trailing 效果用扩展版
    dsa_paper)

出场优先级 (引擎口径): 绝对止损/止盈线 > 移动参数 > 卖出类建议 (次日开盘) >
max_hold (10 根, DSA §4.6-2 持有窗) > 数据末尾。
"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix

META = {
    "id": "dsa_points",
    "name": "DSA 点位",
    "description": (
        "DSA 分析报告点位重放: 买入/加仓建议次日触及 ideal_buy 建仓 (未触及不建仓), "
        "按报告绝对止损/止盈线离场, 卖出类建议次日开盘离场 (固定线口径, 非移动止盈)"
    ),
    "tags": ["DSA"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 100,
    # 回测特判标记 (§4.7-2b): 信号行由报告物化, 撮合口径在 backtest/strategy.py 钉死
    "dsa_signal_rows": True,
}

EXECUTION_BACKEND = "matrix_native"
MAX_HOLD_DAYS = 10   # DSA §4.6-2: 持有满 10 根日K 到期


class DsaPointsMatrixStrategy:
    """占位矩阵策略: 回测路径在矩阵构建处特判接入报告物化器, 本类不产生真实信号。"""

    def required_fields(self) -> frozenset[str]:
        return frozenset()

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 0

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        # 空信号: 选股器/实时路径安全空跑; 回测的信号由报告物化接管 (见模块 docstring)
        del params
        return make_signal_matrix(market.shape, entry=np.zeros(market.shape, dtype=np.uint8))


MATRIX_STRATEGY = DsaPointsMatrixStrategy()
