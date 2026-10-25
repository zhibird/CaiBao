import Foundation

enum ListeningAuthorization {
    case authorized, denied, restricted
}

struct ListeningFailure: Error {
    let reason: String
}

/// Every callback and method is delivered on the main queue. Constructing the
/// boundary must not inspect permissions, create an input device, or record audio.
protocol NativeListeningDriver: AnyObject {
    func requestSpeechAuthorization(_ completion: @escaping (ListeningAuthorization) -> Void)
    func prepare(language: String) -> String?
    func requestMicrophoneAuthorization(_ completion: @escaping (ListeningAuthorization) -> Void)
    func start(_ completion: @escaping (Result<String, ListeningFailure>) -> Void) throws
    func finishAudio()
    func cancel()
}

protocol ListeningTimer: AnyObject { func cancel() }
protocol ListeningScheduler {
    func schedule(after seconds: TimeInterval, _ action: @escaping () -> Void) -> ListeningTimer
}

private final class MainListeningTimer: ListeningTimer {
    private var timer: Timer?
    init(seconds: TimeInterval, action: @escaping () -> Void) {
        let scheduled = Timer(timeInterval: seconds, repeats: false) { _ in action() }
        timer = scheduled
        RunLoop.main.add(scheduled, forMode: .common)
    }
    func cancel() { timer?.invalidate(); timer = nil }
    deinit { cancel() }
}

struct MainListeningScheduler: ListeningScheduler {
    func schedule(after seconds: TimeInterval, _ action: @escaping () -> Void) -> ListeningTimer {
        MainListeningTimer(seconds: seconds, action: action)
    }
}

/// An explicit, single voice turn. Recognition can only return draft text; this
/// component has no Agent, network, conversation, persistence, or send interface.
final class NativeListeningController {
    private enum Phase { case authorizing, listening, processing }
    private struct Turn {
        let id: String
        let generation: UUID
        var phase: Phase
    }

    private let driver: NativeListeningDriver
    private let scheduler: ListeningScheduler
    private let contextValid: () -> Bool
    private let stopSpeaking: () -> Void
    private let emit: ([String: Any]) -> Void
    private let recordingLimit: TimeInterval
    private let finalLimit: TimeInterval
    private var turn: Turn?
    private var timer: ListeningTimer?

    var isActive: Bool { turn != nil }

    init(driver: NativeListeningDriver, scheduler: ListeningScheduler = MainListeningScheduler(),
         recordingLimit: TimeInterval = 30, finalLimit: TimeInterval = 5,
         contextValid: @escaping () -> Bool, stopSpeaking: @escaping () -> Void,
         emit: @escaping ([String: Any]) -> Void) {
        self.driver = driver
        self.scheduler = scheduler
        self.recordingLimit = min(30, max(0.1, recordingLimit))
        self.finalLimit = min(5, max(0.1, finalLimit))
        self.contextValid = contextValid
        self.stopSpeaking = stopSpeaking
        self.emit = emit
    }

    func start(turnID: String, language: String) {
        guard Self.validTurnID(turnID), language == "zh-CN" else {
            emit(["turn_id": String(turnID.prefix(80)), "state": "error", "reason": "invalid_request"])
            return
        }
        // A duplicate click cannot request another permission or restart capture.
        if turn?.id == turnID { return }
        cancel(reason: "replaced")
        guard contextValid() else {
            emit(["turn_id": turnID, "state": "error", "reason": "not_interactive"])
            return
        }
        let generation = UUID()
        turn = Turn(id: turnID, generation: generation, phase: .authorizing)
        stopSpeaking()
        event("authorizing")
        driver.requestSpeechAuthorization { [weak self] authorization in
            guard let self, self.current(generation) else { return }
            guard authorization == .authorized else {
                self.fail(authorization == .restricted ? "speech_restricted" : "speech_denied")
                return
            }
            if let reason = self.driver.prepare(language: language) { self.fail(reason); return }
            self.driver.requestMicrophoneAuthorization { [weak self] authorization in
                guard let self, self.current(generation) else { return }
                guard authorization == .authorized else { self.fail("microphone_denied"); return }
                self.beginRecording(generation)
            }
        }
    }

    func finish(turnID: String) {
        guard let current = turn, current.id == turnID else { return }
        if current.phase == .authorizing { cancel(turnID: turnID, reason: "user"); return }
        guard current.phase == .listening else { return }
        timer?.cancel()
        turn?.phase = .processing
        // Stop the microphone before waiting for the final recognition callback.
        driver.finishAudio()
        guard self.current(current.generation) else { return }
        event("processing")
        timer = scheduler.schedule(after: finalLimit) { [weak self] in
            guard let self, self.current(current.generation), self.turn?.phase == .processing else { return }
            self.fail("final_timeout")
        }
    }

    func cancel(turnID: String? = nil, reason: String = "user") {
        guard let current = turn, turnID == nil || turnID == current.id else { return }
        // Invalidate before touching the driver: cancellation can itself complete a task.
        turn = nil
        timer?.cancel(); timer = nil
        driver.cancel()
        emit(["turn_id": current.id, "state": "cancelled", "reason": reason])
    }

    static func validTurnID(_ id: String) -> Bool {
        !id.isEmpty && id.utf8.count <= 80 && id.utf8.allSatisfy {
            (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 95
        }
    }

    private func current(_ generation: UUID) -> Bool {
        guard turn?.generation == generation else { return false }
        guard contextValid() else { cancel(reason: "context_lost"); return false }
        return true
    }

    private func beginRecording(_ generation: UUID) {
        guard current(generation) else { return }
        do {
            try driver.start { [weak self] result in
                guard let self, self.current(generation) else { return }
                switch result {
                case .failure(let error): self.fail(error.reason)
                case .success(let raw):
                    let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !text.isEmpty else { self.fail("no_speech"); return }
                    // Match the web textarea's UTF-16 limit without silently
                    // turning a truncated spoken instruction into a full draft.
                    guard text.utf16.count <= 4000 else { self.fail("transcript_too_long"); return }
                    self.complete(state: "result", additional: ["text": text])
                }
            }
            // The production driver dispatches asynchronously. This guard also
            // keeps a synchronous fake/error completion from creating a stale timer.
            guard current(generation) else { return }
            turn?.phase = .listening
            event("listening")
            timer = scheduler.schedule(after: recordingLimit) { [weak self] in
                guard let self, self.current(generation), let id = self.turn?.id else { return }
                self.finish(turnID: id)
            }
        } catch let error as ListeningFailure { fail(error.reason) }
        catch { fail("audio_unavailable") }
    }

    private func fail(_ reason: String) { complete(state: "error", additional: ["reason": reason]) }

    private func complete(state: String, additional: [String: Any]) {
        guard let current = turn else { return }
        turn = nil
        timer?.cancel(); timer = nil
        driver.cancel()
        var detail: [String: Any] = ["turn_id": current.id, "state": state]
        additional.forEach { detail[$0.key] = $0.value }
        emit(detail)
    }

    private func event(_ state: String) {
        guard let current = turn else { return }
        emit(["turn_id": current.id, "state": state])
    }
}
