# 集成到私有订阅服务

把 URnetwork 接进一个已有的多机场聚合订阅服务（Node + nginx + systemd），
让它和 FlyBird / LeapVPN 一样出现在管理面板里，共用 `/s/<token>/` 分发通道。

下面以一个真实部署为例，路径和域名按自己的改。

---

## 一、目标架构

```
                        ┌─────────────────────┐
nginx (443)  ──────────►│ Node :3100 订阅服务  │
  /admin/  (Basic Auth) │                     │
  /s/<tok>/             │  providers:         │
  /rules/               │    flybird          │
                        │    leapvpn          │
                        │    monocloud        │
                        │    urnetwork  ←新增 │
                        └──────────┬──────────┘
                                   │ spawn
                                   ▼
                        /opt/ur-sub/ur-refresh-run.sh
                                   │
                                   ▼
                        ur_scan.py → runtime/mihomo.yaml
                                   │
                          Node 读回，套路由模板
                                   │
                                   ▼
                        cache/urnetwork.yaml
```

**分工**：

- Python 只负责**产出节点**（proxies 段）
- Node 负责**套模板**（dns / sniffer / 策略组 / 规则）并**写缓存**

这样 urnetwork 和另外三家用的是同一套模板，格式完全一致。

---

## 二、部署 Python 侧

### 2.1 目录

```bash
sudo mkdir -p /opt/ur-sub/{scripts,runtime}
sudo chown -R subsvc:subsvc /opt/ur-sub
sudo chmod 750 /opt/ur-sub

# 复制脚本
sudo cp src/ur_scan.py src/ur_hunt.py src/geo_batch.py /opt/ur-sub/scripts/
sudo cp deploy/ur-refresh-run.sh /opt/ur-sub/
sudo chown subsvc:subsvc /opt/ur-sub/scripts/*.py /opt/ur-sub/ur-refresh-run.sh
sudo chmod 640 /opt/ur-sub/scripts/*.py
sudo chmod 750 /opt/ur-sub/ur-refresh-run.sh
```

### 2.2 key 文件

```bash
sudo touch /opt/ur-sub/keys.txt
sudo chown subsvc:subsvc /opt/ur-sub/keys.txt
sudo chmod 600 /opt/ur-sub/keys.txt

# 每行一个 urn_xxx
sudo -u subsvc tee -a /opt/ur-sub/keys.txt < keys.txt
```

### 2.3 日志目录

日志放 `/var/lib` 下——systemd 沙箱只允许写白名单路径：

```bash
sudo mkdir -p /var/lib/private-subscription/logs
sudo chown subsvc:subsvc /var/lib/private-subscription/logs
sudo chmod 750 /var/lib/private-subscription/logs
```

---

## 三、改 Node 侧

### 3.1 注册 provider

`src/providers.mjs`：

```javascript
export const PROVIDERS = new Set(["flybird", "leapvpn", "monocloud", "urnetwork"]);
```

### 3.2 加 tokenStore

`src/server.mjs` 的 `tokenStores` 里加一行（**不加载会 500**）：

```javascript
const tokenStores = new Map([
  ["flybird", await createTokenStore(readTokenFile)],
  ["leapvpn", await createTokenStore(process.env.LEAPVPN_READ_TOKEN_FILE)],
  ["monocloud", await createTokenStore(process.env.MONOCLOUD_READ_TOKEN_FILE)],
  ["urnetwork", await createTokenStore(process.env.URNETWORK_READ_TOKEN_FILE)],  // ← 新增
]);
```

生成 token 文件：

```bash
TOK=$(openssl rand -hex 32)
echo "$TOK" | sudo tee /var/lib/private-subscription/read-tokens/urnetwork
sudo chown subsvc:subsvc /var/lib/private-subscription/read-tokens/urnetwork
sudo chmod 600 /var/lib/private-subscription/read-tokens/urnetwork

# 写进环境变量
echo "URNETWORK_READ_TOKEN_FILE=/var/lib/private-subscription/read-tokens/urnetwork" \
  | sudo tee -a /etc/private-subscription/service.env
```

### 3.3 刷新函数

`src/refresh-provider.mjs` 加：

```javascript
async function refreshUrnetwork() {
  const wrapper = process.env.URNETWORK_WRAPPER || "/opt/ur-sub/ur-refresh-run.sh";
  const runtime = process.env.URNETWORK_RUNTIME || "/opt/ur-sub/runtime";

  await new Promise((resolve, reject) => {
    execFile(wrapper, [], {
      timeout: 7200_000,
      maxBuffer: 16 * 1024 * 1024,
      env: {
        ...process.env,
        PARALLEL: process.env.URNETWORK_PARALLEL || "8",
        PROTOCOL: process.env.URNETWORK_PROTOCOL || "socks5",
        MAX_PROBE_MS: process.env.URNETWORK_MAX_PROBE_MS || "25000",
        // 让 wrapper 只生成到 runtime，不直接写 cache
        SKIP_CACHE_WRITE: "1",
      },
    }, (err, stdout, stderr) => {
      if (err) return reject(new Error(String(stderr || err.message).slice(0, 800)));
      resolve(stdout);
    });
  });

  const rawYaml = await fs.readFile(path.join(runtime, "mihomo.yaml"), "utf8");
  if (!rawYaml.trim()) throw new Error("urnetwork raw yaml is empty");

  let previousYaml = null;
  try {
    previousYaml = await fs.readFile(path.join(cacheDirectory, "urnetwork.yaml"), "utf8");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }

  // 关键：套路由模板，和 monocloud 走同一条路
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
  if (routed.routing) prepared.routing = routed.routing;

  return commitLastGood(cacheDirectory, "urnetwork", prepared.yaml, prepared);
}
```

分发链加分支：

```javascript
const result = await (
  provider === "flybird" ? refreshFlyBird()
  : provider === "leapvpn" ? refreshLeapVpn()
  : provider === "urnetwork" ? refreshUrnetwork()
  : refreshMonoCloud()
);
```

### 3.4 面板显示名

`public/admin.html` 第 73 行附近：

```javascript
const names = {
  flybird: 'FlyBird',
  leapvpn: 'LeapVPN',
  monocloud: 'MonoCloud',
  urnetwork: 'URnetwork',   // ← 不加会显示 undefined
};
```

---

## 四、systemd 单元

### 4.1 刷新服务（模板单元已有）

如果服务里已经有 `private-subscription-refresh@.service`，只需确认
`ReadWritePaths` 包含新路径：

```ini
ReadWritePaths=/var/lib/private-subscription/cache \
                /var/lib/private-subscription/read-tokens \
                /var/lib/private-subscription/state/leapvpn \
                /var/lib/private-subscription/state/subscription-server \
                /var/lib/private-subscription/logs \
                /opt/ur-sub \
                /var/www/ur
```

### 4.2 timer

```ini
# /etc/systemd/system/private-subscription-refresh-urnetwork.timer
[Unit]
Description=Evaluate the URnetwork refresh schedule every five minutes

[Timer]
OnCalendar=*-*-* *:03/5:00
RandomizedDelaySec=20s
AccuracySec=15s
Persistent=true
Unit=private-subscription-refresh@urnetwork.service

[Install]
WantedBy=timers.target
```

实际刷新间隔由 `refresh-settings.json` 的 `intervalMinutes` 控制（面板可改），
timer 只是每 5 分钟问一次"该不该刷"。

避开另外三家的分钟位（`:00` / `:01` / `:02`），用 `:03`。

### 4.3 面板服务的沙箱

**面板的 Refresh 按钮**走的是 `private-subscription.service` 里的
`triggerRefresh()` → `spawn()`，**子进程继承父服务的沙箱**。

只给 `refresh@.service` 加路径是不够的，`private-subscription.service` 也要加：

```ini
# /etc/systemd/system/private-subscription.service
ReadWritePaths=/var/lib/private-subscription/cache \
                /var/lib/private-subscription/read-tokens \
                /var/lib/private-subscription/state/leapvpn \
                /var/lib/private-subscription/state/subscription-server \
                /var/lib/private-subscription/logs \
                /opt/ur-sub \
                /var/www/ur
```

否则点面板 Refresh 报：

```
Refresh urnetwork failed: mkdir: cannot create directory '/opt/ur-sub/logs':
Read-only file system
```

---

## 五、静态目录（可选）

如果想让 `/ur/<file>` 直接可访问，**只放无凭据的文件**：

```nginx
location /ur/ {
    alias /var/www/ur/;
    autoindex off;
    add_header Cache-Control "no-store";
    add_header Access-Control-Allow-Origin "*";
}
```

`ur-refresh-run.sh` 只往这里复制 `report.md` 和 `state.json`。

`mihomo.yaml` / `nodes.txt` 含 `auth_token`，不放进静态目录。
含凭据的配置只走 `/s/<token>/` 鉴权通道，用 `tools/check_config.py` 事后核查。

---

## 六、验证

```bash
# 1. 手动跑一次（绕过 due 检查）
sudo systemd-run --unit=ur-refresh-once --property=Type=oneshot \
  --property=User=subsvc --property=Group=subsvc \
  --property=WorkingDirectory=/opt/private-subscription/apps/subscription-server \
  --property=EnvironmentFile=/etc/private-subscription/service.env \
  --property=ReadWritePaths="/var/lib/private-subscription/cache \
    /var/lib/private-subscription/state/subscription-server \
    /var/lib/private-subscription/logs /opt/ur-sub /var/www/ur" \
  /usr/local/bin/node /opt/private-subscription/apps/subscription-server/src/refresh-provider.mjs urnetwork

# 2. 看结果
journalctl -u ur-refresh-once -n 5 --no-pager | grep -oE '\{"ok".*\}'

# 3. 看缓存
ls -la /var/lib/private-subscription/cache/urnetwork*

# 4. 订阅能否下载
TOK=$(cat /var/lib/private-subscription/read-tokens/urnetwork)
curl -sk -o /dev/null -w "%{http_code} %{size_download}B\n" \
  "https://<你的域名>/s/$TOK/urnetwork.yaml"

# 5. 面板 status
curl -s http://127.0.0.1:3100/admin/api/status | \
  python3 -c 'import sys,json;d=json.load(sys.stdin);print(json.dumps(d["providers"]["urnetwork"],ensure_ascii=False,indent=2))'
```

**面板点 Refresh 的 API**（需要特殊 header，前端会自动带）：

```bash
curl -X POST -H "X-Admin-Action: private-subscription-admin" \
  http://127.0.0.1:3100/admin/api/refresh/urnetwork
```

---

## 七、排错对照表

| 现象 | 原因 | 修法 |
|---|---|---|
| 面板显示 `undefined` | `admin.html` 的 `names` 没加新 provider | 加映射 |
| `/s/<tok>/urnetwork.yaml` 返回 500 | `tokenStores` 缺这一项，`.get()` 抛异常 | `server.mjs` 加一行 |
| 点 Refresh 报 Read-only | 父服务沙箱没加路径 | 改 `private-subscription.service` |
| 订阅只有 7 个键、无策略组 | 没调 `applyRoutingTemplate` | 补模板调用 |
| 国家组里全是美国节点 | 节点名不满足 `\bCC\b` | 改成 `CC-NN-...` |
| 客户端全部 timeout | 用了 http/https 协议 | 换 SOCKS5 |
| 刚刷新完就全失效 | 用了签发模式，槽位被删 | 换 `ur_scan.py` |
| `too slow` 很多 | `--max-probe-ms` 太严 | 提到 25000 |
| 国家显示 `??` | ip-api 限流 | 用 `geo_batch.py` 批量查 |
| 日志写不进去 | 日志路径不在沙箱白名单 | 放 `/var/lib/.../logs` |
