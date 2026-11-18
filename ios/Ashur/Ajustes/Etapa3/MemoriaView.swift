import SwiftUI

/// Ajustes › Memoria (data-page="memory"), nativo: por revisar, recuerdos, conversaciones resumidas e importar.
struct MemoriaView: View {
    @Environment(Session.self) private var session

    struct Recuerdo: Decodable, Identifiable { let id: Int; let text: String; let created: Double; let tier: String?; let source: String?; let nuevo: Bool?; let tema: String? }
    struct Propuesta: Decodable, Identifiable { let id: Int; let text: String; let source: String?; let owner: String? }
    struct Resumen: Decodable, Identifiable { let id: Int; let text: String; let ts_from: Double; let ts_to: Double; let provider: String? }
    struct Vectores: Decodable { let done: Int; let total: Int }
    struct Invitado: Decodable { let id: Int; let name: String }
    struct Corrida: Decodable { let added: Int?; let reason: String? }
    struct Importe: Decodable { let accepted: [String]; let skipped: [Omitido]; struct Omitido: Decodable { let text: String; let reason: String } }

    @State private var recuerdos: [Recuerdo] = []
    @State private var propuestas: [Propuesta] = []
    @State private var resumenes: [Resumen] = []
    @State private var invitados: [String: String] = [:]
    @State private var vectores: Vectores?
    @State private var propNota = "Ashur lee lo nuevo de tu diario con un modelo local, nunca con Claude, y te propone lo que parece duradero. Aquí también llega lo sensible (salud, dinero, otras personas) que aprende al cerrar cada conversación. Nada de esto se guarda sin que lo aceptes, y lo aceptado se guarda «solo local» salvo que tú digas otra cosa."
    @State private var leyendo = false
    @State private var editando: Int?
    @State private var borrador = ""
    @State private var importar = ""
    @State private var importeNota: String?
    @State private var listos = 0
    @State private var verTodos = false

    private static let temas = [("trabajo", "Trabajo"), ("gustos", "Gustos"), ("ashur", "Ashur"), ("privado", "Privado")]

    var body: some View {
        List {
            Section {
                if propuestas.isEmpty { Text("Nada por revisar.") }
                ForEach(propuestas) { p in propuesta(p) }
                Button(leyendo ? "Leyendo…" : "Buscar recuerdos nuevos ahora") { Task { await buscar() } }.disabled(leyendo)
                Nota(texto: propNota)
            } header: { Text("Por revisar") }
            Section {
                if let v = vectores, v.total > 0 {
                    Nota(texto: "Buscados por significado: \(v.done) de \(v.total)" + (v.done < v.total ? " (los demás se preparan cuando un equipo con Ollama está libre)." : "."))
                }
                if recuerdos.isEmpty { Text("Ashur todavía no ha guardado recuerdos.") }
                ForEach(verTodos ? recuerdos : Array(recuerdos.prefix(5))) { m in recuerdo(m) }
                if recuerdos.count > 5 { Button(verTodos ? "Ver menos" : "Ver los \(recuerdos.count)") { verTodos.toggle() } }
            } header: { Text("Recuerdos de Ashur") } footer: {
                Text("Lo que aprende solo al cerrar cada conversación aparece con un punto hasta que lo veas; corrígelo o bórralo si quedó mal. Los que dicen «solo local» solo los leen los modelos de tus equipos; los demás pueden viajar a Claude en cada mensaje. Puedes cambiar el nivel o borrar cualquiera.")
            }
            Section {
                if resumenes.isEmpty { Text("Todavía no hay conversaciones para resumir.") }
                ForEach(resumenes.prefix(5)) { s in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(s.text)
                        HStack {
                            Text(dia(s.ts_from) + (dia(s.ts_from) == dia(s.ts_to) ? "" : " a " + dia(s.ts_to)) + (s.provider == "local" ? ", modelo local" : ", Claude"))
                                .font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            Button("Olvidar", role: .destructive) { Task { await borrar("/api/summaries/\(s.id)") } }.buttonStyle(.borderless)
                        }
                    }
                }
            } header: { Text("Conversaciones resumidas") } footer: {
                Text("Un resumen corto de cada conversación, escrito por el modelo local al cerrarla (los de antes de 0.62 son del chat viejo). No entran a ningún prompt: son para que tú repases. Olvidar quita el resumen, no los mensajes.")
            }
            Section("Importar recuerdos") {
                Text("Pega un recuerdo por línea. Las líneas que empiezan con # se ignoran. Primero revisas, luego guardas.").font(.footnote)
                TextEditor(text: $importar).frame(minHeight: 110)
                Button("Revisar") { Task { await importe(dry: true) } }
                if let importeNota { Nota(texto: importeNota) }
                if listos > 0 { Button("Guardar \(listos)") { Task { await importe(dry: false) } } }
            }
        }
        .refreshable { await cargar() }
        .task { await cargar() }
    }

    private func dia(_ ts: Double) -> String { Date(timeIntervalSince1970: ts).formatted(.dateTime.day().month(.abbreviated)) }

    private func propuesta(_ p: Propuesta) -> some View {
        let invitado = p.owner.flatMap { $0 != "sergio" ? (invitados[$0] ?? "un invitado") : nil }
        return VStack(alignment: .leading, spacing: 4) {
            Text(p.text)
            Text(invitado.map { "De \($0) · irá a su perfil, solo local" } ?? (p.source == "diary" ? "Del diario" : "Del chat"))
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button(invitado != nil ? "Guardar en su perfil" : p.source == "diary" ? "Guardar (solo local)" : "Guardar") {
                    Task { await accion("/api/proposals/\(p.id)/approve") }
                }.buttonStyle(.borderedProminent)
                Button("Descartar") { Task { await accion("/api/proposals/\(p.id)/reject") } }.buttonStyle(.bordered)
            }
        }
    }

    private func recuerdo(_ m: Recuerdo) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if editando == m.id {
                TextField("Recuerdo", text: $borrador, axis: .vertical)
                Button("Guardar") { Task { await corregir(m) } }.buttonStyle(.borderedProminent)
            } else {
                HStack(alignment: .top) {
                    if m.nuevo == true { Circle().fill(Color.accentColor).frame(width: 8, height: 8).padding(.top, 6) }
                    Text(m.text)
                }
            }
            Text(Date(timeIntervalSince1970: m.created).formatted(.dateTime.day().month(.abbreviated).year())
                 + " · " + (m.tier == "local" ? "solo local" : "puede ir a Claude")
                 + ((m.source ?? "").hasPrefix("conv:") ? " · aprendido de una conversación" : ""))
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                if editando != m.id { Button("Corregir") { editando = m.id; borrador = m.text } }
                Menu(Self.temas.first { $0.0 == m.tema }?.1 ?? "sin tema") {
                    ForEach(Self.temas, id: \.0) { t in Button(t.1) { Task { await poner("/api/memory/\(m.id)/tema", ["tema": t.0]) } } }
                }
                Button(m.tier == "local" ? "Permitir Claude" : "Solo local") {
                    Task { await poner("/api/memory/\(m.id)/tier", ["tier": m.tier == "local" ? "claude" : "local"]) }
                }
                Button("Olvidar", role: .destructive) { Task { await borrar("/api/memory/\(m.id)") } }
            }
            .buttonStyle(.borderless).font(.caption)
        }
    }

    private func cargar() async {
        guard let api = session.api else { return }
        if let l: [Recuerdo] = try? await api.get("/api/memory") {
            recuerdos = l.reversed().sorted { ($0.nuevo == true ? 1 : 0) > ($1.nuevo == true ? 1 : 0) }
            if l.contains(where: { $0.nuevo == true }) { let _: Ignorado? = try? await api.post("/api/memory/seen") }
        }
        vectores = try? await api.get("/api/memory/vectors")
        if let p: [Propuesta] = try? await api.get("/api/proposals") {
            propuestas = p
            if p.contains(where: { ($0.owner ?? "sergio") != "sergio" }), let g: [Invitado] = try? await api.get("/api/guests") {
                invitados = Dictionary(uniqueKeysWithValues: g.map { ("g\($0.id)", $0.name) })
            }
        }
        if let s: [Resumen] = try? await api.get("/api/summaries") { resumenes = s.reversed() }
    }

    private func accion(_ ruta: String) async {
        guard let api = session.api else { return }
        let _: Ignorado? = try? await api.post(ruta, body: [String: String]())
        await cargar()
    }

    private func poner(_ ruta: String, _ body: [String: String]) async {
        guard let api = session.api else { return }
        let _: Ignorado? = try? await api.put(ruta, body: body)
        await cargar()
    }

    private func borrar(_ ruta: String) async {
        guard let api = session.api else { return }
        let _: Ignorado? = try? await api.delete(ruta)
        await cargar()
    }

    private func corregir(_ m: Recuerdo) async {
        let t = borrador.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        editando = nil
        await poner("/api/memory/\(m.id)", ["text": t])
    }

    private func buscar() async {
        guard let api = session.api else { return }
        leyendo = true
        defer { leyendo = false }
        do {
            let j: Corrida = try await api.post("/api/proposals/run")
            propNota = (j.added ?? 0) > 0 ? "Encontré \(j.added ?? 0) para que revises." : (j.reason ?? "No encontré nada nuevo.")
        } catch { propNota = "No pude leer ahora. Inténtalo en un rato." }
        await cargar()
    }

    private struct ImportBody: Encodable { let lines: [String]; let dry_run: Bool; let source = "claude" }

    private func importe(dry: Bool) async {
        guard let api = session.api else { return }
        do {
            let d: Importe = try await api.post("/api/memory/import", body: ImportBody(lines: importar.components(separatedBy: "\n"), dry_run: dry))
            if dry {
                listos = d.accepted.count
                var msg = "\(d.accepted.count) " + (d.accepted.count == 1 ? "recuerdo listo para guardar." : "recuerdos listos para guardar.")
                if !d.skipped.isEmpty { msg += " Omitidos: " + d.skipped.map { "«\($0.text)» (\($0.reason))" }.joined(separator: "; ") + "." }
                importeNota = msg
            } else {
                importeNota = "Guardados \(d.accepted.count)."
                importar = ""; listos = 0
                await cargar()
            }
        } catch { importeNota = dry ? "No pude revisar la lista." : "No pude guardar." }
    }
}
