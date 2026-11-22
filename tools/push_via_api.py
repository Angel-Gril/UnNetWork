#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
push_via_api.py —— 通过 GitHub REST API 提交文件
================================================
当 github.com:443 被阻断、git push 走不通时，用 api.github.com
逐个文件提交。API 走的是不同的 CDN 节点，通常不受影响。

用法
----
    python push_via_api.py --repo Angel-Gril/UnNetWork \
        --token <PAT> --root . --branch main --message "..."

行为
----
    1. 取当前 branch 的最新 commit sha
    2. 取它的 tree sha
    3. 为每个文件创建 blob
    4. 创建新 tree（基于旧 tree，覆盖变化的文件）
    5. 创建 commit（parent = 旧 commit）
    6. 更新 branch ref
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
SKIP_EXT = {".pyc", ".pyo", ".log"}


def req(method, path, token, body=None):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", f"Bearer {token}")
    r.add_header("Accept", "application/vnd.github+json")
    r.add_header("User-Agent", "push-via-api")
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=45) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw[:300]}
    except Exception as e:
        return 0, {"message": f"{type(e).__name__}: {e}"}


def collect_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1] in SKIP_EXT:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            out.append((rel, full))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", required=True)
    args = ap.parse_args()

    files = collect_files(args.root)
    print(f"待提交 {len(files)} 个文件\n")

    # 1) 当前 branch 的 head
    st, ref = req("GET", f"/repos/{args.repo}/git/ref/heads/{args.branch}", args.token)
    if st != 200:
        sys.exit(f"取 branch 失败 ({st}): {ref.get('message')}")
    parent = ref["object"]["sha"]
    print(f"parent commit: {parent[:12]}")

    # 2) 其 tree
    st, commit = req("GET", f"/repos/{args.repo}/git/commits/{parent}", args.token)
    if st != 200:
        sys.exit(f"取 commit 失败 ({st}): {commit.get('message')}")
    base_tree = commit["tree"]["sha"]

    # 3) 逐个建 blob
    entries = []
    for rel, full in files:
        raw = open(full, "rb").read()
        st, blob = req("POST", f"/repos/{args.repo}/git/blobs", args.token, {
            "content": base64.b64encode(raw).decode(),
            "encoding": "base64",
        })
        if st != 201:
            print(f"  ✗ {rel}: {blob.get('message')}")
            continue
        entries.append({
            "path": rel,
            "mode": "100755" if os.access(full, os.X_OK) and not rel.endswith((".py", ".md", ".txt", ".yaml", ".service", ".timer", ".gitignore")) else "100644",
            "type": "blob",
            "sha": blob["sha"],
        })
        print(f"  ✓ {rel}")

    if not entries:
        sys.exit("没有成功上传的文件")

    # 4) 新 tree
    st, tree = req("POST", f"/repos/{args.repo}/git/trees", args.token, {
        "base_tree": base_tree,
        "tree": entries,
    })
    if st != 201:
        sys.exit(f"建 tree 失败 ({st}): {tree.get('message')}")
    print(f"\ntree: {tree['sha'][:12]}")

    # 5) 新 commit
    st, newc = req("POST", f"/repos/{args.repo}/git/commits", args.token, {
        "message": args.message,
        "tree": tree["sha"],
        "parents": [parent],
    })
    if st != 201:
        sys.exit(f"建 commit 失败 ({st}): {newc.get('message')}")
    print(f"commit: {newc['sha'][:12]}")

    # 6) 更新 ref
    st, upd = req("PATCH", f"/repos/{args.repo}/git/refs/heads/{args.branch}",
                  args.token, {"sha": newc["sha"], "force": False})
    if st != 200:
        sys.exit(f"更新 ref 失败 ({st}): {upd.get('message')}")

    print(f"\n✓ 已推送到 {args.repo}@{args.branch}")
    print(f"  https://github.com/{args.repo}/commit/{newc['sha']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
