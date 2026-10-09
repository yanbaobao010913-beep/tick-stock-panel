"""kline 分区 schema 演进 (新增 quote_ts 列) 不得弄坏模拟盘行情读取。

线上事故 (2026-10-09): kline_daily 历史分区被新同步链路加写 quote_ts 列后,
read_daily_bar / _prev_close / _index_close 的原生 pl.scan_parquet 全分区扫描
schema 冲突报错, 全部被 except 吞成 None —— 结算条件单读不到 bar 顺延,
净值退化为「缺行情按成本计」, 面板收益恒 0%。

兼容读法已有现成工具: app.parquet.scan_daily_parquet (missing_columns=insert,
extra_columns=ignore)。本测试用「旧分区无 quote_ts + 新分区有 quote_ts」的
混合布局复现事故现场。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.strategy import paper

SYM = "600519.SH"
IDX = "000300.SH"
D1 = date(2026, 9, 29)
D2 = date(2026, 9, 30)

_BASE_COLS = {
    "open": [10.0, 10.2],
    "high": [10.1, 10.9],
    "low": [9.9, 10.1],
    "close": [10.0, 10.8],
    "volume": [10000.0, 12000.0],
    "amount": [100000.0, 129600.0],
}


def _write_mixed(base: Path, symbol: str) -> None:
    """同一数据集写两个分区: D1 无 quote_ts (旧 schema), D2 有 quote_ts (新 schema)。"""
    for day, with_ts in ((D1, False), (D2, True)):
        part_dir = base / f"date={day.isoformat()}"
        part_dir.mkdir(parents=True)
        i = 0 if day == D1 else 1
        cols = {"symbol": [symbol], "date": [day]}
        cols.update({k: [v[i]] for k, v in _BASE_COLS.items()})
        if with_ts:
            cols["quote_ts"] = [1780000000000]
        pl.DataFrame(cols).write_parquet(part_dir / "part.parquet")


def test_read_daily_bar_tolerates_added_quote_ts(tmp_path: Path) -> None:
    _write_mixed(tmp_path / "kline_daily", SYM)
    bar_old = paper.read_daily_bar(tmp_path, SYM, "stock", D1.isoformat())
    bar_new = paper.read_daily_bar(tmp_path, SYM, "stock", D2.isoformat())
    assert bar_old is not None and bar_old["close"] == 10.0
    assert bar_new is not None and bar_new["close"] == 10.8
    assert bar_new["high"] == 10.9 and bar_new["low"] == 10.1


def test_prev_close_tolerates_added_quote_ts(tmp_path: Path) -> None:
    _write_mixed(tmp_path / "kline_daily", SYM)
    assert paper._prev_close(tmp_path, SYM, "stock", D2.isoformat()) == 10.0


def test_index_close_tolerates_added_quote_ts(tmp_path: Path) -> None:
    _write_mixed(tmp_path / "kline_index_daily", IDX)
    assert paper._index_close(tmp_path, D2.isoformat()) == 10.8


def test_etf_daily_tolerates_added_quote_ts(tmp_path: Path) -> None:
    _write_mixed(tmp_path / "kline_etf_daily", "510300.SH")
    bar = paper.read_daily_bar(tmp_path, "510300.SH", "etf", D2.isoformat())
    assert bar is not None and bar["close"] == 10.8
