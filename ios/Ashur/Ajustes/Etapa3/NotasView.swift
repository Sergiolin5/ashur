import SwiftUI

/// Ajustes › Notas del panel (data-page="notes"), nativo: pendientes con sus imágenes, resolver/reabrir, borrar,
/// copiar el texto para Claude y compartir con las imágenes.
struct NotasView: View {
    @Environment(Session.self) private var session

    struct Nota_: Decodable, Identifiable {
        let id: Int; let ts: Double; let text: String; let status: String; let author: String?; let ctx: String?
        let images: [Img]; let done_ts: Double?
        struct Img: Decodable { let id: Int }
        var contexto: [String: JSONValue] {
            guard let c = ctx, let d = c.data(using: .utf8) else { return [:] }
            return (try? JSONDecoder().decode([String: JSONValue].self, from: d)) ?? [:]
        }
    }

    @State private var notas: [Nota_] = []
    @State private var imagenes: [Int: [UIImage]] = [:]
    @State private var version = ""
    @State private var copiado: String?
    @State private var compartir: [Any]?

    var body: some View {
        let abiertas = notas.filter { $0.status == "open" }, hechas = notas.filter { $0.status == "done" }
        List {
            Section {
                if abiertas.isEmpty { Text("Nada pendiente.") }
                ForEach(abiertas) { fila($0) }
                Button(copiado ?? "Copiar texto para Claude") {
                    UIPasteboard.general.string = texto(); copiado = "Copiado: pégalo en el chat"
                }
                Button("Compartir con las imágenes") { compartir = [texto()] + abiertas.reversed().flatMap { imagenes[$0.id] ?? [] } }
            } header: { Text("Por llevarle a Claude") } footer: {
                Text("Toca la banderita arriba a la derecha desde cualquier pestaña para anotar algo en el momento.")
            }
            Section("Resueltos") {
                if hechas.isEmpty { Text("Aún no has resuelto ninguno.") }
                ForEach(hechas) { fila($0) }
            }
        }
        .refreshable { await cargar() }
        .task { await cargar() }
        .sheet(isPresented: Binding(get: { compartir != nil }, set: { if !$0 { compartir = nil } })) {
            Compartir(items: compartir ?? [])
        }
    }

    private func donde(_ n: Nota_) -> String {
        let c = n.contexto
        var s = c["page"]?.text ?? c["tab"]?.text ?? ""
        if let at = c["at"]?.text { s += " · " + at }
        if let w = c["w"]?.text, let h = c["h"]?.text { s += " · \(w)×\(h)" }
        return s
    }

    private func lapso(_ s: Double) -> String {
        s < 90 ? "\(Int(s.rounded())) s" : s < 5400 ? "\(Int((s / 60).rounded())) min"
            : s < 129600 ? "\((s / 360).rounded() / 10) h" : "\((s / 8640).rounded() / 10) días"
    }

    private func fila(_ n: Nota_) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(n.text).foregroundStyle(n.status == "done" ? .secondary : .primary)
            Text(((n.author ?? "Sergio") != "Sergio" ? "de \(n.author ?? "") · " : "") + donde(n) + (n.done_ts.map { " · resuelto en " + lapso($0 - n.ts) } ?? ""))
                .font(.caption).foregroundStyle(.secondary)
            if let imgs = imagenes[n.id], !imgs.isEmpty {
                ScrollView(.horizontal) {
                    HStack { ForEach(imgs.indices, id: \.self) { Image(uiImage: imgs[$0]).resizable().scaledToFill().frame(width: 70, height: 70).clipped().cornerRadius(6) } }
                }
            }
            HStack {
                Button(n.status == "open" ? "Resuelto" : "Reabrir") {
                    Task {
                        let _: Ignorado? = try? await session.api?.post("/api/notes/\(n.id)/status", body: ["status": n.status == "open" ? "done" : "open"])
                        await cargar()
                    }
                }
                DosToques(titulo: "Borrar", armado: "Toca otra vez", rol: .destructive) {
                    let _: Ignorado? = try? await session.api?.delete("/api/notes/\(n.id)")
                    await cargar()
                }
            }.buttonStyle(.borderless)
        }
    }

    /// notesText() del panel.
    private func texto() -> String {
        let o = Array(notas.filter { $0.status == "open" }.reversed())
        return "Pendientes del panel (\(o.count)), panel v\(version):\n" + o.enumerated().map { i, n in
            "\(i + 1). [" + ((n.author ?? "Sergio") != "Sergio" ? "opinión de \(n.author ?? "") · " : "") + (donde(n).isEmpty ? "?" : donde(n)) + "] " + n.text
                + (n.images.isEmpty ? "" : " (\(n.images.count) imagen" + (n.images.count > 1 ? "es" : "") + ")")
        }.joined(separator: "\n")
    }

    private func cargar() async {
        guard let api = session.api else { return }
        if let l: [Nota_] = try? await api.get("/api/notes") { notas = l }
        if let o: Overview = try? await api.get("/api/overview") { version = o.version }
        for n in notas where !n.images.isEmpty && imagenes[n.id] == nil {
            var imgs: [UIImage] = []
            for im in n.images { if let d = try? await api.data("/api/notes/\(n.id)/images/\(im.id)"), let u = UIImage(data: d) { imgs.append(u) } }
            imagenes[n.id] = imgs
        }
    }
}

/// La hoja de compartir de iOS (texto e imágenes).
struct Compartir: UIViewControllerRepresentable {
    let items: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController { UIActivityViewController(activityItems: items, applicationActivities: nil) }
    func updateUIViewController(_ c: UIActivityViewController, context: Context) {}
}
