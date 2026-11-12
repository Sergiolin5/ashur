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
    private let api: APIClient
    private(set) var lines: [ChatLine] = []
    private(set) var sending = false
    private(set) var loading = false
    var pendingApproval: ClaudeApproval?
    var error: String?

    init(api: APIClient) { self.api = api }

    func loadHistory() async {
        loading = true
        defer { loading = false }
        do {
            let h: ChatHistory = try await api.get("/api/chat/history", query: [URLQueryItem(name: "limit", value: "50")])
            lines = h.messages.map {
                ChatLine(role: $0.role == "user" ? .sergio : .ashur, text: $0.content,
                         date: Date(timeIntervalSince1970: $0.ts))
            }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// Claude no responde solo: `allowClaude` solo es true cuando Sergio lo autoriza para este mensaje.
    func send(_ text: String, allowClaude: Bool = false) async {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sending else { return }
        if !allowClaude { lines.append(ChatLine(role: .sergio, text: text, date: .now)) }
        pendingApproval = nil
        sending = true
        defer { sending = false }
        do {
            let r: ChatReply = try await api.post("/api/chat", body: ChatRequest(message: text, allow_claude: allowClaude))
            lines.append(ChatLine(role: .ashur, text: r.reply, date: .now))
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

    func approveClaude() async {
        guard let p = pendingApproval else { return }
        await send(p.message, allowClaude: true)
    }
}
