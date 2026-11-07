"""运行期配置。

所有可调项都可在容器环境变量或 Web 设置页中修改；
设置页写入的值存进 sqlite 的 settings 表，优先级高于环境变量默认值。
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.environ.get("APP_DATA", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "app.db"
TOKEN_FILE = DATA_DIR / "token.txt"

PORT = int(os.environ.get("APP_PORT", "8890"))
APP_VERSION = "1.0.1"

# 新建 target 的 IQN 前缀（不含实例名）
DEFAULT_IQN_PREFIX = os.environ.get("IQN_PREFIX", "iqn.2026-11.com.fylun")
DEFAULT_INSTANCE = os.environ.get("IQN_INSTANCE", "steam")

# 默认 portal
DEFAULT_PORTAL = os.environ.get("ISCSI_PORTAL", "0.0.0.0")
DEFAULT_PORTAL_PORT = 3260

# 母版默认参数
DEFAULT_VOLBLOCKSIZE = "16K"
DEFAULT_COMPRESSION = "lz4"
DEFAULT_SYNC = "standard"


def load_token() -> str:
    """优先环境变量，其次 /data/token.txt，都没有就生成一个并落盘。"""
    env = os.environ.get("APP_TOKEN", "").strip()
    if env:
        return env
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    t = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    TOKEN_FILE.chmod(0o600)
    return t


APP_TOKEN = load_token()
