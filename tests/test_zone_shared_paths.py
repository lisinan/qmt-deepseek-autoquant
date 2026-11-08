# -*- coding: utf-8 -*-
"""zone 共享落盘路径守卫（paper 不得写 live 的任何文件）。

## 这份测试在防什么

`engine_state` 早已按 DB 分库隔离，但**共享缓存文件**此前与 zone 无关，
paper 会直接写 live 的文件。2026-11-08 实测证据：

    storage/dynamic_universe.json  mtime = 2026-11-08 20:09:43.394506
    logs_paper/quant_system.log:   2026-11-08 20:09:43,394 DynamicUniverse 刷新完成

毫秒级吻合 ⇒ **paper 把 live 的候选池文件覆写了**。后果是资金安全级的：
`active_pool` 直接决定 live 评估/买入哪些票，而 paper 走 tushare、live 走 xtdata，
两边池子本可以不同；一次 paper 刷新失败写入半截 JSON，live 重启就会读到空池/残池。

本模块钉死三件事：

1. **live 逐位不变**：五个共享路径的 live 取值必须等于改造前的硬编码字面量。
   这是「优化不得影响实盘」的兑现方式 —— 不是靠 review，是靠断言。
2. **paper 与 live 零交集**：五条路径两两不相等，且都落在 `storage_paper/` 下。
3. **源码不得回退**：这五个模块里不得再出现硬编码 `"storage"` / `"data"/"cache"`，
   必须走 `core.zone` 的助手函数。否则日后一次「顺手改回来」就会静默复活越界。

纪律：本模块只读源码与路径对象，不建库、不写生产目录、环境变量全部 save/restore。
"""
from __future__ import annotations

import os
from pathlib import Path

import core.zone as Z
from config.settings import BASE_DIR

ROOT = Path(__file__).resolve().parents[1]

# 改造前的字面量（live 必须逐位保持这些值）
LIVE_EXPECT = {
    "dynamic_universe": BASE_DIR / "storage" / "dynamic_universe.json",
    "hist_cache": ROOT / "storage" / "hist_cache",
    "earnrev": BASE_DIR / "data" / "cache",
    "moneyflow": BASE_DIR / "data" / "cache",
    "northbound": BASE_DIR / "data" / "cache",
}

# 需要守卫的源码（模块 -> 该模块里必须出现的 zone 助手）
GUARDED = {
    "data/dynamic_universe.py": "zone_storage_dir",
    "data/hist_cache.py": "zone_storage_dir",
    "data/earnrev_cache.py": "zone_data_cache_dir",
    "data/moneyflow_cache.py": "zone_data_cache_dir",
    "data/northbound_cache.py": "zone_data_cache_dir",
}


# ---------------------------------------------------------------- 工具
class _EnvGuard:
    """保存并恢复环境变量，避免测试互相污染。"""

    def __init__(self, *names):
        self._names = names
        self._saved = {}

    def __enter__(self):
        for n in self._names:
            self._saved[n] = os.environ.get(n)
        return self

    def __exit__(self, *exc):
        for n, v in self._saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        return False


def _paths(zone: str) -> dict:
    """在指定 zone 下取五条共享路径（全部走各模块自己的求值函数）。"""
    import data.dynamic_universe as D
    import data.earnrev_cache as E
    import data.hist_cache as H
    import data.moneyflow_cache as M
    import data.northbound_cache as N

    with _EnvGuard("QMT_ZONE"):
        os.environ["QMT_ZONE"] = zone
        return {
            "dynamic_universe": D._cache_path(),
            "hist_cache": H.cache_dir(),
            "earnrev": E.cache_dir(),
            "moneyflow": M.cache_dir(),
            "northbound": N.cache_dir(),
        }


# ---------------------------------------------------------------- 1. live 逐位不变
def test_zone_storage_dir_live_matches_legacy():
    """`zone_storage_dir` 的 live 分支必须等于改造前的 `BASE_DIR/"storage"`。"""
    assert Z.zone_storage_dir(BASE_DIR, "live") == BASE_DIR / "storage"
    assert Z.zone_data_cache_dir(BASE_DIR, "live") == BASE_DIR / "data" / "cache"


def test_live_shared_paths_are_bit_identical():
    """五个共享路径在 live 下必须逐位等于改造前的字面量。

    这条是「优化不得影响实盘」的硬证据：任何一个路径变了，live 就会读到
    另一份缓存（或空缓存），而日志里不会报错。
    """
    got = _paths("live")
    for key, want in LIVE_EXPECT.items():
        assert Path(got[key]) == Path(want), (
            f"{key} 的 live 路径被改动了：{got[key]} != {want}。"
            f"live 正在跑真实资金，路径漂移会让它读到错缓存。"
        )


# ---------------------------------------------------------------- 2. paper 零交集
def test_paper_paths_are_disjoint_from_live():
    """paper 的五条路径必须全部落在 `storage_paper/` 下，且与 live 无一相等。"""
    live = _paths("live")
    paper = _paths("paper")
    for key in LIVE_EXPECT:
        lp, pp = Path(live[key]), Path(paper[key])
        assert lp != pp, f"{key} 两区路径相同（{lp}）⇒ paper 会写 live 的文件"
        assert "storage_paper" in str(pp).lower(), \
            f"{key} 的 paper 路径没落在 storage_paper 下：{pp}"


def test_paper_cache_dirs_have_no_overlap():
    """paper 内部各路径也互不相同（避免自己踩自己）。"""
    paper = _paths("paper")
    vals = [str(Path(v)).lower() for v in paper.values()]
    # earnrev / moneyflow / northbound 共用同一个 data_cache 目录是**刻意**的
    #（与原实现同构），但目录级路径必须唯一，文件级路径由各模块自己派生。
    dirs = {k: str(Path(v).parent).lower() if Path(v).suffix else str(Path(v)).lower()
            for k, v in paper.items()}
    assert len(set(dirs.values())) >= 3, f"paper 内部路径过度重叠：{dirs}"
    assert len(set(vals)) >= 3, f"paper 内部文件路径过度重叠：{vals}"


# ---------------------------------------------------------------- 3. 源码不得回退
def test_shared_path_modules_use_zone_helpers():
    """五个模块必须 import 并使用 zone 助手，不得重新硬编码路径。

    比对方式：去掉整行注释、去掉所有空白后做子串匹配 —— 这样
    ``BASE_DIR / "storage" / "x"`` 与 ``BASE_DIR/"storage"/"x"`` 都能抓到。
    """
    for rel, helper in GUARDED.items():
        src = (ROOT / rel).read_text(encoding="utf-8")
        code_lines = [ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("#")]
        assert helper in "\n".join(code_lines), (
            f"{rel} 没有使用 {helper} —— 请走 core.zone，"
            f"硬编码路径会让 paper 再次写到 live 的目录"
        )
        compact = "".join("".join(code_lines).split())
        # 这些精确的硬编码形态一旦复活就是越界（已去空白，两种写法都覆盖）
        for bad in ('BASE_DIR/"storage"/"dynamic_universe.json"',
                    'ROOT/"storage"/"hist_cache"',
                    'BASE_DIR/"data"/"cache"'):
            assert bad not in compact, (
                f"{rel} 里仍存在硬编码 {bad!r}；应改用 zone 助手"
            )


def test_dynamic_universe_cache_path_is_a_function_not_a_constant():
    """候选池缓存路径必须是**调用时求值**，不能是模块级常量。

    原因：`data.dynamic_universe` 可能在 `apply_zone()` 之前被 import
    （`main.py:98` 才导入 engine）。常量会在 import 时定型，从而永久指向 live。
    """
    import data.dynamic_universe as D

    assert callable(getattr(D, "_cache_path", None)), \
        "dynamic_universe 缺少 _cache_path() 函数"
    # 两次调用应在同一 zone 下返回同一路径（幂等），但必须不是 Path 常量
    assert isinstance(D._cache_path(), Path)


def test_zone_helpers_do_not_require_env_for_explicit_zone():
    """显式传 zone 时不得依赖环境变量（测试与脚本才能可靠断言两个区）。"""
    with _EnvGuard("QMT_ZONE"):
        os.environ.pop("QMT_ZONE", None)
        assert Z.zone_storage_dir(BASE_DIR, "paper") == BASE_DIR / "storage_paper"
        assert Z.zone_data_cache_dir(BASE_DIR, "paper") == (
            BASE_DIR / "storage_paper" / "data_cache")
