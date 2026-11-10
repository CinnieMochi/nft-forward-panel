# 仓库托管与发布流程

本文说明这个仓库怎么从源码走到「用户能一键安装的 Release」。日常只需记住一条主线：

> **改代码 → 本地跑测试 → 提交推送 → 打 `v*` 标签 → CI 自动产出 Release → 安装器就能装到它。**

---

## 1. 仓库

| 项目 | 值 |
| --- | --- |
| 仓库 | `CinnieMochi/nft-forward-panel`（**公开**） |
| 默认分支 | `main` |
| 发布方式 | 推送 `v*` 标签触发 GitHub Actions |
| 发布产物 | `nft-forward-panel.tar.gz` + `nft-forward-panel.tar.gz.sha256` |

仓库必须保持**公开**：安装器通过 `https://github.com/<repo>/releases/` 匿名下载，私有仓库需要额外设计 Token 下载流程。

---

## 2. 修改与提交

工作副本放在本机，改完后同步到 git 克隆再推送：

```bash
# 1. 在本地工作副本改代码并自测
PYTHONPATH=. python3 -m unittest discover -s tests -v

# 2. 同步到克隆目录（保持布局一致，排除 .git）
rsync -a --delete --exclude .git --exclude __pycache__ --exclude '*.pyc' \
  "<工作副本>/" "<克隆目录>/"

# 3. 在克隆目录里检查差异、提交、推送
cd "<克隆目录>"
git status --short
git add -A
git commit -m "Describe the change"
git push origin main
```

提交前请确认 `git status` 里**没有**数据库、头像、`.env` 或 `dist/`——`.gitignore` 已经挡掉了这些，但别用 `git add -f` 绕过它。

---

## 3. 发布一个版本

```bash
git tag -a v1.2.0 -m "v1.2.0"
git push origin v1.2.0
```

### CI 会依次做什么

`.github/workflows/release.yml` 在 `v*` 标签推送时运行：

1. **Run test suite** — 在 Python 3.12 上跑完整的 `unittest` 套件。
2. **Check shell scripts** — `bash -n install.sh`、`bash -n nft.sh`。
3. **Build release archive and checksum** — 用 `rsync` 打包（排除 `.git`、`.github`、`dist`、`.venv`、`__pycache__`、`*.pyc`、`*.db*`），再生成 SHA-256。
4. **Verify archive layout and checksum** — `sha256sum -c` 自校验，并逐项确认包内有 `app.py`、`wsgi.py`、`requirements.txt`、`install.sh`、`nft.sh`、`nfpctl.py`、systemd unit、`templates/`、`static/`、`tests/`；若夹带 `.git` 或 `.github` 直接失败。
5. **Publish GitHub release** — 上传两个资产，自动生成 Release Notes。
6. **upload-artifact** — 两个资产另存为 workflow artifact。

**任何一步失败都不会产生 Release。** 这是刻意的：安装器只应下载到通过测试的包。

### 失败之后

```bash
gh run list --repo CinnieMochi/nft-forward-panel --limit 5
gh run view <run-id> --repo CinnieMochi/nft-forward-panel --log-failed
```

修好问题后**不要移动或复用已有标签** —— 那会让已经下载过该版本的人拿到不同内容。永远递增新版本号。

---

## 4. 应急：手工补发一个 Release

只有当 CI 环境本身不可用、又必须发出某个已打标签的版本时才这么做。产物必须来自**同一个标签对应的提交**，不能拿工作区里的半成品打包。

```bash
git checkout v1.2.0
mkdir -p /tmp/nfp-dist/nft-forward-panel
rsync -a ./ /tmp/nfp-dist/nft-forward-panel/ \
  --exclude .git --exclude .github --exclude dist --exclude .venv \
  --exclude .preview-data --exclude __pycache__ --exclude '*.pyc' --exclude '*.db*'
tar -C /tmp/nfp-dist -czf /tmp/nfp-dist/nft-forward-panel.tar.gz nft-forward-panel
cd /tmp/nfp-dist
sha256sum nft-forward-panel.tar.gz > nft-forward-panel.tar.gz.sha256

gh release create v1.2.0 --repo CinnieMochi/nft-forward-panel --verify-tag \
  --generate-notes nft-forward-panel.tar.gz nft-forward-panel.tar.gz.sha256
```

`--verify-tag` 保证它只是给**已存在**的标签建 Release，不会顺手新建一个标签。

---

## 5. 验收一个 Release

```bash
# 最新 Release 应该是你要的版本
curl -fsSL https://api.github.com/repos/CinnieMochi/nft-forward-panel/releases/latest \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["tag_name"]);
print("\n".join(f"{a[\"name\"]}  {a[\"digest\"]}" for a in d["assets"]))'
```

然后真的下载一次并核对哈希：

```bash
curl -fsSL -o /tmp/nfp.tar.gz \
  https://github.com/CinnieMochi/nft-forward-panel/releases/latest/download/nft-forward-panel.tar.gz
sha256sum /tmp/nfp.tar.gz
tar -tzf /tmp/nfp.tar.gz | head
```

只有确认过 `digest` 与下载结果一致，才认为这个 Release 可用于一键安装。

---

## 6. 注意事项

- **不要**用 `curl | bash` 安装。先把 `install.sh` 下载下来看一眼，再以 root 执行。
- **不要**把 `/etc/nft-forward-panel.env`、生产数据库、头像或备份提交进仓库。
- **不要**在云安全组放行 `8108`；只放 80/443 和实际业务转发端口。
- 面板与 Caddy 同机时，`PANEL_TRUSTED_PROXY_COUNT` 必须是 `1`；不经过反代则设 `0`。
- 标签只增不改：`v1.2.0` 发布后，下一个版本就是 `v1.2.1` 或 `v1.3.0`。
- 工作流里的所有 `uses:` 都锁在 commit SHA 上，升级 Action 时请连注释里的版本号一起改。
