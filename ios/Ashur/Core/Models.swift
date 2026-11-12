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
}

struct ChatReply: Decodable {
    let reply: String
    let note: String?
    let provider: String?
    let needs_approval: Bool?
    let estimate_usd: Double?
}

/// GET /api/memory
struct MemoryItem: Decodable, Identifiable {
    let id: Int
    let text: String
    let source: String?
    let tier: String?
}

/// GET /api/habits (fila de la tabla habits + descripcion y puede_hacer)
struct Habit: Decodable, Identifiable {
    let id: Int
    let accion: String
    let descripcion: String?
    let veces: Int
    let dias: Int
    let estado: String
}

/// GET /api/claude-requests: los Pedidos (Claude y hábitos)
struct Pedido: Decodable, Identifiable {
    let id: Int
    let ts: Double
    let action: String
    let reason: String
    let status: String
    let result: String?
    let label: String?

    var isPending: Bool { status == "pending" }
}

struct ApproveResult: Decodable {
    let status: String            // "approved" o "not_done"
    let result: String?
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
