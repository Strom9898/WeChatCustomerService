import ctypes
import ctypes.wintypes
import json
import os


KEYWORDS = ["企业微信", "WeCom"]
EXECUTABLES = {"wxwork.exe", "wxworkweb.exe", "wecom.exe"}


def main():
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    matches = []

    def executable_name(hwnd):
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
        if executable_name(hwnd) not in EXECUTABLES:
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

        if width >= 300 and height >= 200:
            matches.append({
                "title": title,
                "manual_window_region": [rect.left, rect.top, width, height],
            })
        return True

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(enum_proc(callback), 0)

    if not matches:
        print("未找到企业微信窗口。请先打开企业微信，并确认窗口未最小化。")
        return

    matches.sort(key=lambda item: item["manual_window_region"][2] * item["manual_window_region"][3], reverse=True)
    print(json.dumps(matches, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
