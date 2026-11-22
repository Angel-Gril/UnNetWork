# UnNetWork — URnetwork 住宅代理订阅工具链

把 [URnetwork](https://urnetwork.com) 的免费住宅代理导出成标准 **mihomo / sing-box** 订阅，
并接入自建的多机场聚合服务。

---

## 这是什么

URnetwork 是一个 P2P 带宽市场：个人用户贡献闲置带宽，换取使用他人带宽的权限。
它的客户端能翻墙，但**不对第三方开放标准代理协议**——你要么用它的 App，
要么用不了那些住宅 IP。

本项目解决的是：**把它的代理端口导出成标准订阅，供 mihomo 等客户端使用。**

核心能力：

| 能力 | 说明 |
|---|---|
| **扫描式刷新** | 读取已有槽位的连接信息，不签发、不删除，节点不会自杀 |
| **住宅筛选** | 自动踢掉机房 IP，只保留真实住宅宽带 |
| **IP 去重** | 同一出口 IP 最多保留 N 条，避免"切换节点等于没换" |
| **国家分类** | 按落地国家分组，命名满足 mihomo 模板的词边界要求 |
| **多协议** | SOCKS5 / HTTP / HTTPS（推荐 SOCKS5，见下文） |
| **订阅集成** | 接入私有订阅服务的 `/s/<token>/` 分发通道 |

---

## 关键发现（踩过的坑）

这些都是实测出来的，写下来避免重复踩。

### 1. SOCKS5 是唯一实用的协议

URnetwork 每个节点开三个端口：

| 协议 | 端口 | 冷启动 | 结论 |
|---|---|---|---|
| **SOCKS5** | 7141 | **1-2 秒** | ✅ 推荐 |
| HTTP | +1 | 约 5 秒 | ⚠️ 常被客户端判超时 |
| HTTPS | +2 | 5-15 秒 | ⚠️ 同上 |

`list_slots()` 返回的 `pro_features` 只登记了 `socks_proxy`——SOCKS5 是原生协议，
http/https 是后加的包装。

mihomo 的 `url-test` 默认 5 秒判定，http/https 常在预热完成前就被测死。

### 2. https 的正确写法

mihomo / sing-box **没有 `type: https` 这种代理类型**，写了会报
`unsupported proxy type` 并让整份配置失效。

```yaml
# ✅ 正确
- name: "US-01"
  type: http          # 不是 https
  port: 7143          # https 端口
  tls: true
  skip-cert-verify: false

# ❌ 错误
- name: "US-01"
  type: https
```

### 3. UDP 不可用

SOCKS5 的 `UDP ASSOCIATE` 握手会成功，但返回的 relay 地址是容器内网
（如 `172.19.0.1`），外部不可达。

```yaml
udp: false   # 必须关，否则 DNS 会卡住
```

### 4. 不要用"签发"模式刷新

早期版本每轮都调 `issue()` 新签节点 + 删最旧槽位腾位置。
165 个任务下来把槽位删空了，**产物里的节点全部失效**——客户端表现是全部 timeout。

正确做法是用 `list_slots()` 读现有槽位：

```python
slots = list_slots(key)
for s in slots:
    pc = s["proxy_client"]     # 已含 host / 三个端口 / auth_token
    # 直接用，不需要 issue()
```

### 5. 节点名要满足词边界

私有订阅服务的 `buildProxyGroups()` 用正则分组：

```javascript
const us = namesMatching(all, [/\bUS\b/i, /USA/i, /美国/]);
// namesMatching 匹配不到时会返回全部节点
```

`US1-xxx` 里 `US` 后紧跟数字，`\bUS\b` **不成立** → 所有国家组被塞满全部节点。

必须写成 `US-01-xxx`（`US` 后是连字符）。

### 6. 授权头格式

服务端两种都接受，但 curl 的 `-U "TOKEN:"` 发的是带冒号的：

```
Basic base64("TOKEN:")   ✅
Basic base64("TOKEN")    ✅
Basic base64(":TOKEN")   ❌ 407
```

---

## 安装

```bash
git clone https://github.com/Angel-Gril/UnNetWork.git
cd UnNetWork
pip install -r requirements.txt    # 只有 pyyaml，其余用标准库
```

依赖：Python 3.10+、curl（脚本用它发 HTTP 请求）。

---

## 快速开始

### 1. 准备 API key

从 URnetwork App 或网页端拿到 `urn_xxx` 格式的 key，每行一个写入 `keys.txt`。

免费账号每 key 有 5 个代理槽位，33 个 key = 165 个槽位上限。

### 2. 扫描现有槽位

```bash
python src/ur_scan.py --keys keys.txt --out ./sub
```

输出：

```
./sub/mihomo.yaml         mihomo / Clash.Meta 配置
./sub/nodes.txt           明文节点列表
./sub/subscription.txt    base64 订阅
./sub/inventory.json      节点明细
./sub/report.md           统计报告
```

### 3. 导入客户端

把 `mihomo.yaml` 或 `subscription.txt` 导入 mihomo / Clash Verge / FlClash。

---

## 命令详解

### ur_scan.py — 扫描刷新（推荐）

```bash
python src/ur_scan.py \
  --keys keys.txt \
  --out ./sub \
  --protocol socks5 \
  --parallel 8 \
  --max-probe-ms 25000 \
  --dedup-per-ip 3
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--protocol` | `https` | 建议改 `socks5` |
| `--parallel` | `8` | 并发数。每 key 限速独立，8 是安全值 |
| `--max-probe-ms` | `25000` | 探测延迟上限。放宽会多收节点但首次连接慢 |
| `--dedup-per-ip` | `3` | 同一出口 IP 最多保留几条。`0` = 不去重 |
| `--residential-only` | 开 | 只保留住宅 IP |
| `--topup` | 关 | 顺便给空槽位补签发 |
| `--only` | 全部 | 只要这些国家，如 `US,CA` |

### ur_refresh.py — 签发模式

**不推荐日常使用**。它会 `issue()` 新节点并删旧槽位，可能把槽位删空。
仅在首次建设账号池时用。

```bash
python src/ur_refresh.py --keys keys.txt --out ./sub --max-per-key 5
```

### 工具

```bash
# 国内三协议可用性探针
python tools/ur_probe_cn.py --inventory sub/inventory.json

# UDP ASSOCIATE 支持测试
python tools/ur_udp_probe.py --inventory sub/inventory.json

# 配置完整性校验（悬空引用 / 空组 / 重复名）
python tools/validate.py sub/mihomo.yaml

# 策略组语义检查
python tools/diag_config.py sub/mihomo.yaml
```

---

## 账号工具

| 脚本 | 用途 |
|---|---|
| `ur_guest.py` | Guest 模式批量铸造账号（无需邮箱，1.1-1.3 秒/个）|
| `ur_register.py` | 邮箱注册（Microsoft 收信 + Graph API）|
| `ur_reach.py` | 循环注册到目标数量 |
| `ur_ops.py` | 多 key 编排，按国家分类 |
| `ur_oneshot.py` | 单 key 快速导出 |

---

## 集成到私有订阅服务

如果你有自己的 nginx + Node 订阅分发服务，可以按 `deploy/` 下的脚本接入。

完整方案见 [docs/integration.md](docs/integration.md)，包括：

- 让 Node 服务识别新 provider
- `/s/<token>/` 鉴权下发
- systemd 定时刷新（含沙箱权限坑）
- 套用路由模板（dns / sniffer / 策略组）

---

## 已知限制

| 限制 | 原因 | 能否解决 |
|---|---|---|
| 出口 IP 只有 15-25 个 | URnetwork 住宅池被 GoCodeIT 一家占八成 | ❌ |
| 延迟 1-13 秒跳变 | P2P 两跳，第二跳是陌生人家的宽带 | ❌ 架构性 |
| UDP 不可用 | relay 返回容器内网地址 | ❌ |
| 无 WireGuard | Pro 专属，`wg_config` 恒空 | ❌ |
| 槽位上限 165 | 33 key × 5 | 加账号 |
| 节点突然失效 | 对方 provider 下线 | 刷新换节点 |

**适用场景**：需要美国住宅 IP 身份（注册、解锁、验证），能容忍几秒延迟。

**不适用**：看视频、打游戏、大文件下载。

---

## 项目结构

```
src/
  ur_scan.py        扫描刷新器（核心）
  ur_hunt.py        底层：API / 探测 / 判定
  geo_batch.py      ip-api 批量查询（绕开 45 次/分钟限流）
  ur_refresh.py     签发模式（首次建设用）
  ur_register.py    邮箱注册
  ur_guest.py       Guest 铸造
  ur_ops.py         多账号编排
tools/
  ur_probe_cn.py    国内可用性探针
  ur_udp_probe.py   UDP 测试
  validate.py       配置校验
  diag_config.py    分组语义检查
deploy/
  ur-refresh-run.sh            刷新包装（systemd 调用）
  patch_urnetwork.py           接入 Node 订阅服务
  fix_panel_perm.py            修面板服务沙箱权限
  fix_apply_template.py        套用路由模板
docs/
  integration.md               服务器集成完整方案
  architecture.md              数据流与设计取舍
examples/
  mihomo.example.yaml          配置样例
  ur-refresh.service           systemd 单元样例
```

---

## 凭据边界

三类数据属于凭据，不进仓库、不进公开目录：

| 数据 | 位置 | 约束 | 检查方式 |
|---|---|---|---|
| API key (`urn_*`) | `keys.txt` | 模式 600，`.gitignore` 排除 | `tools/check_secrets.py` |
| 代理凭据 (`auth_token`) | `cache/*.yaml` | 只经 `/s/<token>/` 分发，不进静态目录 | `tools/check_config.py` |
| 助记词 | 由 `ur_guest.py` 输出 | 无备份即不可恢复 | — |

### 落地方式

**pre-commit hook**（拦截提交）：

```bash
cp tools/check_secrets.py .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

检测 `urn_*`、`06GV*`、GitHub PAT、PEM 私钥、硬编码密码，
命中则中断提交并列出文件行号。

**部署后检查**（拦截暴露）：

```bash
python tools/check_config.py --public-dir /var/www/ur --key-dir /opt/ur-sub
```

检查三件事：

- 公开目录里是否存在含 `auth_token` 的文件
- `keys.txt` 等凭据文件权限是否为 600
- `.bak-*` 备份是否残留凭据

退出码非 0 表示存在暴露面。

**分发通道**：含 `auth_token` 的配置只通过 `/s/<token>/` 下发，
静态目录 `/ur/` 只放 `report.md` 和 `state.json`（无凭据）。

---

## License

MPL-2.0（与 URnetwork 的 `urnetwork/connect` 保持兼容）
