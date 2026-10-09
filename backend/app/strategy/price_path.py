"""价格路径告警状态机 — 单标的 close 阈值规则的「触价→反弹→收复→再穿越」周期。

解决的问题 (logs/glm-deepv/task.md): DSA 点位规则统一 cooldown_seconds=86400,
上午触发后下午的低位反弹、反向收复、再次原方向穿越整天被压。本模块用显式
启用 (rule.price_path=True) 的路径状态机替代冷却语义:

  - idle: 初次观测即满足触发方向 → 立即报一次 (不等确认, 兼容无 timestamp 源)。
  - triggered: 同一触发区间内持续下跌/上涨不重复; 区间内观测更新极值
    (下行→低点 / 上行→高点), 仅供文案, 不用日 high/low 伪造顺序。
  - 确认收复/回落: 价格越过 阈值±max(0.5%, 1 tick) 缓冲线后, 连续交易时间 ≥60s
    且 ≥2 条时间递增的新鲜快照 → 报一条信息事件 (info_only, 不进自动跟单)。
  - recovered: 再次原方向穿越 → 立即再报, 全天冷却不压新周期。

低位反弹观察 (按 symbol, 独立于是否收复): 只有用「下行触发区间内」的新鲜观测
累积出低点后, 从低点反弹 ≥3% 且满足同样确认条件时报一次; 同一反弹段只报一次,
出现比上一段低点再低 max(0.5%, 1 tick) 的新低才允许新的反弹段。

新鲜口径: quote_ts 必须有限、属当日连续竞价时间 (北京时间)、严格递增,
且距服务端时钟不超过180秒、不能超前超过5秒。重复/倒序快照不推进确认;
旧日、失效、缺失数据打断确认。缺失 timestamp 仅兼容 idle→triggered 首报,
不把服务端轮询时间当新行情。相邻新鲜快照的连续竞价间隔 >180s 视为断流,
打断连续确认 (午休不计入间隔, 不会误判为断流)。连续竞价秒差按北京墙上钟映射
9:30-11:30 / 13:00-15:00 两段 (与星期无关, 回放口径确定)。

持久化: data/user_data/price_path/state.json, 版本 + sha256 校验和 + 原子写;
仅当日有效, 跨日整体重置; 坏文件保守降级为全新状态 (记日志, 不抛)。规则条目带
签名 (op|value|symbol|asset_type|kind), 对账 reload 同签名保留, 改价/改向/停用/删除/
关闭模式即失效重置; 规则删改同时清掉相关 symbol 的低点。写入按 dirty 节流
(≥10s 一次), 事件后立即提交异步原子写; 单个后台写入器合并待写快照, 磁盘 I/O 在锁外。

规则契约: type=price + scope=symbols 单标的 + 单条件 close <=/>= 有限数值 +
asset_type ∈ {stock, etf} (tick 分别 0.01/0.001); 不满足者在 monitor_rules.validate
明确拒绝, 引擎侧兜底跳过并告警 — 绝不退化成错误逻辑。

信息事件 (收复/回落/反弹) 一律 info_only=True: paper_auto.on_rule_events 在真实
匹配边界跳过 info_only 事件 (防御在下单入口, 不只靠文案); 原方向触价事件结构与
旧 price 事件一致 (source/type/symbol/price), 跟单行为不变。所有路径事件带
path_kind (托管 kind 标注), quote_service 在记录/SSE/语音/Webhook 前的统一出口用
merge_round_events 把同 symbol 同 kind 同事件类型的 接近/逼近/到价 合并成最强一条 (主规则 >
逼近 > 接近; 同档取更深入触发方向的阈值), 不同 kind (止损 vs 加仓) 互相保留。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import CN_TZ, cn_today
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# ── 确认参数 (任务书建议值) ──────────────────────────────
RECOVERY_BUFFER_PCT = 0.005   # 收复/回落回离缓冲 0.5%
BOUNCE_PCT = 0.03             # 低位反弹幅度 3%
BOUNCE_NEW_LOW_PCT = 0.005    # 新反弹段需要的低点深度 0.5%
CONFIRM_SECONDS = 60.0        # 确认所需连续交易时间
CONFIRM_MIN_SNAPSHOTS = 2     # 确认所需时间递增新鲜快照数
GAP_RESET_SECONDS = 180.0     # 相邻新鲜快照间隔超此值 → 打断连续确认 (断流)
TICK_BY_ASSET = {"stock": 0.01, "etf": 0.001}
DEFAULT_TICK = 0.01
_EPS = 1e-9                   # 等号/浮点边界容差: 线上等号算越过

# ── 持久化 ───────────────────────────────────────────────
STATE_VERSION = 1
STATE_DIR = Path("user_data") / "price_path"
STATE_FILE = "state.json"
_STATE_WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="price-path-state")

_MORNING_START = 9 * 3600 + 30 * 60
_MORNING_SECONDS = 2 * 3600.0
_AFTERNOON_START = 13 * 3600
_DAY_SECONDS = 4 * 3600.0

_PHASES = {"idle", "triggered", "confirm", "recovered"}


def _beijing(ts_ms: Any) -> datetime | None:
    """epoch ms → 北京时间 datetime; 非法返回 None。"""
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=CN_TZ)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _session_seconds(dt: datetime) -> float:
    """北京墙上钟 → 当日已连续竞价秒数。午休保持上午累计; 与星期无关。"""
    t = dt.hour * 3600 + dt.minute * 60 + dt.second
    if t <= _MORNING_START:
        return 0.0
    if t < _MORNING_START + _MORNING_SECONDS:
        return float(t - _MORNING_START)
    if t < _AFTERNOON_START:
        return _MORNING_SECONDS  # 午休
    if t < _AFTERNOON_START + _MORNING_SECONDS:
        return _MORNING_SECONDS + (t - _AFTERNOON_START)
    return _DAY_SECONDS


def _valid_price(v: Any) -> bool:
    return (
        isinstance(v, (int, float)) and not isinstance(v, bool)
        and math.isfinite(v) and v > 0
    )


def _tick_of(asset_type: str) -> float:
    return TICK_BY_ASSET.get(asset_type, DEFAULT_TICK)


def rule_supported(rule: dict) -> bool:
    """价格路径模式的规则形状 (真实 DSA 契约): 单标的单 close 阈值。"""
    symbols = rule.get("symbols")
    conds = rule.get("conditions")
    return (
        rule.get("type") == "price"
        and rule.get("scope") == "symbols"
        and isinstance(symbols, list) and len(symbols) == 1 and bool(symbols[0])
        and isinstance(conds, list) and len(conds) == 1 and isinstance(conds[0], dict)
        and conds[0].get("field") == "close"
        and conds[0].get("op") in ("<=", ">=")
        and _valid_price(conds[0].get("value"))
        and rule.get("asset_type", "stock") in TICK_BY_ASSET
    )


def rule_signature(rule: dict) -> str:
    """规则内容签名: 阈值/方向/标的/资产任一变化 → 路径状态失效重置。"""
    cond = rule["conditions"][0]
    return "|".join((
        str(cond["op"]), repr(float(cond["value"])),
        str(rule["symbols"][0]), str(rule.get("asset_type", "stock")),
        str(rule.get("path_kind") or ""),
    ))


# ================================================================
# 每轮同 kind 事件合并 (接近/逼近/到价 → 最强一条)
# ================================================================

def _base_kind(kind: str) -> str:
    for tier in ("near_", "mid_"):
        if kind.startswith(tier):
            return kind[len(tier):]
    return kind


def _tier_rank(kind: str) -> int:
    if kind.startswith("mid_"):
        return 1
    if kind.startswith("near_"):
        return 2
    return 0


def _stronger(a: dict, b: dict) -> bool:
    """事件 a 是否比 b 更强: 主规则 > 逼近 > 接近; 同档取更深入触发方向的阈值。"""
    ka, kb = str(a.get("path_kind") or ""), str(b.get("path_kind") or "")
    ra, rb = _tier_rank(ka), _tier_rank(kb)
    if ra != rb:
        return ra < rb
    va = a.get("conditions") or []
    vb = b.get("conditions") or []
    op_a = va[0].get("op") if va and isinstance(va[0], dict) else None
    op_b = vb[0].get("op") if vb and isinstance(vb[0], dict) else None
    if op_a != op_b or op_a is None:
        return False
    try:
        x, y = float(va[0]["value"]), float(vb[0]["value"])
    except (KeyError, TypeError, ValueError):
        return False
    return x < y if op_a == "<=" else x > y


def merge_round_events(events: list[dict]) -> list[dict]:
    """同轮路径事件合并: (symbol, 基础kind, 事件类型) 组内保留最强一条, 其余丢弃。

    只作用于带 path_kind + symbol + rule_id 的事件 (即显式启用路径模式的托管
    规则); 其它事件原样透传。不同 kind 互不合并 — 止损不会被加仓事件吞掉。
    合并后的赢家带上 merged_count (本轮同组事件数, 含自身)。
    """
    groups: dict[tuple[str, str, str], dict] = {}
    for ev in events:
        kind = ev.get("path_kind")
        sym = ev.get("symbol")
        if not kind or not sym or not ev.get("rule_id"):
            continue
        key = (str(sym), _base_kind(str(kind)), str(ev.get("type")))
        group = groups.get(key)
        if group is None:
            groups[key] = {"winner": ev, "count": 1}
        else:
            group["count"] += 1
            if _stronger(ev, group["winner"]):
                group["winner"] = ev
    if not groups:
        return events
    out: list[dict] = []
    for ev in events:
        kind = ev.get("path_kind")
        sym = ev.get("symbol")
        if not kind or not sym or not ev.get("rule_id"):
            out.append(ev)
            continue
        group = groups[(str(sym), _base_kind(str(kind)), str(ev.get("type")))]
        if ev is group["winner"]:
            ev["merged_count"] = group["count"]
            out.append(ev)
    return out


# ================================================================
# 状态机
# ================================================================

class _Obs:
    """一条 symbol 快照的新鲜度判定 (每轮每 symbol 一次)。"""

    __slots__ = ("close", "fresh", "gap", "missing_ts", "session_ss", "ts")

    def __init__(self, close: float, fresh: bool, ts: int | None,
                 session_ss: float | None, gap: bool):
        self.close = close
        self.fresh = fresh
        self.ts = ts
        self.session_ss = session_ss
        self.gap = gap
        self.missing_ts = False


def _new_rule_state(sig: str) -> dict:
    return {"sig": sig, "phase": "idle", "ext": None,
            "cf_ts": None, "cf_ss": None, "cf_n": 0}


def _new_symbol_state(tick: float) -> dict:
    return {"tick": tick, "low": None, "fired": False, "src_rule": None,
            "cf_ts": None, "cf_ss": None, "cf_n": 0}


class PricePathTracker:
    """规则级穿越/收复状态 + symbol 级低位反弹状态, 含当日持久化。

    线程模型: 所有状态访问持自锁; 引擎行情线程 (evaluate) 与 API 线程
    (set_rules/reload) 并发安全。锁内不做网络/全量磁盘扫描。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data_dir: Path | None = None
        self._today = cn_today()
        self._loaded = False
        self._rules: dict[str, dict] = {}      # rule_id → 状态 (含 sig)
        self._meta: dict[str, dict] = {}       # rule_id → 规则要素 (kind/op/value/...)
        self._symbols: dict[str, dict] = {}    # symbol → 反弹段状态
        self._fresh: dict[str, int] = {}       # symbol → 最近一条新鲜快照 ts
        self._round_memo: dict[str, _Obs] = {}
        self._dirty = False
        self._last_save = 0.0
        self._warned_shape: set[str] = set()
        self._urgent = False
        self._pending_snapshot = None
        self._writer_running = False
        self._write_future = None

    # ── 引擎接线 ─────────────────────────────────────
    def set_data_dir(self, data_dir: Path | None) -> None:
        with self._lock:
            self._data_dir = Path(data_dir) if data_dir is not None else None

    def sync_rules(self, rules: dict[str, dict]) -> None:
        """引擎规则集变更后同步: 同签名保留状态, 其余 (删除/停用/改内容/关模式) 重置。"""
        with self._lock:
            self._ensure_loaded_locked()
            meta: dict[str, dict] = {}
            kept: dict[str, dict] = {}
            for rid, rule in list(rules.items()):
                if not rule.get("price_path") or not rule.get("enabled", True):
                    continue
                if not rule_supported(rule):
                    if rid not in self._warned_shape:
                        self._warned_shape.add(rid)
                        logger.warning(
                            "price_path 规则 %s 形状不支持 (需单标的单 close 阈值), 跳过路径评估", rid,
                        )
                    continue
                sig = rule_signature(rule)
                cond = rule["conditions"][0]
                meta[rid] = {
                    "sym": str(rule["symbols"][0]),
                    "op": str(cond["op"]),
                    "value": float(cond["value"]),
                    "kind": str(rule.get("path_kind") or ""),
                    "name": str(rule.get("name") or ""),
                    "severity": str(rule.get("severity") or "info"),
                    "asset_type": str(rule.get("asset_type") or "stock"),
                }
                old = self._rules.get(rid)
                kept[rid] = old if old is not None and old.get("sig") == sig else _new_rule_state(sig)
            if kept != self._rules or meta != self._meta:
                self._dirty = True
                self._urgent = True
            changed_symbols = set()
            for rid, old in self._rules.items():
                if rid not in kept or kept[rid]["sig"] != old["sig"]:
                    parts = old["sig"].split("|")
                    if len(parts) >= 4:
                        changed_symbols.add(parts[2])
            active_symbols = {entry["sym"] for entry in meta.values()}
            for sym in set(self._symbols) | set(self._fresh):
                if sym in changed_symbols or sym not in active_symbols:
                    self._symbols.pop(sym, None)
                    self._fresh.pop(sym, None)
                    self._round_memo.pop(sym, None)
            self._rules = kept
            self._meta = meta
        self.flush()

    def begin_round(self) -> None:
        with self._lock:
            today = cn_today()
            if today != self._today:
                self._reset_for_new_day_locked(today)
            self._round_memo.clear()

    def interrupt_asset(self, asset_type: str) -> None:
        """An empty provider snapshot cannot establish continuous confirmation."""
        with self._lock:
            symbols = {m["sym"] for m in self._meta.values() if m["asset_type"] == asset_type}
            states = [s for rid, s in self._rules.items() if self._meta[rid]["sym"] in symbols]
            states += [s for sym, s in self._symbols.items() if sym in symbols]
            for state in states:
                if state["cf_ts"] is not None:
                    state["cf_ts"] = state["cf_ss"] = None
                    state["cf_n"] = 0
                    self._dirty = True

    def end_round(self, now: float, name_map: dict[str, str] | None = None) -> list[dict]:
        """轮末统一处理 symbol 级反弹 (与规则评估顺序无关)。"""
        with self._lock:
            if not self._round_memo:
                return []
            events: list[dict] = []
            for sym, obs in self._round_memo.items():
                events.extend(self._step_bounce_locked(sym, obs, now, name_map or {}))
            self._urgent |= bool(events)
            return events

    def flush(self, force: bool = False) -> None:
        """dirty 状态落盘 (节流 ≥10s; force 立即)。异常只记日志。"""
        try:
            with self._lock:
                if self._data_dir is None:
                    return
                if not (self._dirty or force):
                    return
                if not force and not self._urgent and (time.monotonic() - self._last_save) < 10.0:
                    return
                path = self._state_path()
                payload = {"version": STATE_VERSION, "date": self._today.isoformat(),
                           "rules": self._rules, "symbols": self._symbols, "fresh": self._fresh}
                body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                doc = json.loads(body)
                doc["checksum"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
                content = json.dumps(doc, ensure_ascii=False, indent=1)
                self._pending_snapshot = (path, content)
                if not self._writer_running:
                    self._writer_running = True
                    self._write_future = _STATE_WRITER.submit(self._drain_snapshots)
                future = self._write_future
                self._dirty = self._urgent = False
                self._last_save = time.monotonic()
            if force:
                future.result()
        except Exception as e:  # noqa: BLE001
            logger.warning("price_path 状态落盘失败 (忽略, 内存态继续): %s", e)

    # ── 评估入口 (每条路径规则一次) ─────────────────
    def evaluate_rule(
        self, df: pl.DataFrame, rule: dict, now: float,
        name_map: dict[str, str] | None = None,
    ) -> list[dict]:
        rid = str(rule.get("id") or "")
        if not rid or not rule_supported(rule):
            return []  # sync_rules 已告警; 兜底不评估
        sym = str(rule["symbols"][0])
        row = self._extract_row(df, sym)
        with self._lock:
            self._ensure_loaded_locked()
            st = self._rules.get(rid)
            sig = rule_signature(rule)
            if rid not in self._meta or st is None or st.get("sig") != sig:
                return []
            if row is None or not _valid_price(row[0]):
                for state in [s for key, s in self._rules.items() if self._meta[key]["sym"] == sym] + [self._symbols.get(sym, {})]:
                    state["cf_ts"] = state["cf_ss"] = None
                    state["cf_n"] = 0
                self._dirty = True
                return []
            close, change_pct, ts_raw = row
            obs = self._observe_locked(sym, ts_raw, float(close))
            events = self._step_rule_locked(rid, rule, st, obs, now, change_pct, name_map or {})
            self._urgent |= bool(events)
            return events

    # ── 内部: 快照/规则状态/反弹 ─────────────────────
    @staticmethod
    def _extract_row(df: pl.DataFrame, sym: str) -> tuple[Any, Any, Any] | None:
        if df.is_empty() or "symbol" not in df.columns or "close" not in df.columns:
            return None
        try:
            sub = df.filter(pl.col("symbol") == sym)
        except Exception:  # noqa: BLE001
            return None
        if sub.is_empty():
            return None
        row = sub.row(0, named=True)
        ts = row.get("quote_ts") if "quote_ts" in row else None
        return row.get("close"), row.get("change_pct"), ts

    def _observe_locked(self, sym: str, ts_raw: Any, close: float) -> _Obs:
        # 一轮 evaluate 内每 symbol 只有一行快照; 首次判定后 memo 复用,
        # 新鲜度推进 (_fresh) 也只发生一次, 重复评估同快照不会重复推进。
        memo = self._round_memo.get(sym)
        if memo is not None and math.isclose(memo.close, close, rel_tol=0, abs_tol=0):
            return memo
        fresh = False
        ts: int | None = None
        ss: float | None = None
        gap = True  # Missing/invalid timestamps interrupt confirmation; duplicates only wait.
        if (
            isinstance(ts_raw, (int, float)) and not isinstance(ts_raw, bool)
            and math.isfinite(ts_raw) and ts_raw > 0
        ):
            dt = _beijing(ts_raw)
            age = time.time() - float(ts_raw) / 1000
            clock = dt.hour * 3600 + dt.minute * 60 + dt.second if dt is not None else -1
            trading = _MORNING_START <= clock <= _MORNING_START + _MORNING_SECONDS or _AFTERNOON_START <= clock <= _AFTERNOON_START + _MORNING_SECONDS
            if dt is not None and dt.date() == self._today and -5 <= age <= 180 and trading:
                gap = False
                ss = _session_seconds(dt)
                ts = int(ts_raw)
                last = self._fresh.get(sym)
                if ts > (last or 0):
                    fresh = True
                    if last is not None:
                        gap = (ss - _session_seconds(_beijing(last))) > GAP_RESET_SECONDS
                    self._fresh[sym] = ts
        obs = _Obs(close, fresh, ts, ss, gap)
        obs.missing_ts = ts_raw is None
        self._round_memo[sym] = obs
        return obs

    def _below_main_stop_locked(self, sym: str, close: float) -> float | None:
        """当前价是否仍低于某条主止损线 (kind=stop_loss); 返回该线价 (多条取最深)。"""
        stop: float | None = None
        for meta in self._meta.values():
            if meta["sym"] == sym and meta["kind"] == "stop_loss" and close <= meta["value"] + _EPS:
                stop = meta["value"] if stop is None else min(stop, meta["value"])
        return stop

    def _risk_suffix_locked(self, sym: str, close: float) -> str:
        stop = self._below_main_stop_locked(sym, close)
        return f" · 风险提示: 现价仍低于主止损线 {stop:.3f}" if stop is not None else ""

    def _step_rule_locked(
        self, rid: str, rule: dict, st: dict, obs: _Obs, now: float,
        change_pct: Any, name_map: dict[str, str],
    ) -> list[dict]:
        cond = rule["conditions"][0]
        op = str(cond["op"])
        value = float(cond["value"])
        down = op == "<="
        tick = _tick_of(str(rule.get("asset_type") or "stock"))
        buffer = max(value * RECOVERY_BUFFER_PCT, tick)
        recover_line = value + buffer if down else value - buffer

        if down:
            trig = obs.close <= value + _EPS
            recovered_side = obs.close >= recover_line - _EPS
        else:
            trig = obs.close >= value - _EPS
            recovered_side = obs.close <= recover_line + _EPS

        if obs.gap and st["cf_ts"] is not None:
            st["cf_ts"] = None
            st["cf_ss"] = None
            st["cf_n"] = 0
            self._dirty = True

        events: list[dict] = []
        phase = st["phase"]
        kind = str(rule.get("path_kind") or "")

        if phase == "idle":
            if trig and (obs.fresh or obs.missing_ts):
                events.append(self._trigger_event(
                    rid, rule, kind, obs, now, change_pct, name_map, down, value,
                ))
                st["phase"] = "triggered"
                st["ext"] = obs.close if obs.fresh else None
                self._dirty = True
        elif phase == "triggered":
            # 确认期间的重复/倒序快照不推进状态:
            # 失效/缺失快照已经由 gap 标记打断确认。
            if obs.fresh and trig:
                if st["cf_ts"] is not None:
                    # 触发侧回归打断连续确认 (确认样本必须持续保持在缓冲线外)
                    st["cf_ts"] = st["cf_ss"] = None
                    st["cf_n"] = 0
                    self._dirty = True
                if st["ext"] is None or (obs.close < st["ext"] if down else obs.close > st["ext"]):
                    st["ext"] = obs.close
                    self._dirty = True
            elif obs.fresh and recovered_side:
                if st["cf_ts"] is None:
                    st["cf_ts"] = obs.ts
                    st["cf_ss"] = obs.session_ss
                    st["cf_n"] = 1
                    self._dirty = True
                else:
                    st["cf_n"] += 1
                if (
                    st["cf_ts"] is not None
                    and st["cf_n"] >= CONFIRM_MIN_SNAPSHOTS
                    and obs.session_ss is not None
                    and obs.session_ss - st["cf_ss"] >= CONFIRM_SECONDS
                ):
                    events.append(self._recovery_event(
                        rid, rule, kind, obs, now, change_pct, name_map, down, value, st["ext"],
                    ))
                    st["phase"] = "recovered"
                    st["cf_ts"] = st["cf_ss"] = None
                    st["cf_n"] = 0
                    self._dirty = True
            elif obs.fresh and st["cf_ts"] is not None:
                # 死区 (阈值与缓冲线之间): 打断连续确认
                st["cf_ts"] = st["cf_ss"] = None
                st["cf_n"] = 0
                self._dirty = True
        elif phase == "recovered" and trig and obs.fresh:
            events.append(self._trigger_event(
                rid, rule, kind, obs, now, change_pct, name_map, down, value,
            ))
            st["phase"] = "triggered"
            st["ext"] = obs.close
            st["cf_ts"] = st["cf_ss"] = None
            st["cf_n"] = 0
            self._dirty = True
        return events

    def _in_down_interval_locked(self, sym: str, close: float) -> bool:
        for meta in self._meta.values():
            if meta["sym"] == sym and meta["op"] == "<=" and close <= meta["value"] + _EPS:
                return True
        return False

    def _down_rule_for_locked(self, sym: str, close: float) -> tuple[str, dict] | None:
        """归属该 symbol 当前价所在下行区间最深的规则 (反弹事件挂靠它)。"""
        best: tuple[str, dict] | None = None
        for rid, meta in self._meta.items():
            if (meta["sym"] == sym and meta["op"] == "<=" and close <= meta["value"] + _EPS
                    and (best is None or meta["value"] < best[1]["value"])):
                best = (rid, meta)
        return best

    def _step_bounce_locked(
        self, sym: str, obs: _Obs, now: float, name_map: dict[str, str],
    ) -> list[dict]:
        if not obs.fresh:
            b = self._symbols.get(sym)
            if obs.gap and b is not None and b["cf_ts"] is not None:
                b["cf_ts"] = b["cf_ss"] = None
                b["cf_n"] = 0
                self._dirty = True
            return []
        in_down = self._in_down_interval_locked(sym, obs.close)
        b = self._symbols.get(sym)
        if b is None:
            if not in_down:
                return []
            tick = DEFAULT_TICK
            for meta in self._meta.values():
                if meta["sym"] == sym:
                    tick = _tick_of(meta["asset_type"])
                    break
            b = _new_symbol_state(tick)
            self._symbols[sym] = b
            self._dirty = True

        def _reset_cf() -> None:
            if b["cf_ts"] is not None:
                b["cf_ts"] = b["cf_ss"] = None
                b["cf_n"] = 0
                self._dirty = True

        if obs.gap:
            _reset_cf()

        low = b["low"]
        if low is None:
            if in_down:
                b["low"] = obs.close
                src = self._down_rule_for_locked(sym, obs.close)
                b["src_rule"] = src[0] if src else None
                self._dirty = True
            return []
        new_low_line = low - max(low * BOUNCE_NEW_LOW_PCT, b["tick"])
        if b["fired"]:
            if obs.close <= new_low_line + _EPS:
                b["low"] = obs.close
                b["fired"] = False
                src = self._down_rule_for_locked(sym, obs.close)
                b["src_rule"] = src[0] if src else None
                _reset_cf()
                self._dirty = True
            return []
        if in_down and obs.close < low:
            b["low"] = obs.close
            src = self._down_rule_for_locked(sym, obs.close)
            b["src_rule"] = src[0] if src else None
            _reset_cf()
            self._dirty = True
            return []
        bounce_line = low * (1.0 + BOUNCE_PCT)
        if obs.close >= bounce_line - _EPS:
            if b["cf_ts"] is None:
                b["cf_ts"] = obs.ts
                b["cf_ss"] = obs.session_ss
                b["cf_n"] = 1
                self._dirty = True
            else:
                b["cf_n"] += 1
            if (
                b["cf_n"] >= CONFIRM_MIN_SNAPSHOTS
                and obs.session_ss is not None
                and obs.session_ss - b["cf_ss"] >= CONFIRM_SECONDS
            ):
                _reset_cf()
                b["fired"] = True
                self._dirty = True
                return [self._bounce_event(sym, b, obs, now, name_map)]
            return []
        _reset_cf()  # 反弹不足退回 / 横盘
        return []

    # ── 事件构造 ─────────────────────────────────────
    def _base_event(
        self, rid: str, rule: dict, kind: str, obs: _Obs, now: float,
        name_map: dict[str, str],
    ) -> dict:
        sym = str(rule["symbols"][0])
        return {
            "ts": int(now * 1000),
            "rule_id": rid,
            "rule_name": rule.get("name", ""),
            "strategy_id": None,
            "source": "price",
            "symbol": sym,
            "name": name_map.get(sym),
            "price": obs.close,
            "signals": [],
            "severity": rule.get("severity", "info"),
            "conditions": list(rule.get("conditions", [])),
            "logic": "and",
            "path_kind": kind,
        }

    def _trigger_event(
        self, rid: str, rule: dict, kind: str, obs: _Obs, now: float,
        change_pct: Any, name_map: dict[str, str], down: bool, value: float,
    ) -> dict:
        ev = self._base_event(rid, rule, kind, obs, now, name_map)
        ev["type"] = "price"
        ev["change_pct"] = change_pct
        message = rule.get("message") or (
            f"价格触发 · 收盘价 {'≤' if down else '≥'} {value:.3f}"
        )
        # 买点类文案: 仍低于主止损线时明确风险 (止损优先, 不丢风险提示)
        if _base_kind(kind) in ("add", "entry"):
            message += self._risk_suffix_locked(ev["symbol"], obs.close)
        ev["message"] = message
        return ev

    def _recovery_event(
        self, rid: str, rule: dict, kind: str, obs: _Obs, now: float,
        change_pct: Any, name_map: dict[str, str], down: bool, value: float,
        ext: float | None,
    ) -> dict:
        ev = self._base_event(rid, rule, kind, obs, now, name_map)
        ev["type"] = "price_path_recovery"
        ev["severity"] = "info"
        ev["info_only"] = True
        ev["conditions"] = []
        ev["change_pct"] = change_pct
        ext_text = (
            f", 本轮区间{'低点' if down else '高点'} {ext:.3f}" if ext is not None else ""
        )
        head = (
            f"价格收复 · 现价 {obs.close:.3f} 已连续确认站回 {value:.3f} 上方"
            if down else
            f"价格回落 · 现价 {obs.close:.3f} 已连续确认回落至 {value:.3f} 下方"
        )
        ev["message"] = (
            f"{head}{ext_text} · 信息提示, 非交易指令"
            + self._risk_suffix_locked(ev["symbol"], obs.close)
        )
        return ev

    def _bounce_event(
        self, sym: str, b: dict, obs: _Obs, now: float, name_map: dict[str, str],
    ) -> dict:
        low = float(b["low"])
        pct = (obs.close / low - 1.0) * 100 if low > 0 else 0.0
        # 归属: 喂入低点的规则优先; 已删则回退当前价所在下行区间最深的规则
        rid = b.get("src_rule")
        meta = self._meta.get(rid or "")
        if meta is None:
            src = self._down_rule_for_locked(sym, obs.close)
            if src is not None:
                rid, meta = src
        rule_name = meta["name"] if meta else ""
        return {
            "ts": int(now * 1000),
            "rule_id": rid,
            "rule_name": rule_name,
            "strategy_id": None,
            "source": "price",
            "type": "price_path_bounce",
            "symbol": sym,
            "name": name_map.get(sym),
            "message": (
                f"低位反弹观察 · 自低点 {low:.3f} 反弹至 {obs.close:.3f} (+{pct:.1f}%)"
                f" · 信息提示, 非买入指令"
                + self._risk_suffix_locked(sym, obs.close)
            ),
            "price": obs.close,
            "change_pct": None,
            "signals": [],
            "severity": "info",
            "conditions": [],
            "logic": "and",
            "info_only": True,
            "path_low": low,
        }

    # ── 持久化 ───────────────────────────────────────
    def _state_path(self) -> Path | None:
        if self._data_dir is None:
            return None
        return self._data_dir / STATE_DIR / STATE_FILE

    def _ensure_loaded_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._state_path()
        if path is None:
            return
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.warning("price_path 状态文件损坏, 保守降级为全新状态: %s", e)
            return
        if not isinstance(doc, dict) or doc.get("version") != STATE_VERSION:
            logger.warning("price_path 状态文件类型或版本不符, 忽略")
            return
        payload = {k: v for k, v in doc.items() if k != "checksum"}
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if doc.get("checksum") != hashlib.sha256(body.encode("utf-8")).hexdigest():
            logger.warning("price_path 状态文件校验和不符, 忽略整份文件")
            return
        if doc.get("date") != self._today.isoformat():
            return  # 跨日: 旧日状态整体失效
        rules = self._adopt_rules(doc.get("rules"))
        symbols, fresh = self._adopt_symbols(doc.get("symbols"), doc.get("fresh"))
        self._rules, self._symbols, self._fresh = rules, symbols, fresh
        logger.info("price_path: 恢复当日状态 (%d 规则 / %d 标的)", len(rules), len(symbols))

    def _adopt_rules(self, raw: Any) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not isinstance(raw, dict):
            return out
        for rid, entry in raw.items():
            if not isinstance(entry, dict) or entry.get("phase") not in _PHASES:
                continue
            ext = entry.get("ext")
            cf_ts = entry.get("cf_ts")
            cf_ss = entry.get("cf_ss")
            cf_n = entry.get("cf_n")
            if not self._valid_confirmation(cf_ts, cf_ss, cf_n):
                continue
            if not isinstance(entry.get("sig"), str):
                continue
            if ext is not None and not _valid_price(ext):
                continue
            if cf_ts is not None and not isinstance(cf_ts, int):
                continue
            if cf_ss is not None and not isinstance(cf_ss, (int, float)):
                continue
            if not isinstance(cf_n, int) or isinstance(cf_n, bool) or cf_n < 0:
                continue
            out[str(rid)] = {
                "sig": entry["sig"], "phase": entry["phase"], "ext": ext,
                "cf_ts": cf_ts, "cf_ss": cf_ss, "cf_n": cf_n,
            }
        return out

    def _adopt_symbols(self, raw: Any, fresh_raw: Any) -> tuple[dict[str, dict], dict[str, int]]:
        out: dict[str, dict] = {}
        if isinstance(raw, dict):
            for sym, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                low = entry.get("low")
                tick = entry.get("tick")
                if low is not None and not _valid_price(low):
                    continue
                if not _valid_price(tick):
                    continue
                cf_ts, cf_ss, cf_n = entry.get("cf_ts"), entry.get("cf_ss"), entry.get("cf_n")
                if not self._valid_confirmation(cf_ts, cf_ss, cf_n):
                    continue
                if cf_ts is not None and not isinstance(cf_ts, int):
                    continue
                if cf_ss is not None and not isinstance(cf_ss, (int, float)):
                    continue
                if not isinstance(cf_n, int) or isinstance(cf_n, bool) or cf_n < 0:
                    continue
                src = entry.get("src_rule")
                out[str(sym)] = {
                    "tick": float(tick), "low": low,
                    "fired": bool(entry.get("fired")),
                    "src_rule": str(src) if src else None,
                    "cf_ts": cf_ts, "cf_ss": cf_ss, "cf_n": cf_n,
                }
        fresh: dict[str, int] = {}
        if isinstance(fresh_raw, dict):
            for sym, ts in fresh_raw.items():
                dt = _beijing(ts)
                if isinstance(ts, int) and not isinstance(ts, bool) and ts > 0 and dt is not None and dt.date() == self._today:
                    fresh[str(sym)] = ts
        return out, fresh

    def _valid_confirmation(self, ts: Any, seconds: Any, count: Any) -> bool:
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False
        if ts is None:
            return seconds is None and count == 0
        dt = _beijing(ts)
        return (isinstance(ts, int) and not isinstance(ts, bool) and dt is not None
                and dt.date() == self._today and isinstance(seconds, (int, float))
                and not isinstance(seconds, bool) and math.isfinite(seconds)
                and seconds == _session_seconds(dt) and count > 0)

    def _drain_snapshots(self) -> None:
        # Coalesce pending writes; at most one worker and one pending snapshot per tracker.
        while True:
            with self._lock:
                snapshot = self._pending_snapshot
                self._pending_snapshot = None
                if snapshot is None:
                    self._writer_running = False
                    return
            path, content = snapshot
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, content)
            except Exception as exc:  # noqa: BLE001 -- persistence must not terminate the worker
                logger.warning("price_path 状态落盘失败: %s", exc)
                with self._lock:
                    self._dirty = self._urgent = True

    def _reset_for_new_day_locked(self, today) -> None:
        logger.info("price_path: 跨交易日, 重置当日低点与待确认状态 (%s)", today)
        self._today = today
        self._rules = {rid: _new_rule_state(state["sig"]) for rid, state in self._rules.items()}
        self._symbols = {}
        self._fresh = {}
        self._round_memo = {}
        self._dirty = True
        self._urgent = True
