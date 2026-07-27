# nftables 端口转发 WebUI

这是一个基于 `nft.sh` 转发逻辑实现的 Web 管理面板。它将规则写入 `/etc/nftables.d/port-forward.conf`，通过 nftables 实现 TCP + UDP 的 DNAT/SNAT 转发，并开启 IPv4 forwarding。

## 主要功能

- 设计化登录页、会话保护、CSRF 防护与登录限流。
- 两种角色：
  - **管理员**：查看、添加及删除所有规则；将规则指派给任何启用用户；管理用户和查看审计日志。
  - **普通用户**：只能查看、添加和删除自己的规则。
- 对监听端口、目标端口、IPv4 地址做严格校验；所有系统命令均通过参数数组执行，**不会**拼接 shell 命令。
- 修改前使用 `nft -c -f` 校验规则；规则文件原子写入，并在加载失败时尝试恢复上一个备份。
- 兼容原脚本生成的已有规则：首次初始化数据库时会读取该配置并归属给首位管理员。
- 支持每用户规则数、监听端口范围、月流量额度与重置时间，以及每条规则的双向带宽限制；管理员可校准用户本周期已用流量。
- 后台约每 2 秒结算 nftables 计数器并更新实时带宽；TCP 连接延迟检测保持不变，但移到后台执行，Web 请求只读取缓存结果。
- 提供 SSH 备用命令 `nfpctl.py` / `nft.sh`，WebUI 出问题时仍可在 VPS 上查看、新增、删除和清空转发规则。
- 同步 firewalld、UFW 或 iptables 的放行/清理规则；失败任务会持久化并由后台自动重试，同时保留提示与审计信息。

## 架构与权限

```mermaid
flowchart LR
  B[浏览器] -->|HTTPS 反向代理| W[Flask / Gunicorn]
  W --> A[SQLite 用户、规则与审计]
  W -->|参数化命令| N[nftables / sysctl / 防火墙]
  S[SSH: nfpctl / nft.sh] --> A
  S -->|参数化命令| N
  N --> C[/etc/nftables.d/port-forward.conf]
```

规则数据库是面板和 SSH 备用工具的唯一数据源，配置文件是它生成的运行产物。不要在面板运行时手工编辑 `port-forward.conf`；需要迁移已有规则时，先在首次启动前保留该文件，面板会自动导入。

## 在 Linux 服务器部署

想把项目托管到 GitHub 并通过交互式安装器一键部署，请先阅读 [`GITHUB_DEPLOY.md`](GITHUB_DEPLOY.md)。该方案会在 GitHub Actions 中运行测试、生成 Release 压缩包和 SHA-256 校验文件，安装时询问域名、证书邮箱和管理员凭据。远程安装必须固定到明确的 Release 标签，下载脚本后先完整审阅，再以 root 执行；不要从可变的 `main` 分支直接下载并运行安装脚本。

下面以 Debian/Ubuntu 为例。面板需要 root 运行，因为 nftables、sysctl 和防火墙规则只能由 root 修改。服务仅监听 `127.0.0.1:8108`，请用 Nginx/Caddy 终止 HTTPS 并反向代理，**不要直接暴露 Gunicorn 端口到公网**。

```bash
sudo apt update
sudo apt install -y nftables python3 python3-venv nginx

sudo install -d -m 0755 /opt/nft-forward-panel
sudo cp -a . /opt/nft-forward-panel/
cd /opt/nft-forward-panel
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

sudo openssl rand -hex 32
sudo install -m 0600 /dev/null /etc/nft-forward-panel.env
sudoedit /etc/nft-forward-panel.env
```

填写 `/etc/nft-forward-panel.env`，示例见 [`.env.example`](.env.example)：

```ini
PANEL_SECRET_KEY=上一步生成的随机值
PANEL_ADMIN_USERNAME=admin
PANEL_ADMIN_PASSWORD=至少12位的强密码
PANEL_COOKIE_SECURE=1
PANEL_TRUSTED_PROXY_COUNT=1
```

请确认 `/etc/nftables.conf` 已存在；面板会安全地补上 `include "/etc/nftables.d/*.conf"`，但为了避免覆盖已有防火墙，绝不会创建或清空主配置文件。首次添加规则时会自动建立 `/etc/nftables.d/port-forward.conf`，并将 `net.ipv4.ip_forward=1` 写入 `/etc/sysctl.d/99-nft-forward.conf`。

```bash
sudo install -m 0755 nft.sh nfpctl.py /opt/nft-forward-panel/
sudo ln -sf /opt/nft-forward-panel/nft.sh /usr/local/sbin/nfpctl
sudo cp deploy/nft-forward-panel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nft-forward-panel
sudo systemctl status nft-forward-panel
```

Nginx 最小反代示例（另行配置证书）：

```nginx
server {
    listen 443 ssl http2;
    server_name panel.example.com;
    # ssl_certificate / ssl_certificate_key 省略
    location / {
        proxy_pass http://127.0.0.1:8108;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

部署后访问 `https://panel.example.com`，使用环境文件中的首次管理员账号登录。首次启动后，请从环境文件中移除 `PANEL_ADMIN_PASSWORD`；该值之后不会再被读取。

## SSH 备用管理

WebUI 或反向代理不可用时，可以通过 SSH 使用同一套后端管理规则。命令需要 root 权限，会读取 `/etc/nft-forward-panel.env` 中的路径配置，默认操作 `/var/lib/nft-forward-panel/panel.db` 和 `/etc/nftables.d/port-forward.conf`。

```bash
sudo nfpctl list
sudo nfpctl status
sudo nfpctl add --port 2443 --target 8.8.8.8 --target-port 443 --owner admin
sudo nfpctl remove 1 --yes
sudo nfpctl clear --yes
sudo nfpctl menu
```

`nfpctl` 与 WebUI 共用 SQLite 数据库、审计日志、端口/IP 校验、规则渲染、`nft -c -f` 校验、文件锁和失败回滚。不要再使用会直接编辑 `/etc/nftables.d/port-forward.conf` 的旧脚本并行管理规则，否则 WebUI 下次保存时会覆盖那些外部改动。

附件中的旧 `nft.sh` 交互体验已融合到新的 `nft.sh` 包装器和 `nfpctl.py` 中；旧脚本里会安装软件包、`flush ruleset`、备份并接管 `/etc/nftables.conf` 的高风险安装流程没有合入。生产初始化仍建议由系统包管理器和本项目的 systemd/Nginx 步骤完成。

## 开发与检查

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export PANEL_SECRET_KEY="$(openssl rand -hex 32)"
export PANEL_ADMIN_USERNAME=admin
export PANEL_ADMIN_PASSWORD='change-this-to-a-strong-password'
export PANEL_DATA_DIR="$(pwd)/.data"
flask --app app run --debug

python3 -m unittest discover -s tests -v
```

在非 Linux 或非 root 开发环境中可测试登录、权限与输入校验；真正加载 nftables 规则时会明确提示权限或系统命令问题。

## 运行限制与建议

- 仅支持 IPv4 转发；与原脚本一致，每条规则同时处理 TCP 和 UDP。
- 不要同时让其他脚本管理同一个 `ip port_forward` 表或这个配置文件。
- 面板不会安装 nftables，也不会执行 `flush ruleset`，避免破坏服务器既有防火墙。
- 请将面板放到 HTTPS、强密码和必要时 VPN/内网访问之后；管理员可查看所有规则和审计数据。
