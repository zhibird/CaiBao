import AVFoundation
import Foundation
import Speech

/// The audio render callback owns its original request, never a later turn's
/// mutable request. Closing this gate prevents append/endAudio races on teardown.
private final class SpeechAudioGate {
    private let lock = NSLock()
    private let request: SFSpeechAudioBufferRecognitionRequest
    private var accepting = true
    init(_ request: SFSpeechAudioBufferRecognitionRequest) { self.request = request }
    func append(_ buffer: AVAudioPCMBuffer) {
        lock.lock(); defer { lock.unlock() }
        if accepting { request.append(buffer) }
    }
    func finish() {
        lock.lock(); defer { lock.unlock() }
        guard accepting else { return }
        accepting = false
        request.endAudio()
    }
}

/// No device, permission status, or recognizer is touched by init. Apple Speech
/// is only used after an explicit startListening request from the trusted page.
final class AppleSpeechDriver: NativeListeningDriver {
    private var recognizer: SFSpeechRecognizer?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var engine: AVAudioEngine?
    private var gate: SpeechAudioGate?
    private var tapInstalled = false
    private var configurationObserver: NSObjectProtocol?

    func requestSpeechAuthorization(_ completion: @escaping (ListeningAuthorization) -> Void) {
        SFSpeechRecognizer.requestAuthorization { status in
            DispatchQueue.main.async {
                switch status {
                case .authorized: completion(.authorized)
                case .restricted: completion(.restricted)
                default: completion(.denied)
                }
            }
        }
    }

    func prepare(language: String) -> String? {
        guard let candidate = SFSpeechRecognizer(locale: Locale(identifier: language)) else {
            return "on_device_unavailable"
        }
        // requiresOnDeviceRecognition alone is not enough: Apple only honors it
        // when this capability is true. There is deliberately no remote fallback.
        guard candidate.supportsOnDeviceRecognition else { return "on_device_unavailable" }
        guard candidate.isAvailable else { return "recognizer_unavailable" }
        recognizer = candidate
        return nil
    }

    func requestMicrophoneAuthorization(_ completion: @escaping (ListeningAuthorization) -> Void) {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: DispatchQueue.main.async { completion(.authorized) }
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async { completion(granted ? .authorized : .denied) }
            }
        case .restricted: DispatchQueue.main.async { completion(.restricted) }
        default: DispatchQueue.main.async { completion(.denied) }
        }
    }

    func start(_ completion: @escaping (Result<String, ListeningFailure>) -> Void) throws {
        guard let recognizer, recognizer.supportsOnDeviceRecognition else {
            throw ListeningFailure(reason: "on_device_unavailable")
        }
        guard recognizer.isAvailable else { throw ListeningFailure(reason: "recognizer_unavailable") }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = false
        request.taskHint = .dictation
        self.request = request

        let engine = AVAudioEngine()
        self.engine = engine
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw ListeningFailure(reason: "audio_unavailable")
        }
        let gate = SpeechAudioGate(request)
        self.gate = gate
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in gate.append(buffer) }
        tapInstalled = true
        task = recognizer.recognitionTask(with: request) { result, error in
            if let result, result.isFinal {
                let text = result.bestTranscription.formattedString
                DispatchQueue.main.async { completion(.success(text)) }
            } else if error != nil {
                DispatchQueue.main.async { completion(.failure(ListeningFailure(reason: "recognition_failed"))) }
            }
        }
        configurationObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { _ in completion(.failure(ListeningFailure(reason: "audio_unavailable"))) }
        engine.prepare()
        do { try engine.start() }
        catch { throw ListeningFailure(reason: "audio_unavailable") }
    }

    func finishAudio() {
        if let observer = configurationObserver { NotificationCenter.default.removeObserver(observer) }
        configurationObserver = nil
        gate?.finish()
        if let engine {
            engine.stop()
            if tapInstalled { engine.inputNode.removeTap(onBus: 0); tapInstalled = false }
        }
    }

    func cancel() {
        finishAudio()
        task?.cancel()
        task = nil
        request = nil
        gate = nil
        engine = nil
        recognizer = nil
    }

    deinit { cancel() }
}
