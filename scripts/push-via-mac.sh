#!/usr/bin/env bash
# push-via-mac.sh —— 经 Mac 的 1Password SSH agent 签名，把本仓库推送到 GitHub
#
# 背景：NAS 上不存放 GitHub 私钥，签名由 Mac（1Password SSH agent 里的
#       "GitHub Mac" ED25519 密钥）完成。本脚本把仓库打包送到 Mac，
#       在 Mac 本地以 git@github.com SSH 方式推送。
#
# 用法：
#   scripts/push-via-mac.sh                    # 推当前分支
#   scripts/push-via-mac.sh main v1.0.1        # 推指定分支 + 指定 tag（tag 需已存在）
#
# 可覆盖的环境变量：
#   ZSL_MAC_HOST    默认 taro@10.31.0.2
#   ZSL_MAC_KEY     默认 /vol2/1000/Docker/zfs-steam-lun/.ssh/zsl_push_ed25519
#   ZSL_MAC_KNOWN   默认 /vol2/1000/Docker/zfs-steam-lun/.ssh/known_hosts
#   ZSL_REMOTE      默认 git@github.com:TaroCats/zfs-steam-lun.git
#
# 前置条件：Mac 上 1Password → 设置 → 开发者 → 已启用 SSH 代理，
#           且 NAS 公钥 marvis-nas-zsl-push 已在 Mac 的 authorized_keys 中。
set -euo pipefail

MAC_HOST="${ZSL_MAC_HOST:-taro@10.31.0.2}"
KEY="${ZSL_MAC_KEY:-/vol2/1000/Docker/zfs-steam-lun/.ssh/zsl_push_ed25519}"
KNOWN="${ZSL_MAC_KNOWN:-/vol2/1000/Docker/zfs-steam-lun/.ssh/known_hosts}"
REMOTE="${ZSL_REMOTE:-git@github.com:TaroCats/zfs-steam-lun.git}"

BRANCH="${1:-$(git -C "$(dirname "$0")/.." rev-parse --abbrev-ref HEAD)}"
TAG="${2:-}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SSH_OPTS=(-i "$KEY" -o IdentitiesOnly=yes -o UserKnownHostsFile="$KNOWN" -o ConnectTimeout=15)

[ -f "$KEY" ] || { echo "缺少登录 Mac 的密钥：$KEY" >&2; exit 1; }

echo "==> 1/4 检查 Mac 连通性"
ssh "${SSH_OPTS[@]}" "$MAC_HOST" 'echo ok: $(hostname -s)' >/dev/null || { echo "无法 SSH 到 $MAC_HOST" >&2; exit 1; }

echo "==> 2/4 打包传输仓库（$REPO）"
tar czf - -C "$(dirname "$REPO")" "$(basename "$REPO")" \
  | ssh "${SSH_OPTS[@]}" "$MAC_HOST" \
      'rm -rf /tmp/zsl && mkdir -p /tmp/zsl && tar xzf - -C /tmp/zsl && echo "    Mac 侧解包完成"'

echo "==> 3/4 Mac 侧提交校验"
ssh "${SSH_OPTS[@]}" "$MAC_HOST" "\
export SSH_AUTH_SOCK=\"\$HOME/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock\"; \
cd /tmp/zsl/$(basename "$REPO") || exit 1; \
git remote set-url origin '$REMOTE'; \
git config user.name 'TaroCats'; git config user.email 'taro@tarocats.dev'; \
echo '    HEAD:' \$(git log --oneline -1); \
ssh-add -l | head -1"

echo "==> 4/4 推送 $BRANCH${TAG:+ 与 tag $TAG}"
ssh "${SSH_OPTS[@]}" "$MAC_HOST" "\
export SSH_AUTH_SOCK=\"\$HOME/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock\"; \
cd /tmp/zsl/$(basename "$REPO") || exit 1; \
GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new' git push origin '$BRANCH' \
  ${TAG:+&& GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new' git push origin 'refs/tags/$TAG'}"

echo "==> 完成。CI：https://github.com/TaroCats/zfs-steam-lun/actions"
