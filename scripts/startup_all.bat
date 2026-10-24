@echo off
REM ============================================================
REM qmtIDE-deepseek 开机自启编排
REM 启动：miniQMT 交易客户端 + 守护进程(自动拉起交易引擎) + WorkBuddy
REM 触发源：① HKCU Run 键（scripts/setup_autostart.py 注册）
REM         ② Startup 文件夹（qmtIDE-startup.bat 调用本脚本）
REM 幂等守卫：守护/进程已在运行则跳过对应步骤，避免双触发重复拉起。
REM 守护进程(scripts/guardian.py)持续 5 分钟轮询，
REM 确保 miniQMT 与交易引擎(127.0.0.1:5000) 停止即自启动。
REM ============================================================
setlocal
set "PYW=C:\Users\lisinan\.conda\envs\qmt\pythonw.exe"
set "PY=C:\Users\lisinan\.conda\envs\qmt\python.exe"
set "PROJ=C:\Users\lisinan\Desktop\qmtIDE-deepseek"
set "MINIQMT=C:\pazq_qmt\bin.x64\XtItClient.exe"
set "GUARDIAN=%PROJ%\scripts\guardian.py"
set "WORKBUDDY=C:\Users\lisinan\AppData\Local\Programs\WorkBuddy\WorkBuddy.exe"
set "LOG=%PROJ%\logs\startup.log"
set "GUARD_PID=%PROJ%\.guardian.pid"

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

REM 1) miniQMT 交易客户端 —— 按用户设置(2026-09-15)已取消开机自动拉起，改为手动启动。
REM    此处仅检测状态并记录，不再自动 start。请手动启动 XtItClient.exe 并完成登录
REM    （勾选记住密码/自动登录）。引擎(5000)与 WorkBuddy 仍由下方逻辑看管。
tasklist /FI "IMAGENAME eq XtMiniQmt.exe" 2>nul | findstr /I "XtMiniQmt.exe" >nul
if not errorlevel 1 (
  echo [%TIME%] 检测到 miniQMT 引擎已运行 >> "%LOG%"
) else (
  echo [%TIME%] miniQMT 未运行（自动拉起已禁用，请手动启动 XtItClient.exe） >> "%LOG%"
)

REM 等 miniQMT 先起来一点，再起守护（守护会再确认并拉起交易引擎）
timeout /t 5 /nobreak >nul

REM 2) 守护进程（5 分钟轮询，自动拉起交易引擎 127.0.0.1:5000；miniQMT 不自动拉起）
REM    【2026-10-24】原实现用「if ... ) else if exist ... ( 嵌套 if/else )」，
REM    在 cmd 里实测会**两个分支都执行**（2026-10-24 09:09 开机日志：「启动守护进程」紧跟
REM    「未找到 guardian.py」）。改用标签 + goto 的线性结构，彻底消除嵌套括号解析歧义。
REM    守护自身仍有 .guardian.pid 原子单实例守卫，即使竞态也不会堆叠。
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
endlocal
