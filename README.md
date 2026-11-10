# nftables 端口转发面板（Mochi Forward）

[![Release](https://github.com/CinnieMochi/nft-forward-panel/actions/workflows/release.yml/badge.svg)](https://github.com/CinnieMochi/nft-forward-panel/actions/workflows/release.yml)

给一台 Linux VPS 加一个 Web 控制台，管理 nftables 的 **TCP + UDP 端口转发**：多用户、配额、限速、审计、实时流量监控，全部由一份 SQLite 数据库驱动。它在内部复刻了经典 `nft.sh` 的转发逻辑（DNAT + SNAT），但不再靠手工编辑 nftables 配置文件。

- 数据源唯一：数据库是真相，`/etc/nftables.d/port-forward.conf` 只是它渲染出来的运行产物。
- 改动可回滚：写入前先 `nft -c -f` 校验，失败自动还原配置文件和上一份备份，线上规则不受影响。
- 只用参数数组调用系统命令，全程不拼接 shell；所有规则改动都走事务锁 + 原子写入。
- 绝不 `flush ruleset`，也绝不创建或清空你的 `/etc/nftables.conf`。

---

## 一键安装（推荐）

**前置条件**：Debian 11/12 或 Ubuntu 22.04/24.04，root 权限，一个已解析到该 VPS 的域名，云安全组放行 **TCP 80 / 443**（面板本身只监听 `127.0.0.1:8108`，不需要也不应该对外开放）。

```bash
# 1. 下载安装器
curl -fsSL https://raw.githubusercontent.com/CinnieMochi/nft-forward-panel/v1.3.0/install.sh -o /tmp/nfp-install.sh

# 2. 先审阅，再执行（不要 curl | bash）
less /tmp/nfp-install.sh

# 3. 安装：全程交互式，先选语言，再选访问方式
sudo bash /tmp/nfp-install.sh
```

仓库名和版本已有默认值（`--version` 默认装 **latest**，即最新 Release），所以不必再传 `--repo`。

### 第一步：选择语言

安装器会先问 `选择语言 / Choose language [1=中文 2=English]`，之后的**所有提示**（域名、证书邮箱、管理员账号密码、进度、报错）都跟着这个选择走。也可以直接用参数跳过：`--lang zh` 或 `--lang en`。

### 第二步：选择访问方式

| 选项 | 说明 | 对你机器的影响 |
|---|---|---|
| **1 = 域名 + HTTPS**（默认） | Caddy 自动申请并续期证书，面板只监听 `127.0.0.1:8108` | **不额外开放任何 HTTP 端口**；公网只需 80/443（云安全组） |
| **2 = IP + 端口 HTTP** | 不用域名，面板直接监听 `0.0.0.0:<端口>`（默认 8108） | 安装器会**自动在本机防火墙放行该端口**；若没有本机防火墙会提示你去云安全组放行 |

> ⚠️ **HTTP 模式是明文传输**：账号密码、Cookie、流量数据都不加密。仅在可信网络或你已有额外防护（VPN、内网、前置反代）时使用。安装器会明确警告。

### 以后从 HTTP 换成域名 HTTPS

重新运行同一个脚本、选 `1` 即可，安装器会：

1. 保留数据库、账号、规则、流量记录；
2. 把面板改成只监听 `127.0.0.1:8108`，并启用 `PANEL_COOKIE_SECURE=1`；
3. 写入 Caddy 站点并申请证书；
4. **关闭原来那个公网 HTTP 端口**（本机防火墙同步移除）。

### 一键卸载

```bash
sudo bash /tmp/nfp-install.sh --uninstall          # 交互确认；默认保留数据库
sudo bash /tmp/nfp-install.sh --uninstall --purge  # 连数据库与头像一起删除
sudo bash /tmp/nfp-install.sh --uninstall --keep-rules --yes   # 保留 nftables 转发表、跳过确认
```

卸载会停止并禁用服务、移除 systemd unit 与 `nfpctl` 链接、删除 Caddy 站点、关闭 HTTP 模式的公网端口、把环境文件备份到 `/root` 后删除，并**默认删除 `ip port_forward` 表**（即停止所有转发；加 `--keep-rules` 可保留）。数据目录默认保留。

安装完成后终端会打印访问地址，浏览器打开 `https://<你的域名>` 即可登录。证书由 Caddy 自动申请和续期。

### 全自动（无交互）安装

适合脚本化或重复部署。密码只通过环境变量传入，不会进 shell 历史：

```bash
curl -fsSL https://raw.githubusercontent.com/CinnieMochi/nft-forward-panel/v1.3.0/install.sh -o /tmp/nfp-install.sh
sudo NFP_ADMIN_PASSWORD='至少12位的强密码' bash /tmp/nfp-install.sh \
  --non-interactive \
  --domain panel.example.com \
  --email you@example.com \
  --admin admin
```

### 安装器做了什么

1. `apt-get` 安装 `nftables`、`conntrack`、`python3-venv`、`sqlite3`、`caddy` 等依赖（Caddy 缺失时自动添加官方 apt 源）。
2. 解压 Release 压缩包到 `/opt/nft-forward-panel`，用 `pip check` 校验 Python 依赖，建立独立 venv。
3. **校验发布包 SHA-256**：从 GitHub Release API 读 asset 的 `digest`，与本地 `sha256sum` 比对，不一致直接中止。
4. 建 `/var/lib/nft-forward-panel`（0700）与 `/etc/nft-forward-panel.env`（0600），自动生成 `PANEL_SECRET_KEY`。
5. 给 `/etc/nftables.conf` 补 `include "/etc/nftables.d/*.conf"`，并用 `nft -c -f` 校验；失败会还原原文件后退出。
6. 安装 systemd 服务并启动，然后**轮询 `/healthz` 最多 30 秒**确认面板真的应答了才继续。
7. 写入 Caddy 站点配置（`/etc/caddy/conf.d/nft-forward-panel.caddy`），`caddy validate` 失败会还原原 `Caddyfile`。
8. 首次安装成功后，自动把 `PANEL_ADMIN_PASSWORD` 从环境文件里删掉——它此后再也不会被读取。

### 更新

重复执行安装器并填入**同一个域名**即可。升级会保留 `/var/lib/nft-forward-panel` 里的数据库和头像，并在覆盖前备份旧程序目录、环境文件、`/etc/nftables.conf` 和 `Caddyfile`。

```bash
curl -fsSL https://raw.githubusercontent.com/CinnieMochi/nft-forward-panel/v1.3.0/install.sh -o /tmp/nfp-install.sh
less /tmp/nfp-install.sh
sudo bash /tmp/nfp-install.sh --version v1.3.0
```

> 固定 `--version` 到明确的 Release 标签，不要跟着可变的 `main` 分支走。

---

## 手动安装（自己负责 Nginx / 证书）

不想让安装器接管 Caddy 时：

```bash
sudo apt update && sudo apt install -y nftables conntrack python3 python3-venv
sudo install -d -m 0755 /opt/nft-forward-panel
sudo cp -a . /opt/nft-forward-panel/
cd /opt/nft-forward-panel
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

sudo openssl rand -hex 32          # 生成 PANEL_SECRET_KEY
sudo install -m 0600 /dev/null /etc/nft-forward-panel.env
sudoedit /etc/nft-forward-panel.env   # 参考 .env.example 填写

sudo install -m 0755 nft.sh nfpctl.py /opt/nft-forward-panel/
sudo ln -sf /opt/nft-forward-panel/nft.sh /usr/local/sbin/nfpctl
sudo cp deploy/nft-forward-panel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nft-forward-panel
```

Nginx 反代最小示例（证书另配）：

```nginx
server {
    listen 443 ssl http2;
    server_name panel.example.com;
    # ssl_certificate / ssl_certificate_key ...
    location / {
        proxy_pass http://127.0.0.1:8108;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

记得保持 `PANEL_TRUSTED_PROXY_COUNT=1`（单层反代）。

---

## 环境变量

系统会读 `/etc/nft-forward-panel.env`（systemd `EnvironmentFile`），完整样例见 [`.env.example`](.env.example)。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PANEL_SECRET_KEY` | 无（必填） | 会话签名密钥，缺失时拒绝启动。`openssl rand -hex 32` |
| `PANEL_ADMIN_USERNAME` | `admin` | 仅在数据库无任何用户时用于创建首个管理员 |
| `PANEL_ADMIN_PASSWORD` | 无 | 同上；首次成功后应从环境文件删除 |
| `PANEL_DATA_DIR` | `/var/lib/nft-forward-panel` | 数据库与头像目录 |
| `PANEL_FORWARD_CONFIG` | `/etc/nftables.d/port-forward.conf` | 面板渲染的规则文件（运行产物） |
| `PANEL_MAIN_CONFIG` | `/etc/nftables.conf` | 主配置，仅用于补 include 行，不会被创建或清空 |
| `PANEL_SYSCTL_CONFIG` | `/etc/sysctl.d/99-nft-forward.conf` | 写入 `net.ipv4.ip_forward=1` |
| `PANEL_COOKIE_SECURE` | `1` | 是否给 Cookie 加 `Secure`；纯 HTTP 调试时设 `0` |
| `PANEL_BIND` | `127.0.0.1:8108` | gunicorn 监听地址。域名 HTTPS 模式固定为 `127.0.0.1:8108`；IP+端口 HTTP 模式为 `0.0.0.0:<端口>` |
| `PANEL_TRUSTED_PROXY_COUNT` | `0` | 信任的反代层数，`0` 表示完全忽略 `X-Forwarded-*` |
| `PANEL_POLICY_SCHEDULER` | `1` | 是否启用 2 秒一次的对账线程，`0` 会让配额/到期/端口范围失效 |
| `PANEL_LOGIN_WINDOW_SECONDS` | `600` | 登录限流窗口 |
| `PANEL_LOGIN_MAX_ATTEMPTS` | `20` | 窗口内允许的失败次数 |
| `PANEL_TIMEZONE` | `Asia/Shanghai` | 月流量周期与日期显示所用时区（IANA 名称） |
| `PANEL_UPDATE_REPO` | `CinnieMochi/nft-forward-panel` | 在线更新时拉取 Release 的仓库，改动后只接受该仓库发布且摘要匹配的包 |

---

## 账号与权限

- **管理员**：查看/新增/修改/删除**所有**规则；把规则指派给任意启用用户；管理用户策略；查看审计日志。
- **普通用户**：只能看到和操作自己的规则；限速继承账号默认值，不能自选端口范围外的端口。

每个账号有：规则上限、允许端口区间、月流量额度（GiB）、本周期已用流量（可被管理员校准）、到期时间、月流量重置日/时间、入口地址、默认双向限速。

规则被暂停的四种原因，优先级从高到低：

1. `disabled` — 账号被停用
2. `expired` — 超过到期时间
3. `quota` — 月流量超额
4. `port_range` — 监听端口落在该账号允许范围之外

暂停**不会删除**规则：nftables 里摘掉、数据库里标记，条件恢复后自动重新加载。

---

## 流量口径与限速

- 每条规则同时转发 TCP 和 UDP，只支持 IPv4。
- 计费口径是**网卡方向的 RX + TX 合计**：`wire_prerouting`(-90) / `wire_postrouting`(+300) 两条 filter 链分别统计物理入向和出向字节。NAT 链只看每个连接的首包，所以逻辑方向另外记在 `forwarding`(+20) 钩子跳转的 `nfp_in_*` / `nfp_out_*` 链里。
- 每 5 分钟聚合进 `traffic_buckets`，保留 60 天。
- 限速用 `limit rate over N kbytes/second drop`，N = Mbps × 125，进出方向各自独立。
- 老版本写入的历史桶只有 `inbound/outbound` 没有 `rx/tx`，计费时会按行回退到逻辑口径，升级不会清空当期已用流量。

---

## 版本信息与在线更新

左下角的「控制台运行中」右侧显示**当前版本号**，点一下可以：

- 看到**当前版本**和 GitHub 上的**最新版本**（只有一个发布渠道，不区分稳定/开发版）
- 点「检查最新版」实时查询（服务端向 GitHub Releases API 查询，结果缓存 15 分钟）
- **管理员**在有新版本时可以直接点「立即更新」

在线更新的实现和边界：

1. 只从 `PANEL_UPDATE_REPO`（默认本仓库）的 **Release** 取包，不接受任意 URL。
2. 下载后必须与 GitHub 报告的 asset **SHA-256 摘要**一致，否则中止。
3. 由 `deploy/self-update.sh` 以独立 systemd 临时单元（`systemd-run`）执行——面板重启不会杀掉更新进程。
4. 更新前把当前 `/opt/nft-forward-panel` 整目录备份为 `.backup-<时间戳>`（只保留最近两份），然后覆盖程序文件、补齐 venv 依赖、重启。
5. **健康检查不通过会自动回滚**旧版本并重启，状态写入 `/var/lib/nft-forward-panel/update.status`，前端实时显示进度。
6. 数据库不在覆盖范围内，升级不会改动你的账号、规则与流量记录。

> 只有管理员能触发；每次触发都会写审计（`panel_update`）。如果服务器出不了网，用安装器或 SSH 手动升级。

## 界面小工具

- **隐藏地址与端口**：概览页「入口地址」右侧的**眼睛按钮**（睁眼 = 明文，闭眼 = 已隐藏），一键把**入口地址**、规则表的**监听端口**和**目标地址**换成占位符；状态记在浏览器本地，刷新后保持。适合截图、录屏或共享屏幕。
- **用户管理**：管理员在「用户与策略」每行有三个入口——**策略**、**编辑**（用户名 / 邮箱，身份 ID 不可改）、**⋯ 更多**（修改密码 / 停用启用 / 删除账户），避免一排按钮挤在一起。
- **修改密码**：新密码支持**眼睛切换明文**、**一键复制**、**生成 20 位随机强密码**，提交前有二次确认，提交后该账户的所有登录会话立即失效。
  > 当前密码**无法查看**：数据库里只存单向哈希（Werkzeug PBKDF2），任何人包括管理员都不可逆推。这是刻意的安全设计，也是"忘记密码只能重置"的原因。
- **实时连接页**：每条规则的**备注**会一并显示，方便辨认是哪条业务；备注在这里**只读**，要改请去「概览与规则」。

## 流量包（临时额度）

计费周期内额度不够时，管理员可以在「用户与策略」的**本期流量**列点「流量包」追加临时额度：

- 一个账户可以加**多个**包，列表里每个包显示大小、已用量与到期时间；**用完的包标红「已用尽」**。
- 生命周期与账单周期一致：**到该账户的月流量重置时刻，所有流量包自动失效**，下个周期只剩策略里配置的额度——无论有没有用完。
- 消耗顺序是确定的：先扣基础额度，再按添加顺序依次消耗流量包。因此每个包的"已用"是真实可核算的，不会随机的把用量摊到多个包上。
- 包可以随时删除，删除后额度立即收回；增删都会写审计（`package_add` / `package_delete`）。
- 「不限量」账户（月额度为 0）不允许加包——它本来就不受限。

可用额度 = 基础月额度 + 本周期内所有流量包之和，概览页的额度进度环与暂停判定都按这个口径。

## SSH 备用管理（nfpctl）

WebUI 或反代挂掉时，用同一套后端继续管理。需要 root，读 `/etc/nft-forward-panel.env` 的路径配置，与 WebUI **共用数据库、审计、校验、渲染、文件锁和回滚**。

```bash
sudo nfpctl list
sudo nfpctl status
sudo nfpctl add --port 2443 --target 8.8.8.8 --target-port 443 --owner admin
sudo nfpctl remove 1 --yes
sudo nfpctl clear --yes
sudo nfpctl menu        # 交互式菜单
```

`nft.sh` 只是 `nfpctl.py` 的包装器（`/usr/local/sbin/nfpctl` 软链到它）。

> 不要再让别的脚本并行编辑 `port-forward.conf` 或接管 `ip port_forward` 表，否则面板下次保存时会覆盖那些外部改动。

---

## 架构

```mermaid
flowchart LR
  B[浏览器] -->|HTTPS| R[Caddy / Nginx]
  R --> W["Flask + Gunicorn<br/>127.0.0.1:8108"]
  W --> A[("SQLite panel.db")]
  S["SSH: nfpctl / nft.sh"] --> A
  W -->|参数化命令| N["nftables / sysctl / firewalld · ufw · iptables"]
  S -->|参数化命令| N
  N --> C["/etc/nftables.d/port-forward.conf"]
  W -.-> T1["对账线程 2s"]
  W -.-> T2["可观测线程 2s"]
```

两个后台守护线程：

| 线程 | 周期 | 职责 |
| --- | --- | --- |
| 策略对账 | 2 s | 结算流量 → 重算每条规则的暂停原因 → 仅在有变化时重建 nft 表 → 同步防火墙 → 写审计 |
| 实时可观测 | 2 s | conntrack 连接快照 + 线程池 TCP 延迟探测，结果进内存缓存 |

HTTP 请求只读缓存，不做任何 conntrack 调用或 socket 探测——这是面板能 2 秒刷一次还不卡住的原因。

---

## 开发与测试

要求 Python **3.10+**（CI 用 3.12）。测试不依赖 root、nftables 或 Linux：`nft`、`conntrack`、防火墙命令在用例里都被 mock 掉了。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

PYTHONPATH=. python3 -m unittest discover -s tests -v
```

本地起开发服务器：

```bash
export PANEL_SECRET_KEY="$(openssl rand -hex 32)"
export PANEL_ADMIN_USERNAME=admin
export PANEL_ADMIN_PASSWORD='change-this-to-a-strong-password'
export PANEL_DATA_DIR="$(pwd)/.data"
export PANEL_POLICY_SCHEDULER=0      # 开发时关掉对账线程
flask --app app run --debug
```

在非 Linux 或非 root 环境下登录、权限、校验、配额逻辑都能跑；真正加载 nftables 规则时会明确提示权限或缺少系统命令，这是预期行为。

健康检查端点：`GET /healthz` → `{"status":"ok"}`，无需登录，不碰数据库。

---

## 发布流程

```bash
git tag -a v1.3.0 -m "v1.3.0"
git push origin v1.3.0
```

推送 `v*` 标签会触发 [`.github/workflows/release.yml`](.github/workflows/release.yml)：跑完整测试 → `bash -n` 检查安装器 → 打包并生成 SHA-256 → **校验包内布局**（必需文件齐全、不含 `.git`/`.github`）→ 创建 GitHub Release 并附上两个资产。

**任何一步失败都不会发布 Release**，这样安装器下载到的包至少是通过测试的。详细步骤见 [`GITHUB_DEPLOY.md`](GITHUB_DEPLOY.md)。

---

## 故障排查

```bash
systemctl status nft-forward-panel caddy nftables
journalctl -u nft-forward-panel -n 100 --no-pager
curl -fsS http://127.0.0.1:8108/healthz
nft list table ip port_forward
sudo nfpctl status
```

| 现象 | 排查方向 |
| --- | --- |
| 安装器报 `panel did not answer /healthz` | 看 `journalctl`；多半是 `PANEL_SECRET_KEY` 缺失或 venv 依赖没装好 |
| 面板起来但规则不生效 | `nft list table ip port_forward` 是否为空；`sysctl -n net.ipv4.ip_forward` 是否为 1 |
| 加了规则但连不上 | 云安全组是否放行该监听端口；本机防火墙是否真的放行（看审计里的警告） |
| 账号到期/超额后规则还在转发 | `PANEL_POLICY_SCHEDULER` 是否被设成 0 |
| 登录一直被限流 | 清 `login_attempts` 表，或调大 `PANEL_LOGIN_WINDOW_SECONDS`/`PANEL_LOGIN_MAX_ATTEMPTS` |
| 忘记管理员密码 | `sudo nfpctl` 不处理密码；用 sqlite 改 `users.password_hash` 或重建账号 |

---

## 安全说明与限制

- 面板以 root 运行（nftables、sysctl、防火墙都需要 root），因此**必须**放在 HTTPS 之后；systemd unit 已启用 `ProtectSystem=full`、`ProtectHome`、`PrivateTmp` 和 `ReadWritePaths` 白名单。
- 会话 Cookie：`HttpOnly` + `SameSite=Lax` + 可选 `Secure`；全站 CSRF；登录限流；改密或停用账号会让旧会话立即失效（`session_version`）。
- 响应头带 CSP（`default-src 'self'`）、`X-Frame-Options: DENY`、`nosniff`、`Referrer-Policy`，HTTPS 时加 HSTS。
- 只支持 **IPv4**，每条规则固定同时转发 TCP 和 UDP。
- 面板不会安装 nftables、不会 `flush ruleset`、不会新建 `/etc/nftables.conf`——它只接管自己那个 `ip port_forward` 表。
- 多副本不适合横向扩展：设计方案就是「单机 + 单库 + 文件锁」。

---

## 版本

### v1.3.0

- 新增：安装器支持**中英双语**（`--lang zh|en`，交互第一步选择），全部提示、进度与报错跟随所选语言。
- 新增：安装时可选**两种访问方式**——域名 + HTTPS（Caddy 自动证书，不开放额外 HTTP 端口），或 IP + 端口 HTTP（自动在本机防火墙放行该端口，并明确警告明文风险）。
- 新增：**访问方式迁移**。HTTP 模式以后再运行一次脚本选 HTTPS，安装器会保留数据、改用 127.0.0.1 监听、写入 Caddy 站点并**关闭原来的公网 HTTP 端口**。
- 新增：**一键卸载** `--uninstall`（可选 `--purge` 删数据、`--keep-rules` 保留转发表、`--yes` 跳过确认）：停服务、删 unit 与 nfpctl 链接、删 Caddy 站点、关闭 HTTP 端口、备份并删除环境文件。
- 改进：新增 `deploy/serve.sh` 作为启动器，监听地址由 `PANEL_BIND` 决定，同一份 systemd unit 适配两种模式。
- 改进：安装器现在会**保留环境文件里你自定义的键**（此前重写会丢，有备份），P8 顺带解决。
- 改进：HTTP 模式不安装 Caddy；HTTPS 模式才安装。
- 测试：75 个（新增 5 个安装器用例，覆盖双语帮助、参数完整性、消息切换与校验器；CI 上运行，Windows 缺 bash 时自动跳过）。

### v1.2.9（安全与健壮性修复，建议所有部署升级）

- **修复（重要）**：在线更新后程序文件属主继承发布包里的 CI uid，而面板以 root 运行 → 本机一旦出现同 uid 账号就可能改写程序取得 root。现在升级覆盖后与备份目录都会 `chown -R root:root`。
- 修复：更新状态加 10 分钟陈旧判定。此前更新中途断电/被杀会把状态留在 `installing`/`restarting`，之后永久提示「已有更新任务正在进行」，只能手工改文件。
- 修复：拒绝把面板"更新"到**不高于当前**的版本（此前可退回任意旧版本）。
- 修复：流量桶 60 天清理移出采样热路径，零转发规则的面板也会按时清理。
- 修复：过期流量包在启动时清理（120 天），不再无限累积。
- 修复：流量包备注复用规则备注校验（≤80 字符、拒绝换行与控制字符），不再允许塞多行。
- 加固：在线更新不再 `source` 由发布 API 生成的 shell 片段，改为读取固定字段，并校验下载地址必须属于 `github.com`。
- 改进：更新时若 systemd unit 有新版本，写入 `.service.new` 并给出提示，**不自动覆盖**（避免冲掉你的本地改动）。
- 测试：70 个（新增 4 个：陈旧状态判定、降级拒绝、忙碌拒绝、升级脚本不变量）。

### v1.2.8

- 改名：概览页「本月总流量」→**「本期总流量」**；「流量统计」页的「本月流量」→**「本期流量」**，与用户表的「本期流量」统一。
- 无障碍标签同步改为「本期已使用 …」，读屏软件读到的口径与界面一致。

### v1.2.7

- 新增：概览页「本月总流量」卡片在**有流量包时**显示蓝色「流量包」胶囊，点击直接查看本周期所有包的大小、已用量、到期时间与状态；没有流量包时不显示。
- 校验：追加流量包后该卡片的**额度分母同步增加**、百分比自动下降、环形弧长按新比例重绘。实测 1000 GiB 额度 + 100 GiB 用量（10%、弧长 7.50）追加 1000 GiB 后变成 1.95 TB 额度、5%、弧长 3.75。
- 修复：≤1400px 窄屏下卡片数值被环形图压住的既有响应式问题（环形缩到 58px、右内边距收紧）。
- 文档：更正 v1.2.6 里关于「本月流量」改名的表述——只改了用户表列头。

### v1.2.6

- 新增：**流量包**。管理员可在「用户与策略」的「本期流量」列给账户追加临时额度，一个账户可加多个，每包单独显示用量与到期时间，用完标红「已用尽」。
- 规则：流量包**只在本计费周期有效**，到该账户的重置时刻自动失效，下个周期仍然只有策略里配置的额度；额度按「基础额度先用完，再按添加顺序消耗流量包」计算，因此每个包的用量是确定的。
- 新增：流量包可随时删除（额度立即收回），增删都会写审计（`package_add` / `package_delete`）。
- 限制：月流量为「不限量」的账户不允许添加流量包（没有意义），接口会直接拒绝。
- 改名：用户表里的「本月流量」列改为**「本期流量」**（概览卡片仍叫「本月总流量」）。

### v1.2.5

- 修复：概览页「入口地址」的数值字体与「规则用量 / 可用端口 / 使用到期」不一致（新增的遮罩 span 命中了标签的 11px 灰色规则）。现在四项数值统一为 14px / `#344258` / 700。
- 修复：「实时连接」的备注只显示字母（HK/UA/TW）而不显示旗帜。动态渲染的备注现在也会走 twemoji，统一使用仓库内置的 `static/vendor/twemoji` 资源，不依赖任何外部 CDN。
- 改进：「修改密码」从「⋯ 更多」移入**编辑账户**对话框，与用户名/邮箱在同一张卡片里分成两段：保存资料 / 重置密码。
- 改进：操作菜单精简为「停用账户 / 删除账户」。

### v1.2.3

- **修复 v1.2.2 的样式表回归**：发布包里的 `static/css/app.css` 被截断（50,061 → 22,905 字节，多行卡在 2034 字符），导致全站样式失效、SVG 尺寸失控。现已从 v1.2.1 逐字节恢复，v1.2.2 的功能改动全部保留。
- 设计语言未改动：样式表前 50,061 字节与 v1.2.1 **哈希一致**，新增规则只针对新组件（眼睛按钮、操作菜单、密码输入行、备注行），仅调整了操作列宽度（100 → 170px）以容纳新的三控件布局。
- 教训：不要用"读全文 → 改 → 写回"的方式编辑单行超长的压缩文件（`read` 会截断超长行）。

### v1.2.2

- 修复：概览页「入口地址」的 IP 不会被隐藏开关遮住；复制与显示/隐藏两个按钮尺寸不一致。
- 改进：显示/隐藏换成**睁眼 / 闭眼**图标，与密码输入框的显隐一致。
- 改进：「连接统计」更名为**「实时连接」**，更贴近实际含义。
- 新增：「实时连接」列表展示每条规则的**备注**（只读），备注仍在「概览与规则」中修改。
- 改进：「用户与策略」操作列由 4 个平铺按钮改为 **策略 / 编辑 / ⋯更多**，菜单里是修改密码、停用启用、删除账户。
- 新增：管理员可为账户**重置密码**——眼睛切换明文、复制、生成随机强密码、二次确认；服务端也校验两次输入一致。
- 改进：版本对话框不再常驻显示"已是最新版本"，只在点「检查最新版」后提示 4 秒。

### v1.2.1

- 新增：左下角显示当前版本号，点开可查看**最新版本**并由管理员**一键在线更新**（摘要校验 + 自动回滚 + 审计）。
- 新增：概览页可一键**隐藏/显示监听端口与目标地址**，适合录屏或截图时脱敏。
- 新增：用户管理支持**编辑账户资料**（用户名 / 邮箱）与**删除账户**，含自我保护与最后管理员保护。
- 修复：README 里的 mermaid 架构图在 GitHub 上无法渲染（未加引号的标签与 `&` 连线语法）。
- 依赖：新增 `deploy/self-update.sh`，安装器与发布包都会带上它。

### v1.2.0

- 修复：升级后当前计费周期的已用流量被清零（老桶只有 `inbound/outbound`，新口径只算 `rx/tx`）。现在按行回退，升级不清零额度。
- 新增：`GET /healthz` 健康检查端点，安装器用它判断面板是否真的就绪。
- 新增：`PANEL_TIMEZONE` 环境变量（默认 `Asia/Shanghai`），缺 tz 数据库时不再导致启动崩溃。
- 修复：测试套件长期红灯——3 个用例与实现脱节，且 CI 注入的 `PANEL_ADMIN_PASSWORD` 会覆盖测试夹具的管理员口令，导致 13 个用例连锁失败。现在夹具自己钉住身份，环境不再能改变它。
- 依赖：补 `tzdata`，Windows/macOS 上也能跑测试。
- 安装器：仓库名和版本有了默认值（不再强制 `--repo`）；就绪检测从「sleep 2 + curl /」改为轮询 `/healthz` 最多 30 秒。
- 流水线：Actions 锁定 commit SHA，打包后校验 SHA-256 与包内布局，校验失败不出 Release。

### v1.1.0

- 网卡方向（RX/TX）计费、实时带宽、连接数与延迟监控、按规则双向限速。
