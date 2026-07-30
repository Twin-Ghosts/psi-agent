#!/usr/bin/bash
# 重启 haitun 服务栈, 顺序正确。
#
# 为什么需要这个脚本: oauth-proxy 用 network_mode: "service:gateway" 借用 gateway
# 的 network namespace。gateway 一被重启/重建, 代理的网络栈就失效, 8090 连不上
# (curl 返回 000), 但容器状态仍显示 Up —— 很容易漏掉。
#
# 用法: ./restart-stack.sh [服务名...]   不带参数则重启 gateway + private-luolin
set -euo pipefail
cd "$(dirname "$0")"

TARGETS=("$@")
if [ ${#TARGETS[@]} -eq 0 ]; then
  TARGETS=(gateway private-luolin)
fi

echo "[restart-stack] 重启: ${TARGETS[*]}"
docker compose restart "${TARGETS[@]}"

# 只要动过 gateway, 就必须重启代理
for t in "${TARGETS[@]}"; do
  if [ "$t" = "gateway" ]; then
    echo "[restart-stack] gateway 动过了, 跟着重启 oauth-proxy (共享 netns)"
    sleep 3
    docker compose restart oauth-proxy
    break
  fi
done

echo "[restart-stack] 自检 8090..."
sleep 8
CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 6 \
  "http://192.168.60.214:8090/oauth/callback" || echo 000)
if [ "$CODE" = "400" ]; then
  echo "[restart-stack] OK: /oauth/callback 可达 (400 = 缺 state, 属正常)"
else
  echo "[restart-stack] 警告: /oauth/callback 返回 $CODE, 期望 400。检查 oauth-proxy 日志。"
  exit 1
fi

BLOCKED=$(curl -s -o /dev/null -w '%{http_code}' -m 6 \
  "http://192.168.60.214:8090/sessions" || echo 000)
if [ "$BLOCKED" = "404" ]; then
  echo "[restart-stack] OK: /sessions 已被白名单拦下 (404)"
else
  echo "[restart-stack] 严重: /sessions 返回 $BLOCKED, 期望 404 —— agent 路由可能暴露了!"
  exit 1
fi
