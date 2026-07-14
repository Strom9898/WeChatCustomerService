#!/usr/bin/env python3
"""
企业微信智能客服 — 全自动版 V4
全流程自包含：红点检测 → 点击进群 → 截图会话 → OCR提取文字 → AI回复 → 打字发送

不需要外部 cron job 配合，独立运行。
"""

import json
import os
import sys
import time
import signal
import logging
import traceback
import hashlib
import subprocess
import platform
import re
import requests
import ctypes
import ctypes.wintypes
from datetime import datetime
from typing import Optional, Tuple, List
from PIL import Image, ImageChops, ImageEnhance
from mano.executor import ActionExecutor
from conversation_store import ConversationStore


def _configure_console_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError):
            pass


_configure_console_encoding()

# ─── 常量 ────────────────────────────────────────────
PID_FILE = "logs/pid.txt"

# 企业微信新版三列布局（左侧导航约72px，会话列表约250px）
COL2_START_RATIO = 0.065
COL2_WIDTH_RATIO = 0.235

# 红点检测参数
# 企业微信未读徽标为浅红色；较深的红色多来自头像或图片内容。
RED_R_MIN = 235
RED_GB_MAX = 120
RED_MIN_PIXELS = 3


class WeComAssistant:
    """企业微信智能助理 — 全自动版"""

    def __init__(self, config_path: str = None):
        self.config = self._load_config(config_path)
        self.running = True
        self.executor = ActionExecutor()
        self.ocr = None  # lazy init

        # 窗口
        self.wecom_region: Optional[Tuple[int, int, int, int]] = None
        self.check_interval = self.config.get("check_interval_seconds", 3)
        self.col2_start_ratio = self.config.get("col2_start_ratio", COL2_START_RATIO)
        self.col2_width_ratio = self.config.get("col2_width_ratio", COL2_WIDTH_RATIO)
        self.red_dot_min_x_ratio = self.config.get("red_dot_min_x_ratio", 0.0)
        self.debug_capture_enabled = self.config.get("debug_capture_enabled", False)
        self.debug_capture_interval = self.config.get("debug_capture_interval_seconds", 15)
        self._last_debug_capture_time = 0.0
        self.console_debug_ocr = self.config.get("console_debug_ocr", False)
        self.chat_area_top_ratio = self.config.get("chat_area_top_ratio", 0.10)
        self.chat_area_bottom_ratio = self.config.get("chat_area_bottom_ratio", 0.77)

        # 去重
        self._processed_hashes = set()

        # 冷却（防止同一群反复回复）
        self._last_reply_time = 0
        self._reply_cooldown = 10

        # AI 配置
        ai_cfg = self.config.get("ai", {})
        self.ai_provider = ai_cfg.get("provider", "deepseek")
        self.ai_model = ai_cfg.get("model", "deepseek-chat")
        self.ai_base_url = ai_cfg.get("base_url", "https://api.deepseek.com").rstrip("/v1")
        self.ai_key = ai_cfg.get("api_key", "")
        self.system_prompt = ai_cfg.get("system_prompt",
            "你是企业微信群聊客服机器人。只回复外部客户消息，语气友好专业，回答简洁。"
            "不知道答案时，引导联系人工客服。")
        self.send_enabled = self.config.get("send_enabled", True)
        self.require_external_marker = self.config.get("require_external_marker", True)
        self.external_marker_keywords = self.config.get(
            "external_marker_keywords", ["@微信", "微信"]
        )
        self.target_chat_names = [
            name.strip() for name in self.config.get("target_chat_names", [])
            if isinstance(name, str) and name.strip()
        ]
        self.memory_enabled = self.config.get("memory_enabled", True)
        self.memory_history_limit = max(1, min(int(self.config.get("memory_history_limit", 12)), 50))
        self.memory_store_ai_replies = self.config.get("memory_store_ai_replies", True)
        self.conversations = ConversationStore()
        repaired_records = self.conversations.repair_saved_messages()
        self._last_reply_times = {}

        # 日志
        os.makedirs("logs", exist_ok=True)
        log_file = self.config.get("logging", {}).get("log_file", "logs/monitor.log")
        logging.basicConfig(
            filename=log_file, level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            encoding="utf-8"
        )
        self.log = logging.getLogger(__name__)
        if repaired_records:
            self._log("INFO", f"🧹 已修正 {repaired_records} 条历史 OCR 记录")

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _load_config(self, config_path: Optional[str]) -> dict:
        if not config_path:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(script_dir, "config.json")

        default = {
            "check_interval_seconds": 3,
            "wecom_process_name": "企业微信",
            "wecom_window_title_keywords": ["企业微信", "WeCom"],
            "wecom_process_executables": ["WXWork.exe", "WXWorkWeb.exe", "WeCom.exe"],
            "col2_start_ratio": COL2_START_RATIO,
            "col2_width_ratio": COL2_WIDTH_RATIO,
            "red_dot_min_x_ratio": 0.0,
            "target_chat_names": [],
            "console_debug_ocr": False,
            "chat_area_top_ratio": 0.10,
            "chat_area_bottom_ratio": 0.77,
            "manual_window_region": [],
            "send_enabled": True,
            "require_external_marker": True,
            "external_marker_keywords": ["@微信", "微信"],
            "memory_enabled": True,
            "memory_history_limit": 12,
            "memory_store_ai_replies": True,
            "logging": {"log_file": "logs/monitor.log"},
            "ai": {}
        }

        if os.path.exists(config_path):
            # 配置文件使用 UTF-8，避免 Windows 中文系统默认 GBK 导致读取失败。
            with open(config_path, encoding="utf-8") as f:
                loaded = json.load(f)
                default.update(loaded)

        # 如果 AI key 没配置，从 .env 或 Hermes config 读取
        ai = default.get("ai", {})
        if not ai.get("api_key"):
            ai["api_key"] = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not ai.get("api_key"):
            # 优先从 .env 文件读取
            env_file = os.path.expanduser("~/.hermes/.env")
            if os.path.exists(env_file):
                try:
                    with open(env_file, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("DEEPSEEK_API_KEY"):
                                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                                if key:
                                    ai["api_key"] = key
                                    break
                except Exception:
                    pass

        if not ai.get("api_key"):
            try:
                hermes_config = os.path.expanduser("~/.hermes/config.yaml")
                if os.path.exists(hermes_config):
                    with open(hermes_config) as f:
                        for line in f:
                            if line.strip().startswith("api_key:"):
                                key = line.split(":", 1)[1].strip().strip('"').strip("'")
                                if key:
                                    ai["api_key"] = key
                                    break
            except Exception:
                pass

        # 尝试环境变量
        if not ai.get("api_key"):
            for env in ["DEEPSEEK_API_KEY", "BAILIAN_API_KEY", "OPENROUTER_API_KEY"]:
                val = os.environ.get(env)
                if val:
                    ai["api_key"] = val
                    break

        default["ai"] = ai
        return default

    def _signal_handler(self, signum, frame):
        print(f"\n🛑 收到信号 {signum}，正在停止...")
        self.running = False

    def _log(self, level: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [{level}] {msg}")
        getattr(self.log, level.lower(), self.log.info)(msg)

    # ─── 窗口管理 ──────────────────────────────────

    def _detect_window(self) -> bool:
        manual_region = self.config.get("manual_window_region") or []
        if len(manual_region) == 4:
            self.wecom_region = tuple(int(v) for v in manual_region)
            return True

        system = platform.system()
        if system == "Windows":
            return self._detect_window_windows()
        if system != "Darwin":
            return False
        try:
            script = (
                f'tell application "System Events" to tell process '
                f'"{self.config["wecom_process_name"]}" to if exists window 1 '
                f'then get {{position of window 1, size of window 1}}'
            )
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=5)
            if r.returncode != 0 or not r.stdout.strip():
                return False
            # Parse AppleScript list output: {{697, 38}, {1060, 640}}
            nums = re.findall(r'\d+', r.stdout)
            if len(nums) >= 4:
                self.wecom_region = (int(nums[0]), int(nums[1]),
                                     int(nums[2]), int(nums[3]))
                return True
        except Exception:
            pass
        return False

    def _detect_window_windows(self) -> bool:
        executables = {
            str(name).lower() for name in self.config.get(
                "wecom_process_executables", ["WXWork.exe", "WXWorkWeb.exe", "WeCom.exe"]
            ) if str(name).strip()
        }
        matches = []
        minimized_matches = []

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        def executable_name(hwnd) -> str:
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return ""
            process = kernel32.OpenProcess(0x1000, False, pid.value)
            if not process:
                return ""
            try:
                path = ctypes.create_unicode_buffer(32768)
                path_length = ctypes.wintypes.DWORD(len(path))
                if not kernel32.QueryFullProcessImageNameW(
                    process, 0, path, ctypes.byref(path_length)
                ):
                    return ""
                return os.path.basename(path.value).lower()
            finally:
                kernel32.CloseHandle(process)

        def callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            if executable_name(hwnd) not in executables:
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            title = ""
            if length > 0:
                title_buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title_buf, length + 1)
                title = title_buf.value.strip()

            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width >= 600 and height >= 400:
                entry = (hwnd, title, rect.left, rect.top, width, height)
                if user32.IsIconic(hwnd) or rect.left <= -30000 or rect.top <= -30000:
                    minimized_matches.append(entry)
                else:
                    matches.append(entry)
            return True

        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(enum_proc(callback), 0)
        if not matches and minimized_matches:
            hwnd, title, _, _, _, _ = max(minimized_matches, key=lambda m: m[4] * m[5])
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.5)
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if rect.left > -30000 and rect.top > -30000 and width >= 600 and height >= 400:
                matches.append((hwnd, title, rect.left, rect.top, width, height))
                self._log("INFO", "已恢复最小化的企业微信窗口")
        if not matches:
            return False

        hwnd, title, left, top, width, height = max(matches, key=lambda m: m[4] * m[5])
        self._wecom_hwnd = hwnd
        self.wecom_region = (left, top, width, height)
        self._log("INFO", f"检测到企业微信窗口: {title} ({left},{top},{width},{height})")
        return True

    def _get_col2_region(self):
        """第二列（群聊列表）区域"""
        if not self.wecom_region:
            return None
        l, t, w, h = self.wecom_region
        col2_x = l + int(w * self.col2_start_ratio)
        col2_w = int(w * self.col2_width_ratio)
        if col2_x + col2_w > l + w:
            col2_w = l + w - col2_x
        return (col2_x, t, col2_w, h)

    def _get_col3_region(self):
        """第三列（会话窗口）区域"""
        if not self.wecom_region:
            return None
        l, t, w, h = self.wecom_region
        col3_x = l + int(w * (self.col2_start_ratio + self.col2_width_ratio))
        col3_w = w - int(w * (self.col2_start_ratio + self.col2_width_ratio))
        return (col3_x, t, col3_w, h)

    def _focus_wecom(self):
        if platform.system() == "Windows":
            try:
                hwnd = getattr(self, "_wecom_hwnd", None)
                if hwnd:
                    user32 = ctypes.windll.user32
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    time.sleep(0.5)
            except Exception:
                pass
            return
        try:
            subprocess.run(["osascript", "-e",
                f'tell application "{self.config["wecom_process_name"]}" to activate'],
                timeout=5, capture_output=True)
            time.sleep(0.5)
        except Exception:
            pass

    def _ensure_wecom_foreground(self):
        """屏幕截图前确保企业微信未被其他窗口覆盖。"""
        if platform.system() != "Windows":
            return
        try:
            hwnd = getattr(self, "_wecom_hwnd", None)
            if hwnd and ctypes.windll.user32.GetForegroundWindow() != hwnd:
                self._focus_wecom()
        except Exception:
            pass

    # ─── 截图 ──────────────────────────────────────

    def _screenshot(self, region: Tuple[int, int, int, int]) -> Image.Image:
        from mano.capture import capture_screen
        return capture_screen(region)

    def _save_debug_list_screenshot(self, img: Image.Image) -> None:
        """保存未检测到红点时的会话列表，便于校准不同客户端版本的界面。"""
        if not self.debug_capture_enabled:
            return
        now = time.time()
        if now - self._last_debug_capture_time < self.debug_capture_interval:
            return
        self._last_debug_capture_time = now
        try:
            screenshot_dir = self.config.get("logging", {}).get(
                "screenshot_dir", "logs/screenshots"
            )
            os.makedirs(screenshot_dir, exist_ok=True)
            filename = f"chat_list_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            path = os.path.join(screenshot_dir, filename)
            img.save(path)
            self._log("INFO", f"📷 未检测到红点，已保存会话列表截图: {path}")
        except Exception as e:
            self._log("WARN", f"⚠️ 保存会话列表截图失败: {e}")

    def _save_debug_session_screenshot(self, img: Image.Image) -> None:
        if not self.debug_capture_enabled:
            return
        try:
            screenshot_dir = self.config.get("logging", {}).get(
                "screenshot_dir", "logs/screenshots"
            )
            os.makedirs(screenshot_dir, exist_ok=True)
            filename = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            path = os.path.join(screenshot_dir, filename)
            img.save(path)
            self._log("INFO", f"📷 已保存会话截图: {path}")
        except Exception as e:
            self._log("WARN", f"⚠️ 保存会话截图失败: {e}")

    # ─── 红点检测 ──────────────────────────────────

    def _find_red_dots(self, img: Image.Image) -> List[Tuple[int, int]]:
        """检测小型、近圆形的红色未读徽标，排除头像中的大块红色图案。"""
        try:
            import cv2
            import numpy as np

            pixels = np.array(img.convert("RGB"))
            red_mask = (
                (pixels[:, :, 0] > RED_R_MIN)
                & (pixels[:, :, 1] >= 70)
                & (pixels[:, :, 1] < RED_GB_MAX)
                & (pixels[:, :, 2] >= 65)
                & (pixels[:, :, 2] < RED_GB_MAX)
            ).astype(np.uint8)
            count, _, stats, centers = cv2.connectedComponentsWithStats(red_mask, 8)
            dots = []
            min_x = int(img.width * self.red_dot_min_x_ratio)

            for index in range(1, count):
                x, y, width, height, area = stats[index]
                if x < min_x or not (4 <= width <= 32 and 4 <= height <= 32):
                    continue
                aspect_ratio = width / height
                fill_ratio = area / (width * height)
                if not (16 <= area <= 500 and 0.5 <= aspect_ratio <= 1.8 and fill_ratio >= 0.35):
                    continue
                center_x, center_y = centers[index]
                dots.append((int(center_x), int(center_y)))
            return dots
        except Exception as e:
            self._log("WARN", f"⚠️ 红点轮廓识别失败: {e}")
            return []

    def _cluster_dots(self, dots: List[Tuple[int, int]],
                       min_per_cluster: int = 3) -> List[Tuple[int, int]]:
        if not dots:
            return []
        dots.sort(key=lambda d: d[1])
        clusters = []
        cur = [dots[0]]
        for i in range(1, len(dots)):
            if abs(dots[i][1] - cur[-1][1]) < 5:
                cur.append(dots[i])
            else:
                if len(cur) >= min_per_cluster:
                    avg_x = sum(d[0] for d in cur) // len(cur)
                    avg_y = sum(d[1] for d in cur) // len(cur)
                    clusters.append((avg_x, avg_y))
                cur = [dots[i]]
        if len(cur) >= min_per_cluster:
            avg_x = sum(d[0] for d in cur) // len(cur)
            avg_y = sum(d[1] for d in cur) // len(cur)
            clusters.append((avg_x, avg_y))
        return clusters

    def _find_column_boundary(self, img: Image.Image) -> int:
        """检测截图中的第一列和第二列之间的分割线位置
        
        通过从左侧向右扫描像素颜色变化，找到第一个明显的颜色突变。
        企业微信第一列和第二列之间的分割线通常在截图左端附近。
        
        Returns:
            分割线的x坐标（相对于截图的像素位置），0表示未找到
        """
        try:
            import numpy as np
            arr = np.array(img.convert('RGB'))
            h, w = arr.shape[:2]
            if w < 20 or h < 20:
                return 0
            
            # 取中间区域（20%-80%高度），避开顶部标题和底部
            y_start = int(h * 0.2)
            y_end = int(h * 0.8)
            if y_end <= y_start:
                return 0
            region = arr[y_start:y_end, :, :]
            
            # 计算每一列的平均颜色
            col_means = np.mean(region, axis=0).astype(float)  # (w, 3)
            
            # 计算相邻列之间的颜色差异
            diffs = np.sqrt(np.sum((col_means[1:] - col_means[:-1])**2, axis=1))
            
            # 找第一个超过阈值的颜色突变（从最左边开始）
            # 阈值：颜色差异大于30通常就是分割线
            threshold = 30.0
            # 限制搜索范围：分割线通常在截图左侧30px以内
            search_limit = min(40, len(diffs))
            
            for i in range(search_limit):
                if diffs[i] > threshold:
                    self._log("DEBUG", f"📏 检测到分割线: cx={i+1}px, 差异值={diffs[i]:.1f}")
                    return i + 1
            
            # 如果没找到，用更低的阈值再试一次
            threshold2 = 15.0
            for i in range(search_limit):
                if diffs[i] > threshold2:
                    self._log("DEBUG", f"📏 分割线(低阈值): cx={i+1}px, 差异值={diffs[i]:.1f}")
                    return i + 1
            
            self._log("DEBUG", "📏 未检测到明显分割线")
            return 0
        except Exception as e:
            self._log("WARN", f"⚠️ 分割线检测异常: {e}")
            return 0

    def _screen_red_dots(self, col2_img: Image.Image,
                          col2_region: Tuple[int, int, int, int]) -> List[Tuple[int, int]]:
        """获取红点在屏幕上的坐标"""
        dots = self._find_red_dots(col2_img)
        return [(col2_region[0] + cx, col2_region[1] + cy) for cx, cy in dots]

    # ─── 点击操作 ──────────────────────────────────

    def _click_at(self, x: int, y: int):
        self.executor.move_to(x, y, 0.3)
        time.sleep(0.15)
        self.executor.click(x, y)
        time.sleep(0.8)

    def _click_external_chat_list(self):
        """点击第一列中的「外部群聊」按钮
        用 OCR 定位文字位置，精确点击
        """
        if not self.wecom_region:
            return
        if not self._init_ocr():
            return

        l, t, w, h = self.wecom_region
        # 截第一列
        col1 = (l, t, int(w * 0.07), h)
        from mano.capture import capture_screen
        col1_img = capture_screen(col1)

        import numpy as np
        img_np = np.array(col1_img)
        results = self.ocr.readtext(img_np)

        target = None
        for box, text, conf in results:
            if any(kw in text for kw in ["外部群聊", "外部聊天", "外部"]):
                # box = [[x1,y1], [x2,y1], [x2,y2], [x1,y2]]
                cx = (box[0][0] + box[2][0]) // 2
                cy = (box[0][1] + box[2][1]) // 2
                target = (col1[0] + cx, col1[1] + cy)
                self._log("INFO", f"🔍 OCR找到「{text}」@ ({target[0]}, {target[1]}) conf={conf:.2f}")
                break

        if not target:
            self._log("WARN", "⚠️ OCR未找到「外部群聊」，使用默认位置")
            target = (l + int(w * 0.067), t + int(h * 0.80))

        self.executor.move_to(target[0], target[1], 0.3)
        time.sleep(0.1)
        self.executor.click(target[0], target[1])
        time.sleep(0.5)
        self._log("INFO", f"👆 点击「外部群聊」 ({target[0]}, {target[1]})")

    def _click_chat_row(self, red_dot_screen: Tuple[int, int],
                         col2_region: Tuple[int, int, int, int]):
        """点击红点所在群聊行
        红点通常在群聊条目的右侧，往左偏移点中条目中间
        确保点击在第二列范围内，绝不误触第一列
        """
        dot_x, dot_y = red_dot_screen
        # 第二列中间偏左位置（确保不在第一列）
        col2_center_x = col2_region[0] + col2_region[2] // 2
        # 第二列安全左边界 = 基于窗口比例计算（与红点过滤一致，~12%窗口宽度）
        if self.wecom_region:
            safe_left = self.wecom_region[0] + int(self.wecom_region[2] * 0.12) + 10
        else:
            safe_left = col2_region[0] + 100
        # 红点往左偏移点标题，但绝不能小于安全左边界
        click_x = max(safe_left, min(dot_x - 40, col2_center_x))
        self.executor.move_to(click_x, dot_y, 0.3)
        time.sleep(0.15)
        self.executor.click(click_x, dot_y)
        time.sleep(1.2)
        self._log("INFO", f"👆 点击群聊 (x={click_x}, y={dot_y}), safe_left={safe_left}")

    # ─── OCR（EasyOCR 本地文字提取） ───────────────

    def _init_ocr(self):
        if self.ocr is not None:
            return True
        try:
            import easyocr
            self.ocr = easyocr.Reader(
                ['ch_sim', 'en'], gpu=False,
                model_storage_directory=os.path.expanduser('~/.EasyOCR/model'),
                download_enabled=self.config.get("ocr", {}).get("download_enabled", True)
            )
            self._log("INFO", "✅ EasyOCR 加载完成")
            return True
        except Exception as e:
            self._log("WARN", f"⚠️ EasyOCR 加载失败: {e}")
            return False

    def _ocr_image(self, img: Image.Image) -> str:
        """提取图片文字（带图像增强预处理）"""
        if not self._init_ocr():
            return ""
        try:
            # 图像增强：聊天气泡文字较小，先放大、增强对比度和锐度。
            w, h = img.size
            if h < 260:
                scale = 320.0 / h
                img = img.resize((int(w * scale), 320), Image.LANCZOS)
            
            # 提高对比度和边缘清晰度，减少“火锅店”被读成“火 店”的情况。
            from PIL import ImageEnhance
            img = ImageEnhance.Contrast(img).enhance(1.8)
            img = ImageEnhance.Sharpness(img).enhance(2.0)
            
            # EasyOCR 需要 numpy array
            import numpy as np
            img_np = np.array(img)
            results = self.ocr.readtext(
                img_np, contrast_ths=0.05, adjust_contrast=0.7, mag_ratio=1.5
            )
            texts = [text.strip() for _, text, conf in results if conf > 0.3]
            return "\n".join(texts)
        except Exception as e:
            self._log("WARN", f"⚠️ OCR 识别失败: {e}")
            return ""

    @staticmethod
    def _normalize_chat_name(name: str) -> str:
        return re.sub(r"\s+", "", name or "")

    def _is_target_chat_name(self, recognized_text: str) -> bool:
        """匹配指定群名，并容忍 OCR 漏识别英文前缀等轻微误差。"""
        normalized_text = self._normalize_chat_name(recognized_text)
        for target in self.target_chat_names:
            normalized_target = self._normalize_chat_name(target)
            if normalized_target in normalized_text:
                return True
            # 例如 ai测试群聊1 被 OCR 识别为 a测试群聊1 时，仍可匹配中文核心名。
            core_target = re.sub(r"[A-Za-z]", "", normalized_target)
            core_text = re.sub(r"[A-Za-z]", "", normalized_text)
            if len(core_target) >= 3 and core_target in core_text:
                return True
        return False

    def _matching_target_chat_name(self, recognized_text: str) -> Optional[str]:
        """Return the configured group name that matches an OCR fragment."""
        normalized_text = self._normalize_chat_name(recognized_text)
        for target in self.target_chat_names:
            normalized_target = self._normalize_chat_name(target)
            if normalized_target in normalized_text:
                return target
            core_target = re.sub(r"[A-Za-z]", "", normalized_target)
            core_text = re.sub(r"[A-Za-z]", "", normalized_text)
            if len(core_target) >= 3 and core_target in core_text:
                return target
        return None

    def _find_target_chat_red_dot(self, col2_img: Image.Image,
                                  col2_region: Tuple[int, int, int, int],
                                  red_dots: List[Tuple[int, int]]) -> Optional[Tuple[Tuple[int, int], str]]:
        """只返回指定群所在行的未读徽标，其他会话一律跳过。"""
        if not self.target_chat_names:
            return (max(red_dots, key=lambda dot: dot[0]), "当前会话") if red_dots else None
        if not self._init_ocr():
            return None
        try:
            import numpy as np

            results = self.ocr.readtext(np.array(col2_img.convert("RGB")))
            target_rows = []
            for box, text, confidence in results:
                chat_name = self._matching_target_chat_name(text) if confidence >= 0.3 else None
                if chat_name:
                    target_rows.append((sum(point[1] for point in box) / len(box), chat_name))

            if not target_rows:
                self._log("INFO", f"⏭️ 当前列表未找到指定群: {self.target_chat_names}")
                return None

            row_tolerance = max(35, int(col2_img.height * 0.06))
            candidates = []
            for dot in red_dots:
                dot_y = dot[1] - col2_region[1]
                row_y, chat_name = min(target_rows, key=lambda row: abs(dot_y - row[0]))
                distance = abs(dot_y - row_y)
                if distance <= row_tolerance:
                    candidates.append((distance, dot, chat_name))

            if not candidates:
                self._log("INFO", "⏭️ 指定群没有未读红点，跳过")
                return None
            _, dot, chat_name = min(candidates, key=lambda item: item[0])
            return dot, chat_name
        except Exception as e:
            self._log("WARN", f"⚠️ 指定群识别失败: {e}")
            return None

    # ─── AI 回复 ───────────────────────────────────

    def _generate_reply(self, ocr_text: str, chat_name: str = "当前会话") -> Optional[str]:
        """调用 AI 生成回复（带知识库检索）"""
        if not self.ai_key:
            self._log("WARN", "⚠️ 未配置 API Key，无法生成回复")
            return None

        try:
            # 检索知识库
            kb_context = ""
            try:
                if not hasattr(self, '_kb') or not self._kb:
                    from scripts.kb_client import KnowledgeBase
                    self._kb = KnowledgeBase()
                    self._kb.load()
                if self._kb.is_ready():
                    kb_context = self._kb.format_context(ocr_text)
                    if kb_context:
                        self._log("INFO", "📚 知识库检索到相关片段")
                    else:
                        self._log("DEBUG", "知识库未找到相关片段，使用模型直接回答")
            except Exception as e:
                self._log("WARN", f"⚠️ 知识库不可用: {e}")

            # 组装 system prompt + 知识库内容
            system_content = self.system_prompt
            if kb_context:
                system_content += kb_context

            messages = [{"role": "system", "content": system_content}]
            if self.memory_enabled:
                history = self.conversations.messages(chat_name, self.memory_history_limit)
                if history and history[-1].get("role") == "user" and history[-1].get("content") == ocr_text:
                    history = history[:-1]
                if history:
                    messages.extend({"role": item["role"], "content": item["content"]} for item in history)
                    self._log("INFO", f"🧠 已加载 {chat_name} 的 {len(history)} 条本地历史消息")
            messages.append({"role": "user", "content":
                f"客户的新问题：\n\n{ocr_text}\n\n直接回复客户问题。不要自我介绍，不要开场白，不要输出「@微信」等内部术语。不超过150字。"})

            resp = requests.post(
                f"{self.ai_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.ai_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": self.ai_model,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 200,
                },
                timeout=20
            )

            if resp.status_code == 200:
                reply = resp.json()["choices"][0]["message"]["content"].strip()
                return reply
            else:
                self._log("WARN", f"⚠️ AI API 失败: {resp.status_code} {resp.text[:200]}")
                return None

        except Exception as e:
            self._log("WARN", f"⚠️ AI 回复异常: {e}")
            return None

    # ─── 智能表格写入（MCP） ─────────────────────

    def _save_to_smart_sheet(self, question: str, reply: str):
        """将客户问题和AI回复写入企业微信智能表格"""
        ss = self.config.get("smart_sheet", {})
        if not ss.get("enabled", False):
            return
        docid = ss.get("docid", "")
        sheet_id = ss.get("sheet_id", "")
        if not docid or not sheet_id:
            self._log("WARN", "⚠️ 智能表格未配置 docid/sheet_id，跳过写入")
            return

        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            records = [{
                "values": {
                    "客户问题": [{"type": "text", "text": question[:500]}],
                    "AI回复": [{"type": "text", "text": reply[:500]}],
                    "记录时间": now
                }
            }]

            params = json.dumps({
                "docid": docid,
                "sheet_id": sheet_id,
                "records": records
            }, ensure_ascii=False)

            # 准备环境变量
            env = os.environ.copy()
            if not env.get("WECOM_BOT_ID") or not env.get("WECOM_SECRET"):
                env_file = os.path.expanduser("~/.hermes/.env")
                if os.path.exists(env_file):
                    with open(env_file) as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("WECOM_BOT_ID"):
                                env["WECOM_BOT_ID"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                            elif line.startswith("WECOM_SECRET"):
                                env["WECOM_SECRET"] = line.split("=", 1)[1].strip().strip('"').strip("'")

            if not env.get("WECOM_BOT_ID") or not env.get("WECOM_SECRET"):
                self._log("WARN", "⚠️ 未配置 WECOM_BOT_ID/WECOM_SECRET，无法写入智能表格")
                return

            mcp_client = os.path.expanduser("~/.hermes/workspace/wecom_mcp_client.py")
            if not os.path.exists(mcp_client):
                self._log("WARN", "⚠️ wecom_mcp_client.py 不存在，跳过智能表格写入")
                return

            r = subprocess.run(
                [sys.executable, mcp_client, "call", "doc", "smartsheet_add_records", params],
                capture_output=True, text=True, timeout=30, env=env
            )
            if r.returncode == 0:
                self._log("INFO", "📊 客户问题已写入智能表格 ✓")
            else:
                err = r.stderr.strip() or r.stdout.strip()[:100]
                self._log("WARN", f"⚠️ 智能表格写入失败: {err[:100]}")

        except subprocess.TimeoutExpired:
            self._log("WARN", "⚠️ 智能表格写入超时(30s)")
        except Exception as e:
            self._log("WARN", f"⚠️ 智能表格写入异常: {e}")

    # ─── 提取底部完整灰色气泡+昵称区域 ───────────

    def _extract_bottom_message(self, img):
        """找到底部最后一个气泡，完整截取气泡+上方50像素昵称区域。
        返回 (裁剪图, 是否是蓝色气泡)
        """
        pixels = img.load()
        w, h = img.size
        
        # 第一步：从底部往上找气泡底部（第一个有内容的行）
        last_content_y = -1
        for y in range(h - 1, max(int(h * 0.15), 1), -1):
            non_white = 0
            for x in range(w):
                r, g, b = pixels[x, y][:3]
                if not (r > 235 and g > 235 and b > 235):  # 放宽到235，浅灰算白
                    non_white += 1
            if non_white > w * 0.03:
                last_content_y = y
                break
        
        if last_content_y < 0:
            return None, False
        
        # 第二步：从底部往上找气泡顶部（消息间隔=连续5行以上基本空白）
        blank_streak = 0
        bubble_top = 0
        for y in range(last_content_y, max(int(h * 0.05), 1), -1):
            non_white = 0
            for x in range(w):
                r, g, b = pixels[x, y][:3]
                if not (r > 235 and g > 235 and b > 235):
                    non_white += 1
            if non_white < w * 0.02:
                blank_streak += 1
                if blank_streak >= 5:  # 连续5行空白=消息间隔
                    bubble_top = y + blank_streak
                    break
            else:
                blank_streak = 0
        
        # 安全保护：如果没找到气泡顶部，fallback
        if bubble_top >= last_content_y - 5 or bubble_top == 0:
            bubble_top = max(0, last_content_y - 200)  # 最多取200像素高区域
        
        # 第三步：往上延伸30像素包含昵称区域
        crop_top = max(0, bubble_top - 30)
        crop_bottom = min(last_content_y + 5, h - 1)
        
        # 确保区域至少50像素高
        if crop_bottom - crop_top < 50:
            crop_top = max(0, crop_bottom - 50)
        
        margin = 5
        msg_img = img.crop((margin, crop_top, w - margin, crop_bottom))
        
        # 第四步：判断气泡类型——只检查右侧50%（蓝色气泡靠右）
        check_pixels = msg_img.load()
        cw, ch = msg_img.size
        check_start_x = cw // 2
        
        blue_px = 0
        total_check = 0
        for by in range(ch):
            for bx in range(check_start_x, cw):
                r, g, b = check_pixels[bx, by][:3]
                total_check += 1
                if b > r + 30 and b > g + 20 and b > 120:
                    blue_px += 1
        
        blue_ratio = blue_px / total_check if total_check > 0 else 0
        is_blue = blue_ratio > 0.05
        
        self._log("DEBUG", f"底部消息: 右侧蓝{blue_ratio:.1%} 区域({crop_top}-{crop_bottom}) {'🟦蓝泡' if is_blue else '⬜灰泡'}")
        
        return msg_img, is_blue

    # ─── 检测 @微信 绿色标识 ──────────────────────

    def _has_green_badge(self, img):
        """检测截图中是否有绿色文字（@微信标识是绿色小字）"""
        try:
            pixels = img.load()
            w, h = img.size
            if w < 10 or h < 10:
                return False
            # 全图扫描（图片已经是裁剪后的消息区域）
            green_count = 0
            for y in range(h):
                for x in range(w):
                    r, g, b = pixels[x, y][:3]
                    if g > 100 and g > r * 1.3 and g > b * 1.3:
                        green_count += 1
                        if green_count > 10:  # 10个绿色像素就够
                            return True
            return False
        except Exception:
            return False

    def _has_customer_in_ocr(self, ocr_text, chat_img=None):
        """检测OCR文本或截图中有没有 @微信 客户标识"""
        if not ocr_text:
            return False
        if '@微信' in ocr_text or '微信' in ocr_text:
            return True
        if chat_img is not None and self._has_green_badge(chat_img):
            self._log("DEBUG", "🟢 绿色文字检测命中!")
            return True
        return False

    # ─── 哈希（去重） ──────────────────────────────

    def _image_hash(self, img: Image.Image) -> str:
        small = img.resize((16, 16)).tobytes()
        return hashlib.md5(small).hexdigest()

    # ─── 主循环 ────────────────────────────────────

    def run(self):
        self._log("INFO", "=" * 55)
        self._log("INFO", "🚀 企业微信智能助理 V4 启动")
        self._log("INFO", f"   检查间隔: {self.check_interval}s")
        self._log("INFO", f"   AI 模型: {self.ai_model}")
        self._log("INFO", f"   OCR: EasyOCR (本地)")
        self._log("INFO", "   全自动: 红点→点击→OCR→AI回复→打字")
        if self.target_chat_names:
            self._log("INFO", f"   仅处理群: {', '.join(self.target_chat_names)}")
        self._log("INFO", "=" * 55)

        cycle_count = 0
        no_window_count = 0

        while self.running:
            try:
                cycle_count += 1

                # ═══ 1. 检测窗口 ═══
                if self.wecom_region is None or cycle_count % 200 == 0:
                    if not self._detect_window():
                        no_window_count += 1
                        if no_window_count % 10 == 0:
                            self._log("WARN", f"⏳ 等待企业微信窗口... (已等{no_window_count * self.check_interval}s)")
                        time.sleep(self.check_interval)
                        continue
                    else:
                        no_window_count = 0

                # 截图来自屏幕坐标；先把企业微信置前，避免截到覆盖它的 VS Code 等窗口。
                self._ensure_wecom_foreground()

                # ═══ 2. 截图第二列（群聊列表） ═══
                col2 = self._get_col2_region()
                if not col2:
                    time.sleep(self.check_interval)
                    continue

                col2_img = self._screenshot(col2)



                # ═══ 3. 红点检测 ═══
                red_dots = self._screen_red_dots(col2_img, col2)

                if not red_dots:
                    self._save_debug_list_screenshot(col2_img)
                    time.sleep(self.check_interval)
                    continue

                # 去重
                h = self._image_hash(col2_img)
                if h in self._processed_hashes:
                    time.sleep(self.check_interval)
                    continue
                self._processed_hashes.add(h)
                if len(self._processed_hashes) > 50:
                    self._processed_hashes.clear()

                self._log("INFO", f"🔴 检测到 {len(red_dots)} 个红点！")

                # ═══ 4. 点击进入群聊 ═══
                self._focus_wecom()
                target = self._find_target_chat_red_dot(col2_img, col2, red_dots)
                if target is None:
                    time.sleep(self.check_interval)
                    continue
                target_dot, chat_name = target
                self._log("INFO", f"🎯 选取指定群未读红点: ({target_dot[0]}, {target_dot[1]})")
                self._click_chat_row(target_dot, col2)

                # ═══ 5. 截图第三列（会话窗口） ═══
                col3 = self._get_col3_region()
                if not col3:
                    time.sleep(self.check_interval)
                    continue

                time.sleep(1.5)
                session_img = self._screenshot(col3)
                self._log("INFO", f"📸 会话截图: {session_img.size}")
                self._save_debug_session_screenshot(session_img)

                # ═══ 6. 提取底部最新消息区域 ═══
                col3_w, col3_h = session_img.size
                # 按当前窗口高度裁剪聊天区，避开顶部标题和底部输入框。
                chat_top = int(col3_h * self.chat_area_top_ratio)
                chat_bottom = int(col3_h * self.chat_area_bottom_ratio)
                chat_right = max(1, col3_w - 162)
                chat_area = session_img.crop((0, chat_top, chat_right, chat_bottom))

                msg_img, is_blue = self._extract_bottom_message(chat_area)

                if msg_img is None:
                    # 没找到气泡，跳过
                    self._log("INFO", "⏭️ 未找到消息区域，跳过")
                    time.sleep(self.check_interval)
                    continue

                if is_blue:
                    self._log("INFO", "⏭️ 最新消息是AI回复（蓝色气泡），跳过")
                    time.sleep(self.check_interval)
                    continue

                # ═══ 7. OCR 识别底部消息 ═══
                ocr_text = self._ocr_image(msg_img)
                if not ocr_text:
                    self._log("WARN", "⚠️ OCR 未提取到文字，跳过")
                    time.sleep(2)
                    continue

                self._log("INFO", f"📝 底部消息OCR ({len(ocr_text)}字符):")
                for line in ocr_text.split("\n")[:5]:
                    if line.strip():
                        self._log("INFO", f"   {line.strip()[:100]}")
                if self.console_debug_ocr:
                    print(f"\n[最新气泡 OCR]\n{ocr_text}\n", flush=True)

                customer_text = self.conversations.clean_customer_content(ocr_text)

                # ═══ 7.5 检测外部联系人标识（OCR文字 + 绿色像素兜底） ═══
                has_external_marker = any(
                    kw and kw in ocr_text for kw in self.external_marker_keywords
                )
                if not has_external_marker and self._has_green_badge(msg_img):
                    self._log("INFO", "🟢 绿色像素检测命中外部联系人标识")
                    has_external_marker = True

                if self.require_external_marker and not has_external_marker:
                    self._log("INFO", "⏭️ 无外部联系人标识（可能是内部成员消息），跳过")
                    time.sleep(2)
                    continue

                if not customer_text:
                    self._log("INFO", "⏭️ 识别结果只有联系人标识，没有客户正文，跳过")
                    time.sleep(2)
                    continue

                self._log("INFO", "🟢 检测到客户消息")
                print(f"\n[客户消息 | {chat_name}]\n{customer_text}\n", flush=True)
                if self.memory_enabled:
                    self.conversations.append(chat_name, "user", customer_text)
                    self._log("INFO", f"💾 已保存 {chat_name} 的客户消息到本地")

                # ═══ 冷却 ═══
                now = time.time()
                last_reply_time = self._last_reply_times.get(chat_name, 0)
                if now - last_reply_time < self._reply_cooldown:
                    remain = int(self._reply_cooldown - (now - last_reply_time))
                    self._log("INFO", f"⏳ 冷却中 (剩余{remain}s)，跳过")
                    time.sleep(1)
                    continue
                self._last_reply_time = now
                self._last_reply_times[chat_name] = now

                # ═══ 8. AI 生成回复 ═══
                reply = self._generate_reply(customer_text, chat_name)

                if not reply:
                    self._log("WARN", "⚠️ 回复生成失败，跳过")
                    time.sleep(2)
                    continue

                self._log("INFO", f"💬 AI 回复: {reply[:80]}...")
                if self.memory_enabled and self.memory_store_ai_replies:
                    self.conversations.append(chat_name, "assistant", reply)

                if not self.send_enabled:
                    self._log("INFO", "send_enabled=false，仅生成回复，不自动发送")
                    self._save_to_smart_sheet(ocr_text, reply)
                    time.sleep(1)
                    continue

                # ═══ 8. 打字发送 ═══
                l, t, w, h = self.wecom_region
                # 先点第三列中间区域激活会话窗口
                col3_mid_x = l + int(w * 0.50)
                col3_mid_y = t + int(h * 0.40)
                self.executor.move_to(col3_mid_x, col3_mid_y, 0.2)
                time.sleep(0.1)
                self.executor.click(col3_mid_x, col3_mid_y)
                time.sleep(0.3)

                # 再点输入框（第三列底部偏左）
                input_x = l + int(w * 0.40)
                input_y = t + int(h * 0.90)
                self.executor.move_to(input_x, input_y, 0.3)
                time.sleep(0.15)
                self.executor.click(input_x, input_y)
                time.sleep(0.3)

                # 粘贴 + 回车
                self.executor.type_text(reply)
                time.sleep(0.3)
                self.executor.press_key("enter")
                time.sleep(0.5)
                self._log("INFO", "✅ 回复已发送 ✓")

                # ═══ 9. 写入智能表格 ═══
                self._save_to_smart_sheet(ocr_text, reply)

                # ═══ 10. 冷却 ═══
                time.sleep(1)

            except KeyboardInterrupt:
                self._log("INFO", "🛑 用户中断")
                self.running = False
                break
            except Exception as e:
                self._log("ERROR", f"❌ 异常: {e}")
                self._log("ERROR", traceback.format_exc())
                time.sleep(self.check_interval * 2)

        self._log("INFO", "👋 智能助理已停止")

    def stop(self):
        self.running = False


def main():
    config_path = os.environ.get("WECOM_CONFIG_PATH")
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    assistant = WeComAssistant(config_path)
    assistant.run()


if __name__ == "__main__":
    main()
