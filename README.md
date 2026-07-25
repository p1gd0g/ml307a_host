# ML307A 4G 模块监控台

全栈网站，部署在 Linux 单片机上，用于监控中移物联 **ML307A** 4G 模块：

1. 检测模块是否拨号、拨号是否成功（SIM / 信号 / 网络注册 / 获取 IP）。
2. 模块收到短信时，在前端实时展示短信信息。
3. 收到短信时自动对配置的 Webhook 地址发起 **GET 请求**。

## 技术栈

- 后端：Python + FastAPI + pyserial
- 实时：WebSocket（前端同时带 5s 轮询兜底）
- 前端：原生 HTML/JS（无构建步骤）

## 目录结构

```
ml307a_host/
├── app.py               # FastAPI 后端（REST + WebSocket）
├── ml307a.py            # ML307A 串口/AT 指令驱动
├── config.py            # 配置加载/保存
├── config.json          # 运行时配置（端口 / webhook / 轮询间隔）
├── requirements.txt
├── ml307a_host.service  # systemd 服务单元
├── templates/index.html # 前端页面
└── static/app.js        # 前端逻辑
```

## 硬件连接

ML307A 通常通过 USB 或 UART 接入单片机：

- **USB 接入**：会出现 `/dev/ttyUSB0~3`，AT 端口通常为 `/dev/ttyUSB2`（可用 `ls /dev/ttyUSB*` 与 `cat /sys/kernel/debug/usb/devices` 确认）。
- **UART 直连（如树莓派）**：通常为 `/dev/ttyS0` 或 `/dev/ttyAMA0`，波特率 115200。

确认端口并赋予权限：

```bash
ls -l /dev/ttyUSB*
sudo usermod -aG dialout $USER      # 当前用户加入 dialout 组
```

### USB 串口驱动（重要）

ML307A 通过 USB 接入时，内核需要 `option` 驱动识别其 VID:PID（`0x2ecc:0x3012`）才会生成 `/dev/ttyUSB*` 端口。若插入后没有 `ttyUSB*` 设备，需执行：

```bash
sudo modprobe option
echo "0x2ecc 0x3012" | sudo tee /sys/bus/usb-serial/drivers/option1/new_id
```

项目已内置 `setup_serial.sh` 封装上述步骤，systemd 服务会在启动前自动执行（`ExecStartPre`）。手动部署/调试时也可直接运行：

```bash
sudo bash setup_serial.sh
```

> 注意：写入 `new_id` 后若仍未生成端口，可拔插一次模块让其重新枚举。

## 安装与运行

```bash
cd /opt/ml307a_host
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 直接运行（调试）
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

浏览器访问 `http://<单片机IP>:8000`。在「Webhook 配置」面板填好串口端口后保存；若模块连接失败，页面会提示，可在该面板修改端口后重新连接。

## 开机自启（systemd）

```bash
sudo cp ml307a_host.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ml307a_host
sudo systemctl status ml307a_host
```

> 服务文件默认使用 `/opt/ml307a_host/venv/bin/python` 与 `root` 用户，请按实际路径修改。

## API 说明

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 返回拨号状态 + 短信列表 + 配置 |
| POST | `/api/config` | 修改串口端口 / webhook 地址 |
| POST | `/api/dial` | 主动激活 PDP 上下文（拨号） |
| POST | `/api/messages/clear` | 清空短信列表 |
| POST | `/api/webhook/test` | 发送一次测试 GET 请求 |
| WS | `/ws` | 实时推送状态与短信 |

## Webhook 行为

收到短信时，后端向 `webhook_url` 发起 GET 请求。

所有短信信息合并到一个 `text` 参数中：

```
text=发件人:13800138000 时间:24/07/24,10:00:00+32 内容:hello
```

此外可在前端「自定义参数」中添加任意键值对，它们会作为额外查询参数一并发送。例如配置 `token=abc` 后请求形如：

```
http://example.com/hook?text=...&token=abc
```

`text` 由系统自动生成、不可被自定义参数覆盖。可在前端点击「测试 Webhook」验证接收端是否正常。

## 拨号状态判断逻辑

- **SIM 就绪**：`AT+CPIN?` 返回 `READY`
- **网络注册**：`AT+CGREG?` 或 `AT+CEREG?` 返回 `1` / `5`
- **拨号成功**：`AT+CGPADDR=1` 获取到有效 IP **且** 已网络注册
- **信号强度**：`AT+CSQ` 转换为百分比

如模块未自动拨号，可点击页面「重新拨号」（`AT+CGACT=1,1`）。
