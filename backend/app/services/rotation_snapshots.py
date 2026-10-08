"""Stock quote samples, independent of OHLC/minute datasets.

One immutable file per Beijing minute; the current minute also retains the latest
successful quote in memory. A bounded daemon queue keeps disk IO off quote threads.
"""
from __future__ import annotations

import logging
import math
import queue
import threading
from datetime import datetime
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ, cn_now, in_continuous_session

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_latest: dict[Path, tuple[str, pl.DataFrame, int]] = {}
_pending: queue.Queue = queue.Queue(maxsize=4)
_worker: threading.Thread | None = None
_files: dict[Path, tuple[int, pl.DataFrame]] = {}


def _write(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    frame.write_parquet(temporary)
    temporary.replace(path)


def _run() -> None:
    while True:
        path, frame = _pending.get()
        try:
            _write(path, frame)
        except (OSError, pl.exceptions.PolarsError):
            logger.warning('Rotation sample persistence failed', exc_info=True)
        finally:
            _pending.task_done()


def capture(data_dir: Path, records: list[dict], now: datetime | None = None) -> None:
    """Accept already classified stock records only; pct is decimal, amount cumulative.

    Missing timestamps are accepted only during today's continuous session, as a
    successful live fetch. Explicit timestamps allow up to 120 seconds of source/
    local clock skew in either direction; older or further future data is rejected.
    """
    global _worker
    now = (now or cn_now()).astimezone(CN_TZ)
    if not in_continuous_session(now):
        return
    day = now.date().isoformat()
    wall = now.replace(tzinfo=None, second=0, microsecond=0)
    rows = []
    for record in records:
        try:
            stamp = record.get('timestamp')
            if stamp:
                quote_time = datetime.fromtimestamp(float(stamp) / 1000, CN_TZ)
                if quote_time.date() != now.date() or not -120 <= (now - quote_time).total_seconds() <= 120:
                    continue
            elif not in_continuous_session(now):
                continue
            price = float(record['last_price'])
            prev = record.get('prev_close')
            pct = price / float(prev) - 1 if prev and float(prev) > 0 else float(record['change_pct'])
            if price <= 0 or not math.isfinite(price) or not math.isfinite(pct):
                continue
            rows.append({'symbol': str(record['symbol']), 'datetime': wall, '_pct': pct})
        except (TypeError, ValueError, KeyError, OverflowError, OSError):
            continue
    if not rows:
        return
    frame = pl.DataFrame(rows).unique('symbol', keep='last')
    root = Path(data_dir).resolve()
    path = root / 'rotation_snapshots' / f'date={day}' / f'{wall:%H%M}.parquet'
    with _lock:
        previous = _latest.get(root)
        generation = previous[2] + 1 if previous else 1
        _latest[root] = (day, frame, generation)
        if _worker is None:
            _worker = threading.Thread(target=_run, name='rotation-samples', daemon=True)
            _worker.start()
        if previous is None or previous[0] != day or previous[1]['datetime'][0] != wall:
            try:
                _pending.put_nowait((path, frame))
            except queue.Full:
                logger.warning('Rotation sample queue full; keeping latest quote in memory')


def version(data_dir: Path) -> tuple[str, int]:
    day = cn_now().date().isoformat()
    with _lock:
        latest = _latest.get(Path(data_dir).resolve())
        return day, latest[2] if latest and latest[0] == day else 0


def read_today(data_dir: Path) -> pl.DataFrame | None:
    root = Path(data_dir).resolve()
    day = cn_now().date().isoformat()
    with _lock:
        latest = _latest.get(root)
        live = latest[1] if latest and latest[0] == day else None
    frames = []
    for path in sorted((root / 'rotation_snapshots' / f'date={day}').glob('*.parquet')):
        try:
            mtime = path.stat().st_mtime_ns
            with _lock:
                cached = _files.get(path)
            if cached is not None and cached[0] == mtime:
                frame = cached[1]
            else:
                frame = pl.read_parquet(path, columns=['symbol', 'datetime', '_pct'])
                frame = frame.filter(
                    (pl.col('datetime').dt.date().cast(pl.String) == day) & pl.col('_pct').is_finite()
                )
                with _lock:
                    for old in list(_files):
                        if old.parent != path.parent:
                            _files.pop(old)
                    _files[path] = (mtime, frame)
            frames.append(frame)
        except (OSError, pl.exceptions.PolarsError):
            logger.warning('Invalid rotation sample: %s', path.name)
    if live is not None:
        frames.append(live)
    if not frames:
        return None
    result = pl.concat(frames).unique(['symbol', 'datetime'], keep='last').sort('datetime')
    return result if not result.is_empty() else None
