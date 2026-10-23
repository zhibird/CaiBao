import AppKit
import AVFoundation
import WebKit

private let serviceURL = URL(string: "http://127.0.0.1:8000")!
private let companionURL = serviceURL.appendingPathComponent("companion").appending(queryItems: [URLQueryItem(name: "desktop", value: "1")])

/// A borderless panel that can still focus the chat input.
final class CompanionPanel: NSPanel {
    var lastMouseDown: NSEvent?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    override func sendEvent(_ event: NSEvent) {
        if event.type == .leftMouseDown { lastMouseDown = event }
        if event.type == .leftMouseUp { lastMouseDown = nil }
        super.sendEvent(event)
    }
}

final class CaiBaoApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler, AVSpeechSynthesizerDelegate {
    private var panel: CompanionPanel!
    private var webView: WKWebView!
    private var statusItem: NSStatusItem!
    private let speaker = AVSpeechSynthesizer()
    private var compact = true
    private var showingConnectionError = false
    private var activeUtterance: AVSpeechUtterance?
    private var retryItem: NSMenuItem!
    private var hideItem: NSMenuItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        speaker.delegate = self
        makeWindow()
        makeApplicationMenu()
        makeMenu()
        reconnect()
        showCompanion()
    }

    private func makeWindow() {
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(self, name: "caibao")
        let bootstrap = "window.caibaoNative = {speech: true, recognition: false, compact: true};"
        configuration.userContentController.addUserScript(WKUserScript(source: bootstrap, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        webView.autoresizingMask = [.width, .height]
        webView.allowsBackForwardNavigationGestures = false
        if #available(macOS 13.3, *) { webView.isInspectable = ProcessInfo.processInfo.environment["CAIBAO_WEB_INSPECTOR"] == "1" }

        let size = desiredSize(compact: true)
        panel = CompanionPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
        panel.title = "彩宝 · CaiBao"
        panel.delegate = self
        panel.contentView = webView
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.isMovableByWindowBackground = false
        panel.minSize = NSSize(width: 240, height: 280)
        if let screen = NSScreen.main {
            let bounds = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: bounds.maxX - size.width - 28, y: bounds.minY + 32))
        }
    }

    private func makeApplicationMenu() {
        // Status-item shortcuts alone are unreliable when the web view owns focus.
        // Register normal application key equivalents so standard macOS commands work.
        let mainMenu = NSMenu()
        let applicationMenuItem = NSMenuItem()
        let applicationMenu = NSMenu(title: "彩宝")
        applicationMenu.addItem(item("收起彩宝", action: #selector(hideCompanion), key: "w"))
        applicationMenu.addItem(item("重新连接本地服务", action: #selector(reconnect), key: "r"))
        applicationMenu.addItem(item("停止说话", action: #selector(stopSpeaking), key: "."))
        applicationMenu.addItem(.separator())
        applicationMenu.addItem(item("退出彩宝", action: #selector(quit), key: "q"))
        applicationMenuItem.submenu = applicationMenu
        mainMenu.addItem(applicationMenuItem)

        let editMenuItem = NSMenuItem()
        let editMenu = NSMenu(title: "编辑")
        for (title, selector, key) in [
            ("剪切", "cut:", "x"), ("复制", "copy:", "c"),
            ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")
        ] {
            // A nil target follows the responder chain to the focused WKWebView input.
            editMenu.addItem(NSMenuItem(title: title, action: NSSelectorFromString(selector), keyEquivalent: key))
        }
        editMenuItem.submenu = editMenu
        mainMenu.addItem(editMenuItem)
        NSApp.mainMenu = mainMenu
    }

    private func makeMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "pawprint.fill", accessibilityDescription: "彩宝")
            button.toolTip = "彩宝 · 你的桌面伙伴"
        }
        let menu = NSMenu()
        menu.addItem(item("唤醒彩宝", action: #selector(showCompanion), key: ""))
        hideItem = item("收起彩宝", action: #selector(hideCompanion), key: "")
        menu.addItem(hideItem)
        menu.addItem(.separator())
        menu.addItem(item("打开工作台…", action: #selector(openWorkbench), key: ""))
        retryItem = item("重新连接本地服务", action: #selector(reconnect), key: "r")
        menu.addItem(retryItem)
        menu.addItem(item("停止说话", action: #selector(stopSpeaking), key: "."))
        menu.addItem(.separator())
        menu.addItem(item("退出彩宝", action: #selector(quit), key: "q"))
        statusItem.menu = menu
    }

    private func item(_ title: String, action: Selector, key: String) -> NSMenuItem {
        let result = NSMenuItem(title: title, action: action, keyEquivalent: key)
        result.target = self
        return result
    }

    @objc private func showCompanion() {
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func hideCompanion() { panel.orderOut(nil) }
    @objc private func openWorkbench() { NSWorkspace.shared.open(serviceURL) }
    @objc private func reconnect() {
        showingConnectionError = false
        webView.load(URLRequest(url: companionURL, cachePolicy: .reloadIgnoringLocalCacheData))
    }
    @objc private func stopSpeaking() {
        let interrupted = activeUtterance
        activeUtterance = nil
        speaker.stopSpeaking(at: .immediate)
        if let interrupted {
            emit("caibao-native-speech", detail: ["state": "end", "text": interrupted.speechString])
        }
    }
    @objc private func quit() { stopSpeaking(); NSApp.terminate(nil) }

    private func desiredSize(compact: Bool) -> NSSize {
        NSSize(width: compact ? 300 : 440, height: compact ? 340 : 740)
    }

    private func setCompact(_ value: Bool) {
        compact = value
        let old = panel.frame
        let visible = (panel.screen ?? NSScreen.main)?.visibleFrame ?? old
        var size = desiredSize(compact: value)
        size.width = min(size.width, visible.width)
        size.height = min(size.height, visible.height - 16)
        var origin = NSPoint(x: old.maxX - size.width, y: old.maxY - size.height)
        origin.x = max(visible.minX, min(origin.x, visible.maxX - size.width))
        origin.y = max(visible.minY, min(origin.y, visible.maxY - size.height))
        panel.setFrame(NSRect(origin: origin, size: size), display: true, animate: true)
        emit("caibao-native-mode", detail: ["compact": value])
    }

    /// Only the main frame of our exact local origin may call native functions.
    private func trusted(_ url: URL?) -> Bool {
        guard let url else { return false }
        return url.scheme == "http" && url.host == "127.0.0.1" && url.port == 8000 && url.user == nil && url.password == nil
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, trusted(message.frameInfo.request.url), trusted(webView.url),
              let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        switch action {
        case "speak":
            guard let raw = body["text"] as? String else { return }
            let text = String(raw.trimmingCharacters(in: .whitespacesAndNewlines).prefix(4000))
            guard !text.isEmpty else { return }
            stopSpeaking()
            let utterance = AVSpeechUtterance(string: text)
            let language = (body["lang"] as? String) ?? "zh-CN"
            utterance.voice = AVSpeechSynthesisVoice(language: language) ?? AVSpeechSynthesisVoice(language: "zh-CN")
            utterance.rate = AVSpeechUtteranceDefaultSpeechRate * 0.94
            activeUtterance = utterance
            speaker.speak(utterance)
        case "stopSpeech": stopSpeaking()
        case "mode": if let value = body["compact"] as? Bool { setCompact(value) }
        case "drag":
            // A held click remains a drag even when the user pauses before moving.
            // Requiring the physical button also rejects stale or synthetic bridge calls.
            if let event = panel.lastMouseDown, event.windowNumber == panel.windowNumber,
               NSEvent.pressedMouseButtons & 1 != 0 {
                panel.lastMouseDown = nil
                panel.performDrag(with: event)
            }
        case "hide": hideCompanion()
        case "quit": quit()
        case "openWorkbench": openWorkbench()
        case "listen": emit("caibao-native-listen", detail: ["available": false, "reason": "browser-fallback"])
        default: break
        }
    }

    private func emit(_ name: String, detail: [String: Any]) {
        guard trusted(webView.url), let data = try? JSONSerialization.data(withJSONObject: detail),
              let json = String(data: data, encoding: .utf8),
              let nameData = try? JSONSerialization.data(withJSONObject: [name]),
              let names = String(data: nameData, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.dispatchEvent(new CustomEvent(\(names)[0], {detail: \(json)}));", completionHandler: nil)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        // Never load a remote site into a web view with native speech and window controls.
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if trusted(url) || (showingConnectionError && url.absoluteString == "about:blank") {
            decisionHandler(.allow)
            return
        }
        decisionHandler(.cancel)
        if navigationAction.navigationType == .linkActivated, ["https", "http"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        emit("caibao-native-ready", detail: ["compact": compact, "speech": true, "recognition": false])
        emit("caibao-native-mode", detail: ["compact": compact])
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { showConnectionError(error) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { showConnectionError(error) }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { reconnect() }

    private func showConnectionError(_ error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        // Static text only: failed origins do not gain access to the native bridge.
        let html = """
        <!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
        <style>html{background:transparent}body{margin:12px;padding:28px 22px;border-radius:28px;background:#ecfaf0;color:#163e32;font:15px -apple-system,sans-serif;line-height:1.7;box-sizing:border-box}h1{font-size:23px}small{color:#527367}</style>
        <h1>🐾 彩宝在等你</h1><p>本地服务还没准备好。</p><p>启动彩宝服务后，点击菜单栏爪印里的「重新连接本地服务」。</p><small>127.0.0.1:8000</small></html>
        """
        showingConnectionError = true
        webView.loadHTMLString(html, baseURL: nil)
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didStart utterance: AVSpeechUtterance) {
        guard activeUtterance === utterance else { return }
        emit("caibao-native-speech", detail: ["state": "start", "text": utterance.speechString])
    }
    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) { finishSpeech(utterance) }
    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) { finishSpeech(utterance) }
    private func finishSpeech(_ utterance: AVSpeechUtterance) {
        // Cancellation of an older utterance can arrive after the next one starts.
        guard activeUtterance === utterance else { return }
        activeUtterance = nil
        emit("caibao-native-speech", detail: ["state": "end", "text": utterance.speechString])
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool { hideCompanion(); return false }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationWillTerminate(_ notification: Notification) { speaker.stopSpeaking(at: .immediate) }
}

let application = NSApplication.shared
let delegate = CaiBaoApp()
application.delegate = delegate
application.run()
