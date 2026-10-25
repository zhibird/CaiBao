# 说完，确认，再交给彩宝

展开聊天后点击麦克风，彩宝先停止朗读，再开始这一轮语音输入。再点一次结束；识别完成的文字进入可编辑草稿。只有你点击发送，才会像输入文字一样创建 Agent 任务；识别到「删除」「记住」等词语不会单独执行任何操作，原有工具审批继续生效。

已有草稿会保留，新识别文字追加到后面。如果草稿放不下，整段识别文字会留在复核区，缩短草稿后可手动加入；不会为了放进去而静默截断。单次识别本身超过4,000个文本单位时会明确报错，保留原草稿。正在识别或整理时先完成或取消这一轮，再发送文字。任务正在执行或等待审批时，不开始新一轮录音。

## macOS 原生输入

第一次主动点击时，macOS 可能请求麦克风和语音识别权限。应用启动、重新连接、打开偏好或读入历史都不会请求这些权限，也不会开始录音。功能可用标识仅表示应用实现了语音入口，实际设备和语言能力在使用时检查。

原生入口使用 Apple Speech，并同时检查设备端支持与设置 `requiresOnDeviceRecognition = true`。不支持设备端识别、识别模型未就绪或权限被拒绝时，彩宝明确提示，保留你的草稿；不会改成云端识别，也不会转用浏览器的识别服务。你可以直接打字或使用 macOS 自带听写。

每轮最多录音30秒；结束录音后最多等待5秒取得最终结果。只采用最终识别文字，未确认的片段不会自动写入输入框。没有说话、识别失败或整理超时都结束这一轮，不无限占用麦克风。

收起窗口、退出、切回紧凑模式、重新连接、页面离开或应用失去焦点时，原生停止录音。网页在换账号、换对话、页面隐藏时也取消当前回合；旧权限结果与旧识别回调不能写入新对话。停止语音输入与停止 Agent 任务是不同的操作，已有后台任务仍按任务记录处理。

彩宝不将原生音频保存为文件，也不将音频送给 Agent 或模型接口。发送后，识别文字和你编辑的其余内容按当前模型配置处理；这与手动输入文字相同。设备权限可以在 macOS 系统设置中调整。

## 普通浏览器

普通网页保留浏览器支持的 `SpeechRecognition`。首次点击会先说明由浏览器提供商处理、可能使用远程识别；确认后才开始。它不等于原生设备端识别，也不是原生失败时的备用通道。浏览器不支持或拒绝权限时仍可输入文字。

## 验证范围

自动检查使用假的权限、录音、识别与时钟驱动，验证回合状态、取消清理、超时及迟到事件；前端测试验证草稿保护和「识别不等于发送」。构建检查只编译应用，不打开窗口或申请权限。

开发时可运行 `./scripts/test-macos-voice.sh` 与 `node --test tests/companion-*.test.mjs`。独立页面检查为 `node tests/e2e/companion-voice.mjs`，需开发环境提供 Playwright 和 Chromium；可用 `NODE_PATH`、`CAIBAO_CHROMIUM_EXECUTABLE` 指定已有依赖。脚本启用独立的临时本机服务与无界面浏览器，不使用用户浏览器或正在运行的彩宝服务。

本次没有调用真实麦克风或远程模型。实际中文识别准确率、当前设备的本地模型、音频输入和系统授权提示，需要你之后主动使用时验证；自动测试通过不能证明这些硬件条件已满足。

## 参考

参考 [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) 将语音输入、打断和角色状态协同组织的产品思路；本模块复用彩宝自己的 Agent、审批和原生桥接，没有引入其代码、语音服务或角色资产。

设备端限制依据 Apple 的 [requiresOnDeviceRecognition](https://developer.apple.com/documentation/speech/sfspeechrecognitionrequest/requiresondevicerecognition) 与 [supportsOnDeviceRecognition](https://developer.apple.com/documentation/speech/sfspeechrecognizer/supportsondevicerecognition)；授权时机参考 [请求语音识别授权](https://developer.apple.com/documentation/speech/asking-permission-to-use-speech-recognition)。
