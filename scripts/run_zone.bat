@echo off
REM ============================================================================
REM 观察区(paper, 127.0.0.1:5001) + 统一展板(127.0.0.1:5002) 启动器
REM 建立：2026-11-09 15:25
REM
REM 为什么需要这个文件：
REM   这两者此前只能手工起，且**不在任何自启链路里**。2026-11-09 10:37:35
REM   两者在同一秒双双消失（无 traceback、无 STOP_ENGINE 哨兵 ⇒ 外部终止），
REM   之后 5001/5002 一直没人管。本文件把它们收进同一条启动链，
REM   由 run_live.bat 与 startup_all.bat 调用。
REM
REM 幂等：按**端口监听**判定，已在跑就跳过（不 kill、不影响 live）。
REM 安全：paper 走 core/broker.py 的 _NoTradeBroker（真实下单硬阻断，
REM       连 xtquant.xttrader 都不 import）；展板纯只读、不 import 项目模块。
REM
REM 停止：
REM   paper : C:\Users\lisinan\.conda\envs\qmt\python.exe main.py --zone paper --stop
REM   展板  : 关掉标题为 qmt-dash 的窗口，或 taskkill /FI "WINDOWTITLE eq qmt-dash"
REM ============================================================================
setlocal
set "PROJ=C:\Users\lisinan\Desktop\qmtIDE-deepseek"
set "PY=C:\Users\lisinan\.conda\envs\qmt\python.exe"
set "LOGZ=%PROJ%\logs\zone_launch.log"
REM sqlite3 的 DLL 在 conda 的 Library\bin 下，缺了会 ImportError: _sqlite3
set "PATH=C:\Users\lisinan\.conda\envs\qmt\Library\bin;C:\Users\lisinan\.conda\envs\qmt\DLLs;%PATH%"
set "PYTHONPATH=%PROJ%"
if not exist "%PROJ%\logs" mkdir "%PROJ%\logs"
echo [%TIME%] run_zone 开始 >> "%LOGZ%"

REM ================= 1) 观察区 paper : 5001 =================
call :PORT_UP 5001
if not errorlevel 1 goto :PAPER_UP
if not exist "%PROJ%\scripts\run_paper.bat" goto :PAPER_MISSING
echo [%TIME%] 启动 paper 区 (5001) >> "%LOGZ%"
start "qmt-paper" /min cmd /c ""%PROJ%\scripts\run_paper.bat""
call :WAIT_PORT 5001 60 PAPER
goto :PAPER_DONE

:PAPER_UP
echo [%TIME%] paper 区已在监听 5001，跳过 >> "%LOGZ%"
goto :PAPER_DONE

:PAPER_MISSING
echo [%TIME%] [ERR] 未找到 run_paper.bat >> "%LOGZ%"

:PAPER_DONE

REM ================= 2) 展板 dashboard : 5002 =================
call :PORT_UP 5002
if not errorlevel 1 goto :DASH_UP
if not exist "%PROJ%\scripts\zone_dashboard.py" goto :DASH_MISSING
echo [%TIME%] 启动展板 (5002) >> "%LOGZ%"
start "qmt-dash" /min cmd /c ""%PY%" -u "%PROJ%\scripts\zone_dashboard.py" 1>>"%PROJ%\logs_paper\dashboard_out.log" 2>&1"
call :WAIT_PORT 5002 30 DASH
goto :DASH_DONE

:DASH_UP
echo [%TIME%] 展板已在监听 5002，跳过 >> "%LOGZ%"
goto :DASH_DONE

:DASH_MISSING
echo [%TIME%] [ERR] 未找到 zone_dashboard.py >> "%LOGZ%"

:DASH_DONE

echo [%TIME%] run_zone 结束 >> "%LOGZ%"
endlocal
exit /b 0

REM ============================================================================
REM 子过程
REM ============================================================================

REM :PORT_UP <port>  —— 端口在 LISTENING 则 errorlevel=0（已就绪）
:PORT_UP
netstat -an | findstr /R /C:":%~1 " | findstr /C:"LISTENING" >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0

REM :WAIT_PORT <port> <秒> <标签>  —— 轮询到端口就绪，超时只记日志不失败
:WAIT_PORT
set "WP_PORT=%~1"
set "WP_LEFT=%~2"
set "WP_TAG=%~3"
:WAIT_LOOP
call :PORT_UP %WP_PORT%
if not errorlevel 1 goto :WAIT_OK
set /a WP_LEFT-=1
if %WP_LEFT% LEQ 0 goto :WAIT_TIMEOUT
"%SystemRoot%\System32\timeout.exe" /t 1 /nobreak >nul
goto :WAIT_LOOP
:WAIT_OK
echo [%TIME%] %WP_TAG% 就绪 (端口 %WP_PORT%) >> "%LOGZ%"
exit /b 0
:WAIT_TIMEOUT
echo [%TIME%] [WARN] %WP_TAG% 未在期限内监听 %WP_PORT%，见各自日志 >> "%LOGZ%"
exit /b 1
