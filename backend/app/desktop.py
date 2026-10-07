"""桌面客户端入口 — uvicorn 后台服务 + pywebview 桌面窗口。

运行方式:
  开发模式: python -m app.desktop  (需 pip install pywebview)
  打包后:   双击可执行文件即可

职责:
  1. 单实例锁 — 已运行则聚焦已有窗口并退出
  2. 选可用端口 — 从 settings.port 起, 被占则递增
  3. 后台线程起 uvicorn (仅监听 127.0.0.1, 不暴露外网)
  4. 主线程起 pywebview 窗口渲染前端
  5. 窗口关闭 → 优雅停止 uvicorn → 进程退出

不含: 业务逻辑、配置持久化、监控告警 (全在 app.main 里)。
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)

_APP_NAME = "TickFlow 股票面板"
_BASE_PORT = 3018
_PORT_PROBE_RANGE = 50  # 从 3018 起最多试 50 个端口

# 加载页: 双击后立即显示, 后端就绪后由 _open_window 的后台线程 load_url 切换
_LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{margin:0;height:100%;background:#0d1117;color:#c9d1d9;
    font-family:"Microsoft YaHei",system-ui,sans-serif;
    display:flex;align-items:center;justify-content:center}
  .box{text-align:center}
  .spin{width:44px;height:44px;margin:0 auto 22px;border-radius:50%;
    border:3px solid #21262d;border-top-color:#2f81f7;animation:s 1s linear infinite}
  @keyframes s{to{transform:rotate(360deg)}}
  h1{font-size:18px;font-weight:500;margin:0 0 10px}
  p{font-size:12px;color:#8b949e;margin:0}
</style></head><body><div class="box">
  <div class="spin"></div>
  <h1>TickFlow 正在加载…</h1>
  <p>启动时需要同步行情数据，约需 1 分钟，请稍候</p>
</div></body></html>"""

# 失败页: 后端 150s 内未就绪时兜底, 提示去看日志
_FAILED_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{margin:0;height:100%;background:#0d1117;color:#c9d1d9;
    font-family:"Microsoft YaHei",system-ui,sans-serif;
    display:flex;align-items:center;justify-content:center}
  .box{text-align:center;max-width:420px}
  h1{font-size:18px;font-weight:500;margin:0 0 12px;color:#f85149}
  p{font-size:13px;color:#8b949e;margin:0 0 6px;line-height:1.7}
</style></head><body><div class="box">
  <h1>TickFlow 启动超时</h1>
  <p>后端未在预期时间内就绪，请关闭窗口后重新打开。</p>
  <p>反复失败时查看日志：data\\desktop.log</p>
</div></body></html>"""


def _ensure_data_dir_writable() -> None:
    """确保用户数据目录可写 (lifespan 会创建子目录, 这里只验证根目录)。

    data_dir 在 frozen 模式下指向用户目录 (见 config.py), 非可写会导致
    DuckDB 视图 / parquet 落盘全失败。提前失败胜过启动后乱报错。
    """
    from app.config import settings

    data_root = settings.data_dir
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        probe = data_root / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.error("数据目录不可写, 桌面版无法运行: %s (%s)", data_root, e)
        raise


def _acquire_single_instance() -> bool:
    """单实例锁。已运行返回 False (本进程应退出), 否则 True。

    用 data_dir/.desktop.lock 文件锁实现。跨进程, 文件存在即视为已运行
    (简单可靠; 不引入 msvcrt/fcntl 平台差异)。
    """
    from app.config import settings

    lock_path = settings.data_dir / ".desktop.lock"
    if lock_path.exists():
        # 软检测: 写入进程 PID, 若该 PID 已不存在则视为残留锁, 允许接管
        try:
            pid_str = lock_path.read_text(encoding="utf-8").strip()
            pid = int(pid_str) if pid_str.isdigit() else None
        except Exception:  # noqa: BLE001
            pid = None

        if pid is not None and _pid_alive(pid):
            logger.warning("检测到已有实例运行 (PID %d), 本进程退出", pid)
            return False
        # 残留锁: 清理后继续
        logger.info("清理残留单实例锁 (PID %s 已不存在)", pid)

    lock_path.write_text(str(_current_pid()), encoding="utf-8")
    return True


def _release_single_instance() -> None:
    from app.config import settings

    lock_path = settings.data_dir / ".desktop.lock"
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _guard_streams() -> None:
    """windowed 模式 (console=False) 下守护 stdout/stderr。

    PyInstaller console=False 用 runw.exe 启动器, 不分配控制台, 此时 sys.stdout /
    sys.stderr 可能为 None (或底层句柄无效)。后果:
      - app/__init__.py 早期 reconfigure 遇 None 虽有 hasattr 保护, 但若是个「写入即崩」
        的伪 stream 对象, reconfigure 会成功、后续写却崩;
      - logging.basicConfig() 默认建 StreamHandler(sys.stderr), stderr 为 None 时
        首次写日志调 None.write() 抛 AttributeError, 此时往往在导入早期,
        Python 异常处理未就绪 → 进程直接闪退, try/except 都拦不住。

    修法: console=False 下把 stdout/stderr 换成丢弃写入的空对象 (devnull),
    让 logging / reconfigure / 任何 print 都安全落地。console=True 不动 (有真控制台)。
    """
    import os

    class _NullStream:
        """丢弃所有写入的空流 (替代 None 的 stdout/stderr)。"""
        def write(self, _s): return 0
        def flush(self): pass
        def reconfigure(self, *a, **kw): pass
        def isatty(self): return False
        def fileno(self): raise OSError("no fileno")

    # 仅在 stdout/stderr 缺失或不可写时替换 (有真控制台时保持原样)
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            setattr(sys, name, _NullStream())


def _setup_logging() -> None:
    """配置日志落盘到 data/desktop.log。

    背景: spec 里 console=False (桌面应用不弹黑窗), 导致 logging 默认输出的
    stderr 被吞掉 —— 启动期任何异常用户都看不到, 表现为「双击一闪退出、查无日志」。
    这里追加一个 FileHandler, 让日志同时落到 data_dir/desktop.log, 事后可查。

    时序注意: data_dir 在 import app.config 时路径已可用, 但目录此刻可能不存在
    (frozen 首次运行), 必须先 mkdir, 否则 FileHandler 打开文件会抛 FileNotFoundError。
    不用第二次 basicConfig (它「首次调用才生效」), 改用 addHandler 追加。
    """
    try:
        from app.config import settings

        log_dir = settings.data_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(
            log_dir / "desktop.log",
            mode="a",            # 追加, 保留历史 (排查时往往需要对比多次启动)
            encoding="utf-8",
            errors="replace",    # 容错: 对齐 __init__.py 的 stderr 重配, 避免中文/emoji 触发 UnicodeEncodeError
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(handler)
    except Exception as e:  # noqa: BLE001
        # 日志落盘失败不阻断启动 (开发模式 data_dir 可能不可写)
        logger.warning("日志文件初始化失败, 仅输出到 stderr: %s", e)


def _show_crash(title: str, text: str) -> None:
    """崩溃时弹原生 MessageBox 提示用户 (仅 Windows)。

    console=False 下用户看不到任何输出, 崩溃时弹一个原生错误框, 让用户至少
    知道「程序崩了 + 原因」, 并可截图反馈。非 Windows 用日志降级, 不调 ctypes。

    ctypes 是 Python 标准库, 不引入新依赖; MessageBoxW 是 Unicode 版本 (W 后缀),
    支持中文标题/正文。0x10 = MB_ICONERROR (红色错误图标)。
    """
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
        except Exception as e:  # noqa: BLE001
            logger.error("弹框失败 (已写日志文件): %s", e)
    else:
        logger.error("%s: %s", title, text)


def _pid_alive(pid: int) -> bool:
    """检查指定 PID 的进程是否存活。"""
    import os

    if os.name == "nt":
        # Windows: 0 表示存在, 其它是异常
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    else:
        try:
            os.kill(pid, 0)  # signal 0 = 探测存活, 不实际发信号
            return True
        except OSError:
            return False


def _current_pid() -> int:
    import os

    return os.getpid()


def _find_free_port(start: int, count: int = _PORT_PROBE_RANGE) -> int:
    """从 start 起找第一个可用端口。全部被占则返回 start (交给 uvicorn 报错)。"""
    for port in range(start, start + count):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def _run_uvmicorn(port: int, ready_event: threading.Event) -> None:
    """后台线程: 启动 uvicorn 服务。ready_event 在线程退出时置位 (通知主线程)。"""
    import uvicorn

    try:
        # 延迟 import app, 确保配置层已就绪 (frozen 检测在 config.py 导入时完成)
        # 放进 try: app.main 模块导入 (含 app.api.* 一长串 import) 若失败,
        # 异常必须落到 except 记录, 否则主线程只看到「后端超时」而查无 traceback。
        from app.main import app
    except Exception:
        logger.exception("后端模块导入失败 (app.main 或其依赖)")
        ready_event.set()
        return

    config = uvicorn.Config(
        app,
        host="127.0.0.1",  # 仅本机, 不暴露外网 (桌面版无需远程访问)
        port=port,
        log_level="info",
        access_log=False,    # 桌面版不需要访问日志
        loop="auto",
    )
    server = uvicorn.Server(config)

    # 线程结束时通知主线程 (无论正常退出还是异常)
    def _signal_done(*exc):
        ready_event.set()
    server.config.callback_notify = None  # 不用 notify 机制

    try:
        server.run()
    except Exception:
        # server.run() 内部跑 lifespan 启动链, 任一步抛异常都会冒到这里。
        # 不捕获则线程静默死亡, 主线程 _wait_for_server 傻等满 60s 后报「超时」,
        # 真正的崩溃原因 (如某个原生库加载失败 / 缺 hidden import) 永远看不到。
        logger.exception("uvicorn 后端启动/运行失败")
    finally:
        ready_event.set()


def _wait_for_server(port: int, timeout: float = 60.0) -> bool:
    """轮询 health 接口直到后端就绪或超时。

    比 monkey-patch uvicorn 内部方法更健壮, 不依赖版本内部实现。
    """
    import urllib.request
    import urllib.error

    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.5)
    return False


def _open_window(port: int) -> None:
    """主线程: 先显示加载页, 后端就绪后自动切到正式界面 (DSA 式加载体验)。

    双击后立即弹窗给出反馈, 消除启动期(行情同步, 冷启动 1-2 分钟)毫无
    反馈的空窗感; 就绪后 load_url 无缝切换, 不再需要 main 里先等 60s 才开窗。
    """
    import webview  # type: ignore[import-not-found]

    window = webview.create_window(
        _APP_NAME,
        html=_LOADING_HTML,
        width=1440,
        height=900,
        min_size=(1024, 700),
        background_color="#0d1117",
        # 桌面版固定单窗口, 禁用外部浏览器跳转
        confirm_close=False,
    )

    def _on_gui_ready() -> None:
        # 启动器(scripts\desktop-hidden.vbs)以 SW_HIDE 隐藏控制台, Windows 会把
        # 隐藏标志传给进程第一个窗口(即本窗口), 故 GUI 循环就绪后显式 show
        try:
            window.show()
        except Exception:
            pass
        # 冷启动含 lifespan 初始化 + 启动期全市场行情同步, 给足 150s
        if _wait_for_server(port, timeout=150.0):
            try:
                window.load_url(f"http://127.0.0.1:{port}")
                logger.info("加载完成, 已切换到正式界面")
            except Exception:
                # 窗口在加载完成前被用户关掉: webview.start 已/即将返回, 静默即可
                logger.info("窗口已关闭, 跳过界面切换")
        else:
            logger.error("后端启动超时, 窗口停留在失败提示页")
            try:
                window.load_html(_FAILED_HTML)
                window.show()
            except Exception:
                pass

    # webview.start(func): GUI 事件循环就绪后在后台线程执行 func
    webview.start(_on_gui_ready, debug=False)


def main() -> int:
    """桌面客户端主入口。返回进程退出码。"""
    # 必须最先执行: console=False 下 stdout/stderr 可能无效, 不守护会导致
    # 后续 logging.basicConfig 创建的 StreamHandler 写日志时进程崩溃。
    _guard_streams()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # 追加文件日志: console=False 下 stderr 被吞, 必须落盘否则查无对证。
    # 放在 basicConfig 之后 (它先建好 root logger 的格式), 这里只追加 handler。
    _setup_logging()

    try:
        _ensure_data_dir_writable()
    except Exception:
        # 数据目录不可写是致命错误, 无法继续
        return 1

    # 单实例: 已运行则退出
    if not _acquire_single_instance():
        return 0

    try:
        port = _find_free_port(_BASE_PORT)
        logger.info("桌面版后端将监听 127.0.0.1:%d", port)

        # 后台线程起 uvicorn
        ready = threading.Event()
        server_thread = threading.Thread(
            target=_run_uvmicorn, args=(port, ready), daemon=True,
            name="uvicorn",
        )
        server_thread.start()

        # 立即弹加载窗口 (DSA 式: 双击马上有反馈), 后端就绪后窗口自动
        # 切到正式界面; 超时则窗口内显示失败提示, 不再让主流程先干等 60s
        _open_window(port)

        # 窗口关闭后, 进程退出 (daemon 线程会被回收)
        logger.info("窗口已关闭, 桌面版退出")
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        # 顶层兜底: console=False 下未捕获异常会「一闪退出」且无任何反馈。
        # 写完整 traceback 到 data/desktop.log, 并弹原生 MessageBox 让用户截图反馈。
        # 必须排在 KeyboardInterrupt 之后 —— Exception 是基类, 在前会遮蔽它。
        logger.exception("桌面客户端启动失败")
        _show_crash("TickFlow 启动失败", traceback.format_exc())
        return 1
    finally:
        _release_single_instance()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
