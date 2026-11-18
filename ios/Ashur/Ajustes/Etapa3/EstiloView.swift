import SwiftUI
import UniformTypeIdentifiers

/// Ajustes › Tu estilo (data-page="estilo"), nativo: subir un chat exportado de WhatsApp (.txt), elegir cuál eres tú,
/// cómo escribes y los chats subidos.
struct EstiloView: View {
    @Environment(Session.self) private var session

    struct Estilo: Decodable {
        let texto: String?; let medidas: Medidas; let ejemplos: Ejemplos; let imports: [Imp]
        struct Medidas: Decodable { let n: Int? }
        struct Ejemplos: Decodable { let listos: Int? }
        struct Imp: Decodable, Identifiable { let id: Int; let name: String; let n_me: Int; let n_others: Int; let learned: Bool?; let approved: Bool? }
    }
    struct Leido: Decodable { let mensajes: Int; let senders: [Remitente]; struct Remitente: Decodable { let name: String; let n: Int } }
    struct Hecho: Decodable { let n_me: Int; let n_others: Int }
    private struct Subida: Encodable { let name: String; let file_name: String; let text: String; var me: String? = nil }

    @State private var estilo: Estilo?
    @State private var eligiendo = false
    @State private var archivo: (nombre: String, texto: String)?
    @State private var nombreChat = ""
    @State private var leido: Leido?
    @State private var yo = ""
    @State private var nota: String?

    var body: some View {
        List {
            Section {
                Text("En WhatsApp: abre el chat › Más › Exportar chat › Sin archivos. Sube el .txt aquí.").font(.footnote)
                Button(archivo.map { "Archivo: " + $0.nombre } ?? "Elegir el archivo del chat") { eligiendo = true }
                TextField("Nombre del chat (por ejemplo, Ana)", text: $nombreChat)
                Button("Leer") { Task { await leer() } }
                if let l = leido {
                    Picker("¿Cuál eres tú?", selection: $yo) {
                        Text("Elige").tag("")
                        ForEach(l.senders, id: \.name) { s in Text("\(s.name) (\(s.n))").tag(s.name) }
                    }
                    Button("Importar") { Task { await importar() } }
                }
                if let nota { Nota(texto: nota) }
            } header: { Text("Subir un chat") } footer: {
                Text("Se guarda todo el chat en el servidor. De los demás solo queda el contexto de a qué respondías: nunca va a Claude ni se vuelve recuerdo. Ashur aprende solo de tus líneas, y solo cuando lo apruebes en «Por aprobar».")
            }
            Section("Así escribes") {
                VStack(alignment: .leading) {
                    Text(estilo?.texto ?? "Todavía no hay chats.")
                    if let n = estilo?.medidas.n, n > 0 {
                        Text("\(n) mensajes tuyos medidos · \(estilo?.ejemplos.listos ?? 0) listos como ejemplos").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Section("Chats subidos") {
                if (estilo?.imports ?? []).isEmpty { Text("Ningún chat todavía.") }
                ForEach(estilo?.imports ?? []) { i in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(i.name)
                            Text("\(i.n_me) tuyos · \(i.n_others) de otros · " + (i.learned == true ? "aprendido" : i.approved == true ? "aprendiendo…" : "espera tu aprobación en «Por aprobar»"))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        DosToques(titulo: "Borrar", armado: "¿Borrar todo?", rol: .destructive) {
                            let _: Ignorado? = try? await session.api?.delete("/api/imports/\(i.id)")
                            await cargar()
                        }.buttonStyle(.borderless)
                    }
                }
            }
        }
        .fileImporter(isPresented: $eligiendo, allowedContentTypes: [.plainText, .text]) { r in
            guard case .success(let url) = r else { return }
            let ok = url.startAccessingSecurityScopedResource()
            defer { if ok { url.stopAccessingSecurityScopedResource() } }
            guard let d = try? Data(contentsOf: url) else { nota = "No pude abrir el archivo."; return }
            if d.count > 8_000_000 { nota = "El archivo pasa de 8 MB."; return }
            archivo = (url.lastPathComponent, String(decoding: d, as: UTF8.self))
            leido = nil
        }
        .refreshable { await cargar() }
        .task { await cargar() }
    }

    private func cargar() async { estilo = try? await session.api?.get("/api/estilo") }

    private func nombreFinal() -> String {
        let n = nombreChat.trimmingCharacters(in: .whitespaces)
        if !n.isEmpty { return n }
        var f = archivo?.nombre ?? ""
        if f.lowercased().hasSuffix(".txt") { f = String(f.dropLast(4)) }
        if let r = f.range(of: "Chat de WhatsApp con ", options: [.caseInsensitive, .anchored]) { f.removeSubrange(r) }
        return f
    }

    private func leer() async {
        guard let api = session.api else { return }
        guard let a = archivo else { nota = "Elige el archivo .txt del chat."; return }
        nombreChat = nombreFinal()
        do {
            let l: Leido = try await api.post("/api/imports", body: Subida(name: nombreChat, file_name: a.nombre, text: a.texto))
            leido = l; yo = ""
            nota = "\(l.mensajes) mensajes. ¿Cuál eres tú?"
        } catch { nota = detalle(error, "No pude leerlo.") }
    }

    private func importar() async {
        guard let api = session.api, let a = archivo else { return }
        guard !yo.isEmpty else { nota = "Marca cuál eres tú."; return }
        do {
            let h: Hecho = try await api.post("/api/imports/confirm", body: Subida(name: nombreChat, file_name: a.nombre, text: a.texto, me: yo))
            nota = "Listo: \(h.n_me) mensajes tuyos y \(h.n_others) de otros. Aprueba en «Por aprobar» para que Ashur aprenda de los tuyos."
            leido = nil; archivo = nil
            await cargar()
        } catch { nota = detalle(error, "No pude importarlo.") }
    }
}
