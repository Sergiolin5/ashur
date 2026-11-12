import AVFoundation
import Speech

/// Funcionalidad (d), entrada. Dictado con el reconocedor de Apple **en el propio iPhone** (decisión de Sergio, 2026-11-12):
/// `requiresOnDeviceRecognition` impide que el audio salga del teléfono. Si el modelo en el dispositivo
/// no está disponible para el idioma, no se dicta: no hay respaldo por red.
@Observable
final class Dictation {
    private(set) var listening = false
    private let engine = AVAudioEngine()
    private let recognizer = SFSpeechRecognizer(locale: Dictation.locale)
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?

    /// Español: el del teléfono si ya está en español; si no, es-MX.
    static let locale: Locale = Locale.current.language.languageCode == .spanish ? Locale.current : Locale(identifier: "es-MX")

    enum Problem: LocalizedError {
        case denied, notOnDevice
        var errorDescription: String? {
            switch self {
            case .denied: "Ashur necesita permiso de micrófono y de reconocimiento de voz. Actívalos en Ajustes › Ashur."
            case .notOnDevice: "El dictado en el teléfono no está disponible para español en este dispositivo. Descarga el idioma en Ajustes › General › Teclado › Dictado."
            }
        }
    }

    /// `onText` recibe la transcripción completa hasta ahora (parcial mientras Sergio habla).
    func start(onText: @escaping @MainActor (String) -> Void) async throws {
        guard !listening else { return }
        guard await Self.authorized() else { throw Problem.denied }
        guard let recognizer, recognizer.isAvailable, recognizer.supportsOnDeviceRecognition else { throw Problem.notOnDevice }

        let audio = AVAudioSession.sharedInstance()
        try audio.setCategory(.playAndRecord, mode: .measurement, options: [.duckOthers, .defaultToSpeaker])
        try audio.setActive(true, options: .notifyOthersOnDeactivation)

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true
        request.addsPunctuation = true

        do {
            try Self.installTap(on: engine.inputNode, feeding: request)
            engine.prepare()
            try engine.start()
        } catch {
            finish()
            throw error
        }
        self.request = request
        listening = true
        task = recognizer.recognitionTask(with: request, resultHandler: Self.handler(for: self, onText: onText))
    }

    /// Deja de escuchar; el reconocedor entrega el texto final y termina solo.
    func stop() {
        guard listening else { return }
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
    }

    private func finish() {
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
        request = nil
        task = nil
        listening = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    // Los bloques de audio y del reconocedor no corren en el hilo principal: se crean fuera del MainActor.

    /// `installAudioTap` (iOS 27) entrega búferes de solo lectura; el reconocedor pide un `AVAudioPCMBuffer`, así que se copian.
    /// El tamaño admitido va de 100 a 400 ms: se piden 100 ms al ritmo del micrófono.
    nonisolated private static func installTap(on node: AVAudioInputNode, feeding request: SFSpeechAudioBufferRecognitionRequest) throws {
        let format = node.outputFormat(forBus: 0)
        nonisolated(unsafe) let request = request     // el tap solo llama a append(_:), igual que con la API anterior
        try node.installAudioTap(onBus: 0, bufferSize: AVAudioFrameCount(format.sampleRate / 10), format: format) { buffer, _ in
            request.append(AVAudioPCMBuffer(copying: buffer))
        }
    }

    nonisolated private static func handler(for dictation: Dictation, onText: @escaping @MainActor (String) -> Void)
        -> (SFSpeechRecognitionResult?, Error?) -> Void {
        { [weak dictation] result, error in
            let text = result?.bestTranscription.formattedString
            let done = error != nil || (result?.isFinal ?? false)
            Task { @MainActor in
                if let text { onText(text) }
                if done { dictation?.finish() }
            }
        }
    }

    nonisolated private static func authorized() async -> Bool {
        let speech = await withCheckedContinuation { c in
            SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0) }
        }
        guard speech == .authorized else { return false }
        return await AVAudioApplication.requestRecordPermission()
    }
}
