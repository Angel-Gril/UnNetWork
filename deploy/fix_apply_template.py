#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_apply_template.py —— 让 refreshUrnetwork 也套路由模板
=========================================================
之前只调了 prepareSubscription（校验），没调 applyRoutingTemplate，
所以产出的 yaml 缺 dns / sniffer / 策略组。

monocloud 的正确流程：
    defaults = YAML.parse(templates/mihomo-base.yaml)
    routed   = applyRoutingTemplate(raw, `https://${domain}`, enhanceMihomoConfig, defaults)
    prepared = prepareSubscription(routed.yaml, {...})
    commitLastGood(...)

照抄这一段。
"""
import os
import re
import shutil
import subprocess
import sys
import time

REFRESH = "/opt/private-subscription/apps/subscription-server/src/refresh-provider.mjs"
TS = time.strftime("%Y%m%d%H%M%S")


def main():
    if os.geteuid() != 0:
        sys.exit("需要 root")

    txt = open(REFRESH, encoding="utf-8").read()

    # 替换 refreshUrnetwork 里 prepareSubscription 那段
    old = """  // 和 flybird 一样：套路由模板、校验、生成策略组
  const prepared = await prepareSubscription(rawYaml, {
    minimum: Number(process.env.URNETWORK_MIN_PROXIES || 1),
    previousYaml,
    allowServerRecovery: true,
    allowProxyResolver: true,
  });"""

    new = """  // 套路由模板（dns / sniffer / 策略组），和 monocloud 走同一条路
  const domain = process.env.PUBLIC_DOMAIN;
  const defaults = YAML.parse(
    await fs.readFile(path.join(projectRoot, "templates/mihomo-base.yaml"), "utf8"));
  const routed = applyRoutingTemplate(
    rawYaml,
    `https://${domain}`,
    enhanceMihomoConfig,
    defaults,
  );

  const prepared = await prepareSubscription(routed.yaml, {
    minimum: Number(process.env.URNETWORK_MIN_PROXIES || 1),
    previousYaml,
    allowServerRecovery: true,
    allowProxyResolver: true,
  });
  if (routed.routing) prepared.routing = routed.routing;"""

    if old in txt:
        shutil.copy2(REFRESH, f"{REFRESH}.bak-{TS}")
        txt = txt.replace(old, new, 1)
        open(REFRESH, "w", encoding="utf-8").write(txt)
        print("  已替换为 applyRoutingTemplate 流程")
    elif "applyRoutingTemplate(\n    rawYaml" in txt or "applyRoutingTemplate(rawYaml" in txt:
        print("  已含 applyRoutingTemplate，跳过")
    else:
        print("  !! 找不到 prepareSubscription 段")
        idx = txt.find("refreshUrnetwork")
        print(txt[idx:idx + 1200] if idx > 0 else "(找不到函数)")
        return 1

    r = subprocess.run(["node", "--check", REFRESH], capture_output=True, text=True)
    ok = r.returncode == 0
    print(f"  node --check: {'OK' if ok else r.stderr[:300]}")
    if not ok:
        shutil.copy2(f"{REFRESH}.bak-{TS}", REFRESH)
        print("  !! 已回滚")
        return 1

    print("\n  --- 改后的 refreshUrnetwork 核心段 ---")
    txt2 = open(REFRESH, encoding="utf-8").read()
    m = re.search(r'// 套路由模板.*?prepared\.routing = routed\.routing;', txt2, re.S)
    if m:
        for ln in m.group(0).split("\n"):
            print("    " + ln)

    subprocess.run(["systemctl", "restart", "private-subscription.service"], check=False)
    print("\n  服务已重启")
    return 0


if __name__ == "__main__":
    sys.exit(main())
