# -*- coding: utf-8 -*-
"""技术面与量化指标计算（分析脚本，只读面板，不触碰任何配置/策略参数）。

数据源: storage/hist_cache/ts_panel_25_901_20211201_20261027.pkl
        (tushare pro.daily + index_daily, 刷新 2026-10-28, as_of 2026-10-27)
口径:
  - 收益率: 近N日 = close[t]/close[t-N] - 1 (N 为交易日数)
  - 年内: base = 2025年最后一个交易日收盘 (即 2026 年首个交易日的前一日)
  - MA_k  = 最近 k 日收盘简单均值
  - 60日动量 = close[t]/close[t-60] - 1 (与 settings.momentum_lookback=60 一致)
  - ATR(14) = 最近14日 TR 的简单均值, TR = max(H-L, |H-PC|, |L-PC|), PC=前收
  - 量能比 = mean(vol 近20日) / mean(vol 前20日, 即 [-40,-20))
  - 距60日高回撤 = close[t]/max(high 近60日) - 1
  - 年内位置 = (close - 年内低) / (年内高 - 年内低)
"""
import pickle
import sys

import numpy as np

PANEL = "storage/hist_cache/ts_panel_25_901_20211201_20261027.pkl"
AS_OF = "20261027"

# 23 只个股(含面板内 688012.SH 中微公司) + 2 指数
STOCKS = {
    "300308.SZ": ("中际旭创", "光模块"),
    "300502.SZ": ("新易盛", "光模块"),
    "300394.SZ": ("天孚通信", "光模块"),
    "300570.SZ": ("太辰光", "光模块"),
    "688256.SH": ("寒武纪", "AI芯片"),
    "688041.SH": ("海光信息", "AI芯片"),
    "300474.SZ": ("景嘉微", "AI芯片"),
    "688008.SH": ("澜起科技", "AI芯片"),
    "688981.SH": ("中芯国际", "晶圆代工"),
    "603986.SH": ("兆易创新", "晶圆代工"),
    "002371.SZ": ("北方华创", "晶圆代工"),
    "000977.SZ": ("浪潮信息", "服务器算力"),
    "603019.SH": ("中科曙光", "服务器算力"),
    "002415.SZ": ("海康威视", "服务器算力"),
    "300383.SZ": ("光环新网", "服务器算力"),
    "002916.SZ": ("深南电路", "PCB互联"),
    "300476.SZ": ("胜宏科技", "PCB互联"),
    "603881.SH": ("数据港", "PCB互联"),
    "002230.SZ": ("科大讯飞", "AI应用"),
    "688111.SH": ("金山办公", "AI应用"),
    "300033.SZ": ("同花顺", "AI应用"),
    "300496.SZ": ("中科创达", "AI应用"),
    "688012.SH": ("中微公司", "半导体设备(面板内第23只)"),
}
INDICES = {
    "000300.SH": ("沪深300", "指数"),
    "399006.SZ": ("创业板指", "指数"),
}


def sma(x, n):
    return float(np.mean(x[-n:]))


def metrics(d):
    c = np.asarray(d["close"], dtype=float)
    h = np.asarray(d["high"], dtype=float)
    l = np.asarray(d["low"], dtype=float)
    v = np.asarray(d["volume"], dtype=float)
    dates = d["date"]
    t = len(c) - 1
    assert dates[t] == AS_OF, f"末日应为 {AS_OF}, 实际 {dates[t]}"

    def ret(n):
        return float(c[t] / c[t - n] - 1.0)

    # 年内基准: 2026 年首个交易日的前一收盘
    idx_2026 = next(i for i, dt in enumerate(dates) if dt >= "20260101")
    ytd_base = c[idx_2026 - 1]

    # MA
    ma5, ma20, ma60 = sma(c, 5), sma(c, 20), sma(c, 60)
    close = float(c[t])

    # 60 日动量
    mom60 = float(c[t] / c[t - 60] - 1.0)

    # ATR(14) 简单均值
    trs = []
    for i in range(t - 13, t + 1):
        pc = c[i - 1]
        tr = max(h[i] - l[i], abs(h[i] - pc), abs(l[i] - pc))
        trs.append(tr)
    atr14 = float(np.mean(trs))
    atr_pct = atr14 / close * 100.0

    # 量能比
    vol_ratio = float(np.mean(v[-20:]) / np.mean(v[-40:-20]))

    # 距 60 日最高价回撤
    dd60 = float(close / np.max(h[-60:]) - 1.0)

    # 年内高低
    yh = float(np.max(h[idx_2026:]))
    yl = float(np.min(l[idx_2026:]))
    pos_y = float((close - yl) / (yh - yl)) if yh > yl else float("nan")

    # 区间高低 (支撑/压力位参考)
    low20 = float(np.min(l[-20:])); low60 = float(np.min(l[-60:]))
    high20 = float(np.max(h[-20:])); high60 = float(np.max(h[-60:]))

    return dict(
        close=close,
        ret1=ret(1) * 100, ret5=ret(5) * 100, ret20=ret(20) * 100,
        ret60=ret(60) * 100, ytd=(close / ytd_base - 1.0) * 100,
        ma5=ma5, ma20=ma20, ma60=ma60,
        vs_ma5=(close / ma5 - 1.0) * 100,
        vs_ma20=(close / ma20 - 1.0) * 100,
        vs_ma60=(close / ma60 - 1.0) * 100,
        above_ma60=bool(close > ma60),
        mom60=mom60 * 100,
        atr14=atr14, atr_pct=atr_pct,
        vol_ratio=vol_ratio,
        vol20=float(np.mean(v[-20:])), vol_prev20=float(np.mean(v[-40:-20])),
        dd60=dd60 * 100,
        yh=yh, yl=yl, pos_y=pos_y * 100,
        low20=low20, low60=low60, high20=high20, high60=high60,
    )


def main():
    with open(PANEL, "rb") as f:
        panel = pickle.load(f)

    rows = []
    for code, (name, sec) in list(STOCKS.items()) + list(INDICES.items()):
        m = metrics(panel[code])
        m.update(code=code, name=name, sector=sec,
                 is_index=code in INDICES)
        rows.append(m)

    # 打印 (供 agent 读取, 单位 %)
    hdr = ("代码", "名称", "板块", "收盘", "1日%", "5日%", "20日%", "60日%",
           "年内%", "MA5", "vsMA5%", "MA20", "vsMA20%", "MA60", "vsMA60%",
           "动量60%", "ATR14", "ATR%", "量能比", "均量20(万手)",
           "距60高%", "年内高", "年内低", "年内位%", "低20", "低60", "高20", "高60")
    print("\t".join(hdr))
    for r in rows:
        print("\t".join(str(x) for x in [
            r["code"], r["name"], r["sector"],
            f'{r["close"]:.2f}', f'{r["ret1"]:+.2f}', f'{r["ret5"]:+.2f}',
            f'{r["ret20"]:+.2f}', f'{r["ret60"]:+.2f}', f'{r["ytd"]:+.2f}',
            f'{r["ma5"]:.2f}', f'{r["vs_ma5"]:+.2f}',
            f'{r["ma20"]:.2f}', f'{r["vs_ma20"]:+.2f}',
            f'{r["ma60"]:.2f}', f'{r["vs_ma60"]:+.2f}', f'{r["mom60"]:+.2f}',
            f'{r["atr14"]:.2f}', f'{r["atr_pct"]:.2f}', f'{r["vol_ratio"]:.2f}',
            f'{r["vol20"]/1e4:.0f}',
            f'{r["dd60"]:+.2f}', f'{r["yh"]:.2f}', f'{r["yl"]:.2f}',
            f'{r["pos_y"]:.1f}',
            f'{r["low20"]:.2f}', f'{r["low60"]:.2f}',
            f'{r["high20"]:.2f}', f'{r["high60"]:.2f}',
            "IDX" if r["is_index"] else
            ("Y" if r["above_ma60"] else "N"),
        ]))

    # 策略口径统计 (仅 23 只个股)
    st = [r for r in rows if not r["is_index"]]
    n = len(st)
    n_ma = sum(1 for r in st if r["above_ma60"])
    n_mom = sum(1 for r in st if r["mom60"] > 15.0)
    n_both = sum(1 for r in st if r["above_ma60"] and r["mom60"] > 15.0)
    print(f"\n[策略口径] 个股 N={n}; 收盘>MA60: {n_ma}; "
          f"60日动量>0.15: {n_mom}; 两者同时满足: {n_both}")
    print(f"[市场环境] regime_index=399006.SZ 收盘>MA60: "
          f"{[r for r in rows if r['code']=='399006.SZ'][0]['above_ma60']}")
    idx300 = [r for r in rows if r["code"] == "000300.SH"][0]
    print(f"[市场环境] 000300.SH 收盘>MA60: {idx300['above_ma60']}")


if __name__ == "__main__":
    sys.exit(main())
