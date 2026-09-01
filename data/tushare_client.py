# -*- coding: utf-8 -*-
"""
Tushare 基本面数据客户端

功能：
- get_stock_basic()       股票基础信息（行业、市值等）
- get_daily_basic(code)   单只股票最新日线基本面（PE/PB/换手率/市值）
- get_indicator(code)     财务指标（ROE/毛利率/净利率）
- get_industry(code)       所属行业
- passes_filter(code)     是否通过 TUSHARE_FUNDAMENTAL_FILTER 配置的过滤

缓存：TUSHARE_CACHE_TTL 秒（默认1 小时），减少 API 调用次数。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from config.settings import (
    TUSHARE_CACHE_TTL, TUSHARE_FUNDAMENTAL_FILTER, TUSHARE_TIMEOUT, TUSHARE_TOKEN,
)

logger = logging.getLogger(__name__)


def _to_ts_code(code: str) -> str:
    """qmt 代码 (300308.SZ) → tushare 代码 (300308.SZ) （同格式）。"""
    return code


def _normalize_basic(df_row: dict) -> Dict[str, Any]:
    """从 daily_basic DataFrame 行提取关键指标。"""
    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except Exception:
            return None
    return {
        "ts_code": df_row.get("ts_code"),
        "trade_date": df_row.get("trade_date"),
        "close": _f(df_row.get("close")),
        "pe": _f(df_row.get("pe")),            # PE_TTM
        "pe_ttm": _f(df_row.get("pe_ttm")),
        "pb": _f(df_row.get("pb")),
        "ps": _f(df_row.get("ps")),
        "ps_ttm": _f(df_row.get("ps_ttm")),
        "dv_ratio": _f(df_row.get("dv_ratio")),
        "dv_ttm": _f(df_row.get("dv_ttm")),
        "total_share": _f(df_row.get("total_share")),
        "float_share": _f(df_row.get("float_share")),
        "free_share": _f(df_row.get("free_share")),
        "total_mv": _f(df_row.get("total_mv")),    # 万元
        "circ_mv": _f(df_row.get("circ_mv")),
        "turnover_rate": _f(df_row.get("turnover_rate")),
        "turnover_rate_f": _f(df_row.get("turnover_rate_f")),
        "volume_ratio": _f(df_row.get("volume_ratio")),
        "pe_industry": _f(df_row.get("pe_industry")),
    }


class TushareClient:
    def __init__(self, token: str = None):
        self.token = (token if token is not None else TUSHARE_TOKEN).strip()
        self._pro = None
        self._lock = threading.RLock()
        # 缓存：(key) -> (data, ts)
        self._cache: Dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    # ---------- 内部 ----------

    def _connect(self) -> bool:
        if self._pro is not None:
            return True
        with self._lock:
            if self._pro is not None:
                return True
            if not self.enabled:
                return False
            try:
                import tushare as ts
                ts.set_token(self.token)
                self._pro = ts.pro_api(timeout=TUSHARE_TIMEOUT)
                logger.info("Tushare 已连接")
                return True
            except Exception as e:
                logger.warning("Tushare 连接失败: %s", e)
                self._pro = None
                return False

    def _cache_get(self, key: str):
        v = self._cache.get(key)
        if v is None:
            return None
        data, ts_val = v
        if time.time() - ts_val > TUSHARE_CACHE_TTL:
            self._cache.pop(key, None)
            return None
        return data

    def _cache_set(self, key: str, data: Any) -> None:
        self._cache[key] = (data, time.time())

    # ---------- 公开 API ----------

    def test_connection(self) -> bool:
        """验证 token（用 stock_basic 拉一只已知股票）。"""
        if not self._connect():
            return False
        try:
            df = self._pro.stock_basic(ts_code="000001.SZ",
                                        fields="ts_code,name")
            return df is not None and not df.empty
        except Exception as e:
            logger.warning("Tushare test_connection: %s", e)
            return False

    def get_stock_basic(self, ts_code: str = None,
                        list_status: str = "L") -> Optional[Any]:
        """股票基础信息（行业/上市日期/全称等）。"""
        if not self._connect():
            return None
        key = f"stock_basic:{ts_code}:{list_status}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            fields = "ts_code,name,industry,fullname,list_date,market,exchange"
            if ts_code:
                df = self._pro.stock_basic(ts_code=ts_code, list_status=list_status,
                                            fields=fields)
                out = df.iloc[0].to_dict() if df is not None and not df.empty else None
            else:
                # 全市场（A 股），慎用：~5000 行
                df = self._pro.stock_basic(list_status=list_status, fields=fields)
                out = df
            self._cache_set(key, out)
            return out
        except Exception as e:
            logger.debug("stock_basic 失败: %s", e)
            return None

    def get_industry_stocks(self, industries: List[str]) -> Optional[Any]:
        """按行业列表过滤股票。返回 DataFrame（ts_code/name/industry/market/list_date）。

        取代原 stock_basic 全市场调用，供 DynamicUniverse 使用。
        """
        if not self._connect():
            return None
        if not industries:
            return None
        key = "industry_stocks:" + ",".join(sorted(industries))
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            df = self._pro.stock_basic(
                list_status="L",
                fields="ts_code,name,industry,fullname,list_date,market,exchange",
            )
            if df is None or df.empty:
                return None
            mask = df["industry"].isin(industries)
            result = df[mask].copy()
            self._cache_set(key, result)
            return result
        except Exception as e:
            logger.warning("get_industry_stocks 失败: %s", e)
            return None

    def get_industry(self, ts_code: str) -> Optional[str]:
        info = self.get_stock_basic(ts_code=ts_code)
        if isinstance(info, dict):
            return info.get("industry")
        return None

    def get_daily_basic(self, ts_code: str) -> Optional[Dict[str, Any]]:
        """单只股票最新一日的基本面（PE/PB/市值/换手率）。"""
        if not self._connect():
            return None
        key = f"daily_basic:{ts_code}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            # 取最近 5 个交易日，取最新一行
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")
            df = self._pro.daily_basic(
                ts_code=_to_ts_code(ts_code),
                start_date=start, end_date=end,
                fields="ts_code,trade_date,close,pe,pe_ttm,pb,ps,ps_ttm,"
                        "dv_ratio,dv_ttm,total_share,float_share,free_share,"
                        "total_mv,circ_mv,turnover_rate,turnover_rate_f,"
                        "volume_ratio,pe_industry",
            )
            if df is None or df.empty:
                return None
            row = df.iloc[0].to_dict()
            out = _normalize_basic(row)
            self._cache_set(key, out)
            return out
        except Exception as e:
            logger.debug("daily_basic(%s) 失败: %s", ts_code, e)
            return None

    def get_indicator(self, ts_code: str,
                      period: str = None) -> Optional[Dict[str, Any]]:
        """财务指标（ROE/毛利率/净利率/资产负债率/营收同比/利润同比）。

        period 例 '20240630'（半年报）/ '20240930'（三季报）
        None 表示最新一期（自动用最近季末日期）
        """
        if not self._connect():
            return None
        key = f"indicator:{ts_code}:{period}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            kwargs = {"ts_code": _to_ts_code(ts_code),
                      "fields": "ts_code,end_date,roe,roe_waa,roe_dt,"
                                "gross_margin,net_margin,debt_to_assets,"
                                "or_yoy,op_yoy,dt_yoy,tr_yoy"}
            if period:
                kwargs["period"] = period
            df = self._pro.fina_indicator(**kwargs)
            if df is None or df.empty:
                return None
            out = df.iloc[0].to_dict()
            self._cache_set(key, out)
            return out
        except Exception as e:
            logger.debug("fina_indicator(%s) 失败: %s", ts_code, e)
            return None

    # ---------- 组合过滤 ----------

    def moneyflow_df(self, ts_code: str, start: str, end: str):
        """单只股票日线主力资金流（net_mf_amount 等）。返回 DataFrame 或 None。

        字段：trade_date, buy_sm/md/lg/elg_vol, sell_sm/md/lg/elg_vol,
              net_mf_vol, net_mf_amount。供「主力资金」alpha 特征使用。
        """
        if not self._connect():
            return None
        try:
            df = self._pro.moneyflow(
                ts_code=_to_ts_code(ts_code),
                start_date=start, end_date=end,
                fields="trade_date,buy_sm_vol,buy_md_vol,buy_lg_vol,buy_elg_vol,"
                        "sell_sm_vol,sell_md_vol,sell_lg_vol,sell_elg_vol,"
                        "net_mf_vol,net_mf_amount",
            )
            return df if (df is not None and not df.empty) else None
        except Exception as e:
            logger.warning("moneyflow(%s) 失败: %s", ts_code, e)
            return None

    def forecast_df(self, ts_code: str, start: str, end: str):
        """单只股票业绩预告（净利润同比变动区间）。返回 DataFrame 或 None。

        字段：ann_date, end_date, type, p_change_min/max, net_profit_min/max。
        供「盈利修正(上修)」alpha 特征使用（与价格动量正交的基本面数据轴）。
        """
        if not self._connect():
            return None
        try:
            df = self._pro.forecast(
                ts_code=_to_ts_code(ts_code),
                start_date=start, end_date=end,
                fields="ann_date,end_date,type,p_change_min,p_change_max,"
                       "net_profit_min,net_profit_max",
            )
            return df if (df is not None and not df.empty) else None
        except Exception as e:
            logger.warning("forecast(%s) 失败: %s", ts_code, e)
            return None

    def passes_filter(self, ts_code: str) -> bool:
        """检查 ts_code 是否通过 TUSHARE_FUNDAMENTAL_FILTER 配置的过滤。

        拿不到数据时返回 True（保守放过，让其他规则挡住）。
        """
        if not self.enabled:
            return True
        f = TUSHARE_FUNDAMENTAL_FILTER
        info = self.get_daily_basic(ts_code)
        if info is None:
            return True

        def _within(v, lo, hi):
            if v is None:
                return True
            return lo <= v <= hi

        pe = info.get("pe_ttm") or info.get("pe")
        pb = info.get("pb")
        mv = info.get("total_mv")   # 万元
        mv_yi = mv / 1e4 if mv else None   # 亿元

        ok = (
            _within(pe, f["min_pe"], f["max_pe"])
            and _within(pb, 0, f["max_pb"])
            and _within(mv_yi, f["min_total_mv"], 1e6)
        )
        if not ok:
            logger.info("[fund] %s 不通过: PE=%s PB=%s MV=%.1f亿",
                        ts_code, pe, pb, mv_yi or 0)
        # ROE 单独拉（不是 daily_basic）
        roe_info = self.get_indicator(ts_code) or {}
        try:
            roe = float(roe_info.get("roe") or 0)
        except Exception:
            roe = 0
        if roe < f["min_roe"]:
            logger.info("[fund] %s 不通过: ROE=%.2f", ts_code, roe)
            ok = False
        return ok

    def summary(self, ts_code: str) -> Optional[Dict[str, Any]]:
        """汇总：基本信息 + 日线基本面 + 财务指标。供 LLM/前端使用。"""
        info = self.get_stock_basic(ts_code=ts_code) or {}
        basic = self.get_daily_basic(ts_code) or {}
        ind = self.get_indicator(ts_code) or {}
        if not info and not basic and not ind:
            return None
        return {
            "ts_code": ts_code,
            "name": info.get("name") if isinstance(info, dict) else None,
            "industry": info.get("industry") if isinstance(info, dict) else None,
            "close": basic.get("close"),
            "pe": basic.get("pe") or basic.get("pe_ttm"),
            "pb": basic.get("pb"),
            "total_mv_yi": (basic.get("total_mv") or 0) / 1e4 if basic else None,
            "turnover_rate": basic.get("turnover_rate"),
            "roe": ind.get("roe"),
            "gross_margin": ind.get("gross_margin"),
            "net_margin": ind.get("net_margin"),
            "debt_to_assets": ind.get("debt_to_assets"),
            "or_yoy": ind.get("or_yoy"),
            "op_yoy": ind.get("op_yoy"),
            "as_of": datetime.now().isoformat(),
        }


# 模块级单例
tushare_client = TushareClient()