#!/usr/bin/env python3
"""本地企业微信助手管理后台。"""

import json
import os
import signal
import subprocess
import sys
import threading
import re
import ctypes
import ctypes.wintypes
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from conversation_store import ConversationStore
from legal_service import ArbitrationService


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "logs"
PID_PATH = LOG_DIR / "web_runner.pid"
RUNNER_LOG_PATH = LOG_DIR / "web_runner.log"
HOST = "127.0.0.1"
PORT = int(os.environ.get("WECOM_WEB_PORT", "8765"))
LOCK = threading.Lock()
CONVERSATIONS = ConversationStore(ROOT / "logs" / "conversations")
ARBITRATION = ArbitrationService(ROOT / "customer_service", ROOT / "logs" / "case_profiles")


def read_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_config(config):
    temporary_path = CONFIG_PATH.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary_path.replace(CONFIG_PATH)


def process_is_running(pid):
    if not pid:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def runner_pid():
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def runner_status():
    pid = runner_pid()
    running = process_is_running(pid)
    if pid and not running:
        PID_PATH.unlink(missing_ok=True)
        pid = None
    return {"running": running, "pid": pid}


def read_tail(path, max_lines=160):
    try:
        raw_lines = path.read_bytes().splitlines()
        lines = [decode_log_line(line) for line in raw_lines]
        return "\n".join(lines[-max_lines:])
    except OSError:
        return ""


def decode_log_line(raw_line):
    """兼容旧 GBK 日志与新的 UTF-8 日志。"""
    for encoding in ("utf-8", "gb18030"):
        try:
            line = raw_line.decode(encoding)
            break
        except UnicodeDecodeError:
            line = raw_line.decode("utf-8", errors="replace")

    def unescape(match):
        value = match.group(0)
        try:
            return chr(int(value[2:], 16))
        except ValueError:
            return value

    return re.sub(r"\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})", unescape, line)


def public_config(config):
    safe = json.loads(json.dumps(config))
    ai = safe.setdefault("ai", {})
    ai["api_key"] = ""
    ai["has_api_key"] = bool(config.get("ai", {}).get("api_key") or os.environ.get("DEEPSEEK_API_KEY"))
    return safe


def text_list(value, max_items=20):
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:100] for item in value if str(item).strip()][:max_items]


def number(value, fallback, minimum, maximum, integer=False):
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def apply_config_update(config, payload):
    for key in ("send_enabled", "require_external_marker", "console_debug_ocr", "debug_capture_enabled", "memory_enabled", "memory_store_ai_replies", "humanized_reply_enabled"):
        if key in payload:
            config[key] = bool(payload[key])

    config["target_chat_names"] = text_list(payload.get("target_chat_names", config.get("target_chat_names", [])))
    config["external_marker_keywords"] = text_list(payload.get("external_marker_keywords", config.get("external_marker_keywords", [])))
    config["check_interval_seconds"] = number(payload.get("check_interval_seconds"), config.get("check_interval_seconds", 3), 1, 60, True)
    config["debug_capture_interval_seconds"] = number(payload.get("debug_capture_interval_seconds"), config.get("debug_capture_interval_seconds", 15), 5, 300, True)
    config["col2_start_ratio"] = number(payload.get("col2_start_ratio"), config.get("col2_start_ratio", 0.065), 0, 0.5)
    config["col2_width_ratio"] = number(payload.get("col2_width_ratio"), config.get("col2_width_ratio", 0.235), 0.1, 0.7)
    config["chat_area_top_ratio"] = number(payload.get("chat_area_top_ratio"), config.get("chat_area_top_ratio", 0.10), 0, 0.4)
    config["chat_area_bottom_ratio"] = number(payload.get("chat_area_bottom_ratio"), config.get("chat_area_bottom_ratio", 0.77), 0.5, 0.95)
    config["memory_history_limit"] = number(payload.get("memory_history_limit"), config.get("memory_history_limit", 12), 1, 50, True)
    config["max_reply_segments"] = number(payload.get("max_reply_segments"), config.get("max_reply_segments", 3), 1, 3, True)
    config["reply_segment_delay_seconds"] = number(payload.get("reply_segment_delay_seconds"), config.get("reply_segment_delay_seconds", 1.1), 0.3, 5.0)

    ai = config.setdefault("ai", {})
    incoming_ai = payload.get("ai", {}) if isinstance(payload.get("ai"), dict) else {}
    for key in ("provider", "model", "base_url", "system_prompt"):
        if key in incoming_ai:
            ai[key] = str(incoming_ai[key]).strip()
    if incoming_ai.get("api_key"):
        ai["api_key"] = str(incoming_ai["api_key"]).strip()
    return config


def start_runner():
    with LOCK:
        status = runner_status()
        if status["running"]:
            return status
        LOG_DIR.mkdir(exist_ok=True)
        with RUNNER_LOG_PATH.open("a", encoding="utf-8") as output:
            output.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 从管理后台启动\n")
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "main.py"), str(CONFIG_PATH)],
                cwd=ROOT,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        PID_PATH.write_text(str(process.pid), encoding="utf-8")
        return {"running": True, "pid": process.pid}


def stop_runner():
    with LOCK:
        pid = runner_pid()
        if pid and process_is_running(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        PID_PATH.unlink(missing_ok=True)
        return {"running": False, "pid": None}


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, format, *args):
        return

    def send_json(self, data, status=HTTPStatus.OK):
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/config":
            return self.send_json(public_config(read_config()))
        if path == "/api/status":
            status = runner_status()
            status["runner_log"] = read_tail(RUNNER_LOG_PATH)
            status["monitor_log"] = read_tail(ROOT / "logs" / "monitor.log")
            return self.send_json(status)
        if path == "/api/conversations":
            return self.send_json({"groups": CONVERSATIONS.groups()})
        if path == "/api/conversation":
            group_name = parse_qs(urlparse(self.path).query).get("group", [""])[0]
            if not group_name:
                return self.send_json({"error": "缺少群聊名称"}, HTTPStatus.BAD_REQUEST)
            return self.send_json({"group_name": group_name, "messages": CONVERSATIONS.messages(group_name)})
        if path == "/api/case-profile":
            group_name = parse_qs(urlparse(self.path).query).get("group", [""])[0]
            if not group_name:
                return self.send_json({"error": "缺少群聊名称"}, HTTPStatus.BAD_REQUEST)
            return self.send_json({"group_name": group_name, "profile": ARBITRATION.get_profile(group_name)})
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_PUT(self):
        if urlparse(self.path).path != "/api/config":
            return self.send_error(HTTPStatus.NOT_FOUND)
        payload = self.read_json()
        if not isinstance(payload, dict):
            return self.send_json({"error": "配置格式不正确"}, HTTPStatus.BAD_REQUEST)
        with LOCK:
            updated = apply_config_update(read_config(), payload)
            write_config(updated)
        return self.send_json(public_config(updated))

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/start":
            return self.send_json(start_runner())
        if path == "/api/stop":
            return self.send_json(stop_runner())
        return self.send_error(HTTPStatus.NOT_FOUND)


def main():
    if not WEB_ROOT.exists():
        raise SystemExit("未找到 web/index.html")
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"管理后台已启动: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
