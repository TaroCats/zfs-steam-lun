"""在宿主机命名空间中执行系统命令。

镜像里没有 zfs / targetcli，因为 fnOS 的 ZFS 是自编译的 2.4.1，
容器内发行版包（2.1/2.3）与宿主内核模块版本不一致会直接失败。

做法：privileged + pid: host 的容器里，用
    nsenter -t 1 -m -- /usr/sbin/zfs ...
进入宿主 mount namespace，调用宿主自身的二进制，版本天然一致。
宿主二进制路径在首次使用时探测并缓存（容器 rootfs 里没有这些文件，不能用 os.path.exists 判断）。
若镜像内确实存在该命令（本地开发场景），则优先用本地。
"""
from __future__ import annotations

import os
import shutil
import subprocess

HOST_BIN_CANDIDATES = {
    "zfs": ["/usr/sbin/zfs", "/sbin/zfs", "/usr/bin/zfs"],
    "zpool": ["/usr/sbin/zpool", "/sbin/zpool", "/usr/bin/zpool"],
    "targetcli": ["/usr/bin/targetcli", "/usr/sbin/targetcli", "/usr/local/bin/targetcli"],
    "udevadm": ["/usr/bin/udevadm", "/sbin/udevadm", "/bin/udevadm"],
    "zdb": ["/usr/sbin/zdb", "/sbin/zdb"],
}

_HOST_PATH_CACHE: dict[str, str] = {}
_NSENTER = "nsenter"


class HostExecError(RuntimeError):
    pass


def _host_lookup(prog: str) -> str | None:
    """在宿主 mount namespace 里查找可执行文件。"""
    if prog in _HOST_PATH_CACHE:
        return _HOST_PATH_CACHE[prog]

    for cand in HOST_BIN_CANDIDATES.get(prog, []):
        try:
            rc = subprocess.run(
                [_NSENTER, "-t", "1", "-m", "--", "/bin/sh", "-c", f"test -x {cand}"],
                capture_output=True, timeout=15,
            ).returncode
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if rc == 0:
            _HOST_PATH_CACHE[prog] = cand
            return cand

    # 回退：宿主 PATH 中查找（登录 shell，确保包含 /usr/sbin）
    try:
        p = subprocess.run(
            [_NSENTER, "-t", "1", "-m", "--", "/bin/sh", "-lc",
             f"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; command -v {prog}"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = (p.stdout or "").strip().splitlines()
    if p.returncode == 0 and out:
        _HOST_PATH_CACHE[prog] = out[0]
        return out[0]
    return None


def wrap(args: list[str]) -> list[str]:
    """返回真正要执行的 argv。"""
    if not args:
        raise HostExecError("空命令")
    prog = args[0]
    local = shutil.which(prog)
    if local:
        return list(args)
    if not os.path.exists("/proc/1/ns/mnt"):
        raise HostExecError("无法访问宿主命名空间，请确认容器使用 pid: host + privileged")
    hp = _host_lookup(prog)
    if not hp:
        raise HostExecError(f"宿主上找不到命令 {prog}，请确认已安装 zfs / targetcli")
    return [_NSENTER, "-t", "1", "-m", "--", hp, *args[1:]]


def run(args: list[str], timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    argv = wrap(args)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise HostExecError(f"命令不可用: {' '.join(argv[:2])}") from e
    except subprocess.TimeoutExpired as e:
        raise HostExecError(f"命令超时: {args[0]}") from e
    if check and p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip() or f"{args[0]} 执行失败"
        raise HostExecError(" ".join(msg.split()))
    return p


def available(prog: str) -> bool:
    try:
        return run([prog, "-V"], timeout=20, check=False).returncode in (0, 1, 2) or bool(_host_lookup(prog))
    except HostExecError:
        return False


def resolved(prog: str) -> str:
    local = shutil.which(prog)
    if local:
        return f"local:{local}"
    hp = _host_lookup(prog)
    return f"host:{hp}" if hp else "missing"
