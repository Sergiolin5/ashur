import SwiftUI

/// Ajustes › Documentos (data-page="docs"), nativo: lo leído por equipo, buscar y releer.
struct DocumentosView: View {
    @Environment(Session.self) private var session

    struct Docs: Decodable {
        let summary: [Resumen]; let hits: [Hit]?
        struct Resumen: Decodable, Identifiable { let node_id: String; let documents: Int; let chunks: Int; var id: String { node_id } }
        struct Hit: Decodable, Identifiable { let path: String; let snippet: String?; let node_id: String; var id: String { node_id + path + (snippet ?? "") } }
    }
    struct Pedido: Decodable { let queued: [String] }

    @State private var docs: Docs?
    @State private var buscar = ""
    @State private var buscado = ""
    @State private var nota: String?

    var body: some View {
        List {
            Section {
                if (docs?.summary ?? []).isEmpty { Text("Ningún equipo ha leído documentos todavía.") }
                ForEach(docs?.summary ?? []) { s in
                    LabeledContent { Text("\(Textos.n(s.chunks)) fragmentos") } label: {
                        Text(s.node_id); Text("\(Textos.n(s.documents)) " + (s.documents == 1 ? "documento leído" : "documentos leídos"))
                    }
                }
                HStack {
                    TextField("Buscar en mis documentos", text: $buscar).onSubmit { Task { await cargar(buscar) } }
                    Button("Buscar") { Task { await cargar(buscar) } }
                }
            } header: { Text("Documentos") }
            if !buscado.isEmpty {
                Section("Resultados") {
                    if (docs?.hits ?? []).isEmpty { Text("No encontré nada con esas palabras.") }
                    ForEach(docs?.hits ?? []) { h in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(h.path).font(.caption)
                            if let s = h.snippet { Text(s).font(.caption2).foregroundStyle(.secondary) }
                            Text(h.node_id).font(.caption2).foregroundStyle(.tertiary)
                        }
                    }
                }
            }
            Section {
                Button("Releer documentos ahora") { Task { await releer() } }
                if let nota { Nota(texto: nota) }
            } footer: {
                Text("Ashur lee el texto de Word, Excel, PowerPoint, PDF y archivos de texto de los equipos que tengan el módulo docs, solo cuando están libres. Lo sensible (claves, bóvedas, perfiles del navegador) no se lee. La búsqueda es local: nada de esto sale a Claude salvo que marques «Compartir fragmentos» en un mensaje.")
            }
        }
        .task { await cargar("") }
    }

    private func cargar(_ q: String) async {
        guard let api = session.api else { return }
        let t = q.trimmingCharacters(in: .whitespaces)
        buscado = t
        docs = try? await api.get("/api/docs", query: t.isEmpty ? [] : [URLQueryItem(name: "q", value: t)])
    }

    private func releer() async {
        guard let api = session.api else { return }
        nota = "Pidiéndolo…"
        do {
            let d: Pedido = try await api.post("/api/docs/refresh")
            nota = d.queued.isEmpty ? "Nada que pedir: ningún equipo con docs activo, o ya tienen un pase en curso."
                : "Pedido para \(d.queued.joined(separator: ", ")). Cada equipo empieza cuando esté libre (10 minutos sin uso y, si es portátil, enchufado)."
        } catch { nota = "No pude pedirlo." }
    }
}
