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

struct ChatHistory: Decodable {
    struct Message: Decodable {
        let ts: Double
        let channel: String
        let role: String
        let content: String
    }
    let messages: [Message]
}
