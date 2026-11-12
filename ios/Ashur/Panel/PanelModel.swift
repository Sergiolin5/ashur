import Foundation

/// El panel derecho con el teléfono abierto: memoria, hábitos y pedidos. Solo lee; el servidor es la verdad.
@Observable
final class PanelModel {
    enum Tab: String, CaseIterable, Identifiable {
        case memoria = "Memoria", habitos = "Hábitos", pedidos = "Pedidos"
        var id: Self { self }
    }

    private let api: APIClient
    var tab: Tab = .pedidos
    private(set) var memories: [MemoryItem] = []
    private(set) var habits: [Habit] = []
    private(set) var pedidos: [Pedido] = []
    private(set) var loading = false
    var error: String?

    init(api: APIClient) { self.api = api }

    var pendingCount: Int { pedidos.filter(\.isPending).count }

    func load(_ tab: Tab) async {
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
