import AVFoundation

/// Funcionalidad (d), salida. Lee en voz alta, en español y en el teléfono, las respuestas de Ashur.
/// Apagado por defecto; Sergio lo activa en el menú del chat y la preferencia se recuerda.
@Observable
final class Speaker {
    var readAloud: Bool {
        didSet {
            UserDefaults.standard.set(readAloud, forKey: Self.key)
            if !readAloud { stop() }
        }
    }
    private let synth = AVSpeechSynthesizer()
    private static let key = "leerEnVozAlta"

    init() { readAloud = UserDefaults.standard.bool(forKey: Self.key) }

    func say(_ text: String) {
        synth.stopSpeaking(at: .immediate)
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio, options: .duckOthers)
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: Dictation.locale.identifier(.bcp47))
            ?? AVSpeechSynthesisVoice(language: "es-MX")
        synth.speak(utterance)
    }

    func stop() { synth.stopSpeaking(at: .immediate) }
}
