import Foundation

struct ChatLine: Identifiable, Equatable {
    enum Role { case sergio, ashur, aviso }
    let id = UUID()
    let role: Role
    let text: String
    let date: Date
}

/// Una respuesta que pide permiso para usar Claude en ese mensaje (local primero).
struct ClaudeApproval: Equatable {
    let message: String
    let estimateUSD: Double?
}

@Observable
final class ChatModel {
    var api: APIClient?        // nil mientras la app está bloqueada
    private(set) var lines: [ChatLine] = []
    private(set) var sending = false
    private(set) var loading = false
    var pendingApproval: ClaudeApproval?
    var draft = ""            // sobrevive a abrir y cerrar el teléfono
    var error: String?
    var compartirDocs = false     // como en el panel: solo para el próximo mensaje
    var compartirDiario = false
    private(set) var convId: Int?
    private var reabrir = false
    let dictation = Dictation()
    let speaker = Speaker()

    init(api: APIClient) { self.api = api }

    /// 0.62 (como el panel): abrir el chat es una conversación nueva; Ashur recuerda por sus recuerdos, no por el hilo viejo.
    func loadHistory() async { nueva() }

    func nueva() {
        convId = nil; reabrir = false
        lines = [ChatLine(role: .aviso, text: "Conversación nueva. Ashur recuerda lo importante de las anteriores.", date: .now)]
    }

    func conversaciones() async -> ([Conversacion], Archivo?) {
        guard let api else { return ([], nil) }
        let l: [Conversacion] = (try? await api.get("/api/conversations")) ?? []
        return (l, try? await api.get("/api/conversations/archivo"))
    }

    /// Retomar una conversación de la lista. Las del archivo de antes de 0.62 solo se leen.
    func abrir(_ id: Int) async {
        guard let api else { return }
        loading = true
        defer { loading = false }
        do {
            let d: ConversacionAbierta = try await api.get("/api/conversations/\(id)")
            lines = d.messages.map { ChatLine(role: $0.role == "user" ? .sergio : .ashur, text: $0.content, date: Date(timeIntervalSince1970: $0.ts)) }
            if d.conversation.archived == true {
                convId = nil; reabrir = false
                lines.append(ChatLine(role: .aviso, text: "Esto es el archivo de antes de 0.62: solo se lee. Lo que escribas empieza una conversación nueva.", date: .now))
            } else { convId = id; reabrir = true }
            error = nil
        } catch { self.error = error.localizedDescription }
    }

    /// Claude no responde solo: `allowClaude` solo es true cuando Sergio lo autoriza para este mensaje.
    func send(_ text: String, allowClaude: Bool = false) async {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let api, !text.isEmpty, !sending else { return }
        if !allowClaude { lines.append(ChatLine(role: .sergio, text: text, date: .now)) }
        pendingApproval = nil
        sending = true
        defer { sending = false }
        do {
            if convId == nil, let n: NuevaConversacion = try? await api.post("/api/conversations") { convId = n.id }
            var req = ChatRequest(message: text, allow_claude: allowClaude)
            req.share_docs = compartirDocs; req.share_diary = compartirDiario
            req.conversation_id = convId; req.reopen = reabrir
            let r: ChatReply = try await api.post("/api/chat", body: req)
            if r.needs_approval != true { compartirDocs = false; compartirDiario = false }   // un reintento tras aprobar los conserva
            lines.append(ChatLine(role: .ashur, text: r.reply, date: .now))
            if speaker.readAloud { speaker.say(r.reply) }
            if let note = r.note, !note.isEmpty {
                lines.append(ChatLine(role: .aviso, text: note, date: .now))
            }
            if r.needs_approval == true {
                pendingApproval = ClaudeApproval(message: text, estimateUSD: r.estimate_usd)
            }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// El dictado se escribe en el borrador (detrás de lo que ya hubiera); Sergio lo revisa y lo envía él.
    func toggleDictation() async {
        if dictation.listening { dictation.stop(); return }
        speaker.stop()
        let before = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            try await dictation.start { [weak self] text in
                self?.draft = before.isEmpty ? text : before + " " + text
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    func approveClaude() async {
        guard let p = pendingApproval else { return }
        await send(p.message, allowClaude: true)
    }
}
