#!/usr/bin/env bash
# 一键部署：把项目复制到 Docker 目录并启动，打印访问地址与令牌。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="${1:-/vol2/1000/Docker/zfs-steam-lun}"

echo "==> 部署到 $DST"
mkdir -p "$DST"
cp -a "$SRC/Dockerfile" "$SRC/docker-compose.yml" "$SRC/requirements.txt" "$SRC/app" "$SRC/static" "$DST/"
mkdir -p "$DST/data"

cd "$DST"
echo "==> 构建镜像"
docker compose build

echo "==> 启动容器"
docker compose up -d

echo "==> 等待健康检查"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8890/api/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "==================================================="
echo " 控制台: http://${IP:-<NAS-IP>}:8890"
if [ "${ZSL_AUTH:-off}" = "on" ]; then
  if [ -f "$DST/data/token.txt" ]; then
    echo " 访问令牌: $(cat "$DST/data/token.txt")"
  else
    echo " 访问令牌: docker logs zfs-steam-lun | head -5"
  fi
else
  echo " 访问方式: 直接打开即可（未启用令牌，APP_AUTH=off）"
fi
echo "==================================================="
echo
echo "首次使用：进入「设置」选择存储池 → 创建母版 → 打快照 → 新增客户端。"
