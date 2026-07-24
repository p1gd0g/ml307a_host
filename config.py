import json
import os

DEFAULT_CONFIG = {
    "serial_port": "/dev/ttyUSB2",   # ML307A AT 端口，常见为 ttyUSB2 / ttyUSB1 / ttyS1
    "baud_rate": 115200,
    "webhook_url": "",               # 收到短信时执行的 GET 请求地址
    "status_interval": 5,            # 拨号/信号状态轮询间隔（秒）
    "host": "0.0.0.0",
    "port": 8000
}


def load_config(path="config.json"):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                cfg.update(json.load(f))
            except Exception:
                pass
    return cfg


def save_config(cfg, path="config.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
