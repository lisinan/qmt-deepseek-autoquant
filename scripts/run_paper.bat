@echo off
REM ============================================================================
REM paper 区启动器（2026-11-08 建立）—— AI 产业链热点/行业观察台
REM
REM 定位（OWNER 2026-11-08 定）：起始资金 100 万，只看「信号层/逻辑层」跑得对不对，
REM   以及哪条 AI 细分链在走强、哪些标的在出信号。它不受 live 的「一手 100 股现金
REM   门槛」约束（live 5.8 万下 41 只候选仅 13 只买得起一手），所以观察面更完整。
REM   ⚠ 代价：绝对收益、持仓数量、暴露比例都**不可**与 live 横比，只能看比率形状。
REM
REM 为什么需要独立启动器：
REM   1) zone 的差异（独立 DB / 日志 / 端口 / 交易通道）由 core/zone.py 推导，
REM      但 zone 身份必须在 import engine 之前确定 ⇒ 只能靠命令行 + 环境变量表达；
REM   2) 把这些写死在一个文件里，杜绝「手工切换漏了一项」。
REM
REM 真实下单为什么不可能发生（三层，互相独立）：
REM   1) core/broker.py::_make_broker() 见到 QMT_ZONE=paper 直接返回 _NoTradeBroker，
REM      连 xtquant.xttrader 都不 import；该函数在模块导入期执行一次，无法中途翻案；
REM   2) main.py 在 zone=paper 下拒绝 --mode live / --init-positions，并强制 mode=paper；
REM   3) exec_mode=paper 时 event_engine 的 qmt_broker.place_order 两个调用点
REM      （:3532 BUY / :3733 SELL）本来就不执行。
REM
REM 停止（不杀进程、不影响 live）：
REM   C:\Users\lisinan\.conda\envs\qmt\python.exe main.py --zone paper --stop
REM   哨兵落在 logs_paper\STOP_ENGINE，pid 读 logs_paper\engine.pid，与 live 完全分离。
REM
REM 用法：
REM   cmd /c C:\Users\lisinan\Desktop\qmtIDE-deepseek\scripts\run_paper.bat
REM ============================================================================
setlocal
set "PROJ=C:\Users\lisinan\Desktop\qmtIDE-deepseek"
set "PY=C:\Users\lisinan\.conda\envs\qmt\python.exe"
if not exist "%PY%" (
    echo [ERR] 找不到 qmt 解释器: %PY%
    endlocal & exit /b 2
)
cd /d "%PROJ%"
set "PYTHONPATH=%PROJ%"
set "PATH=C:\Users\lisinan\.conda\envs\qmt\Library\bin;C:\Users\lisinan\.conda\envs\qmt\DLLs;%PATH%"

REM ---- 1) zone 身份（唯一事实源；main.py 会与 --zone 交叉校验）----
set "QMT_ZONE=paper"

REM ---- 2) 行情：真实源优先（xtdata → tushare），与 live 同一条链，只读 ----
REM     不设为 mock：观察台必须在真实价上评估，合成价（可差 1~6 倍）产生的归因是废数据。
REM     两个实例同时订阅 xtdata 若冲突，会自动退到 tushare（仍属真实行情）。
set "QMT_QUOTE_SOURCE=auto"

REM ---- 3) 其余（DB / 日志 / notices / 端口 / 桥接投信桶）全部由 core/zone.py 推导 ----
REM     刻意不在这里重复写 QMT_DB / QMT_NOTICES_LOG：单一事实源，避免两处漂移。

REM ---- 4) 重定向目录先建，否则 bat 的重定向会在 python 启动前就失败 ----
if not exist "%PROJ%\logs_paper" mkdir "%PROJ%\logs_paper"

echo [paper] zone=paper  起始资金 1000000  port=5001  真实下单已硬阻断
echo [paper] 账本 storage_paper\qmt.db   日志 logs_paper\
echo [paper] 停止：%PY% main.py --zone paper --stop
"%PY%" -u main.py --zone paper --mode paper --web >> "%PROJ%\logs_paper\engine_paper_out.log" 2>&1
endlocal
