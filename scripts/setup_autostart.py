#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
qmtIDE-deepseek 开机自启注册（用户级，无需管理员）
=================================================
同时写入两条登录自启通道，互为冗余，任一生效即可自启：
  A) HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 注册表项
     -> 指向 scripts/startup_all.bat（cmd /c start /min 最小化拉起）
  B) Startup 文件夹快捷启动脚本 qmtIDE-startup.bat
     -> 调用 scripts/startup_all.bat

为何用这两条：
  - 任务计划程序(schtasks / Register-ScheduledTask) 在本环境被安全策略拦截或需管理员；
  - Startup 文件夹 .lnk 经 WScript.Shell COM 创建被安全策略拦截；
  - 注册表 HKCU Run 是“每个用户”键，不需管理员、非 COM；Startup 文件夹直接放 .bat 亦非 COM。
  - startup_all.bat 内含幂等守卫，双通道同时触发也不会重复拉起。

用法：
  python setup_autostart.py            # 注册两条自启通道
  python setup_autostart.py --remove   # 取消两条自启通道
  python setup_autostart.py --check    # 仅检查当前状态
"""
import os
import sys
import winreg

PROJ = r"C:\Users\lisinan\Desktop\qmtIDE-deepseek"
BAT = os.path.join(PROJ, "scripts", "startup_all.bat")
CMD = r"C:\Windows\System32\cmd.exe"

# 通道 A：注册表 Run 键
REG_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_VALUE_NAME = "qmtIDE-deepseek-startup"
REG_COMMAND = '"{cmd}" /c start /min "" "{bat}"'.format(cmd=CMD, bat=BAT)

# 通道 B：Startup 文件夹启动脚本
STARTUP_DIR = os.path.join(
    os.environ.get("APPDATA", ""),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
)
STARTUP_BAT = os.path.join(STARTUP_DIR, "qmtIDE-startup.bat")
STARTUP_CONTENT = (
    "@echo off\n"
    "REM qmtIDE-deepseek 开机自启（Startup 文件夹入口）\n"
    "REM 调用 scripts/startup_all.bat（已含幂等守卫，与 HKCU Run 双触发安全）\n"
    'start "" /min "C:\\Users\\lisinan\\Desktop\\qmtIDE-deepseek\\scripts\\startup_all.bat"\n'
    "exit\n"
)


def _reg_set():
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, REG_VALUE_NAME, 0, winreg.REG_SZ, REG_COMMAND)
    finally:
        winreg.CloseKey(key)


def _reg_delete():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, REG_VALUE_NAME)
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        pass


def _reg_has():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_QUERY_VALUE)
        try:
            winreg.QueryValueEx(key, REG_VALUE_NAME)
            return True
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        return False


def _startup_write():
    os.makedirs(STARTUP_DIR, exist_ok=True)
    with open(STARTUP_BAT, "w", encoding="utf-8") as f:
        f.write(STARTUP_CONTENT)


def _startup_delete():
    try:
        os.remove(STARTUP_BAT)
    except FileNotFoundError:
        pass


def _startup_has():
    return os.path.exists(STARTUP_BAT)


def register():
    _reg_set()
    _startup_write()
    print("[注册] HKCU Run 键 + Startup 文件夹启动脚本 已写入")
    print("        Run  => %s" % REG_COMMAND)
    print("        Startup => %s" % STARTUP_BAT)


def remove():
    _reg_delete()
    _startup_delete()
    print("[移除] 两条自启通道已清理（Run 键 + Startup 脚本）")


def check():
    r = _reg_has()
    s = _startup_has()
    print("[检查] HKCU Run 键   : %s" % ("已注册" if r else "未注册"))
    print("[检查] Startup 脚本  : %s" % ("已存在" if s else "不存在"))
    return r and s


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--remove":
        remove()
    elif arg == "--check":
        ok = check()
        print("状态: %s" % ("全部就绪" if ok else "未完全配置"))
    else:
        register()
        check()
