@echo off
REM ============================================================
REM qmtIDE-deepseek 开机自启编排
REM 启动：miniQMT 交易客户端 + 守护进程(自动拉起交易引擎) + WorkBuddy
REM 触发源：① HKCU Run 键（scripts/setup_autostart.py 注册）
REM         ② Startup 文件夹（qmtIDE-startup.bat 调用本脚本）
REM 幂等守卫：守护/进程已在运行则跳过对应步骤，避免双触发重复拉起。
REM 守护进程(scripts/guardian.py)持续 5 分钟轮询，
REM 确保 miniQMT 与交易引擎(127.0.0.1:5000) 停止即自启动。
REM ------------------------------------------------------------
REM 【2026-10-29】本次两处改动：
REM   a) 恢复 miniQMT 开机自动拉起。2026-09-15 曾因「自动拉起与券商断线重连乒乓」
REM      改为手动启动；因近期频繁非正常重启、每次开机都要人工补启动，改回自动。
REM      若断线乒乓复现，把 MQ_AUTOSTART 置 0 即可退回手动模式。
REM   b) 新增单实例守卫。logs/startup.log 证实每次开机两个触发源各跑一轮
REM      （15:09:20 与 15:09:32），守护进程被重复拉起；加入 miniQMT 自动拉起后
REM      双实例会产生两个 miniQMT 客户端，因此必须先串行化。
REM ============================================================
setlocal
set "PYW=C:\Users\lisinan\.conda\envs\qmt\pythonw.exe"
set "PY=C:\Users\lisinan\.conda\envs\qmt\python.exe"
set "PROJ=C:\Users\lisinan\Desktop\qmtIDE-deepseek"
set "MINIQMT_DIR=C:\pazq_qmt\bin.x64"
set "MINIQMT=%MINIQMT_DIR%\XtItClient.exe"
set "GUARDIAN=%PROJ%\scripts\guardian.py"
set "WORKBUDDY=C:\Users\lisinan\AppData\Local\Programs\WorkBuddy\WorkBuddy.exe"
set "LOG=%PROJ%\logs\startup.log"
set "GUARD_PID=%PROJ%\.guardian.pid"
set "LOCK=%PROJ%\.startup.lock"
set "MQ_AUTOSTART=1"

REM ===== 单实例守卫 =====
REM HKCU Run 键与 Startup 文件夹两个入口在登录时几乎同时触发，必须串行化，
REM 否则 miniQMT 会被拉起两次。用带时间戳的锁文件：
REM   锁龄 < 10 分钟 => 视为另一实例正在执行，本实例退出；
REM   锁龄 >= 10 分钟 => 视为上次异常残留（例如启动途中掉电），直接接管。
REM 失败即放行：若 PowerShell 无法启动（errorlevel>=2），走 :LOCK_OK 继续，
REM 不让守卫本身变成自启动的单点故障。
powershell -NoProfile -ExecutionPolicy Bypass -Command "try{ if((Test-Path '%LOCK%') -and ((Get-Date)-(Get-Item '%LOCK%').LastWriteTime).TotalMinutes -lt 10){ exit 1 } else { Set-Content -LiteralPath '%LOCK%' -Value (Get-Date).ToString('o') -Encoding ASCII; exit 0 } }catch{ exit 0 }" >nul 2>&1
if errorlevel 2 goto :LOCK_OK
if errorlevel 1 goto :SKIP_DUPLICATE
:LOCK_OK

echo [%TIME%] qmtIDE 自启编排开始 >> "%LOG%"

REM ===== 幂等守卫：探测守护是否已在运行（用于决定是否重复启动守护）=====
set "SKIP_GUARDIAN=0"
REM 先把 pid 读进变量（for /f 在括号块内可用，set /p 在块内需延迟展开），
REM 判定放到块外单个 if defined 里，避免多层括号嵌套导致的解析歧义。
set "GUARD_PID_VAL="
if exist "%GUARD_PID%" for /f "usebackq delims=" %%p in ("%GUARD_PID%") do set "GUARD_PID_VAL=%%p"
if defined GUARD_PID_VAL (
  tasklist /FI "PID eq %GUARD_PID_VAL%" 2>nul | findstr /C:"%GUARD_PID_VAL%" >nul
  if not errorlevel 1 (
    echo [%TIME%] 守护 PID %GUARD_PID_VAL% 已在运行，本次跳过重复启动 >> "%LOG%"
    set "SKIP_GUARDIAN=1"
  )
)

REM 启动缓冲：等桌面/网络就绪，避免开机初期资源争抢
timeout /t 15 /nobreak >nul
echo [%TIME%] 系统就绪，开始拉起应用 >> "%LOG%"

REM ============================================================
REM 1) miniQMT 交易客户端
REM    XtItClient.exe 只是「拉起器」，真正常驻的是 XtMiniQmt.exe；拉起器启动后即退出，
REM    因此存活判定必须以 XtMiniQmt.exe 为准（2026-09-15 确诊，见 health_check.py）。
REM    另：必须以 bin.x64 为工作目录启动，否则客户端找不到 Qt 依赖与自身配置。
REM    本段全部用 goto 线性结构，不用嵌套 if/else（2026-10-24 教训：嵌套块会双分支执行）。
REM ============================================================
if "%MQ_AUTOSTART%"=="0" goto :MQ_DISABLED

tasklist /FI "IMAGENAME eq XtMiniQmt.exe" 2>nul | findstr /I "XtMiniQmt.exe" >nul
if not errorlevel 1 goto :MQ_RUNNING

tasklist /FI "IMAGENAME eq XtItClient.exe" 2>nul | findstr /I "XtItClient.exe" >nul
if not errorlevel 1 goto :MQ_LAUNCHING

if not exist "%MINIQMT%" goto :MQ_MISSING
echo [%TIME%] 拉起 miniQMT: %MINIQMT%  (CWD=%MINIQMT_DIR%) >> "%LOG%"
start "" /D "%MINIQMT_DIR%" "%MINIQMT%"
goto :MQ_WAIT

:MQ_LAUNCHING
echo [%TIME%] miniQMT 拉起器已在运行，等待引擎就绪 >> "%LOG%"
goto :MQ_WAIT

:MQ_RUNNING
echo [%TIME%] 检测到 miniQMT 引擎已运行，跳过拉起 >> "%LOG%"
goto :AFTER_MQ

:MQ_MISSING
echo [%TIME%] [ERR] 未找到 miniQMT: %MINIQMT% >> "%LOG%"
goto :AFTER_MQ

:MQ_DISABLED
echo [%TIME%] miniQMT 自动拉起已关闭 (MQ_AUTOSTART=0)，请手动启动 >> "%LOG%"
goto :AFTER_MQ

:MQ_WAIT
REM 给引擎 25s 初始化（含 xtquant 数据服务与行情连接）。
REM 超时不等于失败：若已配置自动登录，登录完成后引擎会自行常驻；
REM 若未勾「记住密码/自动登录」，此处只会停在登录界面，需人工输入。
timeout /t 25 /nobreak >nul
tasklist /FI "IMAGENAME eq XtMiniQmt.exe" 2>nul | findstr /I "XtMiniQmt.exe" >nul
if errorlevel 1 goto :MQ_NOT_READY
echo [%TIME%] miniQMT 引擎已就绪 (XtMiniQmt.exe) >> "%LOG%"
goto :AFTER_MQ

:MQ_NOT_READY
echo [%TIME%] [WARN] miniQMT 引擎 25s 内未就绪：可能停在登录界面（未勾自动登录）或拉起失败，需人工确认 >> "%LOG%"
:AFTER_MQ

REM 等 miniQMT 服务端口稳定一点，再起守护（守护会再确认并拉起交易引擎）
timeout /t 5 /nobreak >nul

REM ============================================================
REM 2) 守护进程（5 分钟轮询，自动拉起交易引擎 127.0.0.1:5000）
REM    【2026-10-24】原实现用「if ... ) else if exist ... ( 嵌套 if/else )」，
REM    在 cmd 里实测会**两个分支都执行**（2026-10-24 09:09 开机日志：「启动守护进程」紧跟
REM    「未找到 guardian.py」）。改用标签 + goto 的线性结构，彻底消除嵌套括号解析歧义。
REM    守护自身仍有 .guardian.pid 原子单实例守卫，即使竞态也不会堆叠。
REM    注：guardian 侧的 miniQMT 二次拉起由 health_check.MINIQMT_AUTOSTART 控制，
REM    当前为 False，因此 miniQMT 若在开机后中途退出不会被守护补起；需要时再打开。
REM ============================================================
if "%SKIP_GUARDIAN%"=="1" goto :GUARD_RUNNING
if not exist "%GUARDIAN%" goto :GUARD_MISSING
if not exist "%PYW%" goto :GUARD_PY
echo [%TIME%] 启动守护进程 [pythonw] >> "%LOG%"
start "" "%PYW%" "%GUARDIAN%"
goto :AFTER_GUARD
:GUARD_PY
echo [%TIME%] 启动守护进程 [python] >> "%LOG%"
start "" "%PY%" "%GUARDIAN%"
goto :AFTER_GUARD
:GUARD_RUNNING
echo [%TIME%] 守护已在运行，跳过启动（幂等） >> "%LOG%"
goto :AFTER_GUARD
:GUARD_MISSING
echo [%TIME%] 未找到 guardian.py: %GUARDIAN% >> "%LOG%"
:AFTER_GUARD

REM 3) WorkBuddy 桌面应用（已在运行则跳过）—— 同样改成 goto 结构
tasklist /FI "IMAGENAME eq WorkBuddy.exe" 2>nul | findstr /I "WorkBuddy.exe" >nul
if not errorlevel 1 goto :WB_RUNNING
if not exist "%WORKBUDDY%" goto :WB_MISSING
echo [%TIME%] 启动 WorkBuddy >> "%LOG%"
start "" "%WORKBUDDY%"
goto :AFTER_WB
:WB_RUNNING
echo [%TIME%] WorkBuddy 已在运行，跳过 >> "%LOG%"
goto :AFTER_WB
:WB_MISSING
echo [%TIME%] 未找到 WorkBuddy: %WORKBUDDY% >> "%LOG%"
:AFTER_WB

echo [%TIME%] qmtIDE 自启编排结束 >> "%LOG%"
del "%LOCK%" 2>nul
endlocal
exit /b 0

REM ===== 第二个实例走到这里：不碰锁（锁属于正在执行的那个实例），直接退出 =====
:SKIP_DUPLICATE
echo [%TIME%] 已有启动流程在执行，本实例退出（单实例守卫） >> "%LOG%"
endlocal
exit /b 0
