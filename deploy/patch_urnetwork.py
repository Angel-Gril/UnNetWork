#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_urnetwork.py —— 把 urnetwork 接入现有订阅服务
===================================================
在服务器上跑（root），做两件事：

1. providers.mjs      加 "urnetwork" 到 PROVIDERS 集合
2. refresh-provider.mjs  加 refreshUrnetwork() 分支

设计原则：
  · 全部先备份（.bak-<时间戳>）
  · 幂等：已打过的补丁不会重复打
  · 改完做 node --check，语法不过就自动回滚
  · wrapper 负责写缓存，Node 这边只调用不重复写
"""
import os
import re
import shutil
import subprocess
import sys
import time

SRC = "/opt/private-subscription/apps/subscription-server/src"
TS = time.strftime("%Y%m%d%H%M%S")

PROVIDERS = os.path.join(SRC, "providers.mjs")
REFRESH = os.path.join(SRC, "refresh-provider.mjs")

WRAPPER = "/opt/ur-sub/ur-refresh-run.sh"


def backup(p):
    b = f"{p}.bak-{TS}"
    if not os.path.exists(b):
        shutil.copy2(p, b)
    return b


def check(p):
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or "").strip()


def patch_providers():
    txt = open(PROVIDERS, encoding="utf-8").read()
    if '"urnetwork"' in txt:
        print("  providers.mjs 已包含 urnetwork，跳过")
        return True

    new = re.sub(
        r'new Set\(\[([^\]]*)\]\)',
        lambda m: f'new Set([{m.group(1).rstrip()}, "urnetwork"])',
        txt, count=1)
    if new == txt:
        print("  !! 无法识别 PROVIDERS 结构")
        return False

    backup(PROVIDERS)
    open(PROVIDERS, "w", encoding="utf-8").write(new)
    ok, err = check(PROVIDERS)
    print(f"  providers.mjs 已改  node --check: {'OK' if ok else err[:200]}")
    return ok


UR_FN = '''
// ---- URnetwork：通过外部 wrapper 拉取（wrapper 自己写缓存，这里只负责调用）
async function refreshUrnetwork() {
  const { execFile } = await import("node:child_process");
  const wrapper = process.env.URNETWORK_WRAPPER || "%s";
  await new Promise((resolve, reject) => {
    execFile(wrapper, [], {
      timeout: 7200_000,
      maxBuffer: 16 * 1024 * 1024,
      env: {
        ...process.env,
        MAX_PER_KEY: process.env.URNETWORK_MAX_PER_KEY || "5",
        MAX_ROUNDS: process.env.URNETWORK_MAX_ROUNDS || "3",
      },
    }, (err, stdout, stderr) => {
      if (err) return reject(new Error(String(stderr || err.message).slice(0, 800)));
      resolve(stdout);
    });
  });

  const yamlPath = path.join(cacheDirectory, "urnetwork.yaml");
  const metaPath = path.join(cacheDirectory, "urnetwork.json");
  const yaml = await fs.readFile(yamlPath, "utf8");
  if (!yaml.trim()) throw new Error("urnetwork.yaml is empty");

  // 复用现成的校验/格式化，但不重复写 yaml（wrapper 已写）
  const prepared = await prepareSubscription(yaml, {
    minimum: Number(process.env.URNETWORK_MIN_PROXIES || 1),
  });
  let meta = {};
  try { meta = JSON.parse(await fs.readFile(metaPath, "utf8")); } catch {}
  return {
    provider: "urnetwork",
    proxyCount: prepared.proxyCount ?? meta.proxyCount ?? 0,
    residentialCount: meta.residentialCount ?? 0,
    updatedAt: meta.updatedAt || new Date().toISOString(),
  };
}
''' % WRAPPER


def patch_refresh():
    txt = open(REFRESH, encoding="utf-8").read()
    if "refreshUrnetwork" in txt:
        print("  refresh-provider.mjs 已包含 urnetwork，跳过")
        return True

    # 1) 插入函数：放在 "const provider = process.argv[2];" 之前
    anchor = "const provider = process.argv[2];"
    if anchor not in txt:
        print("  !! 找不到分发锚点")
        return False
    txt = txt.replace(anchor, UR_FN + "\n" + anchor, 1)

    # 2) 加分支
    old_chain = ('const result = await (provider === "flybird" ? refreshFlyBird()\n'
                 '      : provider === "leapvpn" ? refreshLeapVpn() : refreshMonoCloud());')
    new_chain = ('const result = await (provider === "flybird" ? refreshFlyBird()\n'
                 '      : provider === "leapvpn" ? refreshLeapVpn()\n'
                 '      : provider === "urnetwork" ? refreshUrnetwork()\n'
                 '      : refreshMonoCloud());')
    if old_chain in txt:
        txt = txt.replace(old_chain, new_chain, 1)
    else:
        # 容错：用正则
        txt2 = re.sub(
            r'provider === "leapvpn"\s*\?\s*refreshLeapVpn\(\)\s*:\s*refreshMonoCloud\(\)',
            'provider === "leapvpn" ? refreshLeapVpn()\n'
            '      : provider === "urnetwork" ? refreshUrnetwork()\n'
            '      : refreshMonoCloud()',
            txt, count=1)
        if txt2 == txt:
            print("  !! 找不到分发三元表达式")
            return False
        txt = txt2

    # 3) 提示信息
    txt = txt.replace(
        'Provider must be flybird, leapvpn or monocloud',
        'Provider must be flybird, leapvpn, monocloud or urnetwork')

    backup(REFRESH)
    open(REFRESH, "w", encoding="utf-8").write(txt)
    ok, err = check(REFRESH)
    print(f"  refresh-provider.mjs 已改  node --check: {'OK' if ok else err[:300]}")
    return ok


def main():
    if os.geteuid() != 0:
        sys.exit("需要 root")
    for p in (PROVIDERS, REFRESH):
        if not os.path.exists(p):
            sys.exit(f"找不到 {p}")
    if not os.path.exists(WRAPPER):
        print(f"  [preflight] wrapper 缺失: {WRAPPER}（刷新功能不可用，分发正常）")

    print("=== 打补丁 ===")
    ok1 = patch_providers()
    ok2 = patch_refresh()

    if not (ok1 and ok2):
        print("\n!! 有补丁失败，回滚")
        for p in (PROVIDERS, REFRESH):
            b = f"{p}.bak-{TS}"
            if os.path.exists(b):
                shutil.copy2(b, p)
                print(f"  回滚 {os.path.basename(p)}")
        sys.exit(1)

    print("\n=== 验证 ===")
    for p in (PROVIDERS, REFRESH):
        ok, err = check(p)
        print(f"  {os.path.basename(p):<24} {'OK' if ok else err[:120]}")

    print("\n=== 结果 ===")
    print(open(PROVIDERS, encoding="utf-8").read().strip())
    print("\n下一步:")
    print("  systemctl restart private-subscription.service")
    print("  curl -s -u admin:<pass> http://127.0.0.1:3100/admin/api/status | head -c 400")


if __name__ == "__main__":
    main()
