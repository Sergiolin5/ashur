import SwiftUI

/// Ajustes › Diagnóstico (data-page="diag"), nativo: prueba completa, equipos (los que tienen problemas primero) y
/// fallas y conexiones de las últimas 24 h. Etapa 2 de docs/ios/NATIVO.md.
struct DiagnosticoView: View {
    @Environment(Session.self) private var session
    @State private var d: Diagnostico?
    @State private var problema: String?
    @State private var alcance = ""
    @State private var probando = false
    @State private var stNota = "Revisa núcleo, equipos, tareas, memoria y le hace una pregunta real a cada modelo local. Tarda hasta un minuto y medio."
    @State private var informe: String?
    @State private var verAntiguos = false

    var body: some View {
        List {
            if let problema { Nota(texto: problema) }
            Section("Prueba completa") {
                VStack(alignment: .leading) {
                    Text("Probar todo y sacar un informe")
                    Text(stNota).font(.caption).foregroundStyle(.secondary)
                }
                Picker("Qué probar", selection: $alcance) {
                    Text("Todo").tag("")
                    Text("Solo el núcleo").tag("nucleo")
                    ForEach(d?.nodes ?? []) { n in Text("Solo " + n.device_id).tag(n.device_id) }
                }
                Button(probando ? "Probando…" : "Probar") { Task { await probar() } }.disabled(probando)
                if let informe {
                    Button("Copiar informe") { UIPasteboard.general.string = informe }
                    Text(informe).font(.caption.monospaced()).textSelection(.enabled)
                }
            }
            if let d {
                Section {
                    ForEach(d.nodes.sorted { problemas($0) > problemas($1) }) { n in equipo(n, d) }
                } header: { Text("Equipos") } footer: { Text("Código de nodos v\(d.core_code_label ?? "?").") }
                let recientes = { (p: Diagnostico.Problema) in d.now - p.ts < 86400 }
                let fallas = d.problems.filter { recientes($0) && $0.kind != "conexion" }
                let conexiones = d.problems.filter { recientes($0) && $0.kind == "conexion" }
                let antiguos = d.problems.filter { !recientes($0) }
                Section("Fallas (últimas 24 h)") {
                    if fallas.isEmpty { Text("Sin fallas en las últimas 24 h.") }
                    ForEach(fallas) { fila($0) }
                }
                Section("Conexiones y mantenimiento (últimas 24 h)") {
                    if conexiones.isEmpty { Text("Sin desconexiones en las últimas 24 h.") }
                    ForEach(conexiones) { fila($0) }
                    if !antiguos.isEmpty {
                        Button((verAntiguos ? "Ocultar " : "Ver ") + "\(antiguos.count) más antiguos") { verAntiguos.toggle() }
                        if verAntiguos { ForEach(antiguos) { fila($0) } }
                    }
                }
            }
        }
        .navigationTitle(d.map { "Diagnóstico · v\($0.version)" } ?? "Diagnóstico")
        .refreshable { await cargar() }
        .task { await cargar() }
    }

    private func problemas(_ n: Diagnostico.Nodo) -> Int { n.mode == "offline" ? 2 : (n.behind == true || n.hint != nil) ? 1 : 0 }

    private func gbs(_ b: Double?) -> String {
        let v = b ?? 0
        return v >= 1e12 ? String(format: "%.1f", v / 1e12).replacingOccurrences(of: ".0", with: "") + " TB" : "\(Int((v / 1e9).rounded())) GB"
    }

    private func equipo(_ n: Diagnostico.Nodo, _ d: Diagnostico) -> some View {
        let sinEscanear = (n.disks ?? []).filter { $0.present == true && $0.scanned != true }.count
        let corto = n.mode == "offline" ? (n.reason == "dormido" ? "Dormido" : n.reason != nil ? "Sin batería" : "Sin señal")
            : n.behind == true ? "Atrasado · código v\(n.code_label ?? "?")" : "Al día"
        return DisclosureGroup {
            LabeledContent("Código", value: "v\(n.code_label ?? "?")" + (n.behind == true ? " (el núcleo reparte v\(d.core_code_label ?? "?"))" : ""))
            LabeledContent("Módulos", value: (n.modules ?? []).joined(separator: ", ").isEmpty ? "ninguno" : (n.modules ?? []).joined(separator: ", "))
            VStack(alignment: .leading) {
                Text("Discos").font(.caption).foregroundStyle(.secondary)
                if let ds = n.disks {
                    if ds.isEmpty { Text("ninguno visible") }
                    ForEach(ds, id: \.mount) { x in
                        Text(x.mount + " · " + (x.present == true ? "\(gbs(x.used)) de \(gbs(x.total))" + (x.scanned == true ? "" : " · sin escanear") : "\(gbs(x.total)) · ausente"))
                    }
                } else { Text("sin informar") }
            }
            LabeledContent("GPU", value: n.gpus.map { $0.isEmpty ? "ninguna visible" : $0.joined(separator: "; ") } ?? "sin informar")
            if let m = n.llm { LabeledContent("Modelo", value: m) }
            if let h = n.hint { Text(h).font(.caption).foregroundStyle(.orange) }
        } label: {
            HStack {
                Circle().fill(n.mode == "offline" ? Color.secondary : n.behind == true ? .orange : .green).frame(width: 8, height: 8)
                VStack(alignment: .leading) {
                    Text(n.device_id)
                    Text(corto + (sinEscanear > 0 ? " · \(sinEscanear) " + (sinEscanear == 1 ? "disco sin escanear" : "discos sin escanear") : ""))
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Text(Textos.hace(n.last_seen_s_ago)).font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private func fila(_ p: Diagnostico.Problema) -> some View {
        VStack(alignment: .leading) {
            Text(p.text)
            Text((p.node_id ?? "Núcleo") + " · " + ((p.count ?? 1) > 1 ? "\(p.count ?? 1) veces, la última " : "")
                 + Date(timeIntervalSince1970: p.ts).formatted(date: .omitted, time: .shortened))
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func cargar() async {
        guard let api = session.api else { return }
        do { d = try await api.get("/api/diagnostics"); problema = nil } catch { problema = "No pude cargar el diagnóstico." }
    }

    /// Lanza la prueba y pregunta cada 2 s, hasta 10 min, como el panel.
    private func probar() async {
        guard let api = session.api else { return }
        probando = true; informe = nil
        stNota = "Probando… (los modelos locales pueden tardar)"
        defer { probando = false }
        do {
            let ruta = "/api/selftest"
            let _: Ignorado = try await api.post(ruta, query: alcance.isEmpty ? [] : [URLQueryItem(name: "only", value: alcance)])
            var ultimo = ""
            let t0 = Date()
            while Date().timeIntervalSince(t0) < 600 {
                try? await Task.sleep(for: .seconds(2))
                guard let j: SelfTest = try? await api.get(ruta) else { stNota = "Probando… (el núcleo tarda en contestar, sigo esperando)"; continue }
                if j.running != true, let r = j.report { informe = r; break }
                if let p = j.progress { ultimo = p; stNota = "Probando… " + p }
            }
            if let r = informe {
                stNota = (r.split(separator: "\n").first.map(String.init) ?? "") + ". Copiado: pégamelo en el chat."
                UIPasteboard.general.string = r
            } else {
                stNota = "La prueba no terminó en 10 minutos." + (ultimo.isEmpty ? "" : " Se quedó en: " + ultimo)
            }
        } catch {
            stNota = "No pude lanzar la prueba (\(detalle(error, "sin respuesta")))."
        }
    }
}
