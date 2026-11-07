FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST_BIN_MODE=nsenter

WORKDIR /app

# 说明：镜像内不安装 zfsutils-linux / targetcli-fb。
# 原因是 fnOS 宿主使用自编译的 ZFS 2.4.1，容器内发行版自带的用户态工具版本不一致会拒绝工作。
# 这里通过 nsenter 进入宿主 mount namespace 直接调用宿主 /usr/sbin/zfs 与 /usr/bin/targetcli，
# 保证用户态与内核模块严格同版本。需要 docker 的 privileged + pid: host。

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

EXPOSE 8890
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8890", "--proxy-headers"]
