# Docker 部署指南

## 快速开始

1. 配置 `.env` 文件
2. 启动服务：
   ```bash
   docker compose up -d --build
   ```

本地默认会让容器通过宿主机代理访问 Telegram：

```bash
http://host.docker.internal:7897
```

如果你要换端口：

```bash
BOT_HTTP_PROXY=http://host.docker.internal:7890 \
BOT_HTTPS_PROXY=http://host.docker.internal:7890 \
docker compose up -d --build
```

注意：在容器里不要用 `127.0.0.1:7897` 指向宿主机代理；那会变成容器自己的 loopback。

## 本地保活

如果 Docker Desktop 或容器经常掉，可以直接跑：

```bash
./infra/docker_keepalive.sh
```

它会：

- 在 macOS 上尝试启动 Docker Desktop
- 构建并拉起 `bot` 服务
- 发现容器 missing / exited / unhealthy 时自动重启
- 每轮间隔默认 30 秒，可用 `INTERVAL_SECONDS=60` 调整

## 停止服务

```bash
docker compose down
```

## 查看日志

```bash
docker compose logs -f bot
```

## 部署到云平台

### Railway

1. 推送项目到 GitHub
2. 在 Railway 创建新项目，选择 GitHub 仓库
3. 添加环境变量（在 Dashboard → Variables）
4. 部署自动开始

### Render

1. 推送到 GitHub
2. 在 Render 创建 Web Service
3. 选择 Python 类型
4. 添加环境变量
5. 部署

## 注意事项

- `data/` 目录用于持久化数据库
- 容器重启后数据库不会丢失
- 健康检查每 30 秒一次
