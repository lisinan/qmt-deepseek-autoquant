# -*- coding: utf-8 -*-
"""zone（live / paper 双实例）隔离守卫。

## 这份测试在防什么

2026-11-08 OWNER 决定把项目分成两个功能区：live（真实资金，唯一交易区）与
paper（AI 产业链热点观察台，起始资金 100 万，禁止真实下单），两个实例可同时在跑。
本模块钉死四类「改错了会静默出事」的约定：

1. **live 逐位不变**：`zone_config("live")` 每一项必须等于既有常量，
   `apply_zone("live")` 除 `QMT_ZONE` 外**不得写任何环境变量**。
2. **paper 不得与 live 共享任何落盘位置**（DB / 日志 / 端口 / 复位标志）。
   共享 `engine_state` 的后果是资金安全级的：`engine_state` 是 `CHECK(id=1)` 单行，
   paper 的 `peak_asset` 是 100 万量级，写进去之后 live 重启会算出
   (57,862.57 − 1,000,000)/1,000,000 = **−94.21%** ⇒ 直接击穿 −20% 断路器并强平
   真实持仓。而 live 的恢复分支在 `event_engine.py` 提前 return、**跳过了
   `_sanitize_restored_peaks()`**，没有任何夹紧保护。
3. **paper 不可能触达真实下单**：`_make_broker()` 见到 `QMT_ZONE=paper` 必须在
   构造期返回拒单桩，且**不得实例化** QMTBroker / BridgeBroker。
4. **单例 pid 与复位标志必须跟随 zone**：`_singleton_pid_path()` 曾硬编码
   `BASE_DIR/"logs"`，与 `main.PID_FILE` 构成同一事实的两份实现；两者不同步会出现
   「第二个实例把自己的 pid 写进第一个实例的 pid 文件 ⇒ 实盘引擎下一轮主动退出」。

纪律：本文件所有用例都不得写生产 `storage/qmt.db`（`apply_zone` 一律传 tmp 作
base_dir），也不得留下环境变量（全部 save/restore）。
"""
from __future__ import annotations

import inspect
import os
import tempfile
from pathlib import Path

import config.settings as settings
from core import zone as Z


# ---------------------------------------------------------------- 工具
class _EnvGuard:
    """保存并恢复一组环境变量，避免测试互相污染（尤其是 QMT_DB）。"""

    KEYS = ("QMT_ZONE", "QMT_DB", "QMT_NOTICES_LOG", "QMT_BRIDGE_ROOT")

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


# ---------------------------------------------------------------- 1. live 不变
def test_live_zone_config_matches_settings_defaults():
    """live 分支必须与既有常量逐项相等 —— 防「顺手改了实盘路径」。"""
    cfg = Z.zone_config("live")
    assert cfg.zone == "live"
    assert Path(cfg.db_path) == Path(settings.BASE_DIR) / "storage" / "qmt.db"
    assert Path(cfg.log_dir) == Path(settings.LOG_DIR)
    assert Path(cfg.notices_log) == Path(settings.BASE_DIR) / "logs" / "notices.log"
    assert cfg.web_port == settings.WEB_PORT
    assert str(cfg.bridge_root) == r"C:\pazq_qmt_bridge"


def test_apply_zone_live_sets_no_path_env():
    """`apply_zone("live")` 只能写 QMT_ZONE，其余一个都不许写。

    这是「改造前后 live 行为逐位不变」的兑现方式。若这里被放宽，将来某个自动化
    在 live 环境里悄悄继承到 QMT_DB，账本就换文件了而日志不会说。
    """
    with _EnvGuard() as g:
        Z.apply_zone("live", base_dir=Path(tempfile.gettempdir()) / "_zone_live")
        assert os.environ.get("QMT_ZONE") == "live"
        for k in ("QMT_DB", "QMT_NOTICES_LOG", "QMT_BRIDGE_ROOT"):
            assert k not in os.environ, f"live 分支不应写 {k}，实际写了 {os.environ.get(k)!r}"


# ---------------------------------------------------------------- 2. 隔离
def test_paper_zone_paths_disjoint_from_live():
    """paper 与 live 的 DB / 日志 / 端口 / 复位目录必须互不相同。"""
    live = Z.zone_config("live")
    paper = Z.zone_config("paper")
    assert paper.zone == "paper"
    assert Path(paper.db_path) != Path(live.db_path)
    assert Path(paper.log_dir) != Path(live.log_dir)
    assert Path(paper.notices_log) != Path(live.notices_log)
    assert paper.web_port != live.web_port
    # 关键：paper 的 DB 目录不能落在 live 的 storage/ 里
    assert Path(paper.db_path).parent != Path(live.db_path).parent
    # 关键：paper 的日志目录不能是 live 日志目录的父/子目录
    assert Path(live.log_dir) not in Path(paper.log_dir).parents
    assert Path(paper.log_dir) not in Path(live.log_dir).parents


def test_apply_zone_paper_respects_preexisting_qmt_db():
    """已设的 QMT_DB 优先（setdefault 语义）。

    为什么必须如此：`tests/run_all.py` 与 `test_no_production_db_pollution`
    依赖「先把 QMT_DB 指到 tmp」来隔离生产库。若 apply_zone 用直接赋值，
    任何走 zone 的测试都会把生产库重新指回来。
    """
    sentinel = str(Path(tempfile.gettempdir()) / "_zone_keep_me.db")
    with _EnvGuard():
        os.environ["QMT_DB"] = sentinel
        with tempfile.TemporaryDirectory() as td:
            Z.apply_zone("paper", base_dir=Path(td))
        assert os.environ["QMT_DB"] == sentinel


def test_apply_zone_paper_sets_and_creates_paths():
    """paper 分支必须把 DB / notices / 日志目录真正建出来并写进环境变量。"""
    with _EnvGuard():
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = Z.apply_zone("paper", base_dir=base)
            assert Path(cfg.db_path).parent.exists()
            assert Path(cfg.log_dir).exists()
            assert os.environ["QMT_DB"] == str(cfg.db_path)
            assert os.environ["QMT_NOTICES_LOG"] == str(cfg.notices_log)
            # 单库单目录：notices 与 db 在同一个 paper 根下，不会回落 live
            assert str(base / "storage_paper") in os.environ["QMT_DB"]


# ---------------------------------------------------------------- 3. zone 解析
def test_resolve_zone_precedence():
    """`--zone` > `QMT_ZONE` > 缺省 live；非法值必须报错而不是静默回落。"""
    with _EnvGuard():
        assert Z.resolve_zone([]) == "live"
        os.environ["QMT_ZONE"] = "paper"
        assert Z.resolve_zone([]) == "paper"
        assert Z.resolve_zone(["--zone", "live"]) == "live"      # argv 胜过 env
        assert Z.resolve_zone(["--zone=paper"]) == "paper"
        os.environ.pop("QMT_ZONE", None)
        try:
            Z.resolve_zone(["--zone", "staging"])
        except SystemExit:
            pass
        else:
            raise AssertionError("非法 zone 必须 SystemExit，不能静默回落成 live")


# ---------------------------------------------------------------- 4. pid 与复位标志
def test_singleton_pid_path_follows_log_dir():
    """`_singleton_pid_path()` 必须走 LOG_DIR，且 live 下与原硬编码逐位相同。

    注意：`tests/run_all.py::_collect_tests()` 会把 `engine.event_engine.LOG_DIR`
    重定向到临时目录（防 snapshot() 往生产 logs/ 写基准行）。所以本用例**不能**
    假设环境里的 LOG_DIR 就是 live 值，必须自己显式设回 `settings.LOG_DIR`
    再验等价性，否则在 run_all 里跑会读到临时路径而误报失败。
    """
    import engine.event_engine as ee

    eng = ee.EventEngine.__new__(ee.EventEngine)   # 不跑 __init__，不建 DB
    saved = ee.LOG_DIR
    try:
        # ① live 缺省值：必须与改造前的硬编码逐位相同
        ee.LOG_DIR = str(settings.LOG_DIR)
        live_expected = Path(ee.__file__).resolve().parent.parent / "logs" / "engine.pid"
        assert str(eng._singleton_pid_path()) == str(live_expected), \
            f"live 缺省路径变了：{eng._singleton_pid_path()} != {live_expected}"

        # ② 重绑 LOG_DIR 后必须跟随（paper 区靠这一条生效）
        tmp = Path(tempfile.gettempdir()) / "logs_paper"
        ee.LOG_DIR = str(tmp)
        assert str(eng._singleton_pid_path()) == str(tmp / "engine.pid")
    finally:
        ee.LOG_DIR = saved


def test_reset_flag_path_follows_db_path_not_log_dir():
    """复位标志必须由 DB 路径推导，不能由 LOG_DIR 反推。

    旧实现 ``Path(LOG_DIR).parent / "storage"`` 假设「LOG_DIR 的父目录是 BASE_DIR」。
    双实例下这会把 paper 的复位标志写进 live 的 storage，而同一函数后半段的
    ``clear_equity_before()`` 会**删掉 live 的权益快照**。
    """
    import engine.event_engine as ee

    src = inspect.getsource(ee.EventEngine._apply_pending_reset)
    # 只看代码行：本函数自己的注释里会引用旧写法作为反例。
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "self.storage.db_path" in code, "复位标志必须从 DB 路径推导"
    assert "Path(LOG_DIR).parent" not in code, \
        "不得再用 Path(LOG_DIR).parent 反推 storage（双实例下会落到 live 目录）"


# ---------------------------------------------------------------- 5. paper 不下单
def test_paper_zone_never_constructs_real_broker():
    """`QMT_ZONE=paper` 时 `_make_broker()` 必须返回拒单桩，且不实例化真实通道。

    这条是「paper 不可能触达真实下单」的钉子：哨兵一旦被构造就 AssertionError，
    所以任何「先构造再判断」的写法都会在这里炸出来。
    """
    import core.broker as B
    import core.broker_bridge as BB

    class _Boom:
        def __init__(self, *a, **kw):
            raise AssertionError("zone=paper 不得实例化真实交易通道")

    saved_qmt, saved_bridge = B.QMTBroker, BB.BridgeBroker
    saved_env = os.environ.get("QMT_ZONE")
    try:
        B.QMTBroker = _Boom
        BB.BridgeBroker = _Boom
        os.environ["QMT_ZONE"] = "paper"
        broker = B._make_broker()
        assert isinstance(broker, B._NoTradeBroker), type(broker)
        assert broker.is_connected is False
        assert broker.mode == "paper_no_trade"
    finally:
        B.QMTBroker, BB.BridgeBroker = saved_qmt, saved_bridge
        if saved_env is None:
            os.environ.pop("QMT_ZONE", None)
        else:
            os.environ["QMT_ZONE"] = saved_env


def test_paper_stub_place_order_rejected():
    """拒单桩的下单/撤单必须一律 ok=False，且带 zone=paper 理由（可审计）。"""
    from core.broker import _NoTradeBroker

    b = _NoTradeBroker()
    r = b.place_order("300308.SZ", "BUY", 100, 448.0, "cash")
    assert r["ok"] is False
    assert "zone=paper" in r["reason"]
    assert r["order_id"] == ""
    c = b.cancel_order_stock("cash", "oid-1")
    assert c["ok"] is False


def test_paper_stub_queries_return_none_not_empty():
    """查询必须返回 None（不可用），**绝不能返回 []**。

    `[]` 在 `_sync_broker_positions` 里表示「券商确认空仓」，会清空本地账本
    （2026-11-02 事故口径）；`None` 才表示「本轮不可用、不要动账本」。
    """
    from core.broker import _NoTradeBroker

    b = _NoTradeBroker()
    assert b.get_positions("cash") is None
    assert b.get_trades("cash") is None
    assert b.get_orders("cash") is None
    assert b.get_asset("cash") is None
    assert b.connect(force=True) is False


# ---------------------------------------------------------------- 6. 注入顺序
def test_main_applies_zone_before_importing_engine():
    """`main.py` 必须在 `from engine.event_engine import EventEngine` **之前**注入 zone。

    为什么这条要单独用源码顺序守卫：`engine/event_engine.py` 是模块级
    `from config.settings import ... LOG_DIR`，且 `STOP_SENTINEL` 是**类属性、
    导入时求值**。一旦有人把 zone 注入挪到 import 之后，代码照样能跑、测试照样绿，
    但 paper 实例的日志/哨兵/pid 会静默落回 live 目录 —— 这正是最难发现的一类回归。
    不用 import main 来验：那会执行模块级 `apply_zone()`，污染测试进程的
    QMT_ZONE。读源码文本即可。
    """
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    # 只看真正的代码行：注释里出现同样的字样（本文档的说明、main.py 的注释）会误判。
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    i_apply = code.find("apply_zone(")
    i_engine = code.find("from engine.event_engine import")
    assert i_apply != -1, "main.py 里找不到 apply_zone(...) 调用"
    assert i_engine != -1, "main.py 里找不到 engine 导入"
    assert i_apply < i_engine, \
        "zone 必须在导入 engine 之前注入，否则 LOG_DIR / STOP_SENTINEL 已绑死在旧 zone"
    # --zone 参数必须存在，否则命令行无法表达 zone
    assert '"--zone"' in src, "main.py 缺少 --zone 参数"

