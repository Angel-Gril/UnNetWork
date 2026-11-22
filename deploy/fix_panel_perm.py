#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_panel_perm.py —— 修面板刷新权限（两个 service 都要改）
========================================================
背景：
  /etc/systemd/system/private-subscription.service         ← 面板（HTTP 服务）
  /etc/systemd/system/private-subscription-refresh@.service ← timer

  面板的 Refresh 按钮走 triggerRefresh() → spawn(refresh-provider.mjs)，
  子进程继承父进程（private-subscription.service）的沙箱。
  之前只给 refresh@.service 加了 /opt/ur-sub，面板那条没加，
  所以点面板刷新报 Read-only file system。

另外 wrapper 里还有 `mkdir -p "$APP/logs"` 的残留，
即使权限给了也会去建一个已经废弃的目录，一并清掉。
"""
import os
import shutil
import subprocess
import sys
import time

PANEL = "/etc/systemd/system/private-subscription.service"
TIMER = "/etc/systemd/system/private-subscription-refresh@.service"
WRAPPER = "/opt/ur-sub/ur-refresh-run.sh"
TS = time.strftime("%Y%m%d%H%M%S")

REQUIRED = [
    "/var/lib/private-subscription/cache",
    "/var/lib/private-subscription/read-tokens",
    "/var/lib/private-subscription/state/leapvpn",
    "/var/lib/private-subscription/state/subscription-server",
    "/var/lib/private-subscription/logs",
    "/opt/ur-sub",
    "/var/www/ur",
]


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def patch_unit(path):
    print(f"\n=== {os.path.basename(path)} ===")
    if not os.path.exists(path):
        print("  !! 不存在")
        return False

    lines = open(path).read().split("\n")
    idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("ReadWritePaths="):
            idx = i
            break

    new_line = "ReadWritePaths=" + " ".join(REQUIRED)

    if idx is None:
        # 找 [Service] 段末尾插入
        for i, ln in enumerate(lines):
            if ln.strip() == "[Service]":
                idx = i
                break
        if idx is None:
            print("  !! 找不到 [Service]")
            return False
        lines.insert(idx + 1, new_line)
        print(f"  插入: {new_line}")
    else:
        old = lines[idx]
        missing = [p for p in REQUIRED if p not in old]
        if not missing:
            print("  已完整，跳过")
            return True
        lines[idx] = new_line
        print(f"  补上: {missing}")

    shutil.copy2(path, f"{path}.bak-{TS}")
    open(path, "w").write("\n".join(lines))
    print(f"  新: {new_line}")
    return True


def patch_wrapper():
    print(f"\n=== {os.path.basename(WRAPPER)} ===")
    txt = open(WRAPPER).read()

    old = 'mkdir -p "$RUNTIME" "$APP/logs"'
    new = 'mkdir -p "$RUNTIME" /var/lib/private-subscription/logs'
    if old in txt:
        shutil.copy2(WRAPPER, f"{WRAPPER}.bak-{TS}")
        txt = txt.replace(old, new, 1)
        open(WRAPPER, "w").write(txt)
        print(f"  改: {old} -> {new}")
    elif old.replace('"$APP/logs"', '/var/lib/private-subscription/logs') in txt:
        print("  已改过")
    else:
        print("  !! 找不到 mkdir 行，检查是否有其他 $APP/logs 引用")
        for ln in txt.split("\n"):
            if "APP/logs" in ln:
                print(f"    {ln}")

    rc, out = sh(f"bash -n {WRAPPER}")
    print(f"  语法: {'OK' if rc == 0 else out[:150]}")


def main():
    if os.geteuid() != 0:
        sys.exit("需要 root")

    # 确保目录存在且可写
    for d in ["/var/lib/private-subscription/logs"]:
        os.makedirs(d, exist_ok=True)
        sh(f"chown subsvc:subsvc {d}")
        sh(f"chmod 750 {d}")
    sh("chown -R subsvc:subsvc /opt/ur-sub/runtime 2>/dev/null || true")

    ok1 = patch_unit(PANEL)
    ok2 = patch_unit(TIMER)
    patch_wrapper()

    print("\n=== 重载并重启 ===")
    sh("systemctl daemon-reload")
    sh("systemctl restart private-subscription.service")
    time.sleep(5)
    rc, out = sh("systemctl is-active private-subscription.service")
    print(f"  面板服务: {out.strip()}")

    # 验证面板 API
    print("\n=== 验证面板 status ===")
    rc, out = sh('curl -s -m 10 http://127.0.0.1:3100/healthz')
    print(f"  {out.strip()[:100]}")

    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
