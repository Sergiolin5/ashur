import SwiftUI

/// Ajustes › Personas (data-page="voices"), nativo: personas por dispositivo, la persona de Ashur (versiones y nivel de
/// madurez) y las señales de tus documentos.
struct PersonasView: View {
    @Environment(Session.self) private var session

    struct Persona: Decodable { let name: String; let text: String }
    struct Base: Decodable { let text: String; let versions: [Version]; let levels: [String]; let level: String?; struct Version: Decodable { let id: String; let chars: Int } }
    struct Madurez: Decodable { let horas_pleno: Double; let dias_conversacion: Int; let sugerido: String?; let siguiente: Sig?; struct Sig: Decodable { let horas: Int; let dias: Int; let nivel: String } }
    struct Texto: Decodable { let text: String }
    struct Senales: Decodable {
        let text: String
        let sin_revisar: SinRevisar
        let health_folders: [[JSONValue]]
        struct SinRevisar: Decodable { let carpetas: Int; let revisadas: Int; let docs_sin_revisar: Int; let top: [Top] }
        struct Top: Decodable, Identifiable { let equipo: String; let carpeta: String; let documentos: Int; var id: String { equipo + carpeta } }
    }

    @State private var personas: [Persona] = []
    @State private var elegida = ""
    @State private var nombre = ""
    @State private var textoPersona = ""
    @State private var renombrando = false
    @State private var personaNota: String?
    @State private var base: Base?
    @State private var textoBase = ""
    @State private var nivel = ""
    @State private var madurez: String?
    @State private var baseNota: String?
    @State private var senales: Senales?
    @State private var contando = false
    @State private var verCrudo = false
    @State private var senalesNota: String?

    var body: some View {
        List {
            Section("Personas por dispositivo") {
                Text("Un solo Ashur, con la misma memoria; cada dispositivo cambia su voz. Elige el dispositivo en el chat, debajo de \"Hablando desde\".").font(.footnote)
                Picker("Persona", selection: $elegida) {
                    Text("Nueva persona…").tag("")
                    ForEach(personas, id: \.name) { Text($0.name).tag($0.name) }
                }
                .onChange(of: elegida) { mostrar() }
                TextField("Nombre del dispositivo (ej. iphone, portatil)", text: $nombre)
                    .disabled(!elegida.isEmpty && !renombrando).textInputAutocapitalization(.never)
                TextField("Cómo habla Ashur aquí: tono, largo de las respuestas, en qué se enfoca, qué evita…", text: $textoPersona, axis: .vertical).lineLimit(4...10)
                Button("Guardar") { Task { await guardarPersona() } }
                if !elegida.isEmpty {
                    Button(renombrando ? "Escribe el nombre nuevo y toca Guardar" : "Cambiar nombre") { renombrando = true }
                    Button("Borrar esta persona", role: .destructive) { Task { await borrarPersona() } }
                }
                if let personaNota { Nota(texto: personaNota) }
            }
            Section("Persona de Ashur") {
                Text("Quién es Ashur en todos los dispositivos. Las líneas que empiezan con «>» dicen de dónde salió cada rasgo: son para ti y no entran en lo que lee el modelo. Cada vez que guardas, el texto anterior queda en Versiones.").font(.footnote)
                TextEditor(text: $textoBase).frame(minHeight: 220).font(.callout)
                Button("Guardar") { Task { await guardarBase() } }
                if let b = base, !b.versions.isEmpty {
                    Menu("Versiones anteriores (\(b.versions.count))") {
                        ForEach(b.versions, id: \.id) { v in Button(fechaVersion(v.id) + " · \(v.chars) caracteres") { Task { await abrirVersion(v.id) } } }
                    }
                }
                if let b = base, !b.levels.isEmpty {
                    Picker("Madurez", selection: $nivel) {
                        Text("ninguna sección").tag("")
                        ForEach(b.levels, id: \.self) { Text($0).tag($0) }
                    }
                    .onChange(of: nivel) { if nivel != (base?.level ?? "") { Task { await guardarNivel() } } }
                }
                if let madurez { Nota(texto: madurez) }
                if let baseNota { Nota(texto: baseNota) }
            }
            Section("Señales de tus documentos") {
                Text("Qué carpetas con documentos leídos te faltan por revisar en el mapa. Lo que no marcas «Mía» o «Ruido» se queda fuera de la mudanza. Cuenta sin modelo y sin leer el texto.").font(.footnote)
                Button(contando ? "Contando…" : "Contar ahora") { Task { await contar() } }.disabled(contando)
                if let s = senales {
                    let u = s.sin_revisar
                    Nota(texto: "\(Textos.n(u.revisadas)) de \(Textos.n(u.carpetas)) carpetas revisadas" + (u.top.isEmpty ? ". No te falta ninguna." : "; faltan \(Textos.n(u.docs_sin_revisar)) documentos. Las que más tienen:"))
                    ForEach(u.top) { x in
                        VStack(alignment: .leading) {
                            Text(x.carpeta)
                            Text("\(x.equipo) · \(Textos.n(x.documentos)) documentos").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Button(verCrudo ? "Ocultar el conteo" : "Ver el conteo completo") { verCrudo.toggle() }
                    if verCrudo { Text(s.text).font(.caption.monospaced()).textSelection(.enabled) }
                    Button("Copiar conteo para Claude") { UIPasteboard.general.string = s.text }
                    if !s.health_folders.isEmpty {
                        Nota(texto: "Solo para ti (salud, no va a Claude): " + s.health_folders.map { f in
                            "\(f.first?.text ?? ""): \(f.count > 1 ? f[1].text ?? "" : "") (\(f.count > 2 ? f[2].text ?? "" : ""))" }.joined(separator: " · "))
                    }
                }
                if let senalesNota { Nota(texto: senalesNota) }
            }
        }
        .task { await cargarPersonas(); await cargarBase() }
    }

    private func fechaVersion(_ id: String) -> String {
        let c = Array(id)
        guard c.count >= 13 else { return id }
        return "\(String(c[0..<4]))-\(String(c[4..<6]))-\(String(c[6..<8])) \(String(c[9..<11])):\(String(c[11..<13]))"
    }

    private func mostrar() {
        let p = personas.first { $0.name == elegida }
        nombre = p?.name ?? ""; textoPersona = p?.text.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""; renombrando = false
    }

    private func cargarPersonas(_ elegir: String? = nil) async {
        guard let api = session.api else { return }
        personas = (try? await api.get("/api/personas")) ?? []
        elegida = elegir.flatMap { e in personas.contains { $0.name == e } ? e : nil } ?? ""
        mostrar()
    }

    private func guardarPersona() async {
        guard let api = session.api else { return }
        let n = nombre.trimmingCharacters(in: .whitespaces).lowercased(), t = textoPersona.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !n.isEmpty, !t.isEmpty else { personaNota = "Falta el nombre o la descripción."; return }
        let n2 = n.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? n
        if !elegida.isEmpty && elegida != n {
            do { let _: Ignorado = try await api.post("/api/personas/\(elegida)/rename", body: ["to": n]) }
            catch { personaNota = detalle(error, "No pude cambiar el nombre. Minúsculas, números y guiones."); return }
        }
        do { let _: Ignorado = try await api.put("/api/personas/\(n2)", body: ["text": t]); personaNota = "Guardada «\(n)»." }
        catch { personaNota = detalle(error, "No pude guardar. Nombre: minúsculas, números y guiones."); return }
        await cargarPersonas(n)
    }

    private func borrarPersona() async {
        guard let api = session.api, !elegida.isEmpty else { return }
        let n = elegida
        let _: Ignorado? = try? await api.delete("/api/personas/\(n)")
        personaNota = "Borrada «\(n)»."
        await cargarPersonas()
    }

    private func cargarBase() async {
        guard let api = session.api, let b: Base = try? await api.get("/api/persona") else { return }
        base = b; textoBase = b.text.trimmingCharacters(in: .whitespacesAndNewlines)
        nivel = b.levels.contains(b.level ?? "") ? (b.level ?? "") : ""
        madurez = nil
        if !b.levels.isEmpty, let m: Madurez = try? await api.get("/api/madurez") {
            let h = Int(m.horas_pleno), d = m.dias_conversacion
            madurez = m.sugerido.map { "Llevas \(h) h en crecimiento pleno y \(d) días de conversación: puedes pasar a \($0)." }
                ?? m.siguiente.map { "Llevas \(h) de \($0.horas) h y \(d) de \($0.dias) días para sugerir \($0.nivel)." }
                ?? "Llevas \(h) h en crecimiento pleno y \(d) días de conversación."
        }
    }

    private func guardarBase() async {
        guard let api = session.api else { return }
        let t = textoBase.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { baseNota = "La persona no puede quedar vacía."; return }
        do { let _: Ignorado = try await api.put("/api/persona", body: ["text": t]); baseNota = "Guardada. La anterior quedó en Versiones."; await cargarBase() }
        catch { baseNota = detalle(error, "No pude guardar la persona.") }
    }

    private func abrirVersion(_ id: String) async {
        guard let api = session.api else { return }
        do { let v: Texto = try await api.get("/api/persona/versions/\(id)"); textoBase = v.text.trimmingCharacters(in: .whitespacesAndNewlines)
             baseNota = "Versión cargada en el cuadro. Toca Guardar para volver a ella." }
        catch { baseNota = detalle(error, "No pude abrir esa versión.") }
    }

    private func guardarNivel() async {
        guard let api = session.api else { return }
        do { let _: Ignorado = try await api.put("/api/persona/level", body: ["level": nivel]); baseNota = "Nivel de madurez guardado."; await cargarBase() }
        catch { baseNota = detalle(error, "No pude guardar el nivel.") }
    }

    private func contar() async {
        guard let api = session.api else { return }
        contando = true
        defer { contando = false }
        do { senales = try await api.get("/api/persona/signals"); senalesNota = nil }
        catch { senalesNota = "No pude contar las señales: " + detalle(error, "error") }
    }
}
