@echo off
REM ============================================================
REM live 模式启动器（2026-10-29 建立）
REM
REM 为什么要这个文件：直接用 Start-Process / Popen 拉起会被调用方
REM   的进程树回收（实测引擎完整启动到 "Running on 127.0.0.1:5000"
REM   后随父 shell 一起消失，日志无任何异常）。本脚本由 WMI
REM   Win32_Process.Create 调用，父进程是 WMI 服务，不受调用方生命周期影响。
REM
REM 用法（管理员无关，普通用户即可）：
REM   powershell -NoProfile -Command "Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c C:\Users\lisinan\Desktop\qmtIDE-deepseek\scripts\run_live.bat'}"
REM
REM 停止：python main.py --stop（协作式，不 kill）
REM ============================================================
setlocal
set "PROJ=C:\Users\lisinan\Desktop\qmtIDE-deepseek"
set "PY=C:\Users\lisinan\.conda\envs\qmt\python.exe"
cd /d "%PROJ%"
set "PYTHONPATH=%PROJ%"
set "PATH=C:\Users\lisinan\.conda\envs\qmt\Library\bin;C:\Users\lisinan\.conda\envs\qmt\DLLs;%PATH%"

REM ---- 观察区(5001) + 展板(5002)：默认随 live 一起起 ----
REM 2026-11-09 新增。此前这两者不在任何启动链路里，只能手工起，
REM 会话一断就消失（当日 10:37:35 两者同秒双亡）。幂等由 run_zone.bat
REM 的端口判定负责，已在跑则跳过；不 kill 任何进程、不影响 live。
REM 需要「只起 live、不要观察区」时： set QMT_WITH_ZONE=0 后再运行本脚本。
if "%QMT_WITH_ZONE%"=="0" goto :SKIP_ZONE
if not exist "%PROJ%\scripts\run_zone.bat" goto :SKIP_ZONE
call "%PROJ%\scripts\run_zone.bat"
:SKIP_ZONE

"%PY%" -u main.py --web --mode live --init-positions >> "%PROJ%\logs\engine_live_out.log" 2>&1
endlocal
