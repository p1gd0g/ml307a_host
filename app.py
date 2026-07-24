"""ML307A Host 后端：FastAPI + WebSocket 实时推送。

提供能力：
- 查询模块拨号状态 / 信号 / 注册 / IP
- 查看收到的短信
- 配置 webhook（收到短信时执行 GET 请求）
- 主动拨号
"""
import asyncio
import json
import logging
import os

import config as cfg
import ml307a
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
CONFIG = cfg.load_config(CONFIG_PATH)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ml307a_host")

device = ml307a.ML307ADevice(CONFIG["serial_port"], CONFIG.get("baud_rate", 115200))
device.status_interval = CONFIG.get("status_interval", 5)
device.webhook_url = CONFIG.get("webhook_url", "")
device.webhook_params = CONFIG.get("webhook_params", {})

main_loop = None
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


class ConnectionManager:
    def __init__(self):
        self.active = []

    async def connect(self, ws):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, text):
        for ws in list(self.active):
            try:
                await ws.send_text(text)
            except Exception:
                self.disconnect(ws)


manager = ConnectionManager()


def device_state():
    with device.lock:
        return {
            "status": dict(device.status),
            "messages": list(device.messages),
            "webhook_url": device.webhook_url,
            "webhook_params": device.webhook_params,
            "serial_port": device.port,
        }


def on_new_message(msg):
    payload = json.dumps({"type": "message", "data": msg})
    if main_loop is not None:
        asyncio.run_coroutine_threadsafe(manager.broadcast(payload), main_loop)


device.on_message = on_new_message


async def broadcast_state():
    while True:
        await asyncio.sleep(3)
        try:
            await manager.broadcast(json.dumps({"type": "state", "data": device_state()}))
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_event_loop()
    try:
        device.connect()
    except Exception as e:
        logger.error("串口连接失败（可在页面重新配置端口）: %s", e)
    asyncio.create_task(broadcast_state())
    yield
    device.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        # 连接后立即推送一次当前状态
        await ws.send_text(json.dumps({"type": "state", "data": device_state()}))
        while True:
            await ws.receive_text()  # 仅保持连接，状态由后台广播
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.get("/api/state")
async def state():
    return device_state()


@app.post("/api/config")
async def update_config(request: Request):
    data = await request.json()
    new_port = data.get("serial_port")
    new_baud = data.get("baud_rate")
    webhook = data.get("webhook_url")
    webhook_params = data.get("webhook_params")

    if webhook is not None:
        device.webhook_url = webhook
        CONFIG["webhook_url"] = webhook

    if webhook_params is not None and isinstance(webhook_params, dict):
        device.webhook_params = webhook_params
        CONFIG["webhook_params"] = webhook_params

    if new_port and new_port != device.port:
        CONFIG["serial_port"] = new_port
        if new_baud:
            CONFIG["baud_rate"] = new_baud
        try:
            device.reconnect(port=new_port, baud=new_baud or device.baud)
        except Exception as e:
            cfg.save_config(CONFIG, CONFIG_PATH)
            return {"ok": False, "error": str(e)}
    elif new_baud and int(new_baud) != device.baud:
        CONFIG["baud_rate"] = int(new_baud)
        device.baud = int(new_baud)

    cfg.save_config(CONFIG, CONFIG_PATH)
    return {"ok": True, "state": device_state()}


@app.post("/api/dial")
async def dial():
    try:
        status = device.dial()
        return {"ok": True, "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/messages/clear")
async def clear_messages():
    device.clear_messages()
    return {"ok": True}


@app.post("/api/webhook/test")
async def test_webhook(request: Request):
    data = await request.json()
    url = data.get("webhook_url") or device.webhook_url
    params = data.get("webhook_params")
    if params is not None and isinstance(params, dict):
        device.webhook_params = params
        CONFIG["webhook_params"] = params
        cfg.save_config(CONFIG, CONFIG_PATH)
    if not url:
        return {"ok": False, "error": "未配置 webhook"}
    import threading
    device.webhook_url = url

    test_msg = {
        "sender": "13800138000",
        "content": "测试短信",
        "timestamp": "",
    }

    def do():
        try:
            import requests
            p = device._build_params(test_msg)
            r = requests.get(url, params=p, timeout=10)
            logger.info("webhook 测试 %s -> %s", url, r.status_code)
        except Exception as e:
            logger.error("webhook 测试失败: %s", e)

    threading.Thread(target=do, daemon=True).start()
    return {"ok": True, "url": url}
