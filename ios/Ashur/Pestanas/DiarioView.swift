import SwiftUI
import UniformTypeIdentifiers

/// Pestaña Diario, nativa (etapa 5 de docs/ios/NATIVO.md): pregunta del día, entrada con borrador en el teléfono,
/// voz (dictado en el iPhone y conversaciones grabadas), avisos y entradas por día.
struct DiarioView: View {
    @Environment(Session.self) private var session

    struct Diario: Decodable {
        let question: String; let entries: [Entrada]; let settings: Ajuste; let devices: Int?
        struct Entrada: Decodable, Identifiable { let id: Int; let ts: Double; let day: String; let text: String }
        struct Ajuste: Decodable { let enabled: Bool; let hour: Int }
    }
    struct Avisos: Decodable {
        let labels: [String: String]; let cats: [String: Bool]; let daily_hour: Int?; let quiet: Quieto
        struct Quieto: Decodable { let enabled: Bool; let from: Int; let to: Int }
    }
    struct Audio: Decodable {
        let voice_ready: Bool?; let jobs: [Trabajo]
        struct Trabajo: Decodable, Identifiable { let id: Int; let ts: Double; let kind: String; let label: String?; let status: String; let notes: String?; let error: String? }
    }
    private static let estadoAudio = ["uploaded": "En cola…", "transcribing": "Transcribiendo en uno de tus equipos…", "noting": "Sacando las notas…", "done": "Listo", "failed": "No salió"]

    @AppStorage("ashur.diary.draft") private var borrador = ""
    @State private var diario: Diario?
    @State private var avisos: Avisos?
    @State private var audio: Audio?
    @State private var estado = ""
    @State private var error: String?
    @State private var guardando = false
    @State private var dictado = Dictation()
    @State private var vozNota = ""
    @State private var eligiendoAudio = false
    @State private var audioArchivo: (nombre: String, datos: Data)?
    @State private var conQuien = ""
    @State private var consentimiento = false
    @State private var subiendo = false

    var body: some View {
        List {
            Section {
                if let q = diario?.question {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Para hoy, si te apetece").font(.caption).foregroundStyle(.secondary)
                        Text(q).font(.headline)
                    }
                }
                TextField("Una línea basta. Lo que no guardes queda como borrador en este dispositivo.", text: $borrador, axis: .vertical)
                    .lineLimit(3...12)
                HStack {
                    Text(estado).font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("Guardar") { Task { await guardar() } }
                        .buttonStyle(.borderedProminent)
                        .disabled(guardando || borrador.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                if let error { Text(error).font(.footnote).foregroundStyle(.red) }
            }
            Section("Voz") { voz() }
            Section {
                if let d = diario {
                    Text("Los avisos de Ashur llegan por la app del panel en tu pantalla de inicio (esta app aún no recibe avisos).")
                        .font(.footnote).foregroundStyle(.secondary)
                    VStack(alignment: .leading) {
                        Text("Recordatorio nocturno")
                        Text(d.settings.enabled ? "Cada día a las \(d.settings.hour):00, y una segunda vez si sigues sin escribir" : "Apagado")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Picker("Hora del aviso", selection: Binding(get: { d.settings.hour }, set: { h in Task { await recordatorio(d.settings.enabled, h) } })) {
                        ForEach(0..<23, id: \.self) { Text("\($0):00").tag($0) }
                    }
                    Button(d.settings.enabled ? "Apagar" : "Encender") { Task { await recordatorio(!d.settings.enabled, d.settings.hour) } }
                    if let n = d.devices { Nota(texto: "Dispositivos con avisos: \(n).") }
                }
            } header: { Text("Avisos") }
            Section {
                if let a = avisos { sistema(a) }
            } header: { Text("Avisos del sistema") } footer: {
                Text("Ashur te avisa de lo que importa y junta varias novedades en un solo mensaje. De noche no suena: lo pendiente llega junto cuando termina el descanso.")
            }
            entradas()
        }
        .fileImporter(isPresented: $eligiendoAudio, allowedContentTypes: [.audio, .mpeg4Audio, .mp3, .wav, .aiff]) { r in
            guard case .success(let url) = r else { return }
            let ok = url.startAccessingSecurityScopedResource()
            defer { if ok { url.stopAccessingSecurityScopedResource() } }
            if let d = try? Data(contentsOf: url) { audioArchivo = (url.lastPathComponent, d) } else { vozNota = "No pude abrir el audio." }
        }
        .refreshable { await cargar() }
        .task { if !borrador.isEmpty { estado = "Borrador recuperado de este dispositivo" }; await cargar() }
        .onDisappear { dictado.stop() }
    }

    @ViewBuilder private func voz() -> some View {
        HStack {
            VStack(alignment: .leading) {
                Text("Dictar al diario")
                Text("Habla; el texto aparece arriba para que lo revises. Se reconoce en el iPhone, sin enviar el audio.").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button(dictado.listening ? "Detener" : "Dictar") {
                if dictado.listening { dictado.stop(); return }
                let base = borrador
                Task {
                    do { try await dictado.start { t in borrador = (base.isEmpty ? "" : base + " ") + t } }
                    catch { vozNota = error.localizedDescription }
                }
            }.buttonStyle(.bordered)
        }
        VStack(alignment: .leading, spacing: 6) {
            Text("Subir una conversación grabada")
            Text("Desde Notas de voz: Compartir → guarda en Archivos y elígela aquí. Se transcribe en tu Mac o Legion Go, el audio se borra al terminar y solo quedan las notas.")
                .font(.caption).foregroundStyle(.secondary)
            Button(audioArchivo.map { "Audio: " + $0.nombre } ?? "Elegir el audio") { eligiendoAudio = true }
            TextField("¿Con quién era? (opcional)", text: $conQuien)
            Toggle("La otra persona sabe que se grabó y está de acuerdo.", isOn: $consentimiento).font(.footnote)
            Button(subiendo ? "Subiendo…" : "Subir y sacar notas") { Task { await subir() } }.buttonStyle(.bordered).disabled(subiendo)
        }
        if !vozNota.isEmpty { Nota(texto: vozNota) }
        else if audio?.voice_ready == false { Nota(texto: "Ningún equipo tiene todavía el módulo de voz: se activa en la Mac o el Legion Go (Ajustes → Comandos → Voz).") }
        ForEach((audio?.jobs ?? []).filter { $0.kind == "conversation" }) { j in
            VStack(alignment: .leading, spacing: 4) {
                Text((j.label.map { "Conversación con " + $0 } ?? "Conversación") + " · " + Date(timeIntervalSince1970: j.ts).formatted(.dateTime.day().month(.abbreviated)))
                Text(Self.estadoAudio[j.status] ?? j.status).font(.caption).foregroundStyle(.secondary)
                if j.status == "done", let n = j.notes { Text(n).font(.callout).textSelection(.enabled) }
                if j.status == "failed", let e = j.error { Text(e).font(.caption).foregroundStyle(.red) }
                HStack {
                    if j.status == "done" { Button("Pasar al diario") { Task { await audioAccion("/api/audio/\(j.id)/to-diary", post: true) } } }
                    if j.status == "failed" { Button("Reintentar") { Task { await audioAccion("/api/audio/\(j.id)/retry", post: true) } } }
                    if j.status != "transcribing" && j.status != "noting" { Button("Borrar", role: .destructive) { Task { await audioAccion("/api/audio/\(j.id)", post: false) } } }
                }.buttonStyle(.borderless)
            }
        }
    }

    @ViewBuilder private func sistema(_ a: Avisos) -> some View {
        ForEach(a.labels.sorted { $0.key < $1.key }, id: \.key) { par in
            let k = par.key
            VStack(alignment: .leading) {
                Toggle(par.value, isOn: Binding(get: { a.cats[k] ?? false }, set: { v in Task { await notify(["cats": [k: v]]) } }))
                if k == "daily" {
                    Text(a.cats["daily"] == true ? "Cada día a las \(a.daily_hour ?? 0):00 (aunque sea descanso nocturno)" : "Apagada").font(.caption).foregroundStyle(.secondary)
                    Picker("Hora de la revisión del día", selection: Binding(get: { a.daily_hour ?? 0 }, set: { h in Task { await notify(["daily_hour": h]) } })) {
                        ForEach(0..<24, id: \.self) { Text("\($0):00").tag($0) }
                    }
                    Button("Enviar ahora") {
                        Task { let _: Ignorado? = try? await session.api?.post("/api/notify/daily/now") }
                    }
                }
            }
        }
        VStack(alignment: .leading) {
            Toggle("Descanso nocturno", isOn: Binding(get: { a.quiet.enabled }, set: { v in Task { await notify(["quiet_enabled": v]) } }))
            Text(a.quiet.enabled ? "Sin avisos de \(a.quiet.from):00 a \(a.quiet.to):00" : "Apagado").font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private func entradas() -> some View {
        let lista = diario?.entries ?? []
        if lista.isEmpty {
            Section("Entradas") { Text("Aquí aparecerá lo que escribas. Una línea ya cuenta.").foregroundStyle(.secondary) }
        }
        let dias = lista.reduce(into: [String]()) { if $0.last != $1.day { $0.append($1.day) } }
        ForEach(dias, id: \.self) { dia in
            Section(dia == dias.first ? "Entradas · " + nombreDia(dia) : nombreDia(dia)) {
                ForEach(lista.filter { $0.day == dia }) { e in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(Date(timeIntervalSince1970: e.ts).formatted(date: .omitted, time: .shortened)).font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            DosToques(titulo: "Borrar", armado: "Toca otra vez para borrar", rol: .destructive) {
                                let _: Ignorado? = try? await session.api?.delete("/api/diary/\(e.id)")
                                await cargar()
                            }.buttonStyle(.borderless).font(.caption)
                        }
                        Text(e.text)
                    }
                }
            }
        }
    }

    /// diaryDay() del panel: «Martes, 17 de noviembre».
    private func nombreDia(_ day: String) -> String {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"
        guard let d = f.date(from: day) else { return day }
        let t = d.formatted(.dateTime.weekday(.wide).day().month(.wide).locale(Locale(identifier: "es-CO")))
        return t.prefix(1).uppercased() + t.dropFirst()
    }

    private func cargar() async {
        guard let api = session.api else { return }
        diario = try? await api.get("/api/diary")
        avisos = try? await api.get("/api/notify")
        audio = try? await api.get("/api/audio")
        if (audio?.jobs ?? []).contains(where: { ["transcribing", "noting", "uploaded"].contains($0.status) }) {
            try? await Task.sleep(for: .seconds(5))
            await cargar()
        }
    }

    private func guardar() async {
        guard let api = session.api else { return }
        let t = borrador.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        guardando = true; error = nil
        defer { guardando = false }
        do {
            let _: Ignorado = try await api.post("/api/diary", body: ["text": t, "source": dictado.listening ? "dictated" : "typed"])
            dictado.stop()
            borrador = ""
            estado = t.count < 60 ? "Guardado. Una línea también cuenta." : "Guardado. Gracias por parar un momento."
            await cargar()
        } catch {
            self.error = "No se pudo guardar (sin conexión con el núcleo). Tu texto sigue aquí y en el borrador de este dispositivo; reintenta."
        }
    }

    private struct AjusteDiario: Encodable { let enabled: Bool; let hour: Int }
    private func recordatorio(_ on: Bool, _ h: Int) async {
        let _: Ignorado? = try? await session.api?.put("/api/diary/settings", body: AjusteDiario(enabled: on, hour: h))
        await cargar()
    }

    private func notify(_ body: [String: Any]) async {
        guard let api = session.api, let data = try? JSONSerialization.data(withJSONObject: body),
              let json = try? JSONDecoder().decode([String: JSONCodable].self, from: data) else { return }
        let _: Ignorado? = try? await api.put("/api/notify", body: json)
        await cargar()
    }

    private func audioAccion(_ ruta: String, post: Bool) async {
        guard let api = session.api else { return }
        if post { let _: Ignorado? = try? await api.post(ruta) } else { let _: Ignorado? = try? await api.delete(ruta) }
        await cargar()
    }

    private func subir() async {
        guard let api = session.api else { return }
        guard let a = audioArchivo else { vozNota = "Elige primero el archivo de audio."; return }
        guard consentimiento else { vozNota = "Marca la casilla: la otra persona debe saber y estar de acuerdo."; return }
        subiendo = true; vozNota = "Subiendo… (los audios largos tardan un poco)"
        defer { subiendo = false }
        let ext = String((a.nombre.split(separator: ".").last.map(String.init) ?? "m4a").lowercased().prefix(5))
        do {
            let _: Ignorado = try await api.upload("/api/audio", query: [
                URLQueryItem(name: "kind", value: "conversation"), URLQueryItem(name: "ext", value: ext),
                URLQueryItem(name: "label", value: conQuien.trimmingCharacters(in: .whitespaces)), URLQueryItem(name: "consent", value: "true")], data: a.datos)
            vozNota = "Subido. Puedes cerrar esta pantalla; te aviso cuando estén las notas."
            audioArchivo = nil; consentimiento = false
            await cargar()
        } catch { vozNota = detalle(error, "No se pudo subir.") }
    }
}

/// Un JSON cualquiera que también se puede mandar (para PUT /api/notify, cuyo cuerpo cambia según el ajuste).
indirect enum JSONCodable: Codable {
    case b(Bool), n(Double), s(String), o([String: JSONCodable]), a([JSONCodable]), null
    init(from d: Decoder) throws {
        let c = try d.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .b(v) }
        else if let v = try? c.decode(Double.self) { self = .n(v) }
        else if let v = try? c.decode(String.self) { self = .s(v) }
        else if let v = try? c.decode([String: JSONCodable].self) { self = .o(v) }
        else { self = .a(try c.decode([JSONCodable].self)) }
    }
    func encode(to e: Encoder) throws {
        var c = e.singleValueContainer()
        switch self {
        case .b(let v): try c.encode(v)
        case .n(let v): if v == v.rounded() { try c.encode(Int(v)) } else { try c.encode(v) }
        case .s(let v): try c.encode(v)
        case .o(let v): try c.encode(v)
        case .a(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
}
