import Foundation

// Formas de las respuestas del núcleo (core/app.py). Solo los campos que la app usa.

struct WhoAmI: Decodable {
    let role: String              // "admin", "self" o "guest"
    let name: String
}

struct ChatRequest: Encodable {
    let message: String
    let channel = "app"
    let persona = "iphone"        // la personalidad vive en el servidor (personas/iphone.md)
    let allow_claude: Bool
    var share_docs = false        // Claude puede leer fragmentos de documentos en este mensaje
    var share_diary = false       // …y entradas del diario
    var conversation_id: Int? = nil   // 0.62: nil = la conversación abierta, o una nueva
    var reopen = false            // seguir una conversación cerrada abierta desde la lista
}

/// GET /api/conversations y /api/conversations/{id} (núcleo 0.62).
struct Conversacion: Decodable, Identifiable {
    let id: Int; let title: String?; let started: Double; let last_msg: Double?; let archived: Bool?
}
struct ConversacionAbierta: Decodable {
    let conversation: Conversacion
    let messages: [ChatHistory.Message]
}
struct Archivo: Decodable { let total: Int?; let done: Int?; let terminado: Bool?; let aprobado: Bool? }
struct NuevaConversacion: Decodable { let id: Int }

struct ChatReply: Decodable {
    let reply: String
    let note: String?
    let provider: String?
    let needs_approval: Bool?
    let estimate_usd: Double?
}

struct OK: Decodable { let ok: Bool }

struct ChatHistory: Decodable {
    struct Message: Decodable {
        let ts: Double
        let channel: String
        let role: String
        let content: String
    }
    let messages: [Message]
}
