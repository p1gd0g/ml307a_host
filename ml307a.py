"""ML307A 4G 模块驱动（串口 AT 指令交互）。

职责：
1. 检测模块是否正常拨号（SIM 就绪 / 信号 / 注册 / 获取 IP）。
2. 监听并解析收到的新短信（+CMTI / +CMT）。
3. 收到短信时触发配置的 webhook（GET 请求）。
"""
import datetime
import json
import logging
import os
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

        # 多段（级联）短信重组缓冲
        self.multipart_timeout = 2.0   # 最后一段到达后多久触发重组（秒）
        self._mpart = {}               # key -> {"_meta":(sender,ts), "parts":{seq:text}}
        self._mpart_timers = {}        # key -> threading.Timer

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
    def _wait_for_port(self, timeout=15):
        if os.path.exists(self.port):
            return
        logger.info("等待串口设备 %s 出现（最多 %ss）...", self.port, timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.port):
                return
            time.sleep(0.5)
        logger.warning("串口设备 %s 在 %ss 内未出现，驱动可能未加载", self.port, timeout)

    def connect(self):
        # 驱动可能刚加载、USB 枚举需要一点时间，先等待设备节点出现
        self._wait_for_port(timeout=15)
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
        return raw.decode("utf-8", errors="ignore").strip("\r\n")

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
        logger.info("捕获短信上报 +CMTI: %s", line)
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

    # ---------- 解码 ----------
    def _try_parse_pdu(self, s):
        """尝试按「带 UDH 的 PDU 用户数据」解析。

        成功返回 (info, payload_hex)，否则返回 None。
        info 含 dest_port / src_port / concat_ref / concat_max / concat_seq。
        """
        if len(s) < 6 or len(s) % 2 != 0:
            return None
        if not re.fullmatch(r"[0-9A-Fa-f]+", s):
            return None
        try:
            b = bytes.fromhex(s)
        except Exception:
            return None
        udh_len = b[0]
        # UDH 长度通常很小；过大则不像 UDH，避免误判
        if udh_len == 0 or udh_len > 40 or udh_len + 1 > len(b):
            return None
        udh = b[1:1 + udh_len]
        info = {"dest_port": None, "src_port": None,
                "concat_ref": None, "concat_max": None, "concat_seq": None}
        valid = False
        i = 0
        while i + 2 <= len(udh):
            iei = udh[i]
            iedl = udh[i + 1]
            i += 2
            if iedl > len(udh) - i:
                break
            data = udh[i:i + iedl]
            i += iedl
            if iei == 0x00 and iedl >= 3:        # 级联短信（8bit 参考号）
                info["concat_ref"] = data[0]
                info["concat_max"] = data[1]
                info["concat_seq"] = data[2]
                valid = True
            elif iei == 0x08 and iedl >= 4:      # 应用端口寻址（16bit）
                info["dest_port"] = int.from_bytes(data[0:2], "big")
                info["src_port"] = int.from_bytes(data[2:4], "big")
                valid = True
        if not valid:
            return None
        payload = b[1 + udh_len:]
        if not payload:
            return None
        return info, payload.hex().upper()

    def _decode_content(self, content):
        """将短信正文解析为结构化 dict：
        - 带 UDH 的 PDU（端口寻址 / 级联）-> 剥离 UDH 后按 UCS2 解码
        - 普通文本（已按 UTF-8 解出）或 UCS2(UTF-16BE) 十六进制 -> {type:"text", text:...}
        - WAP Push / MMS 通知（二进制 WBXML）-> {type:"wap_push", url, sender_hint, raw}
        多段（级联）短信会带 multipart 信息，交由 _store_message 重组。
        """
        s = (content or "").strip()
        logger.info("短信原始内容(decode前): %r", s)

        # 0) 带 UDH 的 PDU（用户数据）：剥离 UDH 后按 UCS2 解码
        pdu = self._try_parse_pdu(s)
        if pdu is not None:
            info, payload_hex = pdu
            try:
                decoded = bytes.fromhex(payload_hex).decode("utf-16-be")
            except Exception:
                decoded = None
            if decoded:
                multipart = None
                if info["concat_ref"] is not None:
                    multipart = {"kind": "concat", "ref": info["concat_ref"],
                                 "seq": info["concat_seq"], "max": info["concat_max"]}
                elif info["dest_port"] is not None:
                    multipart = {"kind": "port", "dest_port": info["dest_port"],
                                 "seq": (info["src_port"] or 0) & 0xFF}
                logger.info("识别为带 UDH 的 PDU 短信，dest_port=%s src_port=%s 多段=%s",
                            info["dest_port"], info["src_port"], multipart is not None)
                return {"type": "text", "text": decoded, "multipart": multipart}

        # 1) WAP Push / MMS 通知：二进制 WBXML，非文本
        up = s.upper()
        if up.startswith("0605040B8423F0") or up.startswith("0B8423F0"):
            url = None
            sender_hint = None
            try:
                b = bytes.fromhex(s)
                for m in re.finditer(rb"[ -~]{3,}", b):
                    seg = m.group().decode()
                    if seg.startswith("http"):
                        url = seg
                    elif "@" in seg and not seg.startswith("#"):
                        sender_hint = seg
                logger.info("识别为 WAP Push(MMS) 短信，URL=%s sender=%s", url, sender_hint)
            except Exception:
                pass
            return {"type": "wap_push", "url": url, "sender_hint": sender_hint, "raw": s, "multipart": None}

        # 2) 纯 UCS2(UTF-16BE) 十六进制 -> 中文文本
        if len(s) >= 4 and len(s) % 4 == 0 and re.fullmatch(r"[0-9A-Fa-f]+", s):
            try:
                decoded = bytes.fromhex(s).decode("utf-16-be")
                logger.info("UCS2 解码后内容: %s", decoded)
                return {"type": "text", "text": decoded, "multipart": None}
            except Exception as e:
                logger.warning("UCS2 解码失败，保留原始内容: %s", e)
                return {"type": "text", "text": content, "multipart": None}

        # 3) 普通文本（已按 UTF-8 解出）
        return {"type": "text", "text": content, "multipart": None}

    def _store_message(self, sender, content, ts):
        parsed = self._decode_content(content)
        mp = parsed.get("multipart")
        if mp is not None:
            self._buffer_multipart(sender, ts, parsed, mp)
            return
        self._commit_message(sender, parsed, ts)

    def _buffer_multipart(self, sender, ts, parsed, mp):
        """缓存多段短信的一段，收齐或超时后重组为一条完整短信。"""
        if mp["kind"] == "concat":
            key = "c:%s:%d" % (sender, mp["ref"])
        else:
            key = "p:%s:%d" % (sender, mp["dest_port"])
        buf = self._mpart.setdefault(key, {"_meta": (sender, ts), "parts": {}})
        buf["_meta"] = (sender, ts)
        buf["parts"][mp["seq"]] = parsed["text"]

        # 标准级联短信：已知总段数且已收齐 -> 立即重组
        if mp["kind"] == "concat" and mp.get("max"):
            if len(buf["parts"]) >= mp["max"]:
                self._flush_multipart(key)
                return

        # 端口寻址等未知总段数的情况：用定时器兜底，最后一段到达后稍候重组
        timer = self._mpart_timers.get(key)
        if timer:
            timer.cancel()
        self._mpart_timers[key] = threading.Timer(
            self.multipart_timeout, self._flush_multipart, args=(key,))
        self._mpart_timers[key].start()

    def _flush_multipart(self, key):
        buf = self._mpart.pop(key, None)
        timer = self._mpart_timers.pop(key, None)
        if timer:
            timer.cancel()
        if not buf:
            return
        sender, ts = buf.get("_meta", ("", ""))
        parts = buf.get("parts", {})
        seqs = sorted(k for k in parts.keys() if isinstance(k, int))
        combined = "".join(parts[s] for s in seqs)
        logger.info("多段短信重组完成，共 %d 段 -> %s", len(seqs), combined[:60])
        self._commit_message(sender, {"type": "text", "text": combined, "multipart": None}, ts)

    def _commit_message(self, sender, content, ts):
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
        # 日志中只打可读文本，避免把二进制 hex 刷屏
        if isinstance(content, dict) and content.get("type") == "wap_push":
            logger.info("收到短信 from=%s [WAP Push] url=%s", sender, content.get("url"))
        else:
            text = content.get("text", "") if isinstance(content, dict) else content
            logger.info("收到短信 from=%s content=%s", sender, text)
        self._trigger_webhook(msg)
        if self.on_message:
            try:
                self.on_message(msg)
            except Exception:
                pass

    # ---------- Webhook ----------
    def _build_text(self, msg):
        """将所有短信信息合并为一个 JSON 字符串参数。"""
        content = msg.get("content", "")
        if isinstance(content, dict):
            if content.get("type") == "wap_push":
                content_out = {
                    "type": "wap_push",
                    "url": content.get("url"),
                    "sender_hint": content.get("sender_hint"),
                    "raw": content.get("raw"),
                }
            else:
                content_out = content.get("text", "")
        else:
            content_out = content
        return json.dumps({
            "sender": msg.get("sender", ""),
            "time": msg.get("timestamp", ""),
            "content": content_out,
        }, ensure_ascii=False)

    def _build_params(self, msg):
        params = {"text": self._build_text(msg)}
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

            # 通过 AT+MDIALUP? 查询拨号状态与 IP
            # 返回格式：+MDIALUP: <cid>,<connect>[,<ipv4>,<v4_gw>,<v4_dns1>[,<v4_dns2>]]
            connected_state = False
            ip = None
            try:
                mdi = self._send("AT+MDIALUP?")
                for l in mdi:
                    if not l.startswith("+MDIALUP:"):
                        continue
                    body = l[len("+MDIALUP:"):].strip()
                    parts = [p.strip() for p in body.split(",")]
                    nums = [p for p in parts if re.fullmatch(r"\d+", p)]
                    if len(nums) >= 2:
                        # 第二个数字字段为 <connect>：1=已拨通
                        if int(nums[1]) == 1:
                            connected_state = True
                            mip = re.search(r'"([\d.]+)"', l)  # 首个引号字段为 ipv4
                            if mip:
                                ip = mip.group(1)
                        break
            except Exception:
                pass

            # connect=1 表示已拨通（已获取 IP）；Ethernet 拨号可能只上报状态不上报 IP
            dialed_up = connected_state and registered

            # 兼容回退：已拨通但无 IP 时再用 CGPADDR 取 IP
            if dialed_up and not ip:
                try:
                    cgpaddr = self._send("AT+CGPADDR=1")
                    for l in cgpaddr:
                        m = re.search(r'\+CGPADDR:\s*\d+,\s*"?([\d.]+)"?', l)
                        if m:
                            ip = m.group(1)
                            break
                except Exception:
                    pass
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

    def dial(self, context=1, timeout=30):
        """主动拨号（MDIALUP 命令：AT+MDIALUP=<cid>,1 建立连接）。"""
        self._send("AT+MDIALUP=%d,1" % context, timeout=timeout)
        self._update_status()
        return self.get_status()

    def hangup(self, context=1, timeout=15):
        """断开数据连接（AT+MDIALUP=<cid>,0 断开连接）。"""
        self._send("AT+MDIALUP=%d,0" % context, timeout=timeout)
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
