import SwiftUI

/// Ajustes › Trabajo en curso (data-page="encargos"), nativo: lotes, cómo clasifican, encargos en curso y terminados.
/// Etapa 2 de docs/ios/NATIVO.md. Mismas acciones que el panel: cancelar, enviar a Claude o no, reintentar, ver las dudas.
struct TrabajoView: View {
    @Environment(Session.self) private var session
    @State private var encargos: [EncargoItem] = []
    @State private var lotes: [LoteItem] = []
    @State private var nombres = ""
    @State private var tipos = ""
    @State private var configNota: String?
    @State private var nota: String?
    @State private var cargado = false

    static let estado = ["pending_approval": "por aprobar", "queued": "en espera", "running": "trabajando", "paused": "pausado", "done": "listo",
                         "released": "enviado a Claude", "kept": "solo en el panel", "rejected": "rechazado", "cancelled": "cancelado", "failed": "falló"]
    static let abiertos: Set<String> = ["pending_approval", "queued", "running", "paused"]
    static let hecho = ["cancel": "cancelado", "release": "enviado a Claude", "keep": "se queda solo en el panel"]

    var body: some View {
        let abiertos = encargos.filter { Self.abiertos.contains($0.status) }, terminados = encargos.filter { !Self.abiertos.contains($0.status) }
        List {
            if !lotes.isEmpty {
                Section {
                    ForEach(lotes) { L in LoteFila(lote: L, nota: $nota, recargar: cargar) }
                } header: { Text("Lotes") } footer: {
                    Text("Un lote hace la misma pregunta a todos los documentos de un alcance, de a uno, cuando no hay un encargo esperando. Lo que aprende se queda aquí; a Claude solo llega el resumen si tú lo envías, y sin las líneas de salud.")
                }
            }
            Section {
                Text("Los nombres con los que apareces en tus documentos (uno por línea). El modelo local los usa para saber qué escribiste o firmaste tú. Se quedan en tu servidor: no van a Claude.")
                    .font(.footnote)
                TextEditor(text: $nombres).frame(minHeight: 90)
                Text("Los tipos de documento, uno por línea. El modelo solo puede elegir de esta lista, o «otro». Déjala vacía para volver a la lista de fábrica.")
                    .font(.footnote)
                TextEditor(text: $tipos).frame(minHeight: 130)
                Button("Guardar") { Task { await guardarConfig() } }
                if let configNota { Nota(texto: configNota) }
            } header: { Text("Cómo clasifican los lotes") }
            Section {
                if cargado && abiertos.isEmpty { Text("Nada en curso.") }
                ForEach(abiertos) { e in fila(e) }
                if let nota { Nota(texto: nota) }
            } header: { Text("En curso") } footer: {
                Text("Claude pide el encargo, tú lo apruebas en Pedidos y tus equipos lo hacen de a un trozo con los modelos locales. El resultado solo llega a Claude si tú lo envías.")
            }
            Section("Terminados") {
                if cargado && terminados.isEmpty { Text("Claude aún no ha encargado nada.") }
                ForEach(terminados) { e in fila(e) }
            }
        }
        .refreshable { await cargar() }
        .task { await cargar(); await cargarConfig() }
    }

    private func fila(_ e: EncargoItem) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(e.label.cap) · #\(e.id)").font(.headline)
            Text((Self.estado[e.status] ?? e.status).cap + (e.error.map { ": " + $0 } ?? "") + " · " + Fechas.cuando(e.updated ?? e.ts))
                .font(.caption).foregroundStyle(.secondary)
            if let r = e.reason { Text(r).font(.caption).foregroundStyle(.secondary) }
            if let t = e.total, t > 0 {
                ProgressView(value: Double(min(e.done ?? 0, t)), total: Double(t))
                Text("\(e.done ?? 0) de \(t) trozos").font(.caption)
            }
            if let r = e.result { Text(r).font(.callout).textSelection(.enabled).padding(8).background(.quaternary, in: RoundedRectangle(cornerRadius: 8)) }
            if let s = e.salud, !s.isEmpty {
                Text("No va a Claude (salud), \(s.count) " + (s.count == 1 ? "línea: " : "líneas: ") + s.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                if Self.abiertos.contains(e.status) {
                    DosToques(titulo: "Cancelar", armado: "Toca otra vez para cancelar", rol: .destructive) { await mover("encargos", e.id, "cancel") }
                } else if e.status == "done" && e.to_claude == true {
                    DosToques(titulo: "Enviar a Claude", armado: "Toca otra vez para enviar") { await mover("encargos", e.id, "release") }
                    Button("No enviar") { Task { await mover("encargos", e.id, "keep") } }
                } else if e.status == "failed" {
                    Button("Reintentar") { Task { await reintentar("encargos", e.id) } }
                }
            }
            .buttonStyle(.bordered)
        }
        .padding(.vertical, 4)
    }

    private func cargar() async {
        guard let api = session.api else { return }
        do { encargos = try await api.get("/api/encargos") } catch { nota = "No pude cargar los encargos." }
        if let l: [LoteItem] = try? await api.get("/api/lotes") { lotes = l }
        cargado = true
    }

    private func cargarConfig() async {
        guard let api = session.api, let c: LoteConfig = try? await api.get("/api/lotes/config") else { return }
        nombres = c.nombres.joined(separator: "\n"); tipos = c.tipos.joined(separator: "\n")
    }

    private func guardarConfig() async {
        guard let api = session.api else { return }
        do {
            let _: Ignorado = try await api.put("/api/lotes/config", body: ["nombres": nombres, "tipos": tipos])
            configNota = "Guardado. Vale desde el próximo lote."
            await cargarConfig()
        } catch { configNota = detalle(error, "No pude guardar.") }
    }

    func mover(_ tipo: String, _ id: Int, _ que: String) async {
        guard let api = session.api else { return }
        let quien = tipo == "lotes" ? "Lote" : "Encargo"
        do {
            let _: Ignorado = try await api.post("/api/\(tipo)/\(id)/\(que)")
            nota = "\(quien) #\(id): \(Self.hecho[que] ?? que)."
        } catch { nota = detalle(error, "No pude cambiar el \(quien.lowercased()).") }
        await cargar()
    }

    func reintentar(_ tipo: String, _ id: Int) async {
        guard let api = session.api else { return }
        do {
            let r: Reintento = try await api.post("/api/\(tipo)/\(id)/retry")
            let que = tipo == "lotes" ? "Lote #\(r.lote ?? 0) (\(Textos.n(r.documentos ?? 0)) documentos)" : "Encargo #\(r.encargo ?? 0)"
            nota = que + " creado: apruébalo en Pedidos para que corra."
        } catch { nota = detalle(error, "No pude crear el reintento.") }
        await cargar()
    }
}

/// Una fila de lote, con su progreso, sus acciones y «Ver las dudas» (corregir Mía / De otro, reclasificar).
private struct LoteFila: View {
    @Environment(Session.self) private var session
    let lote: LoteItem
    @Binding var nota: String?
    let recargar: () async -> Void
    @State private var muestra: Muestra?
    @State private var marcadas: [String: String] = [:]
    @State private var reclasificar: String?

    var body: some View {
        let L = lote, abierto = TrabajoView.abiertos.contains(L.status)
        VStack(alignment: .leading, spacing: 4) {
            Text("\(L.label.cap) · #\(L.id)").font(.headline)
            Text((TrabajoView.estado[L.status] ?? L.status).cap + (L.error.map { ": " + $0 } ?? "") + " · " + Fechas.cuando(L.updated ?? L.ts))
                .font(.caption).foregroundStyle(.secondary)
            if L.total > 0 {
                let visto = L.done + L.failed + L.skipped
                ProgressView(value: Double(min(visto, L.total)), total: Double(L.total))
                Text("\(Textos.n(L.done)) de \(Textos.n(L.total)) documentos (\(Int((100 * Double(min(visto, L.total)) / Double(L.total)).rounded())) %)").font(.caption)
                let b = lineas(L)
                if !b.isEmpty { Text(b).font(.caption).foregroundStyle(.secondary) }
            }
            if let s = L.summary { Text(s).font(.callout).textSelection(.enabled).padding(8).background(.quaternary, in: RoundedRectangle(cornerRadius: 8)) }
            if let s = L.salud, !s.isEmpty { Text("No va a Claude (salud), \(s.count) " + (s.count == 1 ? "línea." : "líneas.")).font(.caption).foregroundStyle(.secondary) }
            HStack {
                if abierto {
                    DosToques(titulo: "Cancelar", armado: "Toca otra vez para cancelar", rol: .destructive) { await mover("cancel") }
                } else if L.status == "done" || L.status == "kept" {
                    DosToques(titulo: "Enviar a Claude", armado: "Toca otra vez para enviar") { await mover("release") }
                    if L.status == "done" { Button("No enviar") { Task { await mover("keep") } } }
                }
                if L.status == "failed" || (L.failed > 0 && ["done", "kept", "released"].contains(L.status)) {
                    Button(L.status == "failed" ? "Reintentar" : "Reintentar \(L.failed) fallidos") { Task { await reintentar() } }
                }
                if !abierto && L.done > 0 && muestra == nil {
                    Button("Ver las dudas") { Task { await verDudas() } }
                }
            }
            .buttonStyle(.bordered)
            if let m = muestra { dudas(m) }
        }
        .padding(.vertical, 4)
    }

    /// loteLines() del panel, segunda línea.
    private func lineas(_ L: LoteItem) -> String {
        var b: [String] = []
        if L.status == "paused" { b.append("en pausa con la cola") }
        if L.status == "queued" { b.append("por empezar") }
        if let e = L.elapsed_s { b.append("lleva " + Textos.duracion(e)) }
        if let left = L.left, left > 0, let eta = L.eta_s { b.append("faltan ~" + Textos.duracion(eta) + " con el equipo libre") }
        if let s = L.s_per_doc { b.append(String(format: "%.1f", s).replacingOccurrences(of: ".", with: ",") + " s por documento") }
        if let d = L.device { b.append("en " + d) }
        if L.failed > 0 { b.append("\(L.failed) fallidos") }
        if L.skipped > 0 { b.append("\(L.skipped) sin texto") }
        if let t = L.took_s { b.append("tardó " + Textos.duracion(t)) }
        return b.joined(separator: " · ")
    }

    @ViewBuilder private func dudas(_ m: Muestra) -> some View {
        Text("\(Textos.n(m.total)) documentos en duda" + (m.total > m.muestra.count ? "; \(m.muestra.count) de muestra" : "")
             + ". Di de quién es cada uno o reclasifícalos todos con el modelo más fuerte:").font(.footnote)
        if m.total > 0 {
            Button(reclasificar ?? "Reclasificar las \(Textos.n(m.total)) dudas") { Task { await reclasificarDudas() } }
                .buttonStyle(.bordered).disabled(reclasificar != nil)
        }
        ForEach(m.muestra) { x in
            VStack(alignment: .leading, spacing: 2) {
                Text(x.ruta.split(whereSeparator: { $0 == "/" || $0 == "\\" }).last.map(String.init) ?? x.ruta).font(.subheadline)
                Text("\(x.equipo): \(x.ruta)").font(.caption2).foregroundStyle(.secondary)
                Text("Tipo: \(x.tipo ?? "otro") · el modelo dijo: \(x.crudo ?? "(nada guardado)")").font(.caption2).foregroundStyle(.secondary)
                if let hecho = marcadas[x.id] {
                    Text(hecho).font(.caption)
                } else {
                    HStack {
                        Button("Mía") { Task { await marcar(x, "sergio") } }
                        Button("De otro") { Task { await marcar(x, "otro") } }
                    }.buttonStyle(.bordered)
                }
            }
            .padding(.leading, 8)
        }
        Button("Copiar para Claude") {
            UIPasteboard.general.string = m.muestra.map { "\($0.equipo): \($0.ruta)\n  → \($0.crudo ?? "(sin respuesta guardada)")" }.joined(separator: "\n")
        }
    }

    private func mover(_ que: String) async {
        guard let api = session.api else { return }
        do {
            let _: Ignorado = try await api.post("/api/lotes/\(lote.id)/\(que)")
            nota = "Lote #\(lote.id): \(TrabajoView.hecho[que] ?? que)."
        } catch { nota = detalle(error, "No pude cambiar el lote.") }
        await recargar()
    }

    private func reintentar() async {
        guard let api = session.api else { return }
        do {
            let r: Reintento = try await api.post("/api/lotes/\(lote.id)/retry")
            nota = "Lote #\(r.lote ?? 0) (\(Textos.n(r.documentos ?? 0)) documentos) creado: apruébalo en Pedidos para que corra."
        } catch { nota = detalle(error, "No pude crear el reintento.") }
        await recargar()
    }

    private func verDudas() async {
        guard let api = session.api else { return }
        do { muestra = try await api.get("/api/lotes/\(lote.id)/muestra", query: [URLQueryItem(name: "tipo", value: ""), URLQueryItem(name: "n", value: "30")]) }
        catch { nota = "No pude traer la muestra." }
    }

    private func marcar(_ x: Muestra.Fila, _ autor: String) async {
        guard let api = session.api else { return }
        do {
            let _: Ignorado = try await api.put("/api/lotes/autor", body: ["equipo": x.equipo, "ruta": x.ruta, "autor": autor])
            marcadas[x.id] = autor == "sergio" ? "Marcado como tuyo." : "Marcado como de otro."
        } catch { marcadas[x.id] = detalle(error, "No pude guardarlo.") }
    }

    private func reclasificarDudas() async {
        guard let api = session.api else { return }
        do {
            let r: Reintento = try await api.post("/api/lotes/\(lote.id)/dudas")
            reclasificar = "Lote #\(r.lote ?? 0) (\(Textos.n(r.documentos ?? 0)) documentos): apruébalo en Pedidos"
            await recargar()
        } catch { reclasificar = detalle(error, "No pude crear el lote.") }
    }
}
