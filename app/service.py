"""业务编排层：把 ZFS 卷、LIO 目标、SQLite 元数据组合成「母版 / 客户端」模型。"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time

from . import db, lio, zfs
from .config import (
    APP_VERSION,
    DEFAULT_COMPRESSION,
    DEFAULT_INSTANCE,
    DEFAULT_IQN_PREFIX,
    DEFAULT_PORTAL,
    DEFAULT_PORTAL_PORT,
    DEFAULT_SYNC,
    DEFAULT_VOLBLOCKSIZE,
)

MASTER_DIR = "masters"
CLIENT_DIR = "clients"


log = logging.getLogger("zsl")


class ServiceError(RuntimeError):
    pass


# ---------------------------------------------------------------- 配置项
def settings() -> dict:
    return {
        "root_dataset": db.get_setting("root_dataset", ""),
        "iqn_prefix": db.get_setting("iqn_prefix", DEFAULT_IQN_PREFIX),
        "instance": db.get_setting("instance", DEFAULT_INSTANCE),
        "portal": db.get_setting("portal", DEFAULT_PORTAL),
        "portal_port": db.get_setting("portal_port", DEFAULT_PORTAL_PORT),
    }


def save_settings(**kw) -> dict:
    allowed = {"root_dataset", "iqn_prefix", "instance", "portal", "portal_port"}
    for k, v in kw.items():
        if k in allowed and v is not None:
            db.set_setting(k, v)
    db.log_op("settings.update", detail=str({k: v for k, v in kw.items() if k in allowed}))
    return settings()


def root() -> str:
    r = db.get_setting("root_dataset", "")
    if not r:
        raise ServiceError("尚未设置根数据集，请先在「设置」中指定池与数据集名称")
    return r.rstrip("/")


def ensure_root() -> str:
    r = root()
    if not zfs.exists(r):
        zfs.create_dataset(r, {"compression": "lz4", "atime": "off"})
        db.log_op("dataset.create", target=r)
    return r


def masters_path() -> str:
    return f"{root()}/{MASTER_DIR}"


def clients_path() -> str:
    return f"{root()}/{CLIENT_DIR}"


def _ensure_dir_dataset(path: str, props: dict | None = None) -> None:
    """幂等地创建目录型数据集（masters / clients）。"""
    if zfs.exists(path):
        return
    zfs.create_dataset(path, props or {"compression": "lz4"})


# ---------------------------------------------------------------- 扫描
def _backstore_map() -> dict[str, str]:
    """dev -> backstore name"""
    m = {}
    for bs in lio.list_backstores():
        dev = bs.get("udev_path", "")
        if dev:
            m[dev] = bs["name"]
    return m


def scan() -> dict:
    """汇总真实状态。ZFS 为准，configfs 补在线与映射信息。"""
    r = db.get_setting("root_dataset", "")
    if not r:
        return {"root": "", "masters": [], "clients": [], "volumes": [], "orphan_targets": []}

    vols = zfs.list_volumes(r)
    targets = {t["iqn"]: t for t in lio.list_targets()}
    online = lio.online_targets()
    bsmap = _backstore_map()
    meta = db.all_clients()

    masters, clients = [], []
    for v in vols:
        dev = zfs.zvol_device(v["name"])
        bs = bsmap.get(dev, "")
        # 通过 backstore → target 反查
        tg = None
        if bs:
            for t in targets.values():
                if any(l["backstore"] == bs for l in t["luns"]):
                    tg = t
                    break
        rec = {
            **v,
            "short": v["name"].rsplit("/", 1)[-1],
            "device": dev,
            "backstore": bs,
            "target": tg["iqn"] if tg else "",
            "lun": next((l["id"] for l in tg["luns"] if l["backstore"] == bs), None) if tg else None,
            "online": bool(tg and tg["iqn"] in online),
            "chap": tg["chap"] if tg else False,
            "acl": tg["acls"][0]["initiator"] if tg and tg["acls"] else "",
        }
        parent = v["name"].rsplit("/", 1)[0]
        if parent == f"{r}/{MASTER_DIR}":
            m = db.get_master_meta(rec["short"]) or {}
            rec["note"] = m.get("note", "")
            rec["clones"] = zfs.dependents(v["name"])
            rec["snapshots"] = len(zfs.list_snapshots(v["name"]))
            masters.append(rec)
        elif parent == f"{r}/{CLIENT_DIR}":
            c = meta.get(rec["short"], {})
            rec["note"] = c.get("note", "")
            rec["initiator"] = c.get("client_iqn", "")
            rec["master_name"] = (v["origin"].split("@")[0].rsplit("/", 1)[-1]
                                  if v["origin"] else c.get("master", ""))
            rec["origin_snap"] = v["origin"]
            clients.append(rec)

    known_bs = {zfs.zvol_device(v["name"]) for v in vols}
    orphans = []
    for t in targets.values():
        for l in t["luns"]:
            bs = l.get("backstore")
            if not bs:
                continue
            dev = next((d for d, n in bsmap.items() if n == bs), "")
            if dev and dev not in known_bs:
                orphans.append({"target": t["iqn"], "backstore": bs, "device": dev})

    masters.sort(key=lambda x: x["short"])
    clients.sort(key=lambda x: x["short"])
    return {
        "root": r,
        "masters": masters,
        "clients": clients,
        "volumes": vols,
        "orphan_targets": orphans,
        "online_targets": sorted(online),
    }


# ---------------------------------------------------------------- 池与容量
def pools() -> list[dict]:
    return zfs.pools()


def overview() -> dict:
    st = settings()
    p = None
    try:
        r = root()
        pool_name = r.split("/")[0]
        p = next((x for x in zfs.pools() if x["name"] == pool_name), None)
    except ServiceError:
        pass
    sc = scan()
    sessions = lio.sessions()
    health = "ok"
    if p and p["health"] not in ("ONLINE", "-"):
        health = "degraded"
    return {
        "settings": st,
        "pool": p,
        "health": health,
        "masters": len(sc["masters"]),
        "clients": len(sc["clients"]),
        "online": len([s for s in sessions if s["online"]]),
        "sessions": sessions,
        "configfs": lio.configfs_ready(),
        "zfs_ok": bool(zfs.run(["zfs", "version"], check=False).returncode == 0),
        "version": APP_VERSION,
    }


# ---------------------------------------------------------------- 母版
def create_master(name: str, size_gb: float, volblocksize: str = DEFAULT_VOLBLOCKSIZE,
                  compression: str = DEFAULT_COMPRESSION, note: str = "") -> dict:
    lio.validate_slug(name)
    if size_gb < 1:
        raise ServiceError("容量至少 1 GB")
    ensure_root()
    path = f"{masters_path()}/{name}"
    if zfs.exists(path):
        raise ServiceError(f"母版 {name} 已存在")
    _ensure_dir_dataset(masters_path())
    zfs.create_zvol(path, size_gb, volblocksize, compression, DEFAULT_SYNC)
    db.set_master_meta(name, note)
    db.log_op("master.create", target=path, detail=f"{size_gb:g}G bs={volblocksize} comp={compression}")
    return {"name": name, "path": path}


def delete_master(name: str, force: bool = False) -> dict:
    path = f"{masters_path()}/{name}"
    if not zfs.exists(path):
        raise ServiceError(f"母版 {name} 不存在")
    if _target_online_by_device(zfs.zvol_device(path)):
        raise ServiceError("母版仍被客户端连接（iSCSI 会话在线），请先在客户端断开后再删除")
    deps = zfs.dependents(path)
    if deps and not force:
        raise ServiceError(f"该母版仍被 {len(deps)} 个克隆依赖: {', '.join(deps[:3])}，需先删除这些客户端")
    # 拆除可能存在的母版挂载
    tg = _target_by_device(zfs.zvol_device(path))
    if tg:
        lio.delete_target(tg)
        lio.delete_backstore(f"bs_master-{name}")
    zfs.destroy(path, recursive=True)
    db.delete_master_meta(name)
    lio.save_config()
    db.log_op("master.delete", target=path)
    return {"deleted": name, "dependents": deps}


def snapshot_master(name: str, snap: str = "") -> dict:
    path = f"{masters_path()}/{name}"
    if not zfs.exists(path):
        raise ServiceError(f"母版 {name} 不存在")
    if _target_online_by_device(zfs.zvol_device(path)):
        raise ServiceError("母版正在被客户端使用（有活跃 iSCSI 会话），请先断开再打快照，否则 NTFS 不一致")
    snap = snap or time.strftime("%Y%m%d-%H%M%S")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", snap):
        raise ServiceError("快照名不合法")
    zfs.snapshot(path, snap)
    db.log_op("master.snapshot", target=f"{path}@{snap}")
    return {"snapshot": f"{path}@{snap}"}


def list_snapshots(name: str) -> list[dict]:
    return zfs.list_snapshots(f"{masters_path()}/{name}")


def delete_snapshot(name: str, snap: str) -> dict:
    full = f"{masters_path()}/{name}@{snap}"
    deps = [d for d in zfs.dependents(f"{masters_path()}/{name}")
            if zfs.get_prop(d, "origin") == full]
    if deps:
        raise ServiceError(f"快照仍被 {len(deps)} 个克隆使用，无法删除")
    zfs.destroy_snapshot(full)
    db.log_op("master.snapshot.delete", target=full)
    return {"deleted": full}


def mount_master(name: str, initiator: str = "", chap_user: str = "", chap_pass: str = "") -> dict:
    """把母版挂给一台电脑（用于首次安装/更新 Steam 库）。"""
    st = settings()
    path = f"{masters_path()}/{name}"
    if not zfs.exists(path):
        raise ServiceError(f"母版 {name} 不存在")
    iqn = lio.build_iqn(st["iqn_prefix"], st["instance"], f"master-{name}")
    bs = f"bs_master-{name}"
    if lio.target_exists(iqn):
        lio.delete_target(iqn)
    if lio.backstore_exists(bs):
        lio.delete_backstore(bs)
    dev = _wait_device(zfs.zvol_device(path))
    lio.create_backstore(bs, dev)
    lio.create_target(iqn, st["portal"], int(st["portal_port"]))
    lio.create_lun(iqn, bs, 0)
    if initiator:
        lio.create_acl(iqn, initiator, chap_user, chap_pass)
    lio.save_config()
    db.log_op("master.mount", target=path, detail=iqn)
    return {"target": iqn, "backstore": bs, "device": dev, "chap": bool(chap_user)}


def unmount_master(name: str) -> dict:
    iqn_prefix = settings()["iqn_prefix"]
    inst = settings()["instance"]
    iqn = lio.build_iqn(iqn_prefix, inst, f"master-{name}")
    if iqn in lio.online_targets():
        raise ServiceError("仍有客户端在线，请先在客户端断开 iSCSI 连接")
    lio.delete_target(iqn)
    lio.delete_backstore(f"bs_master-{name}")
    lio.save_config()
    db.log_op("master.unmount", target=name)
    return {"unmounted": name}


# ---------------------------------------------------------------- 客户端
def create_client(name: str, master: str, initiator: str = "",
                  chap_user: str = "", chap_pass: str = "", note: str = "",
                  snapshot: str = "") -> dict:
    lio.validate_slug(name)
    st = settings()
    ensure_root()
    mpath = f"{masters_path()}/{master}"
    if not zfs.exists(mpath):
        raise ServiceError(f"母版 {master} 不存在")
    if _target_online_by_device(zfs.zvol_device(mpath)):
        raise ServiceError("母版正在被使用（有活跃会话），先断开再克隆，否则数据不一致")

    cpath = f"{clients_path()}/{name}"
    if zfs.exists(cpath):
        raise ServiceError(f"客户端 {name} 已存在")

    # 快照：默认复用最新快照，保证「母版更新后不自动漂移」
    if snapshot:
        snap_full = f"{mpath}@{snapshot}"
    else:
        snaps = zfs.list_snapshots(mpath)
        if snaps:
            snap_full = snaps[-1]["full"]
        else:
            snap_name = "baseline-" + time.strftime("%Y%m%d-%H%M%S")
            snap_full = zfs.snapshot(mpath, snap_name)

    if not zfs.exists(snap_full):
        raise ServiceError(f"快照 {snap_full} 不存在")

    _ensure_dir_dataset(clients_path())
    zfs.clone(snap_full, cpath)

    iqn = lio.build_iqn(st["iqn_prefix"], st["instance"], name)
    bs = f"bs_{name}"
    try:
        dev = _wait_device(zfs.zvol_device(cpath))
        if lio.target_exists(iqn):
            lio.delete_target(iqn)
        if lio.backstore_exists(bs):
            lio.delete_backstore(bs)
        lio.create_backstore(bs, dev)
        lio.create_target(iqn, st["portal"], int(st["portal_port"]))
        lio.create_lun(iqn, bs, 0)
        if initiator:
            lio.create_acl(iqn, initiator, chap_user, chap_pass)
        lio.save_config()
    except Exception as exc:
        # 回滚卷，避免半成品；任何清理失败都不能掩盖原始错误
        log.exception("client.create 失败，开始回滚 %s", name)
        cleanup_errs = []
        for fn, arg in ((lio.delete_target, iqn), (lio.delete_backstore, bs)):
            try:
                fn(arg)
            except Exception as ce:  # noqa: BLE001
                cleanup_errs.append(f"{fn.__name__}: {ce}")
        try:
            zfs.destroy(cpath, recursive=True)
        except Exception as ce:  # noqa: BLE001
            cleanup_errs.append(f"zfs.destroy: {ce}")
        msg = str(exc)
        if cleanup_errs:
            msg += "（回滚告警：" + "; ".join(cleanup_errs) + "）"
        raise ServiceError(msg) from exc

    db.upsert_client(
        name,
        client_iqn=initiator,
        target_iqn=iqn,
        master=master,
        chap_user=chap_user,
        chap_pass=chap_pass,
        note=note,
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    db.log_op("client.create", target=name, detail=f"{cpath} <- {snap_full}")
    return {
        "name": name,
        "path": cpath,
        "target": iqn,
        "device": dev,
        "origin": snap_full,
        "chap": bool(chap_user),
    }


def delete_client(name: str, purge: bool = True) -> dict:
    """purge=True 时连同克隆卷一起删除；False 只摘除 iSCSI 映射保留数据。"""
    cpath = f"{clients_path()}/{name}"
    rec = db.get_client(name) or {}
    iqn = rec.get("target_iqn") or lio.build_iqn(
        settings()["iqn_prefix"], settings()["instance"], name)
    if iqn in lio.online_targets():
        raise ServiceError("客户端仍在线（有活跃 iSCSI 会话），请先在 Windows 中「断开」该目标")
    lio.delete_target(iqn)
    lio.delete_backstore(f"bs_{name}")
    lio.save_config()
    if purge:
        if zfs.exists(cpath):
            zfs.destroy(cpath, recursive=True)
        db.delete_client(name)
        db.log_op("client.delete", target=name, detail="purge")
    else:
        db.log_op("client.detach", target=name, detail="keep data")
    return {"deleted": name, "purged": purge}


def attach_client(name: str, initiator: str = "", chap_user: str = "", chap_pass: str = "") -> dict:
    """卷已存在但 target 丢失（如宿主重启后未恢复）时，重新挂载。"""
    st = settings()
    cpath = f"{clients_path()}/{name}"
    if not zfs.exists(cpath):
        raise ServiceError(f"客户端卷 {name} 不存在")
    iqn = lio.build_iqn(st["iqn_prefix"], st["instance"], name)
    bs = f"bs_{name}"
    dev = _wait_device(zfs.zvol_device(cpath))
    if lio.target_exists(iqn):
        lio.delete_target(iqn)
    if lio.backstore_exists(bs):
        lio.delete_backstore(bs)
    lio.create_backstore(bs, dev)
    lio.create_target(iqn, st["portal"], int(st["portal_port"]))
    lio.create_lun(iqn, bs, 0)
    if initiator:
        lio.create_acl(iqn, initiator, chap_user, chap_pass)
    lio.save_config()
    db.upsert_client(name, target_iqn=iqn, client_iqn=initiator,
                     chap_user=chap_user, chap_pass=chap_pass)
    db.log_op("client.attach", target=name, detail=iqn)
    return {"name": name, "target": iqn, "device": dev}


def reclone_client(name: str, snapshot: str = "") -> dict:
    """用母版快照重建客户端卷（会清空该客户端的 Steam 库，需二次确认）。"""
    rec = db.get_client(name) or {}
    master = rec.get("master") or ""
    if not master:
        # 从 origin 反推
        cpath = f"{clients_path()}/{name}"
        origin = zfs.get_prop(cpath, "origin")
        if not origin:
            raise ServiceError("无法确定该客户端的母版")
        master = origin.split("@")[0].rsplit("/", 1)[-1]
    iqn = lio.build_target_placeholder if False else (rec.get("target_iqn") or
          lio.build_iqn(settings()["iqn_prefix"], settings()["instance"], name))
    if iqn in lio.online_targets():
        raise ServiceError("客户端在线，请先断开 iSCSI 连接再重建")

    cpath = f"{clients_path()}/{name}"
    lio.delete_target(iqn)
    lio.delete_backstore(f"bs_{name}")
    if zfs.exists(cpath):
        zfs.destroy(cpath, recursive=True)
    db.delete_client(name)

    res = create_client(
        name, master,
        initiator=rec.get("client_iqn", ""),
        chap_user=rec.get("chap_user", ""),
        chap_pass=rec.get("chap_pass", ""),
        note=rec.get("note", ""),
        snapshot=snapshot,
    )
    db.log_op("client.reclone", target=name, detail=res["origin"])
    return res


def resize_client(name: str, size_gb: float) -> dict:
    cpath = f"{clients_path()}/{name}"
    if not zfs.exists(cpath):
        raise ServiceError(f"客户端 {name} 不存在")
    zfs.set_prop(cpath, "volsize", f"{size_gb:g}G")
    db.log_op("client.resize", target=name, detail=f"{size_gb:g}G")
    return {"name": name, "volsize": size_gb}


def client_secret(name: str) -> dict:
    rec = db.get_client(name) or {}
    return {
        "name": name,
        "target": rec.get("target_iqn", ""),
        "initiator": rec.get("client_iqn", ""),
        "chap_user": rec.get("chap_user", ""),
        "chap_pass": rec.get("chap_pass", ""),
    }


# ---------------------------------------------------------------- 内部
def _wait_device(dev: str, timeout: float = 15.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(dev):
            return dev
        time.sleep(0.3)
    # zvol 符号链接可能因容器内无 udev 而未生成，改用 udevadm 触发
    zfs.run(["udevadm", "trigger", "--subsystem-match=block"], check=False)
    for _ in range(20):
        if os.path.exists(dev):
            return dev
        time.sleep(0.5)
    raise ServiceError(f"zvol 设备节点未就绪: {dev}")


def _target_by_device(dev: str) -> str:
    bsmap = _backstore_map()
    bs = bsmap.get(dev)
    if not bs:
        return ""
    for t in lio.list_targets():
        if any(l["backstore"] == bs for l in t["luns"]):
            return t["iqn"]
    return ""


def _target_online_by_device(dev: str) -> bool:
    iqn = _target_by_device(dev)
    return bool(iqn and iqn in lio.online_targets())


def gen_chap_pass(n: int = 16) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))
