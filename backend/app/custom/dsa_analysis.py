"""DSA 式 LLM 分析流水线 (迁移计划 P1 §2/§3.1/§3.2 + 18:00 定时任务)。

与 TSP 核心 stock_analyzer 的区别 (迁移计划拍板决策 #1): 本扩展输出 DSA 式
实战决策报告 (四点位 + 操作建议 + 盯盘条件), 不改变核心"四维分析不出买卖
建议"的设计。数据组装复用 stock_analyzer / levels, 不平行实现第二套。

报告存储: data/user_data/dsa_reports/ 每报告一个 JSON 文件 (文件名含 id),
临时文件 + os.replace 原子写, 进程内锁串行化读写; 单文件损坏跳过并记日志,
不拖垮列表。未复用 JsonReportStore: 其"单文件 + 条数上限"设计会把全部报告
重写进一个 JSON 并裁剪历史, 而报告库是归档语义 (P2 还要按 symbol 读最新
报告), 不适合条数上限, 故自建每报告一文件的存储, 原子写与锁语义保持一致。

任务模型: 内存态任务表 (重启丢任务可接受) + 模块级单工作线程串行消费队列,
避免并发打爆 LLM 配额。单票失败只记 item 级 error, 不拖垮整任务; 全部 item
终态后任务置 done (含失败 item); 流水线自身崩溃才把任务置 failed。

定时: daemon 调度线程每 30s 检查一次偏好 (便于响应变更), 交易日偏好时刻
对全量自选股创建 mode=full 任务; 当日只跑一次 (内存 last-run 日期)。

符号口径 (2026-09-29 P1 修复): 任务创建 / 报告过滤 / 单票分析三个入口统一经
_canonical_symbol 规范化为 `代码.交易所` 后缀式——仓库 parquet 与 instruments
的 symbol 均为后缀式 (600519.SH), 裸 6 位代码经维表反查或代码段规则补齐
交易所, 否则 _load_kline 匹配不到会误报"暂无日K" (迁移计划 §3.1 注记)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import queue
import re
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Body, HTTPException, Request

from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)
from app.indicators.levels import compute_levels, summarize_levels
from app.market_time import cn_now
from app.services.ai_provider import ai_configured, generate_ai_text
from app.services.preferences import load as preferences_load
from app.services.preferences import save as preferences_save
from app.services.stock_analyzer import (
    _KLINE_KEEP_COLS,
    _clean_rows,
    _load_financials,
    _load_kline,
)
from app.services.trading_day import is_trading_day
from app.services.watchlist import list_symbols

logger = logging.getLogger(__name__)

EXTENSION_ID = "dsa.analysis"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

_MAX_SYMBOLS = 50
_MAX_LIMIT = 100
_MODES = ("full", "brief")
_LLM_TIMEOUT_SECONDS = 300.0
_CHECK_INTERVAL_SECONDS = 30.0

_SUMMARY_FIELDS = (
    "id", "symbol", "name", "created_at", "mode", "sentiment_score",
    "operation_advice", "trend_prediction", "analysis_summary", "points",
)
_POINT_KEYS = ("ideal_buy", "secondary_buy", "stop_loss", "take_profit")


# ================================================================
# 报告存储: 每报告一个 JSON 文件, 原子写 + 锁
# ================================================================

def _now_iso() -> str:
    """当前北京时间 ISO 字符串 (naive 北京墙钟, 与 json_report_store 同口径)。"""
    return cn_now().replace(tzinfo=None).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """先写临时文件再 os.replace 原子替换; 失败时清理临时文件后重抛。"""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _safe_symbol(symbol: str) -> str:
    """symbol 限制进文件名 (id 即文件名), 非文件名字符替换为下划线。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", symbol) or "x"


def _report_filename(report_id: str) -> Path | None:
    """id → 存储路径; 含路径分隔符等非法字符时返回 None (fail-closed)。"""
    if not isinstance(report_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", report_id):
        return None
    return Path(f"{report_id}.json")


class DsaReportStore:
    """DSA 报告文件存储: 目录内每报告一个 JSON, 锁内读-改-写。"""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root
        self._lock = threading.Lock()

    def _dir(self) -> Path:
        if self._root is not None:
            return self._root
        from app.config import settings

        return settings.data_dir / "user_data" / "dsa_reports"

    def _make_id(self, symbol: str, created_at: str) -> str:
        """r_{yyyymmdd}_{symbol}_{三位序号}, 序号按同日同 symbol 既有文件递增。"""
        day = str(created_at)[:10].replace("-", "")
        prefix = f"r_{day}_{_safe_symbol(symbol)}_"
        seq = 1
        for p in self._dir().glob(f"{prefix}*.json"):
            try:
                seq = max(seq, int(p.stem[len(prefix):]) + 1)
            except ValueError:
                continue
        return f"{prefix}{seq:03d}"

    def save(self, report: dict) -> dict:
        """补全 id/created_at 后原子落盘, 返回保存后的报告。"""
        with self._lock:
            if not report.get("created_at"):
                report["created_at"] = _now_iso()
            if not report.get("id"):
                symbol = str(report.get("symbol") or "x")
                report["id"] = self._make_id(symbol, str(report["created_at"]))
            d = self._dir()
            d.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(d / f"{report['id']}.json", report)
        logger.info("dsa report saved: %s (%s)", report["id"], report.get("symbol"))
        return report

    def list_reports(self, symbol: str | None = None) -> list[dict]:
        """全部报告按 created_at 倒序; 单文件损坏跳过并记日志。"""
        reports: list[dict] = []
        with self._lock:
            d = self._dir()
            if not d.exists():
                return []
            for p in sorted(d.glob("*.json")):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning("dsa report %s malformed, skipped: %s", p.name, e)
                    continue
                if not isinstance(data, dict):
                    continue
                if symbol and data.get("symbol") != symbol:
                    continue
                reports.append(data)
        reports.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return reports

    def get(self, report_id: str) -> dict | None:
        rel = _report_filename(report_id)
        if rel is None:
            return None
        with self._lock:
            p = self._dir() / rel
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except Exception as e:
                logger.warning("dsa report %s malformed: %s", report_id, e)
                return None
        return data if isinstance(data, dict) else None

    def delete(self, report_id: str) -> bool:
        rel = _report_filename(report_id)
        if rel is None:
            return False
        with self._lock:
            p = self._dir() / rel
            try:
                p.unlink()
            except FileNotFoundError:
                return False
            except OSError as e:
                logger.warning("dsa report %s delete failed: %s", report_id, e)
                return False
        return True


_STORE = DsaReportStore()


# ================================================================
# 符号规范化: 入口统一为 `代码.交易所` 后缀式 (迁移计划 §3.1 注记 2026-09-29)
# ================================================================

# 交易所前缀式 (SZ000636)、裸 6 位 (600519) 与后缀点分式 (600519.SH), 大小写不敏感
_SYMBOL_FORM_RE = re.compile(r"(SH|SZ|BJ)?(\d{6})(?:\.(SH|SZ|BJ))?", re.IGNORECASE)

# 报告日期过滤参数: created_at 为北京时间墙钟 ISO, 取前 10 位做日粒度比较
_DATE_PARAM_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _exchange_suffix(code: str) -> str | None:
    """裸 6 位代码的交易所段规则兜底 (92 北交所段先于 9 开头判定, 不误判 SH)。"""
    if code.startswith("92"):
        return "BJ"
    head = code[0]
    if head in "69":
        return "SH"
    if head in "023":
        return "SZ"
    if head in "48":
        return "BJ"
    return None


def _canonical_symbol(repo, raw: str) -> str:
    """标的符号规范化为 `代码.交易所` 后缀点分式 (P2/P3 模块直接 import 复用)。

    前缀/点分式直接归一大小写 (幂等); 裸 6 位优先经维表 (get_name_map 的 key
    集合, code = symbol 点分前缀) 反查交易所, repo 不可用/异常/未命中时按代码段
    规则兜底; 仍无法判定 (如 5/1/7 开头且维表没有) 原样返回, 让 _load_kline
    自然报暂无日K, 不编造。
    """
    text = str(raw).strip()
    m = _SYMBOL_FORM_RE.fullmatch(text)
    if m is None:
        return text
    code = m.group(2)
    explicit = (m.group(1) or m.group(3) or "").upper()
    if explicit:
        return f"{code}.{explicit}"
    try:
        name_map = repo.get_name_map()
    except Exception as e:
        logger.warning("dsa analysis: 裸代码 %s 维表反查失败, 按代码段规则兜底: %s", code, e)
        name_map = {}
    for symbol in name_map:
        # 维表 keys 为后缀式; 同 code 跨市场时插入序保证股票/ETF 先于指数
        if symbol.partition(".")[0] == code:
            return symbol
    suffix = _exchange_suffix(code)
    return f"{code}.{suffix}" if suffix else text


# ================================================================
# LLM 提示词
# ================================================================

_SYSTEM_PROMPT = """你是一位拥有 15 年 A 股一线实战经验的操盘手风格分析师, 擅长把 K 线、量价、关键价位与基本面交叉验证为可执行的交易决策 (观察-判断-决策-复盘闭环)。

## 任务
基于用户提供的个股数据, 产出一份完整的 markdown 实战分析报告, 并在报告最末尾附一个 ```json 围栏块承载结构化决策字段。

## markdown 报告必须包含 (自然语言版)
1. **核心结论与趋势预判**: 当前阶段 (底部企稳/上升途中/高位震荡/下跌趋势 等) 与一句话定调。
2. **四点位** (必须给具体价格与依据, 基于提供的关键价位/前高前低/均线推导):
   - 理想买点 (ideal_buy) 与次级买点 (secondary_buy)
   - 止损位 (stop_loss) 与止盈位 (take_profit)
   - 数据不足以给出某点位时明确写"暂不适用", 严禁编造数字。
3. **操作建议**: 买入/加仓/持有/减仓/清仓/观望 等, 并说明触发条件。
4. **盯盘条件**: 次日盯什么 —— 触发条件、风险条件 (跌破/涨破什么价位做什么)、下次检查时间。

## 结尾 json 围栏块 (键名固定, 不要增删)
```json
{
  "operation_advice": "买入|加仓|持有|减仓|清仓|观望 或简短自由文本",
  "sentiment_score": 0,
  "trend_prediction": "一句话趋势预判",
  "analysis_summary": "2-3 句摘要",
  "points": {"ideal_buy": null, "secondary_buy": null, "stop_loss": null, "take_profit": null},
  "phase_decision": {
    "action_window": null,
    "immediate_action": null,
    "next_check_time": null,
    "watch_conditions": [],
    "risk_conditions": [{"kind": "reduce", "text": "跌破 xx 减半仓", "price": null}]
  }
}
```

## 硬性要求
1. 数据不支持的点位/字段一律填 null, 严禁编造价格、分数与数字; 点位必须来自提供的行情数据。
2. json 围栏块必须是合法 JSON 且放在报告最末尾; 除该块外不要再输出其他代码块。
3. 每个判断引用具体数值, 禁止空泛套话。
4. 报告末尾附一行: "> ⚠️ 本内容由 AI 基于公开行情与财务数据生成, 仅供参考, 不构成投资建议。交易有风险, 入市需谨慎。"

现在请基于下方数据进行分析。"""

_BRIEF_SUFFIX = """

## 精简模式
本报告为精简版: markdown 正文控制在 800 字以内, 直接给结论与要点, 但四点位与操作建议必须保留; 结尾 json 围栏块仍必须完整。"""


def _build_messages(
    symbol: str,
    name: str | None,
    kline_rows: list[dict],
    fins: dict[str, list[dict]],
    levels_summary: str,
    mode: str,
) -> list[dict[str, str]]:
    system = _SYSTEM_PROMPT + (_BRIEF_SUFFIX if mode == "brief" else "")
    parts: list[str] = [
        f"标的标准代码: {symbol}",
        f"标的名称: {name or '未知'}",
        f"关键价位概览: {levels_summary}",
        "",
        "以下是该标的最近日 K 数据(JSON, 含 OHLCV 与已计算的技术指标, 升序):",
        "```json",
        json.dumps(kline_rows, ensure_ascii=False),
        "```",
    ]
    if any(fins.values()):
        parts.extend([
            "",
            "以下是该标的最新财务数据(JSON, 核心指标 + 利润表, 金额单位为元):",
            "```json",
            json.dumps(fins, ensure_ascii=False),
            "```",
        ])
    else:
        parts.extend([
            "",
            "(该标的暂无财务数据: 基本面维度请基于已有信息谨慎表述, 不要编造财务数字。)",
        ])
    parts.extend(["", "请基于以上数据输出实战分析报告 (结构见系统提示词)。"])
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ================================================================
# LLM 输出解析 (没给的字段一律置空, 不许编造)
# ================================================================

_JSON_FENCE_RE = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_float(value: Any) -> float | None:
    """数值字段容忍字符串数字; 解析不了置空 (不许编造)。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _coerce_price(value: Any) -> float | None:
    """价格点位: 有限正数才有效 (负/零价格视为无效置空)。"""
    f = _coerce_float(value)
    return f if f is not None and f > 0 else None


def _empty_structured() -> dict:
    return {
        "sentiment_score": None,
        "operation_advice": None,
        "trend_prediction": None,
        "analysis_summary": None,
        "points": {k: None for k in _POINT_KEYS},
        "phase_decision": None,
    }


def _normalize_phase(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None

    def s(key: str) -> str | None:
        return _clean_str(raw.get(key))

    watch_raw = raw.get("watch_conditions")
    watch = (
        [t for t in (_clean_str(x) for x in watch_raw) if t]
        if isinstance(watch_raw, list) else []
    )
    risks_raw = raw.get("risk_conditions")
    risks: list[dict] = []
    if isinstance(risks_raw, list):
        for item in risks_raw:
            if not isinstance(item, dict):
                continue
            kind = _clean_str(item.get("kind"))
            text = _clean_str(item.get("text"))
            if not kind and not text:
                continue
            risks.append({"kind": kind, "text": text, "price": _coerce_price(item.get("price"))})
    if not (s("action_window") or s("immediate_action") or s("next_check_time") or watch or risks):
        return None
    return {
        "action_window": s("action_window"),
        "immediate_action": s("immediate_action"),
        "next_check_time": s("next_check_time"),
        "watch_conditions": watch,
        "risk_conditions": risks,
    }


def _normalize_structured(data: Any) -> dict:
    out = _empty_structured()
    if not isinstance(data, dict):
        logger.warning("dsa analysis: json 围栏块不是对象, 结构化字段置空")
        return out
    score = _coerce_float(data.get("sentiment_score"))
    if score is not None and 0 <= score <= 100:
        out["sentiment_score"] = round(score)
    out["operation_advice"] = _clean_str(data.get("operation_advice"))
    out["trend_prediction"] = _clean_str(data.get("trend_prediction"))
    out["analysis_summary"] = _clean_str(data.get("analysis_summary"))
    points = data.get("points")
    if isinstance(points, dict):
        for key in _POINT_KEYS:
            out["points"][key] = _coerce_price(points.get(key))
    out["phase_decision"] = _normalize_phase(data.get("phase_decision"))
    return out


def _parse_llm_output(text: str) -> tuple[str, dict]:
    """取最后一个 ```json 围栏块解析为结构化字段。

    返回 (markdown, structured): 解析成功时把该围栏块从 markdown 剥离
    (结构化数据单独存储, 报告正文不重复展示机器块); 无围栏或解析失败时
    保留原始全文, 结构化字段全空, 由调用方记 warning。
    """
    matches = list(_JSON_FENCE_RE.finditer(text))
    if not matches:
        logger.warning("dsa analysis: LLM 输出未包含 JSON 围栏块, 结构化字段置空")
        return text.strip(), _empty_structured()
    last = matches[-1]
    try:
        data = json.loads(last.group(1).strip())
    except json.JSONDecodeError as e:
        logger.warning("dsa analysis: JSON 围栏块解析失败, 结构化字段置空: %s", e)
        return text.strip(), _empty_structured()
    markdown = (text[:last.start()] + text[last.end():]).strip()
    return markdown, _normalize_structured(data)


# ================================================================
# 任务管理: 内存任务表 + 单工作线程串行队列
# ================================================================

_task_lock = threading.Lock()
_tasks: dict[str, dict] = {}
_queue: queue.Queue[str] = queue.Queue()
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()

# startup(context) 注入的运行时 (定时任务创建时使用; API 任务按请求解析)
_RUNTIME: dict[str, Any] = {"repo": None, "data_dir": None}


def _create_task(symbols: list[str], mode: str, *, repo, data_dir: Path | None) -> dict:
    task_id = f"t_{cn_now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
    task = {
        "task_id": task_id,
        "status": "running",
        "mode": mode,
        "total": len(symbols),
        "_repo": repo,
        "_data_dir": data_dir,
        "items": [{"symbol": s, "status": "pending", "report_id": None, "error": None} for s in symbols],
    }
    with _task_lock:
        _tasks[task_id] = task
    _ensure_worker()
    _queue.put(task_id)
    return task


def _task_public(task: dict) -> dict:
    with _task_lock:
        items = []
        for it in task["items"]:
            entry: dict = {"symbol": it["symbol"], "status": it["status"]}
            if it.get("report_id"):
                entry["report_id"] = it["report_id"]
            if it.get("error"):
                entry["error"] = it["error"]
            items.append(entry)
        done = sum(1 for it in task["items"] if it["status"] in ("done", "failed"))
        return {
            "task_id": task["task_id"],
            "status": task["status"],
            "total": task["total"],
            "done": done,
            "items": items,
        }


def _ensure_worker() -> None:
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(
            target=_worker_loop, name="dsa-analysis-worker", daemon=True,
        )
        _worker_thread.start()


def _worker_loop() -> None:
    while True:
        task_id = _queue.get()
        try:
            _process_task(task_id)
        except Exception:
            logger.exception("dsa analysis worker crashed on task %s", task_id)
        finally:
            _queue.task_done()


def _process_task(task_id: str) -> None:
    with _task_lock:
        task = _tasks.get(task_id)
    if task is None:
        return
    try:
        for item in task["items"]:
            with _task_lock:
                item["status"] = "running"
            try:
                report = _analyze_one(
                    task["_repo"], task["_data_dir"], item["symbol"], task["mode"],
                )
                with _task_lock:
                    item["status"] = "done"
                    item["report_id"] = report["id"]
            except Exception as e:
                logger.warning("dsa analysis item failed %s: %s", item["symbol"], e)
                with _task_lock:
                    item["status"] = "failed"
                    item["error"] = str(e) or e.__class__.__name__
        with _task_lock:
            task["status"] = "done"
        # P2 挂接点 1: 任务收尾自动对账 (只有终态任务触发; 对账异常全吞,
        # 不让 item 全成功的任务被外层 except 冤枉成 failed)
        try:
            from app.custom import dsa_watch  # 函数级导入防循环

            dsa_watch.on_task_done(task)
        except Exception:
            logger.exception("dsa watch reconcile hook failed")
    except Exception as e:
        logger.exception("dsa analysis task %s crashed: %s", task_id, e)
        with _task_lock:
            task["status"] = "failed"


# ================================================================
# 单票分析流水线 (复用 stock_analyzer 数据组装 + levels 价位)
# ================================================================

def _resolve_data_dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    from app.config import settings

    return Path(settings.data_dir)


def _analyze_one(repo, data_dir: Path | None, symbol: str, mode: str) -> dict:
    """分析单票并落盘, 返回保存后的报告; 任何失败抛异常 (item 级 error)。"""
    # 入口规范化 (定时路径自选 symbol 已是后缀式, 此处为 no-op), 报告落盘用规范化值
    symbol = _canonical_symbol(repo, symbol)
    if repo is None:
        raise RuntimeError("数据仓库未初始化")
    df = _load_kline(repo, symbol)
    if df.is_empty():
        raise RuntimeError(f"标的 {symbol} 暂无日K数据")

    levels = compute_levels(df)
    close = float(df.tail(1)["close"][0]) if "close" in df.columns else None
    levels_summary = summarize_levels(levels, close)

    name: str | None = None
    try:
        name = repo.get_name_map([symbol]).get(symbol)
    except Exception as e:
        logger.warning("dsa analysis: 读取 %s 名称失败: %s", symbol, e)

    # ETF/指数无公司报表, 与 stock_analyzer 同口径跳过财务
    try:
        asset_type = repo.resolve_asset_type(symbol)
    except Exception:
        asset_type = "stock"
    fins: dict[str, list[dict]] = {}
    if asset_type not in {"etf", "index"}:
        try:
            fins = _load_financials(_resolve_data_dir(data_dir), symbol)
        except Exception as e:
            logger.warning("dsa analysis: 读取 %s 财务失败: %s", symbol, e)

    kline_rows = _clean_rows(df, _KLINE_KEEP_COLS)
    messages = _build_messages(symbol, name, kline_rows, fins, levels_summary, mode)
    text = asyncio.run(generate_ai_text(
        messages, temperature=0.5, max_tokens=None, timeout=_LLM_TIMEOUT_SECONDS,
    ))
    markdown, structured = _parse_llm_output(text)
    if not markdown:
        raise RuntimeError("AI 未返回报告正文")

    report: dict = {
        "symbol": symbol,
        "name": name,
        "mode": mode,
        "markdown": markdown,
        **structured,
    }
    return _STORE.save(report)


# ================================================================
# 18:00 定时调度
# ================================================================

_SCHEDULE_KEY = "dsa-analysis-schedule"
_SCHEDULE_DEFAULT = {"enabled": True, "hour": 18, "minute": 0}

_last_run_date: date | None = None


def _load_schedule() -> dict:
    """读取定时偏好; 历史值缺字段/类型不对时回退默认。"""
    raw = preferences_load().get(_SCHEDULE_KEY)
    if not isinstance(raw, dict):
        return dict(_SCHEDULE_DEFAULT)
    enabled = raw.get("enabled", _SCHEDULE_DEFAULT["enabled"])
    if not isinstance(enabled, bool):
        return dict(_SCHEDULE_DEFAULT)
    try:
        hour = int(raw.get("hour", _SCHEDULE_DEFAULT["hour"]))
        minute = int(raw.get("minute", _SCHEDULE_DEFAULT["minute"]))
    except (TypeError, ValueError):
        return dict(_SCHEDULE_DEFAULT)
    return {"enabled": enabled, "hour": hour, "minute": minute}


def _decide_scheduled_run(
    *,
    now: datetime,
    prefs: dict,
    last_run_date: date | None,
    trading_day: bool | None,
    ai_ready: bool,
    symbols: list[str],
) -> tuple[bool, str]:
    """调度判定纯函数: 返回 (是否触发, 原因)。"""
    if not prefs.get("enabled"):
        return False, "disabled"
    if (now.hour, now.minute) != (int(prefs.get("hour", 18)), int(prefs.get("minute", 0))):
        return False, "not_time"
    if last_run_date == now.date():
        return False, "already_ran"
    if trading_day is None:
        logger.info("dsa scheduled: is_trading_day 未知, 退化为周一~五判断 (%s)", now.date())
        is_open = now.weekday() < 5
    else:
        is_open = trading_day
    if not is_open:
        return False, "not_trading_day"
    if not ai_ready:
        return False, "no_ai_key"
    if not symbols:
        return False, "watchlist_empty"
    return True, "trigger"


def _scheduler_tick(now: datetime | None = None) -> tuple[bool, str]:
    """单次调度检查 (30s 一拍): 判定并在触发时对全量自选创建 mode=full 任务。"""
    global _last_run_date
    now = now or cn_now()
    prefs = _load_schedule()
    symbols: list[str] = []
    try:
        symbols = [str(e.get("symbol")).strip() for e in list_symbols() if e.get("symbol")]
    except Exception as e:
        logger.warning("dsa scheduled: 读取自选股失败, 本次跳过: %s", e)
    run, reason = _decide_scheduled_run(
        now=now,
        prefs=prefs,
        last_run_date=_last_run_date,
        trading_day=is_trading_day(),
        ai_ready=ai_configured(),
        symbols=symbols,
    )
    if not run:
        if reason not in ("disabled", "not_time"):
            logger.info("dsa scheduled: 到点但跳过 (%s)", reason)
        return False, reason
    task = _create_task(symbols, "full", repo=_RUNTIME.get("repo"), data_dir=_RUNTIME.get("data_dir"))
    _last_run_date = now.date()
    logger.info("dsa scheduled: 已创建分析任务 %s (%d 只)", task["task_id"], len(symbols))
    return True, reason


def _schedule_loop() -> None:
    while True:
        time.sleep(_CHECK_INTERVAL_SECONDS)
        try:
            _scheduler_tick()
        except Exception:
            logger.exception("dsa scheduled tick failed")
        # P2 挂接点 2: 复用调度循环做 dsa_watch 周期兜底 (30min 对账 + 盘前
        # 自检门控), 不自起线程 (单线程单睡眠; 异常全吞不拖垮调度)
        try:
            from app.custom import dsa_watch  # 函数级导入防循环

            dsa_watch.scheduler_tick()
        except Exception:
            logger.exception("dsa watch scheduler tick failed")


# ================================================================
# HTTP 路由 (契约 §3.1/§3.2 + schedule 补充端点)
# ================================================================

def _resolve_runtime(request: Request) -> tuple[Any, Path]:
    # P2 挂接点 3: 分析路由顺带捕获 monitor 引擎 (含 pending flush), 让 18:00
    # 定时分析收尾对账写盘后引擎能及时拿到最新规则; 失败不影响本请求。
    try:
        from app.custom import dsa_watch  # 函数级导入防循环

        dsa_watch.capture_engine(getattr(request.app.state, "monitor_engine", None))
    except Exception:
        logger.exception("dsa watch capture engine failed")
    repo = getattr(request.app.state, "repo", None)
    data_dir = getattr(getattr(repo, "store", None), "data_dir", None)
    if data_dir is None:
        data_dir = _RUNTIME.get("data_dir")
    if data_dir is None:
        from app.config import settings

        data_dir = settings.data_dir
    return repo, Path(data_dir)


def _report_summary(report: dict) -> dict:
    return {key: report.get(key) for key in _SUMMARY_FIELDS}


def _report_detail(report: dict) -> dict:
    return {
        **_report_summary(report),
        "phase_decision": report.get("phase_decision"),
        "markdown": report.get("markdown"),
    }


def setup(registrar: BackendExtensionRegistrar) -> None:
    router = APIRouter(prefix="/api/ext/dsa", tags=["dsa-analysis"])

    @router.post("/analysis/tasks")
    def create_analysis_task(
        request: Request,
        payload: Annotated[dict, Body()],
    ) -> dict:
        symbols_raw = payload.get("symbols")
        if not isinstance(symbols_raw, list) or not symbols_raw:
            raise HTTPException(status_code=400, detail="symbols 必须是非空数组")
        repo, data_dir = _resolve_runtime(request)
        symbols: list[str] = []
        seen: set[str] = set()
        for raw in symbols_raw:
            if not isinstance(raw, str):
                raise HTTPException(status_code=400, detail="symbols 元素必须是字符串")
            # 规范化 (裸 6 位/前缀式/点分式 → 后缀点分式) 后再去重保序
            text = _canonical_symbol(repo, raw)
            if text and text not in seen:
                seen.add(text)
                symbols.append(text)
        if not 1 <= len(symbols) <= _MAX_SYMBOLS:
            raise HTTPException(status_code=400, detail=f"symbols 去重后须为 1~{_MAX_SYMBOLS} 只")
        mode = payload.get("mode") or "full"
        if mode not in _MODES:
            raise HTTPException(status_code=400, detail="mode 仅支持 full|brief")
        task = _create_task(symbols, mode, repo=repo, data_dir=data_dir)
        # P2 挂接点 3: 捕获 monitor 引擎引用 (对账落盘后 set_rules 生效用;
        # ExtensionContext 不带 engine, 只能在请求 handler 捕获)
        try:
            from app.custom import dsa_watch  # 函数级导入防循环

            dsa_watch.capture_engine(getattr(request.app.state, "monitor_engine", None))
        except Exception:
            logger.exception("dsa watch engine capture failed")
        return {"task_id": task["task_id"]}

    @router.get("/analysis/tasks/{task_id}")
    def get_analysis_task(task_id: str) -> dict:
        with _task_lock:
            task = _tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return _task_public(task)

    @router.get("/analysis/reports")
    def list_analysis_reports(
        request: Request, symbol: str | None = None, limit: int = 20, offset: int = 0,
        date: str | None = None, before: str | None = None,
    ) -> dict:
        if limit < 1:
            raise HTTPException(status_code=400, detail="limit 须 >= 1")
        if offset < 0:
            raise HTTPException(status_code=400, detail="offset 须 >= 0")
        limit = min(limit, _MAX_LIMIT)
        for label, raw in (("date", date), ("before", before)):
            if raw is not None and not _DATE_PARAM_RE.fullmatch(raw):
                raise HTTPException(status_code=400, detail=f"{label} 须为 YYYY-MM-DD")
        if symbol:
            # 查询参数同样规范化: 裸 600519 能命中落盘为 600519.SH 的报告
            repo, _ = _resolve_runtime(request)
            symbol = _canonical_symbol(repo, symbol)
        reports = _STORE.list_reports(symbol)
        if date:
            reports = [r for r in reports if str(r.get("created_at") or "")[:10] == date]
        if before:
            reports = [r for r in reports if str(r.get("created_at") or "")[:10] < before]
        items = [_report_summary(r) for r in reports[offset:offset + limit]]
        return {"total": len(reports), "items": items}

    @router.get("/analysis/reports/{report_id}")
    def get_analysis_report(report_id: str) -> dict:
        report = _STORE.get(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="报告不存在")
        return _report_detail(report)

    @router.delete("/analysis/reports/{report_id}")
    def delete_analysis_report(report_id: str) -> dict:
        if not _STORE.delete(report_id):
            raise HTTPException(status_code=404, detail="报告不存在")
        return {"ok": True}

    @router.get("/analysis/schedule")
    def get_analysis_schedule() -> dict:
        return _load_schedule()

    @router.put("/analysis/schedule")
    def put_analysis_schedule(payload: Annotated[dict, Body()]) -> dict:
        missing = [k for k in ("enabled", "hour", "minute") if k not in payload]
        if missing:
            raise HTTPException(status_code=400, detail=f"缺少字段: {', '.join(missing)}")
        enabled = payload["enabled"]
        if type(enabled) is not bool:
            raise HTTPException(status_code=400, detail="enabled 必须是布尔值")
        hour, minute = payload["hour"], payload["minute"]
        if type(hour) is not int or type(minute) is not int:
            raise HTTPException(status_code=400, detail="hour/minute 必须是整数")
        if not 0 <= hour <= 23:
            raise HTTPException(status_code=400, detail="hour 须在 0~23")
        if not 0 <= minute <= 59:
            raise HTTPException(status_code=400, detail="minute 须在 0~59")
        value = {"enabled": enabled, "hour": hour, "minute": minute}
        preferences_save({_SCHEDULE_KEY: value})
        return value

    registrar.include_router(router)


def startup(context: ExtensionContext) -> None:
    """记录运行时并启动 daemon 调度线程 (不阻止进程退出)。"""
    _RUNTIME["repo"] = context.repository
    _RUNTIME["data_dir"] = Path(context.data_dir)
    threading.Thread(
        target=_schedule_loop, name="dsa-analysis-scheduler", daemon=True,
    ).start()
