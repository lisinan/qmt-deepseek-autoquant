# 项目迭代优化待办（OPTIMIZATION BACKLOG）

> 由「盘后复盘 + 迭代驱动」自动化（交易日 15:35）每日追加生成。**仅追加，永不改写历史**。
> 目的：从当日真实运行记录中发现本项目的优化内容与方向，持续迭代「稳定 / 安全 / 准确 / 高效」四大维度，最终获得优秀收益。
> 每条格式：`- [优先级/轴/级别] 标题 ｜ 证据：… ｜ 建议：… ｜ 定位：…`
> 跨日累积后便于复盘长期主题（反复出现的券商断线、滑点、AI 全中性等），是项目持续演进的路线图。

---

## 2026-09-02

稳定 70 / 安全 100 / 准确 100 / 高效 100 ｜ 综合 92

- [P1/稳定/warn] 券商连接断开/重连 33 次 ｜ 证据：notices 含 disconnected/重连关键词 33 次 ｜ 建议：复查断线自修复是否真正复用单一 XtQuantTrader 实例，避免 force 重连自伤 ｜ 定位：core/broker.py / core/auto_reconnect.py
- [P2/安全/info] 仍出现 7 次每日风险预算 WARNING ｜ 证据：notices 含「风险预算/max_single_position_pct」关键词 7 次 ｜ 建议：确认 settings.max_single_position_pct=0.19 已与现金夹紧自洽；若持续，查波动率目标仓位对低波大票的裁剪是否过激 ｜ 定位：config/settings.py
- [P2/准确/info] AI 当日仍全中性（无方向性增量） ｜ 证据：AI 立场分布：{'neutral': 5} ｜ 建议：AI 为观察层：bullish 不影响交易、bearish+高置信可抑制买入；全中性说明技术面混杂或模型保守，属预期，不作为收益拖累 ｜ 定位：ai/analyst.py（已放宽中性偏置）
