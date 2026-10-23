# 彩宝 macOS 桌宠

原生 AppKit 菜单栏 + 透明悬浮窗口，使用 WKWebView 显示本地 `/companion` 页面；复用彩宝的 Agent、会话和记忆。系统语音合成无需 API Key，也不会把文本发送到新的第三方语音服务。系统中可用的声音由 macOS 决定。

## 启动

需要 macOS 13+、Python 3.11+ 和 Apple Command Line Tools。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./scripts/run-macos.sh
```

启动脚本先构建 `build/CaiBao.app`，复用已有的本地服务，或通过 `scripts/run-local.sh` 启动一个只监听本机的服务，然后打开桌宠。退出桌宠会停止**此次脚本启动的服务**；复用的服务继续运行。服务启动日志位于 `build/service.log`。直接双击 `.app` 时需先自行启动本地服务。

只构建应用：

```sh
./scripts/build-macos.sh
```

应用采用本地 ad-hoc 签名。面向其他电脑公开分发时仍需 Apple Developer 签名和 notarization。构建脚本会自动尝试已安装的兼容 SDK；也可通过 `CAIBAO_MACOS_SDK_PATH` 指定 SDK。调试时设置 `CAIBAO_WEB_INSPECTOR=1` 可启用网页检查器。

## 交互

- 桌宠浮在普通窗口上，可通过角色上的拖动手柄移动；聊天输入框可以正常获得键盘焦点。
- 紧凑窗口为 300 × 340，展开聊天为 440 × 740，超出可用屏幕时自动缩小。
- 菜单栏爪印可以唤醒、收起、重新连接、停止说话、打开工作台和退出。
- 发音使用系统 `AVSpeechSynthesizer`；首次使用对应语言可在 macOS「系统设置 → 辅助功能 → 朗读内容」下载偏好的声音。
- 此版本不申请麦克风或语音识别权限。原生聊天输入框可使用 macOS 自带听写；浏览器中的语音输入取决于浏览器对 `SpeechRecognition` 的支持。

## 网页与原生桥接

仅 `http://127.0.0.1:8000` 的**主框架**可调用桥接。远程站点不会在带原生桥接的窗口中打开；用户点击的普通网页链接交给默认浏览器。iframe 无法调用原生操作。允许的操作不包括文件、终端、系统命令或任意 URL。

网页可检查 `window.caibaoNative`，并通过以下方式发送消息：

```js
window.webkit.messageHandlers.caibao.postMessage({ action: "speak", text: "我在呀！", lang: "zh-CN" });
```

| action | 字段 | 行为 |
| --- | --- | --- |
| `speak` | `text`, `lang` 可选 | 替换当前朗读，最多 4,000 字符 |
| `stopSpeech` | — | 停止当前朗读 |
| `mode` | `compact: boolean` | 改变窗口尺寸，尽量保留右上角位置 |
| `drag` | — | 从最近一次鼠标按下事件拖动窗口 |
| `hide` | — | 收起到菜单栏 |
| `openWorkbench` | — | 默认浏览器打开本地工作台 |
| `listen` | — | 告知网页使用浏览器或系统听写 |

原生通过 `window` 上的 CustomEvent 通知网页：

- `caibao-native-ready`：`{ compact, speech: true, recognition: false }`
- `caibao-native-mode`：`{ compact }`
- `caibao-native-speech`：`{ state: "start" | "end", text }`；停止也会发送 `end`。
- `caibao-native-listen`：`{ available: false, reason: "browser-fallback" }`

这是与本地服务同源的可信界面桥接，而非执行模型生成代码的通道。网页仍需将模型输出作为文本呈现、保留任务权限边界。

## 发布架构与边界

当前发布单元由三个部分构成：FastAPI 本地服务、同源 Companion 网页、薄原生 macOS 壳。原生壳不复制 Agent 运行逻辑，不直接访问模型密钥或数据库；所有会话、工具确认和长期记忆继续经过服务端接口。网页通过标准 SSE 读取任务状态，以相同状态驱动表情和动作。

应用包仅包含原生可执行文件与配置，不包含 Python 解释器、服务端或模型。第一阶段适合从源码本机构建；迁移到独立安装包时，可单独打包签名后的服务启动器及运行时，保留 `127.0.0.1` 单机访问和显式工具确认。对外分发前需在 Intel 和 Apple Silicon 两种架构分别构建，执行 Developer ID 签名与 Apple 公证，并提供可追踪的版本号和更新策略。

语音播放拥有唯一的当前 utterance；切换朗读和停止时，旧音频的延迟回调不会覆写新的嘴型状态。窗口拖动要求来自当前窗口的原生鼠标按下事件，且物理左键仍然按住；网页调用本身不会生成拖动权限。
