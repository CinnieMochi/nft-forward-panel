# GitHub 托管与一键安装

## 1. 创建仓库

在 GitHub 创建空仓库，例如 `YOUR_NAME/nft-forward-panel`。不要勾选自动创建 README、许可证或 `.gitignore`。

在项目目录执行：

```bash
git init
git add .
git commit -m "Initial release"
git branch -M main
git remote add origin https://github.com/YOUR_NAME/nft-forward-panel.git
git push -u origin main
```

公开仓库可以直接使用一键安装器。私有仓库的 Raw 文件与 Release 下载需要鉴权，不能直接使用文档中的公开下载命令。

## 2. 发布第一个版本

工作流 `.github/workflows/release.yml` 会在推送 `v*` 标签时执行 20 项测试、检查安装脚本语法、生成压缩包及 SHA-256 文件并创建 GitHub Release。

```bash
git tag -a v1.0.0 -m "v1.0.0"
git push origin v1.0.0
```

前往仓库的 `Actions` 页面确认工作流通过，再在 `Releases` 页面确认存在：

```text
nft-forward-panel.tar.gz
nft-forward-panel.tar.gz.sha256
```

## 3. 在 VPS 一键安装

先把域名 A 记录指向 VPS，并在云安全组开放 TCP 80、443。然后执行：

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_NAME/nft-forward-panel/main/install.sh \
  -o /tmp/nft-forward-panel-install.sh
sudo bash /tmp/nft-forward-panel-install.sh --repo YOUR_NAME/nft-forward-panel
```

安装器会询问域名、证书邮箱、管理员用户名和密码。密码使用隐藏输入，不会出现在 shell 历史中。

## 4. 更新

提交更新并发布新标签：

```bash
git add .
git commit -m "Release v1.0.1"
git push origin main
git tag -a v1.0.1 -m "v1.0.1"
git push origin v1.0.1
```

在 VPS 重新下载并执行安装器，选择相同域名即可：

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_NAME/nft-forward-panel/main/install.sh \
  -o /tmp/nft-forward-panel-install.sh
sudo bash /tmp/nft-forward-panel-install.sh --repo YOUR_NAME/nft-forward-panel
```

更新会保留 `/var/lib/nft-forward-panel` 中的数据库和头像，并备份旧程序、环境文件、nftables 主配置及 Caddy 配置。

## 注意事项

- 推荐公开仓库；私有仓库需要另行设计 GitHub Token 下载流程。
- 不要将 `/etc/nft-forward-panel.env`、生产数据库、头像或备份提交到 GitHub。
- 不要直接使用 `curl | bash`。先下载并审阅脚本，再以 root 执行。
- Caddy 和面板部署在同一 VPS 时使用 `PANEL_TRUSTED_PROXY_COUNT=1`。
- 不要在安全组开放 8108；只开放 80、443 和实际业务转发端口。
- 更新标签不要覆盖或强制移动，始终创建递增的新版本。
