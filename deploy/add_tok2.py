#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add_tok2.py —— 按行插入 urnetwork tokenStore
"""
import os
import secrets
import shutil
import subprocess
import sys
import time

SERVER = "/opt/private-subscription/apps/subscription-server/src/server.mjs"
ENVFILE = "/etc/private-subscription/service.env"
TOKDIR = "/var/lib/private-subscription/read-tokens"
TOKFILE = os.path.join(TOKDIR, "urnetwork")
TS = time.strftime("%Y%m%d%H%M%S")


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    if os.geteuid() != 0:
        sys.exit("需要 root")

    lines = open(SERVER, encoding="utf-8").read().split("\n")

    if any('"urnetwork"' in ln for ln in lines):
        print("  server.mjs 已含 urnetwork")
    else:
        # 找 monocloud 那一行，在它后面插入
        target = None
        for i, ln in enumerate(lines):
            if '["monocloud"' in ln:
                target = i
                break
        if target is None:
            print("  !! 找不到 [\"monocloud\" 行")
            for i, ln in enumerate(lines):
                if "tokenStores" in ln:
                    print(f"  第{i+1}行: {ln}")
            return 1

        indent = "  "
        new_line = f'{indent}["urnetwork", await createTokenStore(process.env.URNETWORK_READ_TOKEN_FILE)],'
        lines.insert(target + 1, new_line)

        shutil.copy2(SERVER, f"{SERVER}.bak-{TS}")
        open(SERVER, "w", encoding="utf-8").write("\n".join(lines))
        print(f"  已在第 {target+2} 行插入 urnetwork")

    # 打印 tokenStores 块
    print("\n  --- tokenStores ---")
    txt = open(SERVER, encoding="utf-8").read()
    idx = txt.find("const tokenStores")
    if idx >= 0:
        for ln in txt[idx:idx + 420].split("\n")[:8]:
            print("    " + ln)

    r = subprocess.run(["node", "--check", SERVER], capture_output=True, text=True)
    ok = r.returncode == 0
    print(f"\n  node --check: {'OK' if ok else r.stderr[:250]}")
    if not ok:
        shutil.copy2(f"{SERVER}.bak-{TS}", SERVER)
        print("  !! 已回滚")
        return 1

    # token 文件
    os.makedirs(TOKDIR, exist_ok=True)
    if os.path.exists(TOKFILE):
        tok = open(TOKFILE).read().strip()
        print(f"  token 已存在: {tok[:16]}…")
    else:
        tok = secrets.token_hex(32)
        open(TOKFILE, "w").write(tok + "\n")
        st = os.stat(os.path.join(TOKDIR, "flybird"))
        os.chown(TOKFILE, st.st_uid, st.st_gid)
        os.chmod(TOKFILE, 0o600)
        print(f"  已生成 token: {tok[:16]}…")

    # service.env
    env = open(ENVFILE, encoding="utf-8").read()
    if "URNETWORK_READ_TOKEN_FILE" not in env:
        shutil.copy2(ENVFILE, f"{ENVFILE}.bak-{TS}")
        with open(ENVFILE, "a", encoding="utf-8") as f:
            f.write(f"\n# URnetwork 订阅读取 token\nURNETWORK_READ_TOKEN_FILE={TOKFILE}\n")
        print("  service.env 已加变量")
    else:
        print("  service.env 已有变量")

    sh("systemctl restart private-subscription.service")
    time.sleep(7)
    rc, out = sh("systemctl is-active private-subscription.service")
    print(f"  服务: {out.strip()}")

    print(f"\n=== 订阅地址 ===")
    print(f"  https://sub.20260610.xyz/s/{tok}/urnetwork.yaml")
    print(f"\n=== 验证 ===")
    for p in ("flybird", "urnetwork"):
        rc2, out2 = sh(
            f'curl -sk -o /dev/null -w "%{{http_code}} %{{size_download}}B" -m 15 '
            f'"https://sub.20260610.xyz/s/{tok}/{p}.yaml"')
        print(f"  {p:<12} -> {out2.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
