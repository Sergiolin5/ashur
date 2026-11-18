import Foundation

/// GET /api/overview: lo mismo que dibuja la pestaña Estado del panel web (render(data) en core/ui/index.html).
struct Overview: Decodable {
    let version: String
    let now: Double
    let status: Status
    let running: [Running]
    let lote: Lote?
    let queued: [Queued]
    let queue_paused: Bool
    let inventory: [Inventario]?           // null mientras el núcleo lo calcula
    let activity: [Evento]
    let node_code_version: String?
    let node_code_label: String?

    struct Status: Decodable {
        let online: Int
        let total: Int
        let nodes: [Nodo]
    }
}

struct Nodo: Decodable, Identifiable {
    let device_id: String
    let os: String?
    let mode: String
    let reason: String?
    let code_version: String?
    let code_label: String?
    let cpu_pct: Double?
    let ram_pct: Double?
    let gpu_pct: Double?
    let battery_pct: Double?
    let on_power: Bool?
    let last_seen_s_ago: Double?
    var id: String { device_id }
}

struct Running: Decodable, Identifiable {
    let id: Int
    let module: String
    let node_id: String?
    let elapsed_s: Double?
    let entries: Int?
    let folder: String?
    let phase: String?
    let found: Int?
    let read: Int?
}

struct Lote: Decodable {
    let id: Int
    let label: String
    let status: String
    let done: Int
    let total: Int
    let failed: Int
    let skipped: Int
    let eta_s: Double?
}

struct Queued: Decodable, Identifiable {
    let id: Int
    let module: String
    let target: String?
    let weight: String?
}

struct Inventario: Decodable, Identifiable {
    let node_id: String
    let entries: Int
    let bytes: Double
    let runnable: Int?
    var id: String { node_id }
}

struct Evento: Decodable, Identifiable {
    let ts: Double
    let node_id: String?
    let event: String
    let detail: [String: JSONValue]?
    var id: String { "\(ts)-\(event)-\(node_id ?? "")" }
}

/// Los mismos textos que el panel web (MODE, MODULE, OS, describe, ago, duration, bytes).
enum Textos {
    static let modo = ["full": "Disponible", "light": "Uso ligero", "busy": "Ocupado", "off": "En pausa", "offline": "Desconectado"]
    static let modulo = ["inventory": "Inventario", "docs": "Lectura de documentos", "llm": "Modelo local", "voice": "Transcripción de voz"]
    static let moduloCola = ["inventory": "Inventario", "docs": "Lectura de documentos", "llm": "Respuesta del modelo local", "voice": "Transcripción de voz"]
    static let os = ["windows": "Windows", "macos": "macOS", "linux": "Linux"]
    static let numero: NumberFormatter = {
        let f = NumberFormatter(); f.numberStyle = .decimal; f.locale = Locale(identifier: "es-CO"); return f
    }()
    static func n(_ v: Int) -> String { numero.string(from: NSNumber(value: v)) ?? String(v) }

    static func hace(_ s: Double?) -> String {
        guard let s else { return "" }
        let v = max(0, Int(s.rounded()))
        if v < 60 { return "hace \(v) s" }
        if v < 3600 { return "hace \(v / 60) min" }
        if v < 86400 { return "hace \(v / 3600) h" }
        return "hace \(v / 86400) d"
    }

    static func duracion(_ s: Double?) -> String {
        let v = max(0, Int((s ?? 0).rounded()))
        if v < 60 { return "\(v) s" }
        if v < 3600 { return "\(v / 60) min" }
        return "\(v / 3600) h \((v % 3600) / 60) min"
    }

    static func bytes(_ b: Double) -> String {
        let u = ["B", "KB", "MB", "GB", "TB"]
        var i = 0, v = b
        while v >= 1024 && i < u.count - 1 { v /= 1024; i += 1 }
        return (i == 0 ? String(Int(v)) : String(format: v < 10 ? "%.1f" : "%.0f", v)) + " " + u[i]
    }

    static func describe(_ ev: Evento) -> String {
        let d = ev.detail ?? [:]
        let s = { (k: String) in d[k]?.text }
        let mod = (modulo[s("module") ?? ""] ?? s("module") ?? "tarea").lowercased()
        switch ev.event {
        case "register": return "Se unió a Ashur"
        case "reregister": return "Volvió a registrarse"
        case "offline": return "Se desconectó"
        case "asleep": return "Se volvió a dormir"
        case "back_online": return "Volvió a conectarse"
        case "mode_change":
            let to = s("to") ?? ""
            return ["full": "Quedó disponible", "light": "Pasó a uso ligero", "busy": "Se ocupó", "off": "Se pausó"][to] ?? "Cambió a \(to)"
        case "task_queued": return "Nueva tarea: " + mod + (s("reason") == "first join" ? " inicial" : "")
        case "task_start": return "Empezó " + mod
        case "task_done": return "Terminó: " + mod
        case "task_failed": return "Falló: " + mod
        case "revoked": return "Acceso revocado"
        case "chat":
            return (s("channel") == "atajo" ? "Conversación desde el Atajo" : "Conversación")
                + (s("provider") == "local" ? ", modelo local" : s("provider") == "claude" ? ", Claude" : "")
        case "chat_unanswered": return "Mensaje sin responder"
        case "modules_changed": return "Cambió sus capacidades: " + (d["to"]?.list ?? []).joined(separator: ", ")
        case "code_updated": return "Actualizó su código"
        case "restart_requeued": return "Reinició y devolvió su tarea a la cola"
        case "task_yielded": return "Pausó " + mod + " para liberar el equipo"
        case "chat_blocked_budget": return "Mensaje bloqueado por el tope de gasto"
        case "chat_error": return "No pudo hablar con Claude"
        case "memory_saved": return "Guardó un recuerdo"
        case "memory_deleted": return s("by") == "sergio" ? "Borraste un recuerdo" : "Borró un recuerdo"
        case "memory_imported": return "Importaste \(s("count") ?? "") recuerdos"
        case "conversation_summarized": return "Resumió \(s("messages") ?? "") mensajes de conversación"
        case "summary_deleted": return "Borraste un resumen de conversación"
        case "summary_failed": return "No pudo resumir la conversación"
        case "feedback_received": return (s("from") ?? "Alguien") + " dejó una opinión"
        case "disk_choice": return (d["scan"]?.bool == true ? "Activaste" : "Quitaste") + " el escaneo del disco " + (s("disk") ?? "")
        case "node_problem": return "Avisó: " + (s("text") ?? "un problema")
        case "diary_saved": return "Nueva entrada en el diario"
        case "push_sent", "notified": return "Te mandó un aviso"
        case "push_failed": return "No pudo mandar un aviso"
        case "memories_proposed":
            if let c = s("count"), c != "0" { return "Propuso \(c) recuerdos para revisar" }
            return "Buscó recuerdos nuevos"
        case "memory_accepted": return "Guardaste un recuerdo propuesto"
        case "memory_tier": return "Cambiaste el nivel de un recuerdo"
        case "audio_uploaded": return "Subiste un audio"
        case "audio_done": return "Terminó de transcribir un audio"
        case "audio_failed": return "Falló una transcripción"
        default: return ev.event.replacingOccurrences(of: "_", with: " ")
        }
    }
}
