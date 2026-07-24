"""ML307A 4G 模块驱动（串口 AT 指令交互）。

职责：
1. 检测模块是否正常拨号（SIM 就绪 / 信号 / 注册 / 获取 IP）。
2. 监听并解析收到的新短信（+CMTI / +CMT）。
3. 收到短信时触发配置的 webhook（GET 请求）。
"""
import datetime
import logging
import re
import threading
import time

import serial

logger = logging.getLogger("ml307a")


class ML307AError(Exception):
    pass


class ML307ADevice:
    def __init__(self, port, baud=115200):
        self.port = port
        self.baud = baud
        self.ser = None
        self.running = False
        self.thread = None
        self.lock = threading.Lock()
        self.status_interval = 5
        self.webhook_url = ""
        self.webhook_params = {}  # 自定义 webhook 参数（dict）
        self.on_message = None  # 业务层回调：收到短信时调用(msg)

        self.status = {
            "connected": False,
            "sim_ready": False,
            "signal": None,
            "signal_percent": None,
            "operator": None,
            "registered": False,
            "dialed_up": False,
            "ip": None,
            "last_update": None,
            "error": None,
        }
        self.messages = []
        self._pending_indices = []
        self._last_status = 0

    # ---------- 连接 / 主循环 ----------
    def connect(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.ser = serial.Serial(self.port, self.baud, timeout=0.5)
        time.sleep(1.2)
        self.ser.reset_input_buffer()
        self._send("ATE0")            # 关闭回显
        self._send("AT+CMGF=1")       # 短信文本模式
        self._send("AT+CNMI=2,1,0,0,0")  # 新短信直接上报
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def close(self):
        self.running = False
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass

    def reconnect(self, port=None, baud=None):
        logger.info("reconnecting ml307a port=%s", port or self.port)
        self.close()
        if port:
            self.port = port
        if baud:
            self.baud = baud
        time.sleep(1.5)
        self.connect()

    # ---------- 底层收发 ----------
    def _read_line(self):
        raw = self.ser.readline()
        if not raw:
            return None
        return raw.decode(errors="ignore").strip("\r\n")

    def _send(self, cmd, timeout=6):
        if not self.ser or not self.ser.is_open:
            raise ML307AError("串口未打开")
        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write((cmd + "\r").encode())
            resp = []
            start = time.time()
            while time.time() - start < timeout:
                line = self._read_line()
                if line is None:
                    continue
                s = line.strip()
                if s == "OK":
                    return resp
                if s.startswith("+CME ERROR") or s == "ERROR" or s.startswith("+CMS ERROR"):
                    raise ML307AError(s or "ERROR")
                if s == "":
                    continue
                if s == cmd or s == cmd.strip():
                    continue  # 跳过命令回显
                # 在等待命令响应期间也可能收到短信上报，照样处理
                if s.startswith("+CMTI:"):
                    self._handle_cmti(s)
                    continue
                if s.startswith("+CMT:"):
                    self._read_cmt_message(s)
                    continue
                resp.append(s)
            raise ML307AError("等待响应超时: %s" % cmd)

    # ---------- 主循环 ----------
    def _loop(self):
        while self.running:
            try:
                line = self._read_line()
                if line is not None:
                    self._handle_unsolicited(line)
                if self._pending_indices:
                    idx = self._pending_indices.pop(0)
                    self._read_message(idx)
                now = time.time()
                if now - self._last_status > self.status_interval:
                    self._update_status()
                    self._last_status = now
            except ML307AError as e:
                self.status["error"] = str(e)
                time.sleep(1)
            except Exception as e:
                logger.error("loop error: %s", e)
                self.status["error"] = str(e)
                time.sleep(1)

    def _handle_unsolicited(self, line):
        s = line.strip()
        if s.startswith("+CMTI:"):
            self._handle_cmti(s)
        elif s.startswith("+CMT:"):
            self._read_cmt_message(s)

    def _handle_cmti(self, line):
        # +CMTI: "SM",3
        m = re.search(r'"SM",\s*(\d+)', line)
        if m:
            self._pending_indices.append(int(m.group(1)))

    def _read_cmt_message(self, header):
        # +CMT: "+8613800138000","","24/07/24,10:00:00+32"  （下一行是正文）
        content = self._read_line() or ""
        parts = re.findall(r'"([^"]*)"', header)
        sender = parts[0] if parts else ""
        ts = parts[2] if len(parts) >= 3 else ""
        self._store_message(sender, content, ts)

    def _read_message(self, idx):
        try:
            resp = self._send("AT+CMGR=%d" % idx)
        except Exception as e:
            logger.error("读取短信 %d 失败: %s", idx, e)
            return
        header = None
        content = None
        for i, l in enumerate(resp):
            if l.startswith("+CMGR:"):
                header = l
                if i + 1 < len(resp):
                    content = resp[i + 1]
                break
        if header:
            parts = re.findall(r'"([^"]*)"', header)
            sender = parts[1] if len(parts) > 1 else ""
            ts = parts[3] if len(parts) > 3 else ""
            self._store_message(sender, content or "", ts)
        # 读取后删除，避免短消息存储满
        try:
            self._send("AT+CMGD=%d" % idx)
        except Exception:
            pass

    def _store_message(self, sender, content, ts):
        msg = {
            "id": int(time.time() * 1000),
            "sender": sender,
            "content": content,
            "timestamp": ts or datetime.datetime.now().strftime("%y/%m/%d,%H:%M:%S"),
            "received_at": datetime.datetime.now().isoformat(),
        }
        with self.lock:
            self.messages.insert(0, msg)
            if len(self.messages) > 200:
                self.messages = self.messages[:200]
        logger.info("收到短信 from=%s content=%s", sender, content)
        self._trigger_webhook(msg)
        if self.on_message:
            try:
                self.on_message(msg)
            except Exception:
                pass

    # ---------- Webhook ----------
    def _build_text(self, msg):
        """将所有短信信息合并为一个文本参数。"""
        return "sender:%s time:%s content:%s" % (
            msg.get("sender", ""), msg.get("timestamp", ""), msg.get("content", "")
        )

    def _build_params(self, msg):
        params = {"text": msg.get("content", "")}
        if isinstance(self.webhook_params, dict):
            for k, v in self.webhook_params.items():
                if k and k != "text":  # text 由系统生成，不被覆盖
                    params[str(k)] = v
        return params

    def _trigger_webhook(self, msg):
        url = self.webhook_url
        if not url:
            return
        params = self._build_params(msg)
        threading.Thread(target=self._do_get, args=(url, params), daemon=True).start()

    def _do_get(self, url, params):
        try:
            import requests
            r = requests.get(url, params=params, timeout=10)
            logger.info("webhook GET %s -> %s", url, r.status_code)
        except Exception as e:
            logger.error("webhook GET 失败 %s: %s", url, e)

    # ---------- 拨号 / 状态查询 ----------
    def _rssi_to_percent(self, rssi):
        if rssi == 99:
            return None
        if rssi <= 0:
            return 0
        if rssi >= 31:
            return 100
        return int((rssi / 31) * 100)

    def _update_status(self):
        try:
            self._send("AT")
            cpin = self._send("AT+CPIN?")
            sim_ready = any("READY" in l for l in cpin)

            csq = self._send("AT+CSQ")
            signal = None
            percent = None
            for l in csq:
                m = re.search(r"\+CSQ:\s*(\d+),", l)
                if m:
                    signal = int(m.group(1))
                    percent = self._rssi_to_percent(signal)
                    break

            cops = self._send("AT+COPS?")
            operator = None
            for l in cops:
                m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]*)"', l)
                if m:
                    operator = m.group(1)
                    break

            registered = False
            for cmd in ("AT+CGREG?", "AT+CEREG?"):
                try:
                    out = self._send(cmd)
                    for l in out:
                        m = re.search(r"\+C[A-Z]+?REG:\s*\d+,(\d+)", l)
                        if m and m.group(1) in ("1", "5"):
                            registered = True
                except Exception:
                    pass

            cgpaddr = self._send("AT+CGPADDR=1")
            ip = None
            for l in cgpaddr:
                m = re.search(r'\+CGPADDR:\s*\d+,\s*"?([\d.]+)"?', l)
                if m:
                    ip = m.group(1)
                    break

            dialed_up = bool(ip) and registered
            with self.lock:
                self.status.update({
                    "connected": True,
                    "sim_ready": sim_ready,
                    "signal": signal,
                    "signal_percent": percent,
                    "operator": operator,
                    "registered": registered,
                    "dialed_up": dialed_up,
                    "ip": ip,
                    "last_update": datetime.datetime.now().isoformat(),
                    "error": None,
                })
        except Exception as e:
            logger.error("状态查询失败: %s", e)
            with self.lock:
                self.status.update({"connected": False, "dialed_up": False, "error": str(e)})

    def dial(self, context=1, timeout=15):
        """主动激活 PDP 上下文（拨号）。"""
        self._send("AT+CGACT=1,%d" % context, timeout=timeout)
        self._update_status()
        return self.get_status()

    def get_status(self):
        with self.lock:
            return dict(self.status)

    def get_messages(self):
        with self.lock:
            return list(self.messages)

    def clear_messages(self):
        with self.lock:
            self.messages = []
