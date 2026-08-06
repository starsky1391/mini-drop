# try1 三机手动部署全流程

本文基于当前 `try1` 分支和当前虚拟机地址编写，目标是手动完成 `control`、`worker1`、`worker2` 的部署与联调。

当前地址如下：

```text
control  -> 172.18.88.237
worker1  -> 172.18.90.144
worker2  -> 172.18.87.120
```

## 0. 宿主机先确认连通性

先在 Windows 宿主机上确认三台 VM 的 22 端口可达。

```powershell
Test-NetConnection 172.18.88.237 -Port 22
Test-NetConnection 172.18.90.144 -Port 22
Test-NetConnection 172.18.87.120 -Port 22
```

如果你要用 PuTTY 的 `plink` 做批量登录，这三台当前的 host key 如下：

```text
control  -> ssh-ed25519 255 SHA256:XzpTmpz7pW7YfV6LPuBMAv2Ov+TZbZnzmm+Ztt4Ay2o
worker1  -> ssh-ed25519 255 SHA256:Ic+eqmkBSQNM4zzBfgaog5y7wSxNMAj9HZi/BaWFQW0
worker2  -> ssh-ed25519 255 SHA256:aYvFCN52/Z1kfSNYJ9mRzi0EGZ+KNx0ilou5Nsgvd5E
```

安装 PuTTY：

```powershell
winget install --id PuTTY.PuTTY -e --accept-package-agreements --accept-source-agreements
```

宿主机上的单次登录验证：

```powershell
& 'C:\Program Files\PuTTY\plink.exe' -ssh -batch -pw admin -hostkey 'ssh-ed25519 255 SHA256:XzpTmpz7pW7YfV6LPuBMAv2Ov+TZbZnzmm+Ztt4Ay2o' control@172.18.88.237 hostname
& 'C:\Program Files\PuTTY\plink.exe' -ssh -batch -pw admin -hostkey 'ssh-ed25519 255 SHA256:Ic+eqmkBSQNM4zzBfgaog5y7wSxNMAj9HZi/BaWFQW0' worker1@172.18.90.144 hostname
& 'C:\Program Files\PuTTY\plink.exe' -ssh -batch -pw admin -hostkey 'ssh-ed25519 255 SHA256:aYvFCN52/Z1kfSNYJ9mRzi0EGZ+KNx0ilou5Nsgvd5E' worker2@172.18.87.120 hostname
```

## 1. Control VM

在 `control` 上执行以下命令。

### 1.1 进入仓库并切到 try1

```bash
cd /home/control/mini-drop
git fetch origin
git checkout try1
git pull --ff-only origin try1
git branch --show-current
```

### 1.2 准备控制端环境文件

```bash
cp deploy/env/control.env.example deploy/env/control.env
vi deploy/env/control.env
```

编辑时至少把这些值改成当前环境可用的内容：

```dotenv
POSTGRES_PASSWORD=请替换成真实密码
MINIO_SECRET_KEY=请替换成真实密钥
MINI_DROP_API_KEY=请替换成真实 API Key
MINI_DROP_GRPC_TOKEN=请替换成真实共享 Token
MINIO_PUBLIC_ENDPOINT=http://172.18.88.237:9000
```

### 1.3 生成控制端证书

```bash
bash deploy/scripts/generate-dev-certs.sh 172.18.88.237
openssl x509 -in deploy/certs/server.crt -noout -subject -issuer -ext subjectAltName
```

### 1.4 启动 Control 栈

```bash
docker compose --env-file deploy/env/control.env -f docker-compose.control.yml config --quiet
docker compose --env-file deploy/env/control.env -f docker-compose.control.yml up -d --build
docker compose --env-file deploy/env/control.env -f docker-compose.control.yml ps
docker compose --env-file deploy/env/control.env -f docker-compose.control.yml logs -f server web
```

### 1.5 导出 CA 证书给 Worker

只把 `deploy/certs/ca.crt` 分发给两台 Worker。`ca.key` 不要分发。

如果你要先放到宿主机，再拷到 Worker：

```powershell
& 'C:\Program Files\PuTTY\pscp.exe' -scp -pw admin -hostkey 'ssh-ed25519 255 SHA256:XzpTmpz7pW7YfV6LPuBMAv2Ov+TZbZnzmm+Ztt4Ay2o' control@172.18.88.237:/home/control/mini-drop/deploy/certs/ca.crt C:\1Project\project_web\mini-drop\deploy\certs\ca.crt
```

## 2. Worker1 VM

在 `worker1` 上执行以下命令。

### 2.1 进入仓库并切到 try1

```bash
cd /home/worker1/mini-drop
git fetch origin
git checkout try1
git pull --ff-only origin try1
git branch --show-current
```

### 2.2 准备 Worker1 环境文件

```bash
cp deploy/env/worker.env.example deploy/env/worker.env
mkdir -p deploy/certs
vi deploy/env/worker.env
```

把 Worker1 的配置改成下面这组：

```dotenv
AGENT_ID=linux-worker-1
AGENT_IP_ADDR=172.18.90.144
AGENT_GRPC_ADDR=172.18.88.237:50051
AGENT_HEARTBEAT_INTERVAL_SEC=5
AGENT_GRPC_TLS_SERVER_NAME=
MINI_DROP_GRPC_TOKEN=请与 Control 保持一致
MINIO_ENDPOINT=http://172.18.88.237:9000
MINIO_ACCESS_KEY=mini_drop_worker
MINIO_SECRET_KEY=请与 Control 保持一致
MINIO_BUCKET=mini-drop
MINIO_SECURE=0
```

### 2.3 放入 CA 证书

把 `control` 上导出的 `ca.crt` 放到：

```bash
cp /path/to/ca.crt deploy/certs/ca.crt
```

如果你是先拷到宿主机，再手动上传到 VM，就把文件放到 `deploy/certs/ca.crt` 这个路径。

### 2.4 启动 Worker1

```bash
docker compose --env-file deploy/env/worker.env -f docker-compose.worker.yml config --quiet
docker compose --env-file deploy/env/worker.env -f docker-compose.worker.yml up -d --build
docker compose --env-file deploy/env/worker.env -f docker-compose.worker.yml ps
docker compose --env-file deploy/env/worker.env -f docker-compose.worker.yml logs -f agent
```

### 2.5 验证 Worker1

```bash
curl --cacert deploy/certs/ca.crt https://172.18.88.237/api/healthz
```

## 3. Worker2 VM

在 `worker2` 上执行以下命令。

### 3.1 进入仓库并切到 try1

```bash
cd /home/worker2/mini-drop
git fetch origin
git checkout try1
git pull --ff-only origin try1
git branch --show-current
```

### 3.2 准备 Worker2 环境文件

```bash
cp deploy/env/worker.env.example deploy/env/worker.env
mkdir -p deploy/certs
vi deploy/env/worker.env
```

把 Worker2 的配置改成下面这组：

```dotenv
AGENT_ID=linux-worker-2
AGENT_IP_ADDR=172.18.87.120
AGENT_GRPC_ADDR=172.18.88.237:50051
AGENT_HEARTBEAT_INTERVAL_SEC=5
AGENT_GRPC_TLS_SERVER_NAME=
MINI_DROP_GRPC_TOKEN=请与 Control 保持一致
MINIO_ENDPOINT=http://172.18.88.237:9000
MINIO_ACCESS_KEY=mini_drop_worker
MINIO_SECRET_KEY=请与 Control 保持一致
MINIO_BUCKET=mini-drop
MINIO_SECURE=0
```

### 3.3 放入 CA 证书

```bash
cp /path/to/ca.crt deploy/certs/ca.crt
```

### 3.4 启动 Worker2

```bash
docker compose --env-file deploy/env/worker.env -f docker-compose.worker.yml config --quiet
docker compose --env-file deploy/env/worker.env -f docker-compose.worker.yml up -d --build
docker compose --env-file deploy/env/worker.env -f docker-compose.worker.yml ps
docker compose --env-file deploy/env/worker.env -f docker-compose.worker.yml logs -f agent
```

### 3.5 验证 Worker2

```bash
curl --cacert deploy/certs/ca.crt https://172.18.88.237/api/healthz
```

## 4. 最终验收

在宿主机或任意可访问 Control 的机器上检查：

```powershell
Test-NetConnection 172.18.88.237 -Port 22
Test-NetConnection 172.18.90.144 -Port 22
Test-NetConnection 172.18.87.120 -Port 22
```

在 `control` 上检查服务：

```bash
docker compose --env-file deploy/env/control.env -f docker-compose.control.yml ps
docker compose --env-file deploy/env/control.env -f docker-compose.control.yml logs server | grep -E 'agent|grpc|task'
curl --cacert deploy/certs/ca.crt https://172.18.88.237/api/healthz
```

如果 Agent 报 `UNAUTHENTICATED`，先核对三台机器上的 `MINI_DROP_GRPC_TOKEN` 是否一致。
如果报证书主机名不匹配，优先检查 `deploy/certs/ca.crt` 是否来自当前这台 `control`，以及 `AGENT_GRPC_TLS_SERVER_NAME` 是否需要填写。
