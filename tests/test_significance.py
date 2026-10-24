# -*- coding: utf-8 -*-
"""WP-O 显著性护栏单测：strategy/significance.py（置换检验 + DSR）。

独立可跑：python tests/test_significance.py
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.significance import (  # noqa: E402
    permutation_pvalue,
    permutation_test_returns,
    deflated_sharpe,
    trial_adjusted_threshold,
)


def t_permutation_pvalue():
    # 空零分布 → p=1
    assert permutation_pvalue(1.0, []) == 1.0
    # 全部低于观测 → p = 1/(n+1)
    assert abs(permutation_pvalue(1.0, [0.0] * 9) - 0.1) < 1e-12
    # 一半高于 → p≈0.5+
    p = permutation_pvalue(0.0, [-1, 1, -1, 1])
    assert abs(p - 3 / 5) < 1e-12
    print("PASS permutation_pvalue")


def t_permutation_test_sign():
    # 全正收益：符号翻转后 Sharpe 期望≈0，观测 Sharpe 应显著 → p 很小
    daily = [0.001 + 0.0001 * (i % 3) for i in range(250)]
    r = permutation_test_returns(daily, n_perm=500, seed=42)
    assert r["observed"] > 0, r
    assert abs(r["null_mean"]) < 0.3, r["null_mean"]
    assert r["p_value"] <= 0.05, r["p_value"]

    # 纯噪声（均值 0 的伪随机）：p 应中等，不极端；用固定 seed 断言区间
    rng = random.Random(7)
    noise = [rng.gauss(0.0, 0.01) for _ in range(250)]
    r2 = permutation_test_returns(noise, n_perm=500, seed=1)
    assert 0.10 <= r2["p_value"] <= 0.95, r2["p_value"]
    assert abs(r2["null_mean"]) < 0.3, r2["null_mean"]

    # 可复现：同 seed 两次结果完全一致
    a = permutation_test_returns(noise, n_perm=100, seed=5)
    b = permutation_test_returns(noise, n_perm=100, seed=5)
    assert a == b
    print(f"PASS permutation_test_returns (正收益p={r['p_value']:.4f}, "
          f"噪声p={r2['p_value']:.4f})")


def t_dsr():
    # n_trials=1：无多重检验膨胀，E[maxSR0]=0，正 Sharpe 的 DSR 应≈Φ(sr/sd)
    d1 = deflated_sharpe(1.0, 1, 250)
    assert abs(d1["sr0"]) < 1e-12
    # n_trials=1：无膨胀 → dsr ≈ Φ(sr/sd)，sd 含 Lo(2002) 的 kurt 修正项
    sr_d = 1.0 / math.sqrt(252.0)
    var = (1.0 + 0.5 * sr_d * sr_d) / 249
    sd_ann = math.sqrt(var * 252.0)
    expect = 0.5 * (1 + math.erf((1.0 / sd_ann) / math.sqrt(2)))
    assert abs(d1["dsr"] - expect) < 1e-9, (d1["dsr"], expect)
    # 单调性：sr 固定，n_trials 越大惩罚越重 → dsr 单调下降、sr0 单调上升
    prev_dsr, prev_sr0 = 2.0, -1.0
    for n in (1, 5, 20, 100, 500):
        d = deflated_sharpe(1.0, n, 250)
        assert d["dsr"] < prev_dsr, (n, d["dsr"])
        assert d["sr0"] >= prev_sr0, (n, d["sr0"])
        prev_dsr, prev_sr0 = d["dsr"], d["sr0"]
    # sr=0 时 dsr<0.5（被期望极大值惩罚）
    assert deflated_sharpe(0.0, 100, 250)["dsr"] < 0.5
    print(f"PASS deflated_sharpe (sr0@100trials={deflated_sharpe(0,100,250)['sr0']:+.3f})")


def t_threshold():
    # 教科书量级：n_trials=1, n_obs=250 → 年化门槛 ≈ z·√(252/249)
    t1 = trial_adjusted_threshold(1, 250)
    assert abs(t1 - 1.645 * math.sqrt(252 / 249)) < 1e-9, t1
    assert 1.5 < t1 < 1.8, t1  # ≈1.65
    # 试验次数越多门槛越高
    t100 = trial_adjusted_threshold(100, 250)
    assert t100 > t1, (t100, t1)
    # z=2.33 档更高
    assert trial_adjusted_threshold(1, 250, z=2.33) > t1
    print(f"PASS trial_adjusted_threshold (t1={t1:.3f}, t100={t100:.3f})")


if __name__ == "__main__":
    t_permutation_pvalue()
    t_permutation_test_sign()
    t_dsr()
    t_threshold()
    print("ALL PASS")
