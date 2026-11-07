"""FastAPI 入口。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, lio, service, zfs
from .config import APP_VERSION

app = FastAPI(title="ZFS Steam LUN", version=APP_VERSION, docs_url=None, redoc_url=None)

STATIC_DIR = Path("/app/static")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    if not db.get_setting("iqn_prefix"):
        s = service.settings()
        db.set_setting("iqn_prefix", s["iqn_prefix"])
        db.set_setting("instance", s["instance"])
        db.set_setting("portal", s["portal"])
        db.set_setting("portal_port", s["portal_port"])
    print(f"[init] ZFS Steam LUN v{APP_VERSION} 启动完成，控制台无需登录", flush=True)


@app.middleware("http")
async def no_cache_middleware(request: Request, call_next):
    """静态资源禁用浏览器缓存，避免升级后仍加载旧版前端。"""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    elif path.startswith("/assets/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


def _err(e: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------- 基础
@app.get("/api/health")
def health():
    return {"ok": True, "version": APP_VERSION}


@app.get("/api/overview")
def overview():
    try:
        return service.overview()
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


@app.get("/api/scan")
def scan():
    try:
        return service.scan()
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


@app.get("/api/settings")
def get_settings():
    return service.settings()


class SettingsIn(BaseModel):
    root_dataset: str | None = None
    iqn_prefix: str | None = None
    instance: str | None = None
    portal: str | None = None
    portal_port: int | None = None


@app.post("/api/settings")
def post_settings(body: SettingsIn):
    try:
        if body.root_dataset:
            svc = service.settings()
            if svc["root_dataset"] and svc["root_dataset"] != body.root_dataset:
                # 切换根数据集前确认旧数据集下没有受管卷
                old = service.scan()
                if old["masters"] or old["clients"]:
                    raise service.ServiceError("当前根数据集下仍有母版/客户端，不能直接切换")
        return service.save_settings(**body.model_dump())
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


@app.get("/api/pools")
def pools():
    try:
        return service.pools()
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


@app.get("/api/pools/{pool}/status")
def pool_status(pool: str):
    return {"pool": pool, "status": zfs.pool_status(pool)}


@app.get("/api/diagnostics")
def diagnostics():
    out = {"zfs": False, "configfs": False, "dev_zfs": False, "targetcli": False,
           "details": {}}
    p = zfs.run(["zfs", "version"], check=False)
    out["zfs"] = p.returncode == 0
    out["details"]["zfs"] = (p.stdout or p.stderr).strip().splitlines()[:2]
    out["dev_zfs"] = os.path.exists("/dev/zfs")
    out["configfs"] = os.path.isdir("/sys/kernel/config/target/iscsi")
    p2 = lio.tc("/backstores/block", "ls", check=False)
    out["targetcli"] = bool(p2.strip())
    out["details"]["targetcli"] = p2.strip().splitlines()[:3]
    p3 = zfs.run(["zpool", "list", "-H", "-o", "name,health"], check=False)
    out["details"]["pools"] = p3.stdout.strip().splitlines()
    return out


# ---------------------------------------------------------------- 母版
class MasterIn(BaseModel):
    name: str
    size_gb: float = Field(gt=0)
    volblocksize: str = "16K"
    compression: str = "lz4"
    note: str = ""


@app.post("/api/masters")
def create_master(body: MasterIn):
    try:
        return service.create_master(body.name, body.size_gb, body.volblocksize,
                                     body.compression, body.note)
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


@app.delete("/api/masters/{name}")
def delete_master(name: str, force: bool = False):
    try:
        return service.delete_master(name, force)
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


class SnapIn(BaseModel):
    name: str = ""


@app.post("/api/masters/{name}/snapshot")
def snapshot_master(name: str, body: SnapIn):
    try:
        return service.snapshot_master(name, body.name)
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


@app.get("/api/masters/{name}/snapshots")
def list_snaps(name: str):
    try:
        return service.list_snapshots(name)
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


@app.delete("/api/masters/{name}/snapshots/{snap}")
def delete_snap(name: str, snap: str):
    try:
        return service.delete_snapshot(name, snap)
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


class MountIn(BaseModel):
    initiator: str = ""
    chap_user: str = ""
    chap_pass: str = ""


@app.post("/api/masters/{name}/mount")
def mount_master(name: str, body: MountIn):
    try:
        return service.mount_master(name, body.initiator, body.chap_user, body.chap_pass)
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


@app.post("/api/masters/{name}/unmount")
def unmount_master(name: str):
    try:
        return service.unmount_master(name)
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


# ---------------------------------------------------------------- 客户端
class ClientIn(BaseModel):
    name: str
    master: str
    initiator: str = ""
    chap_user: str = ""
    chap_pass: str = ""
    note: str = ""
    snapshot: str = ""


@app.post("/api/clients")
def create_client(body: ClientIn):
    try:
        return service.create_client(**body.model_dump())
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


class AttachIn(BaseModel):
    initiator: str = ""
    chap_user: str = ""
    chap_pass: str = ""


@app.post("/api/clients/{name}/attach")
def attach_client(name: str, body: AttachIn):
    try:
        return service.attach_client(name, body.initiator, body.chap_user, body.chap_pass)
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


@app.delete("/api/clients/{name}")
def delete_client(name: str, purge: bool = True):
    try:
        return service.delete_client(name, purge)
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


class RecloneIn(BaseModel):
    snapshot: str = ""


@app.post("/api/clients/{name}/reclone")
def reclone_client(name: str, body: RecloneIn):
    try:
        return service.reclone_client(name, body.snapshot)
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


class ResizeIn(BaseModel):
    size_gb: float = Field(gt=0)


@app.post("/api/clients/{name}/resize")
def resize_client(name: str, body: ResizeIn):
    try:
        return service.resize_client(name, body.size_gb)
    except (service.ServiceError, zfs.ZfsError, lio.LioError) as e:
        raise _err(e)


@app.get("/api/clients/{name}/secret")
def client_secret(name: str):
    return service.client_secret(name)


# ---------------------------------------------------------------- 杂项
@app.get("/api/sessions")
def sessions():
    return lio.sessions()


@app.get("/api/logs")
def logs(limit: int = 200):
    return db.list_ops(limit)


@app.post("/api/saveconfig")
def saveconfig():
    lio.save_config()
    db.log_op("target.saveconfig")
    return {"ok": True}


@app.get("/api/chap")
def gen_chap():
    return {"password": service.gen_chap_pass()}


# ---------------------------------------------------------------- 静态
app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/{full_path:path}")
def spa(full_path: str):
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")
    f = STATIC_DIR / full_path
    if f.is_file():
        return FileResponse(f)
    return FileResponse(STATIC_DIR / "index.html")
