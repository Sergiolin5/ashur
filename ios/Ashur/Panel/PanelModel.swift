import Foundation

/// El panel derecho con el teléfono abierto: memoria, hábitos y pedidos. Solo lee; el servidor es la verdad.
@Observable
final class PanelModel {
    enum Tab: String, CaseIterable, Identifiable {
        case memoria = "Memoria", habitos = "Hábitos", pedidos = "Pedidos"
        var id: Self { self }
    }

    var api: APIClient?        // nil mientras la app está bloqueada
    var tab: Tab = .pedidos
    private(set) var memories: [MemoryItem] = []
    private(set) var habits: [Habit] = []
    private(set) var pedidos: [Pedido] = []
    private(set) var loading = false
    var error: String?

    init(api: APIClient) { self.api = api }

    var pendingCount: Int { pedidos.filter(\.isPending).count }
    private(set) var deciding: Int?          // id del pedido que se está decidiendo
    var notice: String?

    /// Funcionalidad (c). Aprobar = leer la clave con biometría (Touch ID en el Duo, Face ID en el 15 Pro)
    /// + POST /api/claude-requests/{id}/approve con esa clave. Sin biometría no sale la petición.
    func approve(_ p: Pedido) async {
        guard deciding == nil else { return }
        deciding = p.id
        defer { deciding = nil }
        do {
            guard let key = try await KeyStore.read(reason: "Aprobar: \(p.label ?? p.action)") else {
                error = "La clave guardada ya no es válida. Ciérrala con «Olvidar la clave» y escríbela otra vez."
                return
            }
            let r: ApproveResult = try await APIClient(key: key).post("/api/claude-requests/\(p.id)/approve")
            notice = r.status == "approved" ? (r.result?.isEmpty == false ? r.result : "Aprobado.")
                                            : "No se hizo: \(r.result ?? "sin detalle")"
        } catch KeyStore.Failure.cancelled {
            return                                   // Sergio canceló Touch ID: nada sale al servidor
        } catch {
            self.error = error.localizedDescription  // 404: ya se decidió o no existe
        }
        await load(.pedidos)
    }

    /// Rechazar no ejecuta nada en el servidor, así que no pide biometría (la clave ya está desbloqueada).
    func reject(_ p: Pedido) async {
        guard let api, deciding == nil else { return }
        deciding = p.id
        defer { deciding = nil }
        do {
            let _: OK = try await api.post("/api/claude-requests/\(p.id)/reject")
        } catch {
            self.error = error.localizedDescription
        }
        await load(.pedidos)
    }

    func load(_ tab: Tab) async {
        guard let api else { return }
        loading = true
        defer { loading = false }
        do {
            switch tab {
            case .memoria: memories = try await api.get("/api/memory")
            case .habitos: habits = try await api.get("/api/habits")
            case .pedidos: pedidos = try await api.get("/api/claude-requests")
            }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
