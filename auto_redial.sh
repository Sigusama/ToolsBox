#!/bin/bash

# PPP0 自动重拨脚本

# 检查 ppp0 接口的 IP 是否以 58.32 开头,如果不是则重拨

#LOG_FILE=”/var/log/ppp0_redial.log”
CHECK_INTERVAL=60

# 日志函数

#log_message() {
#echo “[$(date +%Y-%m-%d\ %H:%M:%S)] $1” >> “$LOG_FILE”
#echo “[$(date +%Y-%m-%d\ %H:%M:%S)] $1”
#}

# 获取 ppp0 的 IP 地址

get_ppp0_ip() {
ip addr show ppp0 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d'/' -f1
}

# 重拨 PPP 连接

redial_ppp() {
echo “开始重拨 PPP 连接…”

# 断开当前连接
poff pppoe1 2>/dev/null
echo "已断开 ppp0 连接"

# 等待几秒确保完全断开
sleep 3

# 重新拨号
pon pppoe1 2>/dev/null
echo "重拨命令已执行"

# 等待连接建立
sleep 10

}

# 检查 IP 是否符合要求

check_and_redial() {
local current_ip
current_ip=$(get_ppp0_ip)

if [ -z "$current_ip" ]; then
    echo "警告: 无法获取 ppp0 IP 地址,接口可能未连接"
    redial_ppp
    return
fi

echo "当前 ppp0 IP: $current_ip"

# 检查 IP 是否以 58.32 开头
if echo "$current_ip" | grep -q "^58\.32\."; then
    echo "IP 地址符合要求 (58.32.x.x),无需重拨"
else
    echo "IP 地址不符合要求 (不是 58.32.x.x),准备重拨"
    redial_ppp
fi

}

# 主循环

echo “========== PPP0 自动重拨脚本启动 ==========”

while true; do
check_and_redial
echo “等待 $CHECK_INTERVAL 秒后再次检查…”
sleep $CHECK_INTERVAL
done