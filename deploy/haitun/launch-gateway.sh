#!/usr/bin/bash
# 生产机 /srv/haitun/psi-agent/workspace/launch-gateway.sh 的版本控制副本。
#
# 注意路径: compose 的 command 是 ["/usr/bin/bash", "/workspace/launch-gateway.sh"],
# 而 ./workspace 挂成容器的 /workspace —— 所以**容器跑的是 workspace/ 下那份**。
# 生产机上 /srv/haitun/psi-agent/launch-gateway.sh 也有一份同内容的 (md5 相同),
# 但它不参与运行。改错了那份不报错、也不生效, 只是下次重启照旧。
#
# 改了这里不会对生产产生任何影响, 生效要人工单文件 cp 到
# /srv/haitun/psi-agent/workspace/launch-gateway.sh 再重启栈。
# **只单文件 cp, 不做任何目录同步** —— workspace/ 下有 agent 自建的 tools/ 与 skills/,
# 目录同步会覆盖掉它们。
set -e
mkdir -p /tmp/psi
mkdir -p /workspace/.psi/appdata
set -a
. /workspace/.env
set +a

AI_ID=337e71aa35b34f8fb92590402a3e6a76
FALLBACK_SOCK=/tmp/psi/channels/5f04c34c55c54568bfb92e18f700b8dc.sock

echo "[launcher] starting gateway..."
# --gateway 是必填参数 (gateway/__init__.py: `gateway: list[GatewayName] = tyro.MISSING`)。
# 生产只需要飞书那面: 装机版的 /spa* /ui/* /auth/* 在这台机上没有用处, 挂上去只是
# 多暴露一批路由。缺这个参数 tyro 直接退出, 下面那 60 次 curl 探活会全部失败 ——
# 与 macOS launcher 漏传 --gateway 装完必死 (e2ce4922) 是同一个坑。
psi-agent gateway --listen http://127.0.0.1:8080 \
  --gateway feishu \
  --socket-path psi \
  --feishu-ai-id "$AI_ID" \
  --feishu-workspace-root /workspace \
  --default-agent /workspace \
  --default-workspace /workspace \
  --appdata /workspace/.psi/appdata &
GW_PID=$!

i=0
while [ $i -lt 60 ]; do
  if curl -sf -m 2 http://127.0.0.1:8080/sessions >/dev/null 2>&1; then
    echo "[launcher] gateway ready"; break
  fi
  i=$((i+1)); sleep 0.5
done

echo "[launcher] starting feishu channel (gateway_url mode)..."
psi-agent channel feishu \
  --session-socket "$FALLBACK_SOCK" \
  --gateway-url http://127.0.0.1:8080 \
  --require-mention &
CH_PID=$!

wait -n $GW_PID $CH_PID
echo "[launcher] a process exited, shutting down"
kill $GW_PID $CH_PID 2>/dev/null || true
exit 1
