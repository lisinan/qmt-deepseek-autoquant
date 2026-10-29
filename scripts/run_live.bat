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
"%PY%" -u main.py --web --mode live --init-positions >> "%PROJ%\logs\engine_live_out.log" 2>&1
endlocal
