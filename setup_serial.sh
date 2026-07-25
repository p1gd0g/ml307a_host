#!/bin/bash
# 启动前确保 ML307A 4G 模块的 USB 串口驱动已加载并识别设备，
# 从而生成 /dev/ttyUSB* 端口（模块通过 USB 接入时需要）。
# 需以 root 权限运行（systemd 服务中已配置）。
set -e

# 加载 USB 串口通用驱动
modprobe option || true

# 注册 ML307A 的 VID:PID（中移物联），使其绑定 option 驱动并生成 ttyUSB 端口
if [ -f /sys/bus/usb-serial/drivers/option1/new_id ]; then
    echo "0x2ecc 0x3012" > /sys/bus/usb-serial/drivers/option1/new_id || true
fi

# 等待内核完成设备枚举
sleep 1
