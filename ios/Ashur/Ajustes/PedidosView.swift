import SwiftUI

/// Ajustes › Por aprobar (data-page="claude-requests"), nativo. Etapa 2 de docs/ios/NATIVO.md.
struct PedidosView: View {
    @Environment(Session.self) private var session
    @State private var lista: [PedidoItem] = []
    @State private var nota: String?
    @State private var cargado = false

    private static let estado = ["running": "En curso", "approved": "Aprobado", "rejected": "Rechazado", "not_done": "No se hizo"]

    var body: some View {
        let pendientes = lista.filter { $0.status == "pending" }, hechos = lista.filter { $0.status != "pending" }
        List {
            Section {
                if cargado && pendientes.isEmpty { Text("Nada por decidir.") }
                ForEach(pendientes) { q in pendiente(q) }
                if let nota { Nota(texto: nota) }
                Nota(texto: "Ni Claude ni tus hábitos hacen nada por su cuenta: lo que piden llega aquí y solo pasa si lo apruebas. Al aprobar, Ashur revisa otra vez si todavía aplica.")
            } header: { Text("Por decidir") }
            Section("Historial") {
                if cargado && hechos.isEmpty { Text("Claude aún no ha pedido nada.") }
                ForEach(hechos) { q in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(titulo(q))
                        Text((Self.estado[q.status] ?? q.status) + (q.result.map { ": " + $0 } ?? "") + " · " + Fechas.cuando(q.decided_at ?? q.ts))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .refreshable { await cargar() }
        .task { await cargar() }
    }

    private func titulo(_ q: PedidoItem) -> String { (q.label ?? q.action).cap + (q.module.map { " (\($0))" } ?? "") }

    private func pendiente(_ q: PedidoItem) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(titulo(q)).font(.headline)
            Text((q.reason.map { $0 + " · " } ?? "") + Fechas.cuando(q.ts)).font(.caption).foregroundStyle(.secondary)
            if let L = q.lote {
                Text("\(L.label.cap) · \(L.documents) documentos (\(L.by_model) los lee el modelo)" + (L.estimate.map { " · " + $0 } ?? ""))
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let e = q.encargo {
                Text(e.label.cap + (e.estimate.map { " · " + $0 } ?? "")).font(.caption).foregroundStyle(.secondary)
                Text(e.docs.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                if e.to_claude == true { Text("El resultado irá a Claude").font(.caption.weight(.semibold)).foregroundStyle(.orange) }
            }
            HStack {
                DosToques(titulo: "Aprobar", armado: "Toca otra vez para aprobar") { await decidir(q.id, "approve") }
                    .buttonStyle(.borderedProminent)
                Button("Rechazar") { Task { await decidir(q.id, "reject") } }
                    .buttonStyle(.bordered)
            }
        }
        .padding(.vertical, 4)
    }

    private func cargar() async {
        guard let api = session.api else { return }
        if let l: [PedidoItem] = try? await api.get("/api/claude-requests") { lista = l }
        cargado = true
    }

    private func decidir(_ id: Int, _ que: String) async {
        guard let api = session.api else { return }
        do {
            if que == "reject" {
                let _: Ignorado = try await api.post("/api/claude-requests/\(id)/reject")
                nota = "Pedido #\(id) rechazado."
            } else {
                let d: Decision = try await api.post("/api/claude-requests/\(id)/approve")
                nota = "Pedido #\(id): " + (Self.estado[d.status] ?? d.status).lowercased() + (d.result.map { ". " + $0 } ?? "")
            }
        } catch {
            nota = detalle(error, "No pude decidir el pedido.")
        }
        await cargar()
    }
}
