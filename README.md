# ZFS Steam LUN

飞牛 fnOS（或任何带 ZFS + LIO 的 Linux）上的一站式「母版克隆 → 多机独立 iSCSI LUN」控制器。
为「多台电脑共享同一份 Steam 库，但每台各有独立可写盘」这个场景做成 Web 应用。

一台母版 zvol，按需打快照、克隆出 N 份独立卷，每份挂一个独立 iSCSI 目标。母版只读不动，克隆各写各的，互不干扰，也不会出现「同一份 NTFS 被两台机器同时挂载」的损坏问题。

---

## 1. 它解决什么问题

| 常见做法 | 问题 |
| --- | --- |
| 直接把同一块 iSCSI LUN 挂给多台电脑 | NTFS 不支持多主机同时读写，必然损坏文件系统 |
| 每台电脑各拷一份 Steam 库 | 占空间 N 倍，更新一次全得重拷 |
| 用 SMB 共享 Steam 库 | Steam 官方不支持，部分游戏会异常，性能差 |
| **本应用：母版 + 快照克隆** | 只存一份基线，克隆用写时复制几乎不额外占空间；每台独立卷、独立盘符 |

克隆用的是 ZFS `clone`，初始不复制数据，随写入才增量占空间，所以 10 台机器复制同一个母版也不会立刻吃掉 10 倍容量。

---

## 2. 功能

- **母版管理**：新建 zvol（可设 volblocksize / 压缩）、打快照、快照列表与删除、临时挂载给某台电脑装库、用后断开
- **客户端管理**：从母版最新快照一键克隆，自动建 block backstore + LUN + 独立 target + ACL（可选 CHAP）
- **重建（reclone）**：某个客户端的库被玩坏了，单独销毁重建，不影响其他机器
- **扩容**：在线调整 zvol volsize，Windows 侧再「扩展卷」
- **连接向导**：直接给出该客户端的门户地址、目标 IQN、CHAP 账号密码，以及 Windows 端 `iscsicpl` 操作步骤
- **状态总览**：池容量/健康、母版与克隆数量、在线 iSCSI 会话、最近操作
- **运维**：操作日志、环境自检、target 配置持久化（`saveconfig`）

---

## 3. 架构与为什么要特权

```
┌─────────────────────────────────────────────┐
│ 容器 zfs-steam-lun (FastAPI + 静态页面)      │
│   8890/tcp  Web 控制台                       │
└──────────────┬──────────────────────────────┘
               │ nsenter -t 1 -m --
               ▼
┌─────────────────────────────────────────────┐
│ 宿主 fnOS                                    │
│   /usr/sbin/zfs       ← ZFS 卷/快照/克隆     │
│   /usr/bin/targetcli  ← LIO 的 iSCSI 目标    │
│   /sys/kernel/config  ← 内核 configfs        │
│   /etc/target         ← target 持久化配置    │
└─────────────────────────────────────────────┘
```

**为什么不把 zfs 装进镜像**：fnOS 的 ZFS 是自编译的 `2.4.1`，Debian 源里的 `zfsutils-linux`（bookworm 2.1.x / trixie 2.3.x）与之版本不匹配，运行时会直接拒绝操作。因此容器内不装 ZFS 用户态工具，而是以 `privileged + pid: host` 运行，用 `nsenter` 进入宿主 mount namespace 调用**宿主自己的** `zfs` / `targetcli`，版本天然一致。

代价是容器权限很高（等同于宿主 root），请只在内网使用，不要暴露到公网。

---

## 4. 部署

### 4.1 前置条件

- 宿主已有 ZFS 池（`zpool list` 能看到，状态 ONLINE）
- 内核 LIO 可用：`ls /sys/kernel/config/target/iscsi` 有内容
- 宿主有 `targetcli`（fnOS 自带）
- 端口 `8890`（Web）与 `3260`（iSCSI）未被占用

### 4.2 一键部署

```bash
# 在项目目录下
bash install.sh
# 或指定安装目录
bash install.sh /vol2/1000/Docker/zfs-steam-lun
```

脚本会：复制文件 → `docker compose build` → `up -d` → 等健康检查 → 打印访问地址。

### 4.3 手工部署

```bash
mkdir -p /vol2/1000/Docker/zfs-steam-lun
cp -a Dockerfile docker-compose.yml requirements.txt app static /vol2/1000/Docker/zfs-steam-lun/
cd /vol2/1000/Docker/zfs-steam-lun
mkdir -p data
docker compose up -d --build
docker logs zfs-steam-lun | head -20     # 查看启动日志
```

### 4.4 从现成镜像部署（不构建）

GitHub Actions 会把多架构镜像发布到 GHCR（`amd64` / `arm64` 同一 manifest）：

```bash
mkdir -p /vol2/1000/Docker/zfs-steam-lun && cd /vol2/1000/Docker/zfs-steam-lun
# 只需要 compose 文件，不需要源码
curl -fsSLO https://raw.githubusercontent.com/TaroCats/zfs-steam-lun/main/docker-compose.yml
mkdir -p data
ZSL_IMAGE=ghcr.io/tarocats/zfs-steam-lun:latest docker compose up -d
docker logs zfs-steam-lun | head -20     # 查看启动日志
```

私有镜像需先 `echo <PAT> | docker login ghcr.io -u <用户名> --password-stdin`。

### 4.5 访问

浏览器打开 `http://<NAS-IP>:8890`，直接进入控制台 —— **本应用没有任何登录/令牌机制**，
后端不校验任何凭证，前端也没有登录页。

> ⚠️ 因为无鉴权，控制台能删除 LUN、回滚快照，属于破坏性操作。
> **不要**把 8890 端口映射到公网；必须远程访问时，请在前面套反向代理并加上 HTTP 认证。

### 4.6 前端缓存

静态资源响应头带 `Cache-Control: no-cache, must-revalidate`，`index.html` 为 `no-store`，
且 HTML 里以 `?v=<版本>` 引用 `app.js` / `style.css`，升级后浏览器不会继续使用旧版前端。

---

## 5. 使用流程

### 5.1 首次配置

1. 打开「设置」→ 选择存储池 → 数据集名（默认 `steamzfs`）→ 保存
   - 会自动创建 `<池>/steamzfs/masters` 与 `<池>/steamzfs/clients` 两个数据集（`mountpoint=none`，不占挂载点）
2. 同一页可改 IQN 前缀与 Portal 端口，一般保持默认

### 5.2 做母版

1. 「母版卷」→ 新建母版：名称 `steam-lib`，容量 500 GB，块大小 16K，压缩 lz4
2. 点该行「挂载」，填要装库那台电脑的 Initiator IQN（可留空）
3. 按弹出的连接指引在这台电脑上 `iscsicpl` 连接 → 磁盘管理初始化 GPT → 新建卷 → Steam 添加库 → 装好游戏
4. **在 Windows 里安全弹出/断开 iSCSI**，回到「母版卷」点「断开」
5. 点「快照」，命名 `baseline` —— 这就是所有客户端的基线

### 5.3 给每台电脑开克隆

「客户端」→ 新增客户端：填名称（如 `pc-study`）、选母版、填该机 IQN → 创建。
创建完会自动弹出连接指引，照着在 Windows 上连即可。克隆继承母版的 NTFS 与已有游戏数据，联机后盘符里就是母版那套库，可以直接开玩。

> 注意：克隆出来的卷与母版**序列号相同**。因为同一时刻只会有一台机器挂载某一份卷，所以不冲突；但不要在同一台机器上同时挂载两份克隆。

### 5.4 母版更新

1. 所有客户端先断开（或至少确保要更新时没有机器在用母版）
2. 母版「挂载」→ 装/更新游戏 → 断开
3. 打新快照 `20261107-update`
4. 需要更新的客户端点「重建」，会从最新快照重新克隆（该机原有存档会丢，注意先备份 `userdata`）
5. 不想重置的客户端保持原样即可，它们继续用旧基线

---

## 6. 目录与数据

| 路径 | 说明 |
| --- | --- |
| `<池>/<数据集>/masters/<名>` | 母版 zvol |
| `<池>/<数据集>/masters/<名>@<快照>` | 快照 |
| `<池>/<数据集>/clients/<名>` | 客户端克隆 zvol（origin 指向母版快照） |
| `./data/app.db` | 客户端元数据（IQN、CHAP、备注）、设置、操作日志 |
| `/etc/target/saveconfig.json` | 宿主 target 持久化配置 |

ZFS 侧建议参数（应用默认已带）：`volblocksize=16K`、`compression=lz4`、`sync=standard`。

---

## 7. 安全与限制

- 容器 `privileged`，请仅内网使用
- 删除母版前必须先删掉依赖它的客户端（ZFS 会因 clone 依赖拒绝 `destroy`，应用会给出提示）
- 同一份 NTFS 卷**严禁**两台机器同时挂载
- iSCSI 走的是明文协议，跨不可信网络请启用 CHAP（应用已支持）或改用 VLAN
- 机械盘做 Steam 库，随机读性能很差（5400rpm 约 100 IOPS），建议用 SSD/NVMe
- 单条 2.5GbE 理论 ~280MB/s，多机并发会互相抢带宽
- `saveconfig` 会把当前所有 target（含 fnOS 自己的）一起写入 `/etc/target/saveconfig.json`，覆盖前请知悉

---

## 8. API 速查

所有接口均无需任何认证头（应用不校验凭证）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/overview` | 总览 |
| GET | `/api/scan` | 扫描母版/克隆/孤儿映射 |
| GET | `/api/pools`、`/api/pools/{p}/status` | 池信息 / 状态 |
| GET/POST | `/api/settings` | 读写设置 |
| GET | `/api/diagnostics` | 环境自检 |
| POST | `/api/saveconfig` | 持久化 target |
| GET | `/api/chap` | 生成随机 CHAP 密码 |
| POST | `/api/masters` | 新建母版 |
| POST | `/api/masters/{n}/snapshot` | 打快照 |
| GET | `/api/masters/{n}/snapshots` | 快照列表 |
| DELETE | `/api/masters/{n}/snapshots/{s}` | 删快照 |
| POST | `/api/masters/{n}/mount` \| `/unmount` | 挂载 / 断开母版 |
| DELETE | `/api/masters/{n}` | 删母版 |
| POST | `/api/clients` | 新建客户端（克隆 + 映射） |
| POST | `/api/clients/{n}/reclone` | 重建 |
| POST | `/api/clients/{n}/resize` | 扩容 |
| POST | `/api/clients/{n}/attach` | 重新映射 |
| GET | `/api/clients/{n}/secret` | 取该机连接参数 |
| DELETE | `/api/clients/{n}?purge=true` | 删客户端 |
| GET | `/api/sessions` | iSCSI 会话 |
| GET | `/api/logs?limit=100` | 操作日志 |

---

## 9. 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| 设置页「环境自检」`zfs 不可用` | 容器缺 `pid: host` 或 `privileged`，或宿主没有 `zfs` |
| `configfs 未挂载` | 确认挂载了 `/sys/kernel/config:/sys/kernel/config:rshared` |
| 创建母版报 `dataset does not exist` | 先在设置页保存一次，让应用创建目录数据集 |
| 删除母版报 `dataset is busy` / `has dependent clones` | 先删除该母版的全部客户端克隆 |
| Windows 连不上目标 | 检查 3260 是否被防火墙拦、IQN 是否填错、CHAP 是否一致 |
| 连上但看不到磁盘 | 回到「客户端」页点「重新映射」重建 LUN；或检查 `/sys/kernel/config/target/iscsi/*/tpgt_1/lun` |
| 克隆后提示「文件系统需要检查」 | 母版打快照前没在 Windows 里安全断开，需重做基线 |
| 宿主重启后 iSCSI 目标消失 | 在「设置」点「持久化 target 配置」，并确保宿主 `target` 服务开机启动 |
| 删除/重建客户端报 `dataset is busy` | 说明该卷还被 LIO backstore 占用。应用已做「摘 LUN → 删 target → 删 backstore → 重试销毁」；若仍报错，手动执行 `targetcli /backstores/block delete bs_<客户端名>` 后重试 |
| 手工排查 backstore 路径 | fnOS 内核里 block backstore 位于 `core/iblock_<n>/`（不是 `core/block/`），应用会自动探测；手工看用 `targetcli ls /backstores/block` 最稳 |
| 手工用 targetcli 指定 TPG | 路径写 `tpg1`（targetcli 别名），写 `tpgt_1` 会报 `No such path` |
| 同一台 NAS 上已有别的 iSCSI target | 不冲突：每个客户端是独立 target + 独立 LUN，共用 3260 端口 |
| 升级后配置丢失 | `docker compose down` 不会删 `./data`，元数据在 `data/app.db`；建议定期复制该文件 |

---

## 10. 卸载

```bash
cd /vol2/1000/Docker/zfs-steam-lun
docker compose down
# 数据卷与 ZFS 卷不会自动删除，如需清理：
# zfs destroy -r <池>/steamzfs
```

---

## 11. CI 与镜像分发

流水线文件：`.github/workflows/build.yml`，用 **矩阵** 分平台构建，最后合成多架构 manifest。

| 事件 | 行为 | 产出 tag |
| --- | --- | --- |
| push tag `v1.2.3` | 构建并推送 + 合并 manifest | `1.2.3`、`1.2`、`1`、`latest` |
| push `main` | 构建并推送 | `edge`、`sha-<短哈希>` |
| Pull Request | 只构建校验，不推送 | — |
| 手动 `workflow_dispatch` | 可勾选是否推送 | 同 main |

矩阵结构：

```yaml
strategy:
  matrix:
    include:
      - { platform: linux/amd64, arch: amd64, qemu: false }
      - { platform: linux/arm64, arch: arm64, qemu: true }
```

流程要点：

1. 每个平台各自 `build-push-action`，用 `outputs: type=image,push-by-digest=true,name-canonical=true` 只推 digest，避免多任务互相覆盖 tag；
2. 各任务把自己的 digest 与 tag 列表作为 artifact 上传；
3. `merge` 任务下载全部 digest，用 `docker buildx imagetools create` 合成一个多架构 manifest 并打 tag；
4. 构建缓存用 GitHub Actions Cache（`type=gha`，按平台分 scope），并输出 `provenance` 与 `sbom`。

镜像地址：`ghcr.io/tarocats/zfs-steam-lun`

可选：在仓库 Secrets 里配 `DOCKERHUB_USERNAME` + `DOCKERHUB_TOKEN`，会自动同时分发到 Docker Hub（`<用户名>/zfs-steam-lun`）。不配则只推 GHCR。

发布新版本：

```bash
git tag v1.0.2 && git push origin v1.0.2
```

arm64 runner 说明：默认用 QEMU 模拟以兼容任意仓库；若仓库可访问 GitHub 原生 arm64 runner（公开仓库免费），把 arm64 项的 `qemu: true` 改成 `false` 并给该矩阵项加 `runner: ubuntu-24.04-arm`、任务的 `runs-on` 改为 `${{ matrix.runner || 'ubuntu-24.04' }}`，构建速度显著提升。

---

## 12. 版本

- 1.2.0 — 彻底移除访问令牌机制（删除 `APP_AUTH` / `APP_TOKEN` / `/api/session` / `/api/login` / `/api/token-hint` 与前端登录页）；静态资源改为禁缓存 + 版本指纹，修复升级后浏览器仍加载旧前端导致的“仍要求输入令牌”问题。
- 1.1.0 — 访问令牌默认关闭（`APP_AUTH=off`），前端启动探测 `/api/session` 自动免登录。该机制已在 1.2.0 中删除。
- 1.0.1 — 修复：父数据集重复创建、`sparse` 选项不兼容、扫描时 `short` 取值错误、targetcli TPG 路径别名、block backstore 路径探测（`core/iblock_<n>`）、卷 busy 重试与回滚错误掩盖。已在 fnOS（ZFS 2.4.1 + 内核 6.18）用临时文件池跑通全链路自测。同时补齐 GitHub Actions 矩阵构建与 GHCR 分发、compose 支持 `ZSL_IMAGE` 变量。
- 1.0.0 — 首个版本：母版/快照/克隆/映射/CHAP/扩容/重建/日志/自检
