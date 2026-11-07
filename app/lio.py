"""LIO / iSCSI 目标管理。

通过 targetcli 操作宿主内核的 configfs（容器已挂载 /sys/kernel/config），
读取状态则直接解析 configfs 与 /sys/class/iscsi_session，避免解析 targetcli 的彩色文本。

拓扑约定（每客户端一个独立 target，隔离最彻底）：
    target IQN  <prefix>:<instance>-<slug>
    backstore   bs_<slug>
    LUN 0       -> /dev/zvol/<clone>
    ACL         客户端自己的 initiator IQN（可选 CHAP）
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import time

from . import hostexec

CONFIGFS = "/sys/kernel/config/target"
ISCSI_ROOT = f"{CONFIGFS}/iscsi"
def _block_root() -> str:
    """block backstore 在 configfs 中位于 core/iblock_<n>/（targetcli 的 /backstores/block）。
    不同内核/版本序号不同，故动态探测。"""
    for cand in (f"{CONFIGFS}/core/block", f"{CONFIGFS}/core/iblock_0"):
        if os.path.isdir(cand):
            return cand
    found = sorted(glob.glob(f"{CONFIGFS}/core/iblock*"))
    return found[0] if found else f"{CONFIGFS}/core/iblock_0"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


class LioError(RuntimeError):
    pass


# ---------------------------------------------------------------- 基础
def tc(*args: str, check: bool = True) -> str:
    cmd = ["targetcli", *args]
    try:
        p = hostexec.run(cmd, timeout=120, check=check)
    except hostexec.HostExecError as e:
        raise LioError(str(e)) from e
    return p.stdout



def configfs_ready() -> bool:
    return os.path.isdir(ISCSI_ROOT)


def save_config() -> None:
    os.makedirs("/etc/target", exist_ok=True)
    tc("saveconfig", check=False)


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return default


def _num(path: str, default: int = 0) -> int:
    try:
        return int(_read(path, str(default)))
    except ValueError:
        return default


def validate_slug(slug: str) -> str:
    if not SLUG_RE.match(slug):
        raise LioError("名称只能是 1-24 位小写字母、数字或短横线，且以字母/数字开头")
    return slug


def build_iqn(prefix: str, instance: str, slug: str) -> str:
    return f"{prefix}:{instance}-{slug}"


# ---------------------------------------------------------------- 查询
def list_backstores() -> list[dict]:
    out = []
    for path in glob.glob(f"{_block_root()}/*"):
        if not os.path.isdir(path):
            continue
        name = os.path.basename(path)
        udev = _read(f"{path}/udev_path")
        size = _num(f"{path}/info")
        out.append({
            "name": name,
            "udev_path": udev,
            "size": size,
            "enabled": _num(f"{path}/enable", 1) == 1,
        })
    return out


def list_targets() -> list[dict]:
    out = []
    if not os.path.isdir(ISCSI_ROOT):
        return out
    for path in sorted(glob.glob(f"{ISCSI_ROOT}/*")):
        if not os.path.isdir(path):
            continue
        iqn = os.path.basename(path)
        if iqn in ("discovery_auth",):
            continue
        out.append(_target_detail(iqn))
    return out


def _target_detail(iqn: str) -> dict:
    tpath = f"{ISCSI_ROOT}/{iqn}"
    tpgt = None
    for d in glob.glob(f"{tpath}/tpgt_*"):
        tpgt = d
        break
    info = {
        "iqn": iqn,
        "tpgt": os.path.basename(tpgt).replace("tpgt_", "") if tpgt else "",
        "portals": [],
        "luns": [],
        "acls": [],
        "sessions": [],
        "online": False,
        "chap": False,
        "auth_user": "",
    }
    if not tpgt:
        return info

    for p in glob.glob(f"{tpgt}/np/*"):
        info["portals"].append(os.path.basename(p))

    for lun_dir in sorted(glob.glob(f"{tpgt}/lun/lun_*")):
        lun_id = int(os.path.basename(lun_dir).replace("lun_", ""))
        backstore = ""
        for entry in os.listdir(lun_dir):
            full = os.path.join(lun_dir, entry)
            if os.path.islink(full):
                backstore = os.path.basename(os.readlink(full))
                break
        info["luns"].append({"id": lun_id, "backstore": backstore})

    for acl_dir in glob.glob(f"{tpgt}/acls/*"):
        acl_iqn = os.path.basename(acl_dir)
        userid = _read(f"{acl_dir}/auth/userid")
        info["acls"].append({
            "initiator": acl_iqn,
            "userid": userid,
            "chap": bool(userid),
            "mapped_luns": [
                int(os.path.basename(x).replace("lun_", ""))
                for x in glob.glob(f"{acl_dir}/luns/lun_*")
            ],
        })
        if userid:
            info["chap"] = True
            info["auth_user"] = userid

    for s in glob.glob(f"{tpgt}/sessions/*"):
        info["sessions"].append(os.path.basename(s))
    if info["sessions"]:
        info["online"] = True

    return info


def target_exists(iqn: str) -> bool:
    return os.path.isdir(f"{ISCSI_ROOT}/{iqn}")


def backstore_exists(name: str) -> bool:
    return os.path.isdir(f"{_block_root()}/{name}")


def sessions() -> list[dict]:
    """读取 /sys/class/iscsi_session，返回所有登录会话。"""
    out = []
    for sdir in sorted(glob.glob("/sys/class/iscsi_session/session*")):
        state = _read(f"{sdir}/state")
        targetname = _read(f"{sdir}/targetname")
        tpgt = _read(f"{sdir}/tpgt")
        if not targetname:
            continue
        out.append({
            "session": os.path.basename(sdir),
            "target": targetname,
            "tpgt": tpgt,
            "state": state,
            "online": state.upper() in ("LOGGED_IN", "LOGIN"),
        })
    return out


def online_targets() -> set[str]:
    return {s["target"] for s in sessions() if s["online"]}


# ---------------------------------------------------------------- 变更
def create_backstore(name: str, dev: str, readonly: bool = False) -> None:
    if backstore_exists(name):
        raise LioError(f"backstore {name} 已存在")
    tc("/backstores/block", "create", f"name={name}", f"dev={dev}")
    if readonly:
        tc(f"/backstores/block/{name}", "set", "attribute", "emulate_write_cache=0")
        tc(f"/backstores/block/{name}", "set", "attribute", "readonly=1", check=False)


def create_target(iqn: str, portal: str = "0.0.0.0", port: int = 3260) -> None:
    if not target_exists(iqn):
        tc("/iscsi", "create", iqn)
    tpgt = f"/iscsi/{iqn}/tpg1"
    tc(tpgt, "set", "attribute", "authentication=0", check=False)
    tc(tpgt, "set", "attribute", "generate_node_acls=0", check=False)
    tc(tpgt, "set", "attribute", "demo_mode_write_protect=0", check=False)
    # portal 已存在时 targetcli 会报错，忽略
    tc(f"{tpgt}/portals", "create", portal, str(port), check=False)


def create_lun(iqn: str, backstore: str, lun_id: int = 0) -> None:
    tc(
        f"/iscsi/{iqn}/tpg1/luns",
        "create",
        f"/backstores/block/{backstore}",
        str(lun_id),
    )


def create_acl(iqn: str, initiator: str, chap_user: str = "", chap_pass: str = "") -> None:
    tpg = f"/iscsi/{iqn}/tpg1"
    acl = f"{tpg}/acls/{initiator}"
    tc(f"{tpg}/acls", "create", initiator)
    if chap_user and chap_pass:
        tc(acl, "set", "auth", f"userid={chap_user}", f"password={chap_pass}")
        tc(acl, "set", "auth", f"mutual_userid={chap_user}", f"mutual_password={chap_pass}", check=False)
        tc(tpg, "set", "attribute", "authentication=1")
    # 仅该 initiator 可见 LUN 0
    tc(f"{acl}/luns", "create", "/backstores/block/" + _single_backstore(iqn), "0", check=False)


def _single_backstore(iqn: str) -> str:
    detail = _target_detail(iqn)
    for lun in detail["luns"]:
        if lun["id"] == 0 and lun["backstore"]:
            return lun["backstore"]
    raise LioError("目标下没有可映射的 LUN 0")


def remove_lun(iqn: str, lun_id: int = 0) -> None:
    for acl_dir in glob.glob(f"{ISCSI_ROOT}/{iqn}/tpgt_*/acls/*"):
        tc(f"/iscsi/{iqn}/tpg1/acls/{os.path.basename(acl_dir)}/luns",
           "delete", str(lun_id), check=False)
    tc(f"/iscsi/{iqn}/tpg1/luns", "delete", str(lun_id), check=False)


def delete_target(iqn: str) -> None:
    if not target_exists(iqn):
        return
    # 先摘 LUN 再删 target，避免 LIO 拒绝删除
    for ldir in glob.glob(f"{ISCSI_ROOT}/{iqn}/tpgt_*/lun/lun_*"):
        lun_id = os.path.basename(ldir).replace("lun_", "")
        tc(f"/iscsi/{iqn}/tpg1/luns", "delete", lun_id, check=False)
    tc("/iscsi", "delete", iqn, check=False)
    if target_exists(iqn):
        # 兜底：targetcli 失败时直接 rmdir（configfs 语义：非空目录不能删）
        for acl_dir in glob.glob(f"{ISCSI_ROOT}/{iqn}/tpgt_*/acls/*"):
            for lnk in glob.glob(f"{acl_dir}/luns/*"):
                os.unlink(lnk)
            try:
                os.rmdir(acl_dir)
            except OSError:
                pass
        for ldir in glob.glob(f"{ISCSI_ROOT}/{iqn}/tpgt_*/lun/lun_*"):
            try:
                os.rmdir(ldir)
            except OSError:
                pass
        try:
            shutil.rmtree(f"{ISCSI_ROOT}/{iqn}")
        except OSError as e:
            raise LioError(f"删除 target 失败: {e}") from e


def delete_backstore(name: str) -> None:
    """删除 block backstore。设备刚被 LUN 释放时内核回收有延迟，故重试。"""
    if not backstore_exists(name):
        return
    for _ in range(5):
        tc("/backstores/block", "delete", name, check=False)
        if not backstore_exists(name):
            return
        try:
            os.rmdir(f"{_block_root()}/{name}")
        except OSError:
            pass
        if not backstore_exists(name):
            return
        time.sleep(0.4)
    raise LioError(f"无法删除 backstore {name}（设备仍被占用）")


def set_backstore_readonly(name: str, readonly: bool) -> None:
    if not backstore_exists(name):
        return
    tc(f"/backstores/block/{name}", "set", "attribute",
       "emulate_write_cache=0" if readonly else "emulate_write_cache=1", check=False)
    tc(f"/backstores/block/{name}", "set", "attribute",
       "readonly=1" if readonly else "readonly=0", check=False)
