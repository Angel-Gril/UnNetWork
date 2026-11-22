#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
geo_batch.py —— ip-api 批量查询（绕开 45/分钟限流）
=================================================
ip-api 免费版限 45 次/分钟，逐个查会大量返回空。
batch 接口一次可以查 100 个，配额按 1 次算。

用法
----
    from geo_batch import batch_lookup
    details = batch_lookup(["1.2.3.4", "5.6.7.8"])
"""
from __future__ import annotations

import json
import subprocess
import shutil
import time

CURL = shutil.which("curl") or shutil.which("curl.exe")
FIELDS = ("countryCode,country,regionName,city,isp,org,as,asname,"
          "proxy,hosting,mobile,query")


def batch_lookup(ips, chunk=100, retries=3):
    """批量查 IP 详情，返回 {ip: detail_dict}"""
    out = {}
    uniq = list(dict.fromkeys(ips))
    for i in range(0, len(uniq), chunk):
        part = uniq[i:i + chunk]
        payload = json.dumps([{"query": ip, "fields": FIELDS} for ip in part])
        for attempt in range(retries):
            try:
                p = subprocess.run(
                    [CURL, "-s", "-m", "30", "-X", "POST",
                     "-H", "Content-Type: application/json",
                     "--data-binary", payload,
                     "http://ip-api.com/batch?fields=" + FIELDS],
                    capture_output=True, text=True, timeout=45, errors="replace")
                data = json.loads(p.stdout or "[]")
                if isinstance(data, list) and len(data) == len(part):
                    for item in data:
                        q = item.get("query")
                        if q:
                            out[q] = item
                    break
                time.sleep(2)
            except Exception:
                time.sleep(2)
        time.sleep(1.2)   # 块之间留间隔
    return out


if __name__ == "__main__":
    import sys
    ips = sys.argv[1:]
    d = batch_lookup(ips)
    for ip, info in d.items():
        print(f"{ip:<18} {info.get('countryCode','?'):<4} "
              f"{str(info.get('isp'))[:30]:<32} "
              f"proxy={info.get('proxy')} hosting={info.get('hosting')}")
