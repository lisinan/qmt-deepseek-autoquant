# -*- coding: utf-8 -*-
"""闸门卡点时间线：跨区 source、跨重启去重、空态诊断、历史共享脚本。

## 这一组测试守的是什么

观察区（paper）页面上的「闸门卡点时间线」曾经是空的，而原因是三层叠加：

1. `Observability.gate_timeline_row()` 把 ``source`` **硬编码成 "live"** ——
   paper 写出来的行也自称 live，两区日志一旦合并/对照就无法区分来源；
2. 去重标志 ``_gate_timeline_date`` **只活在引擎内存里**，盘中重启即丢 ⇒
   同一交易日被重复追加。生产文件 ``logs/gate_timeline.jsonl`` 里已经有
   2026-10-29（3 行）、10-30（2 行）、11-03（2 行）共 6 行重复，
   前端热力矩阵会把这些天画成多个格子、日期横向错位；
3. 读端 ``/api/gate_timeline`` 不去重、空态无说明 —— 页面上只有一句
   「暂无时间线数据」，看不出「是坏了还是本来就没有」。

本文件逐个钉死。**不碰生产库、不碰生产日志**：所有路径都在临时目录下，
且只改 ``config.settings.LOG_DIR`` 这一个模块属性（用完还原）。

⚠ 本项目的 runner 是 ``tests/run_all.py``，用例以 ``fn()`` 无参调用，
所以这里**不用 pytest fixture**（tmp_path / monkeypatch），一律自己建/清临时目录。
"""
from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ------------------------------------------------------------------ 临时环境
@contextlib.contextmanager
def _tmpdir():
    d = tempfile.mkdtemp(prefix="tl_zone_")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def _log_dir(path: Path):
    """临时把 config.settings.LOG_DIR 指到临时目录，退出还原。

    ``api_gate_timeline`` 在**函数内** ``from config.settings import LOG_DIR``，
    所以改为模块属性即可生效 —— 这也是它能跟着 zone 走的原因。
    """
    import config.settings as S

    marker = object()
    orig = getattr(S, "LOG_DIR", marker)
    S.LOG_DIR = str(path)
    try:
        yield
    finally:
        if orig is marker:
            with contextlib.suppress(Exception):
                delattr(S, "LOG_DIR")
        else:
            S.LOG_DIR = orig


# ------------------------------------------------------------------ source 口径
def _obs_with(exec_mode=None, has_attr=True):
    """构造一个最小 Observability 宿主；daily 为空时 gate_detail 返回无行。"""
    from engine.observability import Observability

    if has_attr:
        owner = SimpleNamespace(daily=None, data_mode="xtdata", exec_mode=exec_mode)
    else:
        owner = SimpleNamespace(daily=None, data_mode="xtdata")
    return Observability(owner)


def test_source_follows_exec_mode():
    """paper 引擎写出的行必须标 paper —— 否则两区时间线无法区分来源。"""
    row = _obs_with("paper").gate_timeline_row()
    assert row["source"] == "paper", row["source"]

    row_live = _obs_with("live").gate_timeline_row()
    assert row_live["source"] == "live", row_live["source"]


def test_source_falls_back_to_live_without_exec_mode():
    """宿主没有 exec_mode（老测试替身 / SimpleNamespace）时回落 live。

    这条是**向后兼容闸门**：``tests/test_gate_detail.py:155`` 断言
    ``row["source"] == "live"``，其替身没有 exec_mode 属性；本改动绝不能把它弄红。
    """
    row = _obs_with(None, has_attr=False).gate_timeline_row()
    assert row["source"] == "live", row["source"]


def test_source_rejects_non_string_exec_mode():
    """exec_mode 是对象 / 数字 / 空串时同样回落 live，绝不把对象写进 jsonl。"""
    for bad in (object(), 123, ""):
        row = _obs_with(bad).gate_timeline_row()
        assert row["source"] == "live", (bad, row["source"])


# ------------------------------------------------------ 跨重启去重（读文件尾）
def test_read_last_timeline_date_normal():
    from engine.event_engine import _read_last_timeline_date

    with _tmpdir() as d:
        p = d / "gate_timeline.jsonl"
        p.write_text(
            json.dumps({"date": "2026-11-05", "universe_n": 16}) + "\n" +
            json.dumps({"date": "2026-11-06", "universe_n": 16}) + "\n",
            encoding="utf-8")
        assert _read_last_timeline_date(p) == "2026-11-06"


def test_read_last_timeline_date_missing_or_empty():
    from engine.event_engine import _read_last_timeline_date

    with _tmpdir() as d:
        assert _read_last_timeline_date(d / "nope.jsonl") == ""
        empty = d / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        assert _read_last_timeline_date(empty) == ""


def test_read_last_timeline_date_skips_corrupt_tail():
    """尾部有半截行 / 坏 JSON 时，往前找到第一条合法行（绝不抛）。"""
    from engine.event_engine import _read_last_timeline_date

    with _tmpdir() as d:
        p = d / "gate_timeline.jsonl"
        p.write_text(
            json.dumps({"date": "2026-11-06"}) + "\n"
            + '{"date": "2026-11-07", "universe_n": \n'      # 写一半的坏行
            + "not json at all\n",
            encoding="utf-8")
        assert _read_last_timeline_date(p) == "2026-11-06"


def test_read_last_timeline_date_ignores_short_date():
    """date 不是 10 位 ISO 时不算数（避免把 '2026-11' 当成一天）。"""
    from engine.event_engine import _read_last_timeline_date

    with _tmpdir() as d:
        p = d / "gate_timeline.jsonl"
        p.write_text(
            json.dumps({"date": "2026-11-06"}) + "\n" +
            json.dumps({"date": "2026-11"}) + "\n",
            encoding="utf-8")
        assert _read_last_timeline_date(p) == "2026-11-06"


# --------------------------------------------------------- /api/gate_timeline
def _row(date, n=16, pass_n=0, src="live", **kw):
    d = {"date": date, "universe_n": n,
         "pass": {k: pass_n for k in ("above_ma20", "above_ma60",
                                      "ma20_over_ma60", "slope_up",
                                      "macd_ok", "mom_ok", "score_ok")},
         "trend_up": 0, "all_pass_n": 0,
         "best": {"code": "", "passed_n": 0, "blocked_key": "", "score": 0.0,
                  "mom60": None},
         "source": src}
    d.update(kw)
    return d


def _get(rel_url: str):
    """在临时 LOG_DIR 下真跑一遍路由，返回 (status_code, json)。"""
    from web.app import create_app

    app = create_app(engine=SimpleNamespace())
    r = app.test_client().get(rel_url)
    return r.status_code, r.get_json()


def test_route_empty_returns_hint_not_silent():
    """文件不存在时必须给出可读的 hint —— 用户不该为一句空话去翻日志。"""
    with _tmpdir() as d, _log_dir(d):
        code, j = _get("/api/gate_timeline")
        assert code == 200
        assert j["rows"] == []
        assert len(j["conditions"]) == 7
        assert j["hint"] and "首个交易日" in j["hint"], j.get("hint")
        assert j["error"], "文件缺失时应给出 error 说明"
        assert j["n_days"] == 0


def test_route_dedupes_same_date_keeping_last():
    """同一天多行 ⇒ 只回一行，且取**最后一条**（重启后追加的更完整）。

    生产文件里 2026-10-29 有 3 行、10-30 有 2 行、11-03 有 2 行；
    不去重就会在热力矩阵里把一天画成 3 个格子。
    """
    with _tmpdir() as d, _log_dir(d):
        lines = [
            _row("2026-10-29", pass_n=1),
            _row("2026-10-29", pass_n=2),
            _row("2026-10-29", pass_n=3),      # 最后一条：pass_n=3 应胜出
            _row("2026-10-30", pass_n=4),
            _row("2026-10-30", pass_n=5),
            _row("2026-11-03", pass_n=6),
        ]
        (d / "gate_timeline.jsonl").write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines),
            encoding="utf-8")

        code, j = _get("/api/gate_timeline")
        assert code == 200
        dates = [x["date"] for x in j["rows"]]
        assert dates == ["2026-10-29", "2026-10-30", "2026-11-03"], dates
        got = {x["date"]: x["pass"]["above_ma20"] for x in j["rows"]}
        assert got["2026-10-29"] == 3, got
        assert got["2026-10-30"] == 5, got
        assert j["n_days"] == 3
        assert j["hint"] is None
        assert j["file"] and j["file"].endswith("gate_timeline.jsonl")


def test_route_respects_days_limit():
    with _tmpdir() as d, _log_dir(d):
        (d / "gate_timeline.jsonl").write_text("".join(
            json.dumps(_row(f"2026-10-{i:02d}"), ensure_ascii=False) + "\n"
            for i in range(1, 11)), encoding="utf-8")
        code, j = _get("/api/gate_timeline?days=3")
        assert code == 200
        assert [x["date"] for x in j["rows"]] == [
            "2026-10-08", "2026-10-09", "2026-10-10"]


def test_route_tolerates_corrupt_lines():
    """坏行不能让整个接口变空 —— 观测接口永不 500、也不该丢好行。"""
    with _tmpdir() as d, _log_dir(d):
        (d / "gate_timeline.jsonl").write_text(
            "garbage\n" +
            json.dumps(_row("2026-11-05")) + "\n" +
            '{"date": \n' +
            json.dumps(_row("2026-11-06")) + "\n",
            encoding="utf-8")
        code, j = _get("/api/gate_timeline")
        assert code == 200
        assert [x["date"] for x in j["rows"]] == ["2026-11-05", "2026-11-06"]


# --------------------------------------------------- scripts/seed_zone_timeline
SEED = ROOT / "scripts" / "seed_zone_timeline.py"


def _seed(root: Path, *extra):
    cmd = [sys.executable, str(SEED), "--to", "paper",
           "--root", str(root), *extra]
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


def _lines(p: Path):
    return [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_seed_dry_run_does_not_write():
    """不加 --apply 时绝不能落盘 —— dry-run 被当成真跑会污染观察区历史。"""
    with _tmpdir() as d:
        (d / "logs").mkdir()
        (d / "logs" / "gate_timeline.jsonl").write_text(
            json.dumps(_row("2026-11-05")) + "\n" +
            json.dumps(_row("2026-11-06")) + "\n", encoding="utf-8")

        r = _seed(d)
        assert r.returncode == 0, r.stderr or r.stdout
        assert "将补入：2 行" in r.stdout, r.stdout
        assert not (d / "logs_paper" / "gate_timeline.jsonl").exists(), \
            "dry-run 不应创建目标文件"


def test_seed_apply_then_idempotent():
    """--apply 补齐缺失日期；再跑一次不得重复（幂等）。"""
    with _tmpdir() as d:
        (d / "logs").mkdir()
        (d / "logs" / "gate_timeline.jsonl").write_text(
            json.dumps(_row("2026-11-05")) + "\n" +
            json.dumps(_row("2026-11-06")) + "\n", encoding="utf-8")

        r1 = _seed(d, "--apply")
        assert r1.returncode == 0, r1.stderr or r1.stdout
        dst = d / "logs_paper" / "gate_timeline.jsonl"
        assert dst.exists()
        rows = [json.loads(x) for x in _lines(dst)]
        assert [x["date"] for x in rows] == ["2026-11-05", "2026-11-06"]
        assert all(x.get("origin_zone") == "live" for x in rows), \
            "补入的行必须标明来源区，否则后人会误以为是本区跑出来的"

        r2 = _seed(d, "--apply")
        assert r2.returncode == 0, r2.stderr or r2.stdout
        assert "无需写入" in r2.stdout, r2.stdout
        assert len(_lines(dst)) == 2, _lines(dst)


def test_seed_never_writes_live():
    """--to live 必须被拒绝：实盘时间线不许被别的区的行污染。"""
    with _tmpdir() as d:
        (d / "logs").mkdir()
        src = d / "logs" / "gate_timeline.jsonl"
        src.write_text(json.dumps(_row("2026-11-05")) + "\n", encoding="utf-8")
        before = src.read_text(encoding="utf-8")

        r = subprocess.run(
            [sys.executable, str(SEED), "--to", "live", "--from", "paper",
             "--root", str(d), "--apply"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False)
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        assert "[拒绝]" in r.stdout, r.stdout
        assert src.read_text(encoding="utf-8") == before, "源文件被改写了"


def test_seed_skips_dates_already_in_target():
    """目标区已有的日期不得被覆盖（本区引擎自己写的行优先）。"""
    with _tmpdir() as d:
        (d / "logs").mkdir()
        (d / "logs_paper").mkdir()
        (d / "logs" / "gate_timeline.jsonl").write_text(
            json.dumps(_row("2026-11-05", src="live")) + "\n" +
            json.dumps(_row("2026-11-06", src="live")) + "\n", encoding="utf-8")
        dst = d / "logs_paper" / "gate_timeline.jsonl"
        dst.write_text(json.dumps(_row("2026-11-06", src="paper")) + "\n",
                       encoding="utf-8")

        r = _seed(d, "--apply")
        assert r.returncode == 0, r.stderr or r.stdout
        rows = [json.loads(x) for x in _lines(dst)]
        assert [x["date"] for x in rows] == ["2026-11-05", "2026-11-06"]
        # 2026-11-06 是 paper 自己写的，必须原样保留
        assert rows[-1]["source"] == "paper", rows[-1]
        assert rows[0].get("origin_zone") == "live"
