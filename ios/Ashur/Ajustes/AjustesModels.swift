import Foundation

/// GET /api/claude-requests
struct PedidoItem: Decodable, Identifiable {
    let id: Int
    let ts: Double
    let action: String
    let reason: String?
    let status: String
    let result: String?
    let label: String?
    let module: String?
    let decided_at: Double?
    let lote: LoteInfo?
    let encargo: EncargoInfo?

    struct LoteInfo: Decodable { let label: String; let documents: Int; let by_model: Int; let estimate: String? }
    struct EncargoInfo: Decodable { let label: String; let estimate: String?; let docs: [String]; let to_claude: Bool? }
}

struct Decision: Decodable { let status: String; let result: String? }

/// GET /api/encargos
struct EncargoItem: Decodable, Identifiable {
    let id: Int
    let ts: Double
    let updated: Double?
    let label: String
    let status: String
    let error: String?
    let reason: String?
    let total: Int?
    let done: Int?
    let result: String?
    let salud: [String]?
    let to_claude: Bool?
}

/// GET /api/lotes
struct LoteItem: Decodable, Identifiable {
    let id: Int
    let ts: Double
    let updated: Double?
    let label: String
    let status: String
    let error: String?
    let total: Int
    let done: Int
    let failed: Int
    let skipped: Int
    let elapsed_s: Double?
    let left: Int?
    let eta_s: Double?
    let s_per_doc: Double?
    let device: String?
    let took_s: Double?
    let summary: String?
    let salud: [String]?
}

struct LoteConfig: Decodable { let nombres: [String]; let tipos: [String] }
struct Muestra: Decodable {
    let total: Int
    let muestra: [Fila]
    struct Fila: Decodable, Identifiable { let equipo: String; let ruta: String; let tipo: String?; let crudo: String?; var id: String { equipo + ruta } }
}
struct Reintento: Decodable { let lote: Int?; let encargo: Int?; let documentos: Int? }

/// GET /api/update
struct UpdateInfo: Decodable {
    let door: String?
    let current: String
    let latest: String?
    let available: Bool?
    let nodes: [Node]?
    let nodes_version: String?
    let updater: Updater?
    let updater_problem: String?
    let totp: Bool?
    struct Node: Decodable { let id: String; let version: String?; let current: Bool }
    struct Updater: Decodable { let busy: Bool?; let last: Last? }
    struct Last: Decodable { let version: String?; let ok: Bool?; let seconds: Double?; let steps: [String]? }
}
struct TotpState: Decodable { let active: Bool }
struct TotpSetup: Decodable { let uri: String; let secret: String }
struct Health: Decodable { let door: String? }

/// GET /api/diagnostics
struct Diagnostico: Decodable {
    let version: String
    let core_code_label: String?
    let now: Double
    let nodes: [Nodo]
    let problems: [Problema]
    struct Nodo: Decodable, Identifiable {
        let device_id: String
        let mode: String
        let reason: String?
        let behind: Bool?
        let hint: String?
        let code_label: String?
        let modules: [String]?
        let disks: [Disco]?
        let gpus: [String]?
        let llm: String?
        let battery_pct: Double?
        let on_power: Bool?
        let last_seen_s_ago: Double?
        var id: String { device_id }
    }
    struct Disco: Decodable { let mount: String; let total: Double?; let used: Double?; let present: Bool?; let scanned: Bool? }
    struct Problema: Decodable, Identifiable {
        let ts: Double
        let first_ts: Double?
        let count: Int?
        let text: String
        let node_id: String?
        let kind: String?
        var id: String { "\(ts)-\(text)" }
    }
}
struct SelfTest: Decodable { let running: Bool?; let report: String?; let progress: String? }
