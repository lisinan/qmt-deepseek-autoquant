# -*- coding: utf-8 -*-
"""R39 判决性诊断：momentum_weight 的效应到底是「结构」还是「窗口/周期运气」。

为什么必须做：四窗口共识里 60x9/90x6/120x4 三窗为正（+0.06~+0.31），
75x7 一窗为负（−0.05~−0.11）；而 IS（900 根，样本最大）却显示收益与
Calmar 单调下降。IS 与 OOS 口径互相矛盾 ⇒ 必须先判定「谁是真信号」。

诊断 1（逐折）：对每个折单独看 dSh 与 dRet，看正向是否**广谱**还是
        由少数折驱动（尖峰 ⇒ 噪声）。
诊断 2（周期切分）：IS 900 根 = OOS 覆盖的末 ~540 根 + 早期 ~360 根。
        把早期段单独跑一遍。若早期为负、近期为正 ⇒ 是**周期依赖**而非结构改进。
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from strategy import _evolve_wf as W  # noqa: E402
from dataclasses import replace  # noqa: E402

COUNT = 900


def main():
    codes = W.wide_universe()
    data = W.preload(codes + [W.MARKET_INDEX_CODE, "000300.SH"], COUNT)
    d0, panel = W.aligned_universe(data, codes)
    n = len(d0)
    print(f"[数据] {len(data)} 只 × {n} 根  {d0[0]} -> {d0[-1]}")

    def bt(cfg, dset, cnt=COUNT):
        ks = [k for k in dset.keys() if k not in W.INDEX_CODES]
        return W.run_backtest(ks, cfg, count=cnt, preloaded=dset)

    base = W.base_cfg()
    cands = {
        "pow1.0": replace(base, momentum_weight=True, momentum_weight_pow=1.0),
        "pow3.0": replace(base, momentum_weight=True, momentum_weight_pow=3.0),
    }

    # ---------- 诊断 2：周期切分 ----------
    # 末 540 根 = OOS 四窗共同覆盖区间；早期 = 其余
    tail = 540
    print("\n" + "=" * 96)
    print("诊断 2：周期切分（IS 全样本 / 末 540 根 / 早期前段）")
    print("=" * 96)

    def seg(dset, lo, hi):
        return {k: {kk: vv[lo:hi] for kk, vv in v.items()} for k, v in dset.items()}

    segs = {
        "全样本900": seg(panel, 0, n),
        "末540根": seg(panel, n - tail, n),
        "前360根": seg(panel, 0, n - tail),
    }
    for sname, dset in segs.items():
        b = bt(base, dset)
        line = f"{sname:<12} P0     ret={b['total_return']*100:+9.2f}% Sh={b['sharpe']:+.4f} MDD={b['max_drawdown']*100:+7.2f}%"
        print(line)
        for cn, cfg in cands.items():
            r = bt(cfg, dset)
            print(f"{'':<12} {cn:<6} ret={r['total_return']*100:+9.2f}% Sh={r['sharpe']:+.4f} "
                  f"MDD={r['max_drawdown']*100:+7.2f}%  "
                  f"| d_ret={(r['total_return']-b['total_return'])*100:+7.2f}pt "
                  f"d_sh={r['sharpe']-b['sharpe']:+.4f}")

    # ---------- 诊断 1：逐折 ----------
    print("\n" + "=" * 96)
    print("诊断 1：逐折 dSh / d_ret（60x9 与 75x7 两套划分）")
    print("=" * 96)
    for fold, nf in [(60, 9), (75, 7)]:
        subs = W.make_folds(panel, codes, n, fold, nf)
        b = [bt(base, s[2]) for s in subs]
        c = {cn: [bt(cfg, s[2]) for s in subs] for cn, cfg in cands.items()}
        print(f"\n--- {fold}x{nf} ---")
        for i in range(nf):
            msg = (f"  F{i+1} {d0[subs[i][0]]}~{d0[subs[i][1]-1]}: "
                   f"P0 ret={b[i]['total_return']*100:+7.2f}% Sh={b[i]['sharpe']:+.3f} | ")
            for cn in cands:
                r = c[cn][i]
                msg += (f"{cn}: d_ret={(r['total_return']-b[i]['total_return'])*100:+6.2f}pt "
                        f"d_sh={r['sharpe']-b[i]['sharpe']:+.3f}  ")
            print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
