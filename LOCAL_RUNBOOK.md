# 企业微信群聊客服机器人本地运行说明

这个版本基于 `JZQiang/wecom-cs-mano` 改造，保留原来的桌面 RPA 流程：

1. 监控企业微信群聊列表未读红点
2. 打开对应群聊
3. 截取最新消息区域并 OCR 识别
4. 判断是否为外部客户消息
5. 调用 DeepSeek 生成回复
6. 粘贴到企业微信群输入框并发送

## Windows 本地准备

```powershell
cd D:\Backup\Documents\wecom-service\wecom-cs-mano-main
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

如果首次安装或首次运行 EasyOCR，可能需要下载 OCR 模型，请保持网络可用。

## 配置 API Key

推荐用环境变量，不要把 Key 写进仓库文件：

```powershell
$env:DEEPSEEK_API_KEY="你的 DeepSeek API Key"
```

也可以在 `config.json` 的 `ai.api_key` 里临时填写。

## 第一次安全试跑

建议先把 `config.json` 里的：

```json
"send_enabled": false
```

这样程序会识别消息并生成回复，但不会真的发到群里。确认日志正常后，再改回：

```json
"send_enabled": true
```

启动：

```powershell
python main.py
```

日志文件：

```text
logs/monitor.log
```

## 网页管理后台

启动本地管理后台：

```powershell
.\.venv\Scripts\python.exe web_app.py
```

然后在浏览器打开：

```text
http://127.0.0.1:8765
```

网页可以保存检测群、自动发送、回复规则和窗口比例配置，并启动或停止本机助手。管理后台只监听本机地址，不对局域网开放。

## 企业微信窗口识别

默认会在 Windows 上按窗口标题查找：

```json
"wecom_window_title_keywords": ["企业微信", "WeCom"]
```

如果识别不到窗口，可以手动填窗口区域：

```json
"manual_window_region": [0, 0, 1200, 800]
```

含义是：

```text
[窗口左上角 x, 窗口左上角 y, 窗口宽度, 窗口高度]
```

填了手动区域后，程序会直接按这个区域截图，不再自动找窗口。

## 外部客户识别

默认只回复带外部联系人标识的消息：

```json
"require_external_marker": true,
"external_marker_keywords": ["@微信", "微信"]
```

如果 OCR 总是识别不到 `@微信`，可以先临时设置：

```json
"require_external_marker": false
```

注意：关掉后可能误回复内部成员消息，正式使用前建议重新打开。

## 使用建议

- 企业微信窗口不要最小化，也不要被其他窗口遮挡。
- 保持企业微信为三列布局：左侧功能栏、中间群列表、右侧聊天窗口。
- 第一次使用时，把企业微信窗口放大到较稳定尺寸，减少坐标偏移。
- 如果发错位置，先关闭程序，再调整 `manual_window_region` 或窗口大小。
