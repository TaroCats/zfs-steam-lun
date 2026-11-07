# 推送通道：NAS → Mac(1Password SSH agent) → GitHub

本仓库在 NAS 上开发，NAS 本身不持有 GitHub 私钥。所有 `git push` 通过 Mac 上的
1Password SSH 代理签名完成。本文记录该通道的构成与排障步骤，避免以后重复摸索。

## 1. 组成

| 环节 | 位置 | 说明 |
|---|---|---|
| 开发仓库 | NAS `/vol2/@appshare/marvis-app/.../output/zfs-steam-lun` | remote 已设为 `git@github.com:TaroCats/zfs-steam-lun.git` |
| 登录 Mac 的密钥 | NAS `/vol2/1000/Docker/zfs-steam-lun/.ssh/zsl_push_ed25519` | 专用 ED25519，指纹 `SHA256:tQ1iZSZCUyrf9EvC93iO33Ky63Zy/01RqDgayvj4aZE`，注释 `marvis-nas-zsl-push` |
| 公钥授权 | Mac `~/.ssh/authorized_keys` | 用户已确认**保留**，可供以后复用 |
| 签名密钥 | Mac 1Password `GitHub Mac`（ED25519） | 只存在 1Password 中，不出 vault |
| 代理 socket | Mac `~/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock` | 不是常见的 `s.sock`，后者已废弃/无响应 |
| 推送执行 | Mac 本地 `/tmp/zsl/<repo>` | 仓库含 `.git` 整体打包过去，Mac 上直接 push |

## 2. 前置开关（1Password）

- 设置 → 开发者 → 启用 **SSH 代理**（`settings.json` 中 `sshAgent.enabled = true`）
- Mac 未锁屏；首次签名时若弹窗，需点「允许」
- `developers.mcpIntegration.enabled = true` 可留开，但 MCP 与推送无关（见第 5 节）

## 3. 标准流程

```bash
cd /vol2/@appshare/marvis-app/.../output/zfs-steam-lun
scripts/push-via-mac.sh              # 推当前分支
scripts/push-via-mac.sh main v1.0.1  # 推分支 + tag
```

脚本做的事：连通性检查 → 打包传输到 Mac `/tmp/zsl` → `remote set-url` 为 SSH 地址 →
`git push origin <branch>`。推送后 CI 自动触发多架构镜像构建。

## 4. 手工命令（脚本失效时）

```bash
KEY=/vol2/1000/Docker/zfs-steam-lun/.ssh/zsl_push_ed25519
KNOWN=/vol2/1000/Docker/zfs-steam-lun/.ssh/known_hosts
REPO=/vol2/@appshare/marvis-app/data/workspace/oAN1i2dymoPdyL-8C4uGbh8fJ61Y/conv_1a1b61aa94f_dfeb52296113/output/zfs-steam-lun

tar czf - -C "$(dirname "$REPO")" zfs-steam-lun \
  | ssh -i "$KEY" -o UserKnownHostsFile="$KNOWN" taro@10.31.0.2 \
      'rm -rf /tmp/zsl && mkdir -p /tmp/zsl && tar xzf - -C /tmp/zsl'

ssh -i "$KEY" -o UserKnownHostsFile="$KNOWN" taro@10.31.0.2 '
export SSH_AUTH_SOCK="$HOME/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock"
cd /tmp/zsl/zfs-steam-lun
git remote set-url origin git@github.com:TaroCats/zfs-steam-lun.git
git push origin main'
```

## 5. 走过的弯路（别再踩）

| 尝试 | 结果 | 结论 |
|---|---|---|
| 直接在 NAS 用 `SSH_AUTH_SOCK` 指向 Mac socket | 不可达，socket 是 Mac 本地文件 | 必须经 SSH 中转或直接在 Mac 执行 |
| `ssh -L /tmp/x.sock:...` 转发 socket 回 NAS | 建连成功但后台易被回收，`ssh-add` 仍报错 | 不稳定，弃用 |
| 1Password MCP（`onepassword-mcp`，rmcp 1.8.0） | 只暴露 8 个 1Password **Environments**(.env) 工具，账户内 `environments: []`，无读取登录项/无 SSH 签名 | 与推送无关，不可用于取 token |
| Mac 上 `ssh-add -l` | 需用 `t/agent.sock`；`t/s.sock` 会报 communication failed | 认准 socket 文件名 |
| CI `if:` 里写 `secrets.DOCKERHUB_USERNAME != ''` | workflow 解析失败，jobs=0，run 直接 failure | secrets 不能出现在 `if`；已改为固定 GHCR 单源 |

## 6. 相关产物

- 工作流：`.github/workflows/build.yml`（matrix amd64/arm64 → push-by-digest → imagetools 合并）
- 镜像：`ghcr.io/tarocats/zfs-steam-lun`，tag `edge` / `sha-<短SHA>` / `vX.Y.Z` / `latest`
- 运行记录：run 42387711303 成功，OCI index 含 `linux/amd64`、`linux/arm64` 及 provenance/SBOM
