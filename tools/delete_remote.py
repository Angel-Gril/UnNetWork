#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
delete_remote.py —— 通过 GitHub API 删除远端文件
================================================
push_via_api.py 只能新增/更新文件（基于 base_tree 覆盖），
不会删除远端已存在但本地已移除的文件。

本脚本补上这个能力：给定路径列表，从 tree 里去掉它们。

用法
----
    python tools/delete_remote.py --repo OWNER/REPO --token PAT \
        --branch main --message "..." path1 path2 path3
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def req(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Authorization", f"Bearer {token}")
    r.add_header("Accept", "application/vnd.github+json")
    r.add_header("User-Agent", "delete-remote")
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", required=True)
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args()

    # head
    st, ref = req("GET", f"/repos/{args.repo}/git/ref/heads/{args.branch}", args.token)
    if st != 200:
        sys.exit(f"取 branch 失败 ({st}): {ref.get('message')}")
    parent = ref["object"]["sha"]

    st, commit = req("GET", f"/repos/{args.repo}/git/commits/{parent}", args.token)
    if st != 200:
        sys.exit(f"取 commit 失败 ({st}): {commit.get('message')}")
    base_tree = commit["tree"]["sha"]

    # 取完整 tree 递归列表
    st, full = req("GET",
                   f"/repos/{args.repo}/git/trees/{base_tree}?recursive=1",
                   args.token)
    if st != 200:
        sys.exit(f"取 tree 失败 ({st}): {full.get('message')}")

    drop = set(p.lstrip("./") for p in args.paths)
    kept = [e for e in full["tree"]
            if e["type"] == "blob" and e["path"] not in drop]
    removed = [e["path"] for e in full["tree"]
               if e["type"] == "blob" and e["path"] in drop]

    if not removed:
        print("远端没有这些文件，无需删除")
        return 0

    print(f"删除 {len(removed)} 个文件：")
    for p in removed:
        print(f"  - {p}")
    print(f"保留 {len(kept)} 个文件")

    # 新 tree（不带 base_tree，给完整列表）
    st, tree = req("POST", f"/repos/{args.repo}/git/trees", args.token, {
        "tree": [{"path": e["path"], "mode": e["mode"],
                  "type": "blob", "sha": e["sha"]} for e in kept],
    })
    if st != 201:
        sys.exit(f"建 tree 失败 ({st}): {tree.get('message')}")

    st, newc = req("POST", f"/repos/{args.repo}/git/commits", args.token, {
        "message": args.message,
        "tree": tree["sha"],
        "parents": [parent],
    })
    if st != 201:
        sys.exit(f"建 commit 失败 ({st}): {newc.get('message')}")

    st, upd = req("PATCH", f"/repos/{args.repo}/git/refs/heads/{args.branch}",
                  args.token, {"sha": newc["sha"], "force": False})
    if st != 200:
        sys.exit(f"更新 ref 失败 ({st}): {upd.get('message')}")

    print(f"\n✓ {newc['sha'][:12]}")
    print(f"  https://github.com/{args.repo}/commit/{newc['sha']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
