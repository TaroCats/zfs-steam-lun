"""ZFS 操作封装（全部使用参数列表调用，不经过 shell）。"""
from __future__ import annotations

import re
import time
from typing import Iterable

from . import hostexec

SNAP_TS_FMT = "%Y%m%d%H%M%S"


class ZfsError(RuntimeError):
    pass


def run(args: list[str], timeout: int = 300, check: bool = True):
    """统一经 hostexec 执行（必要时进入宿主命名空间，保证 ZFS 版本一致）。"""
    try:
        return hostexec.run(args, timeout=timeout, check=check)
    except hostexec.HostExecError as e:
        raise ZfsError(str(e)) from e



# ---------------------------------------------------------------- 池
def pools() -> list[dict]:
    out = run(["zpool", "list", "-Hp", "-o", "name,size,alloc,free,health,altroot,fragmentation"]).stdout
    result = []
    for line in out.strip().splitlines():
        f = line.split("\t")
        if len(f) < 5:
            continue
        try:
            size = int(f[1]); alloc = int(f[2]); free = int(f[3])
        except ValueError:
            size = alloc = free = 0
        result.append({
            "name": f[0],
            "size": size,
            "alloc": alloc,
            "free": free,
            "health": f[4],
            "altroot": f[5] if len(f) > 5 else "-",
            "frag": f[6] if len(f) > 6 else "-",
        })
    return result


def pool_status(pool: str) -> str:
    return run(["zpool", "status", "-L", pool], check=False).stdout


# ---------------------------------------------------------------- 数据集 / 卷
def list_volumes(root: str) -> list[dict]:
    """列出 root 下所有 zvol（含子级）。"""
    p = run(
        ["zfs", "list", "-Hp", "-r", "-t", "volume", "-o",
         "name,volsize,used,refer,origin,compressratio,volblocksize,written"],
        check=False,
    )
    if p.returncode != 0:
        return []
    vols = []
    root_slash = root.rstrip("/") + "/"
    for line in p.stdout.strip().splitlines():
        f = line.split("\t")
        if len(f) < 7:
            continue
        name = f[0]
        if name != root and not name.startswith(root_slash):
            continue
        vols.append({
            "name": name,
            "volsize": _int(f[1]),
            "used": _int(f[2]),
            "refer": _int(f[3]),
            "origin": "" if f[4] in ("-", "") else f[4],
            "compress": f[5],
            "volblocksize": f[6],
            "written": _int(f[7]) if len(f) > 7 else 0,
        })
    return vols


def list_datasets(root: str) -> list[dict]:
    p = run(
        ["zfs", "list", "-Hp", "-r", "-o",
         "name,type,used,avail,refer,mountpoint,compression"],
        check=False,
    )
    if p.returncode != 0:
        return []
    rows = []
    for line in p.stdout.strip().splitlines():
        f = line.split("\t")
        if len(f) < 5:
            continue
        if f[0] != root and not f[0].startswith(root.rstrip("/") + "/"):
            continue
        rows.append({
            "name": f[0], "type": f[1], "used": _int(f[2]),
            "avail": _int(f[3]), "refer": _int(f[4]),
            "mountpoint": f[5] if len(f) > 5 else "-",
            "compression": f[6] if len(f) > 6 else "-",
        })
    return rows


def exists(path: str) -> bool:
    return run(["zfs", "list", "-H", "-o", "name", path], check=False).returncode == 0


def create_dataset(path: str, props: dict | None = None) -> None:
    args = ["zfs", "create"]
    for k, v in (props or {}).items():
        args += ["-o", f"{k}={v}"]
    args.append(path)
    run(args)


def create_zvol(
    path: str,
    size_gb: float,
    volblocksize: str = "16K",
    compression: str = "lz4",
    sync: str = "standard",
) -> None:
    run([
        "zfs", "create", "-s", "-V", f"{size_gb:g}G",
        "-o", f"volblocksize={volblocksize}",
        "-o", f"compression={compression}",
        "-o", f"sync={sync}",
        "-o", "volmode=dev",
        path,
    ])


def set_prop(path: str, key: str, value: str) -> None:
    run(["zfs", "set", f"{key}={value}", path])


def get_prop(path: str, key: str) -> str:
    p = run(["zfs", "get", "-H", "-o", "value", key, path], check=False)
    return p.stdout.strip() if p.returncode == 0 else ""


def set_readonly(path: str, ro: bool) -> None:
    set_prop(path, "readonly", "on" if ro else "off")


# ---------------------------------------------------------------- 快照
def snapshot(path: str, snap: str) -> str:
    full = f"{path}@{snap}"
    run(["zfs", "snapshot", full])
    return full


def list_snapshots(path: str) -> list[dict]:
    p = run(
        ["zfs", "list", "-Hp", "-t", "snapshot", "-o", "name,used,creation,refer",
         "-s", "creation", "-r", "-d", "1", path],
        check=False,
    )
    if p.returncode != 0:
        return []
    snaps = []
    for line in p.stdout.strip().splitlines():
        f = line.split("\t")
        if len(f) < 4 or "@" not in f[0]:
            continue
        snaps.append({
            "name": f[0].split("@", 1)[1],
            "full": f[0],
            "used": _int(f[1]),
            "creation": _int(f[2]),
            "refer": _int(f[3]),
        })
    return snaps


def destroy_snapshot(full: str) -> None:
    run(["zfs", "destroy", full])


def rollback(path: str, snap: str) -> None:
    run(["zfs", "rollback", "-r", f"{path}@{snap}"])


# ---------------------------------------------------------------- 克隆 / 删除
def clone(snap_full: str, dest: str) -> None:
    run(["zfs", "clone", "-o", "volmode=dev", snap_full, dest])


def destroy(path: str, recursive: bool = False) -> None:
    """销毁数据集；设备刚被 iSCSI 释放时内核可能仍占用，故对 busy 重试。"""
    args = ["zfs", "destroy"]
    if recursive:
        args.append("-r")
    args.append(path)
    last: Exception | None = None
    for attempt in range(6):
        try:
            run(args)
            return
        except ZfsError as exc:  # noqa: PERF203
            last = exc
            if "busy" not in str(exc).lower():
                raise
            time.sleep(0.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def promote(path: str) -> None:
    run(["zfs", "promote", path])


def dependents(path: str) -> list[str]:
    """返回直接以 path 为 origin 的克隆卷。"""
    p = run(["zfs", "list", "-H", "-o", "name,origin", "-t", "volume"], check=False)
    if p.returncode != 0:
        return []
    deps = []
    for line in p.stdout.strip().splitlines():
        f = line.split("\t")
        if len(f) == 2 and f[1] == path:
            deps.append(f[0])
    return deps


def zvol_device(path: str) -> str:
    return f"/dev/zvol/{path}"


def _int(v: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse_size(text: str) -> float:
    """把 '500G' / '1.5T' 转成 GB 数值。"""
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([KMGTkmgt]?)[iI]?[bB]?\s*", text)
    if not m:
        raise ZfsError(f"无法解析容量: {text}")
    num = float(m.group(1))
    unit = m.group(2).upper()
    factor = {"": 1 / 1024 ** 3, "K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1, "T": 1024}[unit]
    return num * factor
