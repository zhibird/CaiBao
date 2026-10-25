import Foundation

private final class FakeDriver: NativeListeningDriver {
    var speechRequests = 0
    var microphoneRequests = 0
    var prepareCalls = 0
    var startCalls = 0
    var finishCalls = 0
    var cancelCalls = 0
    var prepareReason: String?
    var startError: ListeningFailure?
    var speech: [(ListeningAuthorization) -> Void] = []
    var microphone: [(ListeningAuthorization) -> Void] = []
    var recognition: [(Result<String, ListeningFailure>) -> Void] = []
    var onFinish: (() -> Void)?
    var onCancel: (() -> Void)?
    func requestSpeechAuthorization(_ completion: @escaping (ListeningAuthorization) -> Void) {
        speechRequests += 1; speech.append(completion)
    }
    func prepare(language: String) -> String? { prepareCalls += 1; return prepareReason }
    func requestMicrophoneAuthorization(_ completion: @escaping (ListeningAuthorization) -> Void) {
        microphoneRequests += 1; microphone.append(completion)
    }
    func start(_ completion: @escaping (Result<String, ListeningFailure>) -> Void) throws {
        startCalls += 1
        if let startError { throw startError }
        recognition.append(completion)
    }
    func finishAudio() { finishCalls += 1; onFinish?() }
    func cancel() { cancelCalls += 1; onCancel?() }
}

private final class FakeTimer: ListeningTimer {
    let seconds: TimeInterval
    let action: () -> Void
    var cancelled = false
    init(_ seconds: TimeInterval, _ action: @escaping () -> Void) { self.seconds = seconds; self.action = action }
    func cancel() { cancelled = true }
    // Firing cancelled timers deliberately checks stale callback fencing too.
    func fire() { action() }
}

private final class FakeScheduler: ListeningScheduler {
    var timers: [FakeTimer] = []
    func schedule(after seconds: TimeInterval, _ action: @escaping () -> Void) -> ListeningTimer {
        let timer = FakeTimer(seconds, action); timers.append(timer); return timer
    }
}

private final class Fixture {
    let driver = FakeDriver()
    let scheduler = FakeScheduler()
    var valid = true
    var stoppedSpeech = 0
    var events: [[String: Any]] = []
    lazy var controller = NativeListeningController(
        driver: driver, scheduler: scheduler,
        contextValid: { [weak self] in self?.valid == true },
        stopSpeaking: { [weak self] in self?.stoppedSpeech += 1 },
        emit: { [weak self] in self?.events.append($0) }
    )
    func start(_ id: String = "turn_1") { controller.start(turnID: id, language: "zh-CN") }
    func authorize(_ index: Int = 0) {
        driver.speech[index](.authorized)
        driver.microphone[index](.authorized)
    }
    var state: String? { events.last?["state"] as? String }
    var reason: String? { events.last?["reason"] as? String }
}

private func expect(_ value: @autoclosure () -> Bool, _ message: String, file: StaticString = #filePath, line: UInt = #line) {
    if !value() { fatalError("\(file):\(line): \(message)") }
}

@main
enum NativeListeningTests {
    static func main() {
        var passed = 0
        func test(_ name: String, _ run: () -> Void) {
            run(); passed += 1; print("✓ \(name)")
        }

        test("construction does not request, inspect, or capture") {
            let f = Fixture(); _ = f.controller
            expect(f.driver.speechRequests == 0 && f.driver.microphoneRequests == 0, "startup prompted")
            expect(f.driver.prepareCalls == 0 && f.driver.startCalls == 0 && f.events.isEmpty, "startup inspected a device")
        }
        test("explicit start stops speech and requests Speech first") {
            let f = Fixture(); f.start()
            expect(f.stoppedSpeech == 1 && f.state == "authorizing", "missing authorizing")
            expect(f.driver.speechRequests == 1 && f.driver.microphoneRequests == 0, "wrong permission order")
        }
        test("Speech denial never requests microphone or starts recording") {
            let f = Fixture(); f.start(); f.driver.speech[0](.denied)
            expect(f.state == "error" && f.reason == "speech_denied", "wrong denial")
            expect(!f.controller.isActive && f.driver.microphoneRequests == 0 && f.driver.startCalls == 0, "recording after denial")
        }
        test("Speech restriction is distinct") {
            let f = Fixture(); f.start(); f.driver.speech[0](.restricted)
            expect(f.reason == "speech_restricted" && !f.controller.isActive, "wrong restriction")
        }
        test("unsupported on-device recognition fails before microphone") {
            let f = Fixture(); f.driver.prepareReason = "on_device_unavailable"
            f.start(); f.driver.speech[0](.authorized)
            expect(f.reason == "on_device_unavailable", "missing fail closed")
            expect(f.driver.microphoneRequests == 0 && f.driver.startCalls == 0, "unsupported captured")
        }
        test("unavailable recognizer fails closed") {
            let f = Fixture(); f.driver.prepareReason = "recognizer_unavailable"
            f.start(); f.driver.speech[0](.authorized)
            expect(f.reason == "recognizer_unavailable" && f.driver.startCalls == 0, "unavailable started")
        }
        test("microphone denial releases prepared recognition") {
            let f = Fixture(); f.start(); f.driver.speech[0](.authorized); f.driver.microphone[0](.denied)
            expect(f.reason == "microphone_denied" && f.driver.cancelCalls == 1 && f.driver.startCalls == 0, "mic denial leaked")
        }
        test("microphone restriction fails closed") {
            let f = Fixture(); f.start(); f.driver.speech[0](.authorized); f.driver.microphone[0](.restricted)
            expect(f.reason == "microphone_denied" && !f.controller.isActive, "mic restriction ignored")
        }
        test("authorized turn records with thirty second deadline") {
            let f = Fixture(); f.start(); f.authorize()
            expect(f.state == "listening" && f.driver.startCalls == 1, "did not listen")
            expect(f.scheduler.timers.count == 1 && f.scheduler.timers[0].seconds == 30, "wrong recording bound")
        }
        test("duplicate start does not request twice or restart") {
            let f = Fixture(); f.start(); f.start(); f.authorize(); f.start()
            expect(f.driver.speechRequests == 1 && f.driver.microphoneRequests == 1 && f.driver.startCalls == 1, "duplicate restarted")
        }
        test("new turn cancels old permission and ignores late old authorization") {
            let f = Fixture(); f.start("old"); f.start("new")
            f.driver.speech[0](.authorized)
            expect(f.driver.prepareCalls == 0 && f.driver.microphoneRequests == 0, "old permission continued")
            expect(f.events[1]["state"] as? String == "cancelled" && f.events[1]["reason"] as? String == "replaced", "old not cancelled")
            f.driver.speech[1](.authorized); f.driver.microphone[0](.authorized)
            expect(f.driver.startCalls == 1 && f.events.last?["turn_id"] as? String == "new", "new blocked")
        }
        test("cancelled microphone prompt cannot later start engine") {
            let f = Fixture(); f.start(); f.driver.speech[0](.authorized)
            f.controller.cancel(reason: "hide"); let count = f.events.count
            f.driver.microphone[0](.authorized)
            expect(f.driver.startCalls == 0 && f.events.count == count, "late mic started")
        }
        test("finish stops audio immediately and waits at most five seconds") {
            let f = Fixture(); f.start(); f.authorize(); f.controller.finish(turnID: "turn_1")
            expect(f.driver.finishCalls == 1 && f.state == "processing", "mic did not stop")
            expect(f.scheduler.timers[0].cancelled && f.scheduler.timers.last?.seconds == 5, "wrong final bound")
            f.scheduler.timers.last?.fire()
            expect(f.reason == "final_timeout" && f.driver.cancelCalls == 1 && !f.controller.isActive, "final wait unbounded")
        }
        test("recording deadline transitions to bounded final wait") {
            let f = Fixture(); f.start(); f.authorize(); f.scheduler.timers[0].fire()
            expect(f.state == "processing" && f.driver.finishCalls == 1 && f.scheduler.timers.last?.seconds == 5, "max duration not enforced")
        }
        test("only final text becomes result and is trimmed") {
            let f = Fixture(); f.start(); f.authorize()
            expect(f.events.allSatisfy { $0["text"] == nil }, "intermediate text escaped")
            f.controller.finish(turnID: "turn_1"); f.driver.recognition[0](.success("  彩宝你好 \n"))
            expect(f.state == "result" && f.events.last?["text"] as? String == "彩宝你好", "missing final draft")
            expect(!f.controller.isActive && f.driver.cancelCalls == 1 && f.scheduler.timers.allSatisfy(\.cancelled), "result leaked resources")
        }
        test("empty final reports no speech") {
            let f = Fixture(); f.start(); f.authorize(); f.driver.recognition[0](.success(" \n"))
            expect(f.reason == "no_speech" && !f.controller.isActive, "empty draft accepted")
        }
        test("oversize final fails explicitly instead of returning a clipped instruction") {
            let f = Fixture(); f.start(); f.authorize()
            f.driver.recognition[0](.success(String(repeating: "a", count: 4001)))
            expect(f.reason == "transcript_too_long" && f.events.last?["text"] == nil && !f.controller.isActive, "oversize instruction clipped")
            let emoji = Fixture(); emoji.start(); emoji.authorize()
            emoji.driver.recognition[0](.success(String(repeating: "🐱", count: 2001)))
            expect(emoji.reason == "transcript_too_long", "UTF-16 textarea limit exceeded")
        }
        test("maximum-length final is returned intact") {
            let f = Fixture(); f.start(); f.authorize(); let text = String(repeating: "a", count: 4000)
            f.driver.recognition[0](.success(text))
            expect(f.state == "result" && f.events.last?["text"] as? String == text, "valid final truncated")
        }
        test("recognition failure releases all resources") {
            let f = Fixture(); f.start(); f.authorize()
            f.driver.recognition[0](.failure(ListeningFailure(reason: "recognition_failed")))
            expect(f.reason == "recognition_failed" && f.driver.cancelCalls == 1 && f.scheduler.timers[0].cancelled, "failure leaked")
        }
        test("audio startup failure releases prepared driver") {
            let f = Fixture(); f.driver.startError = ListeningFailure(reason: "audio_unavailable")
            f.start(); f.authorize()
            expect(f.reason == "audio_unavailable" && f.driver.cancelCalls == 1 && f.scheduler.timers.isEmpty, "startup failure leaked")
        }
        test("stale turn stop and cancel cannot affect current recording") {
            let f = Fixture(); f.start(); f.authorize()
            f.controller.finish(turnID: "old"); f.controller.cancel(turnID: "old")
            expect(f.state == "listening" && f.driver.finishCalls == 0 && f.driver.cancelCalls == 0, "stale action stopped current")
        }
        test("finish during permission request is cancellation") {
            let f = Fixture(); f.start(); f.controller.finish(turnID: "turn_1")
            f.driver.speech[0](.authorized)
            expect(f.state == "cancelled" && f.driver.startCalls == 0 && f.driver.microphoneRequests == 0, "finish started late recording")
        }
        test("duplicate finish cannot extend final deadline") {
            let f = Fixture(); f.start(); f.authorize()
            f.controller.finish(turnID: "turn_1"); f.controller.finish(turnID: "turn_1")
            expect(f.driver.finishCalls == 1 && f.scheduler.timers.count == 2, "second finish extended deadline")
        }
        test("all lifecycle cancellation reasons fence late final and timeout") {
            for reason in ["hide", "compact", "quit", "reconnect", "navigation", "inactive", "pagehide", "user"] {
                let f = Fixture(); f.start(); f.authorize(); f.controller.finish(turnID: "turn_1")
                f.controller.cancel(reason: reason); let count = f.events.count
                f.driver.recognition[0](.success("do not restore")); f.scheduler.timers.forEach { $0.fire() }
                expect(f.state == "cancelled" && f.reason == reason && f.events.count == count, "late result after \(reason)")
                expect(f.driver.cancelCalls == 1 && f.scheduler.timers.allSatisfy(\.cancelled), "cleanup missing for \(reason)")
            }
        }
        test("old recognition and timers cannot affect a replacement turn") {
            let f = Fixture(); f.start("old"); f.authorize(); let oldTimer = f.scheduler.timers[0]
            f.start("new"); f.driver.speech[1](.authorized); f.driver.microphone[1](.authorized)
            let count = f.events.count
            f.driver.recognition[0](.success("old text")); oldTimer.fire()
            expect(f.events.count == count && f.state == "listening" && f.controller.isActive, "old callback completed new turn")
        }
        test("generation also isolates accidental reused public turn id") {
            let f = Fixture(); f.start(); f.authorize(); f.controller.cancel()
            f.start(); f.driver.speech[1](.authorized); f.driver.microphone[1](.authorized)
            let count = f.events.count; f.driver.recognition[0](.success("old generation"))
            expect(f.events.count == count && f.state == "listening", "id reuse leaked stale recognition")
        }
        test("context loss before authorization denies subsequent capture") {
            let f = Fixture(); f.start(); f.valid = false; f.driver.speech[0](.authorized)
            expect(f.reason == "context_lost" && f.state == "cancelled" && f.driver.prepareCalls == 0, "invisible context continued")
        }
        test("context loss before final rejects draft") {
            let f = Fixture(); f.start(); f.authorize(); f.valid = false
            f.driver.recognition[0](.success("must not enter draft"))
            expect(f.state == "cancelled" && f.reason == "context_lost" && f.events.last?["text"] == nil, "lost page got text")
        }
        test("invalid request and hidden start do not request permissions") {
            let f = Fixture(); f.controller.start(turnID: "with space", language: "zh-CN")
            expect(f.reason == "invalid_request", "invalid id accepted")
            f.controller.start(turnID: "valid", language: "en-US")
            expect(f.reason == "invalid_request", "unexpected locale accepted")
            f.valid = false; f.start()
            expect(f.reason == "not_interactive" && f.driver.speechRequests == 0, "hidden recording requested")
        }
        test("final arriving synchronously while ending audio cannot leave timer") {
            let f = Fixture(); f.start(); f.authorize()
            f.driver.onFinish = { f.driver.recognition[0](.success("finished")) }
            f.controller.finish(turnID: "turn_1")
            expect(f.state == "result" && f.scheduler.timers.count == 1 && f.scheduler.timers[0].cancelled, "stale final wait timer")
            f.driver.onFinish = nil
        }
        test("cancel invalidates before driver cancellation callback") {
            let f = Fixture(); f.start(); f.authorize()
            f.driver.onCancel = { f.driver.recognition[0](.success("cancel callback")) }
            f.controller.cancel()
            expect(f.state == "cancelled" && f.events.filter { $0["state"] as? String == "result" }.isEmpty, "cancel callback escaped")
            f.driver.onCancel = nil
        }
        test("a failure allows another explicit attempt") {
            let f = Fixture(); f.start("first"); f.driver.speech[0](.denied)
            f.start("retry"); f.driver.speech[1](.authorized); f.driver.microphone[0](.authorized)
            expect(f.state == "listening" && f.driver.startCalls == 1 && f.driver.speechRequests == 2, "retry blocked")
        }
        print("\(passed) native voice tests passed; no microphone, Speech, or UI was accessed.")
    }
}
