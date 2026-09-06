# TraceRT-PEK

基于高德在线路网的 GCJ-02 路线规划服务。运行时不加载本地道路图或本地路由索引。

## 坐标约定

- 浏览器定位、地点搜索、地图点选、探头、手动规避点、区域边界、路线结果均为 `GCJ-02`。
- API 不进行坐标系转换。

## 区域策略

- 区域内 -> 区域内：高德迭代规避导航。
- 区域内 -> 区域外：计算到区域外安全接驳点的蓝线，剩余路程显示为绿色虚线。
- 区域外 -> 区域外：不算路线，提示使用高德 App 配合六环外进京证。
- 区域外 -> 区域内：以目的地执行出区搜索确定接驳点，再正向计算接驳点到目的地的蓝线；起点到接驳点显示为绿色虚线。
- 区域外 5 公里缓冲区 -> 区域内：从实际起点直接计算完整规避路线。

规避区域为六环以内加通州区。

## Docker Compose 部署

### 自动构建与镜像发布

推送到 `main` 后，GitHub Actions 先运行测试，再构建 `linux/amd64`、`linux/arm64`
双架构镜像并上传到 `ghcr.io/injectrl/tracert-pek`。提供 `latest`、UTC 时间戳、
`sha-<完整提交 SHA>` 三种标签。PR 只测试和构建，不发布镜像。
发布使用 GitHub 自动提供的 `GITHUB_TOKEN`（`packages: write`），不需要个人 PAT、
Docker Hub 密码、高德密钥或生产环境后台密码，也不执行生产环境部署。

首次发布后，在 GitHub Packages 检查该镜像是否为 Public；如果是 Private，匿名拉取会失败。
新机器拉取已发布镜像：

```bash
# 填写本地 .env 后执行；不在 GitHub 仓库中提交真实值。
docker compose pull
docker compose up -d --no-build
```

更新使用相同命令。需要固定版本时，在 `.env` 设置 `IMAGE=ghcr.io/injectrl/tracert-pek:sha-...`。
摄像头数据来自项目配置的公开数据源，镜像内不含用户手动修正或导航历史。

### 本地构建

```bash
# 仅首次配置时复制模板，不要覆盖已有 .env。
cp .env.example .env
chmod 600 .env
# 编辑 .env 后执行
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100
```

四项必填凭据为 `AMAP_API_KEY`、`AMAP_JS_KEY`、`AMAP_JS_SECURITY_CODE`、
`ADMIN_PASSWORD`。Compose 将它们通过 `environment` 注入 Web 容器，缺失或为空时
拒绝部署；也可直接在 `compose.yaml` 的 `environment` 中填写值，但不要提交含凭据的文件。
密码建议在 `.env` 中用单引号包裹，特别是包含 `$`、空格或 `#` 时。
`ADMIN_USERNAME` 默认 `admin`。环境变量在容器运行时注入，不需要重建镜像即可更换，
修改后执行 `docker compose up -d` 重建容器，单纯 `restart` 不会加载新配置。

镜像不包含 `.env`、旧密钥文件、本地修正或导航记录，构建上下文采用白名单。
前端 JS Key 和安全码由后端运行时填入页面，仍然对浏览器可见；Web 服务 Key 和
后台密码不会写入页面。旧 `.amap-key`、`.admin-password` 不再被程序读取，暂保留原文件。
不要公开 `docker compose config` 或 `docker inspect` 的完整输出，它们可能包含凭据。

- `web`：Web/API 服务；以非 root 用户运行，配置健康检查。
- `camera-updater`：独立常驻更新器，无需获得 Web 容器的凭据。Web 健康后启动，
  立即检查一次摄像头数据，然后每 6 小时检查，失败 5 分钟后重试。
- `app-data` 卷：保存摄像头数据、数据更新时间、本地修正、SQLite 导航记录/路线缓存、错误日志。
  首次启动从镜像初始化摄像头数据，后续部署不覆盖已有数据。
- 标准输出日志由 Docker 限额轮转；`routing_errors.log` 在持久化卷内，需要另行归档。

默认端口为 `127.0.0.1:8765`，可继续配合宿主机 Cloudflare Tunnel；此配置不迁移
现有 Cloudflare Tunnel 凭据或进程。如需直接对外监听，可配置 `WEB_BIND=0.0.0.0`。
定位和后台登录应通过 HTTPS 使用，容器本身只提供 HTTP，由隧道或反向代理提供 HTTPS。
`WEB_PORT` 可修改宿主机端口，容器内部固定为 8765。

```bash
docker compose stop      # 停止两个容器
docker compose start     # 启动已有容器
docker compose down      # 删除容器，保留数据卷
```

不要使用 `docker compose down -v`，除非确定要删除全部持久化数据。

### 迁移当前电脑的数据

当前本地服务占用 8765。迁移前先停服务和更新器，导出一致的数据副本；导出脚本使用
SQLite backup API，不直接拷贝可能未合并 WAL 的数据库。下面的导入仅用于首次迁移，
不要覆盖一个已经使用中的 Docker 数据卷：

```bash
./service.py stop
python3 export_data.py backups/docker-migration
docker compose build
docker compose run --rm --no-deps --user 0 \
  -v "$PWD/backups/docker-migration:/import:ro" web \
  sh -c 'cp /import/* /data/ && chown -R 10001:10001 /data'
docker compose up -d
```

如果构建失败或暂不迁移，可以运行 `./service.py start` 恢复原服务。
不要让宿主机与 Docker 更新器同时操作同一个数据目录。

## 本地 Python 启动

```bash
./service.py start
./service.py status
./service.py restart
./service.py stop
```

本地 `service.py start` 也会读取 `.env` 中上述五项配置（已有环境变量优先），
支持普通值及引号包裹的字面值，不执行 shell 展开。直接运行 `server.py` 则必须先导出环境变量。

`service.py` 会同时管理 Web 服务和每 6 小时运行一次的摄像头数据更新器；`stop`、
`restart` 和 `status` 也会同时操作或检查这两个进程。默认监听 `0.0.0.0:8765`，
服务日志位于 `logs/server-service.log`，更新日志位于 `logs/update-scheduler.log`。
摄像头数据手动更新：

```bash
python3 update_avoid_points.py
```

也可以单独管理摄像头更新器：

```bash
python3 update_scheduler.py start
```

用户对探头位置的修正单独保存在 `camera_corrections.json`。更新脚本只替换数据源文件
`camera_points.json`，加载时再叠加本地修正，因此上游探头数据更新不会覆盖修正位置。
