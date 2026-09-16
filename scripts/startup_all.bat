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
if exist "%GUARD_PID%" (
  set /p PID=<"%GUARD_PID%"
  if defined PID (
    tasklist /FI "PID eq %PID%" 2>nul | findstr /C:"%PID%" >nul
    if not errorlevel 1 (
      echo [%TIME%] 守护 PID %PID% 已在运行，本次跳过氧化 >> "%LOG%"
      set "SKIP_GUARDIAN=1"
    )
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
REM    守护自身有单实例守卫(.guardian.pid)，即使竞态也不会堆叠。
if "%SKIP_GUARDIAN%"=="1" (
  echo [%TIME%] 守护已在运行，跳过启动（幂等） >> "%LOG%"
) else if exist "%GUARDIAN%" (
  if exist "%PYW%" (
    echo [%TIME%] 启动守护进程(pythonw,无控制台) >> "%LOG%"
    start "" "%PYW%" "%GUARDIAN%"
  ) else (
    echo [%TIME%] 启动守护进程(python) >> "%LOG%"
    start "" "%PY%" "%GUARDIAN%"
  )
) else (
  echo [%TIME%] 未找到 guardian.py: %GUARDIAN% >> "%LOG%"
)

REM 3) WorkBuddy 桌面应用（已在运行则跳过）
tasklist /FI "IMAGENAME eq WorkBuddy.exe" 2>nul | findstr /I "WorkBuddy.exe" >nul
if not errorlevel 1 (
  echo [%TIME%] WorkBuddy 已在运行，跳过 >> "%LOG%"
) else if exist "%WORKBUDDY%" (
  echo [%TIME%] 启动 WorkBuddy >> "%LOG%"
  start "" "%WORKBUDDY%"
) else (
  echo [%TIME%] 未找到 WorkBuddy: %WORKBUDDY% >> "%LOG%"
)

echo [%TIME%] qmtIDE 自启编排结束 >> "%LOG%"
endlocal
