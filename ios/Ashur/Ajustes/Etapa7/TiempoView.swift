import SwiftUI

/// Ajustes › Mi tiempo (data-page="time"), nativo: lo que está en marcha, empezar, últimos 7 días, estimado contra real
/// y el registro.
struct TiempoView: View {
    @Environment(Session.self) private var session

    struct Tiempo: Decodable {
        let now: Double; let running: Corriendo?; let categories: [String]; let week: [Semana]; let week_minutes: Double?
        let estimates: [Estimado]; let recent: [Registro]
        struct Corriendo: Decodable { let task: String; let category: String; let est_min: Double?; let start_ts: Double }
        struct Semana: Decodable, Identifiable { let category: String; let minutes: Double; let before: Double?; var id: String { category } }
        struct Estimado: Decodable, Identifiable { let task: String; let est_min: Double; let minutes: Double; var id: String { task + "\(est_min)" } }
        struct Registro: Decodable, Identifiable { let id: Int; let task: String; let category: String; let start_ts: Double; let minutes: Double; let running: Bool? }
    }
    private struct Empiezo: Encodable { let task: String; let category: String; let est_min: Int? }

    @State private var t: Tiempo?
    @State private var tarea = ""
    @State private var categoria = ""
    @State private var estimado = ""
    @State private var nota: String?
    @State private var copiado = false

    static func mins(_ m: Double) -> String { m >= 60 ? "\(Int(m) / 60) h " + String(format: "%02d", Int(m.truncatingRemainder(dividingBy: 60).rounded())) + " min" : "\(Int(m.rounded())) min" }

    var body: some View {
        List {
            Section {
                if let r = t?.running {
                    TimelineView(.periodic(from: .now, by: 1)) { ctx in
                        let skew = Date().timeIntervalSince1970 - (t?.now ?? Date().timeIntervalSince1970)
                        let s = max(0, Int(ctx.date.timeIntervalSince1970 - r.start_ts - skew))
                        LabeledContent {
                            Text((s >= 3600 ? "\(s / 3600):" + String(format: "%02d", s % 3600 / 60) : "\(s % 3600 / 60)") + ":" + String(format: "%02d", s % 60)).monospacedDigit()
                        } label: {
                            Text(r.task); Text(r.category + (r.est_min.map { " · estimado " + Self.mins($0) } ?? ""))
                        }
                    }
                    Button("Termino") { Task { t = try? await session.api?.post("/api/time/stop") } }
                } else { Text("Nada en marcha") }
                TextField("¿Qué vas a hacer?", text: $tarea)
                TextField("Categoría", text: $categoria)
                if !(t?.categories ?? []).isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack { ForEach(t?.categories ?? [], id: \.self) { c in Button(c) { categoria = c }.buttonStyle(.bordered).font(.caption) } }
                    }
                }
                TextField("Estimado en minutos (opcional)", text: $estimado).keyboardType(.numberPad)
                Button("Empiezo") { Task { await empezar() } }
                if let nota { Nota(texto: nota) }
            }
            Section {
                let tope = max(t?.week.first?.minutes ?? 1, 1)
                if (t?.week ?? []).isEmpty { Text("Aún no hay tiempo registrado.") }
                ForEach(t?.week ?? []) { c in
                    VStack(alignment: .leading) {
                        LabeledContent(c.category, value: Self.mins(c.minutes))
                        Text(c.before.map { (c.minutes >= $0 ? "▲ " : "▼ ") + Self.mins(abs(c.minutes - $0)) + " vs. semana anterior" } ?? "sin semana anterior")
                            .font(.caption).foregroundStyle(.secondary)
                        ProgressView(value: max(0.02, c.minutes / tope))
                    }
                }
            } header: { Text("Últimos 7 días") } footer: {
                if let w = t?.week_minutes, !(t?.week ?? []).isEmpty { Text("Total: \(Self.mins(w)). Pasar de una tarea a otra detiene la anterior.") }
            }
            Section("Estimado contra real") {
                if (t?.estimates ?? []).isEmpty { Text("Pon un estimado al empezar y aquí verás cuánto te desvías.") }
                ForEach(t?.estimates ?? []) { e in
                    LabeledContent { Text(Self.mins(e.minutes) + (e.minutes > e.est_min * 1.2 ? " ▲" : "")) } label: { Text(e.task); Text("estimado " + Self.mins(e.est_min)) }
                }
            }
            Section {
                let l = (t?.recent ?? []).filter { $0.running != true }
                if l.isEmpty { Text("Sin registros todavía.") }
                ForEach(l.prefix(8)) { e in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(e.task)
                            Text(e.category + " · " + Date(timeIntervalSince1970: e.start_ts).formatted(.dateTime.weekday(.abbreviated).hour().minute())).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text(Self.mins(e.minutes)).font(.caption)
                        DosToques(titulo: "Borrar", armado: "¿Seguro?", rol: .destructive) { t = try? await session.api?.delete("/api/time/\(e.id)") }
                            .buttonStyle(.borderless).font(.caption)
                    }
                }
                Button(copiado ? "Copiado" : "Copiar todos los datos (JSON)") { Task { await exportar() } }
            } header: { Text("Registro") } footer: {
                Text("Los datos quedan guardados tal cual (tarea, categoría, inicio, fin, estimado) para que Ashur los analice después.")
            }
        }
        .refreshable { t = try? await session.api?.get("/api/time") }
        .task { t = try? await session.api?.get("/api/time") }
    }

    private func empezar() async {
        guard let api = session.api else { return }
        let task = tarea.trimmingCharacters(in: .whitespaces)
        guard !task.isEmpty else { return }
        let cat = categoria.trimmingCharacters(in: .whitespaces)
        let est = Int(estimado).flatMap { $0 > 0 ? $0 : nil }
        do { t = try await api.post("/api/time/start", body: Empiezo(task: task, category: cat.isEmpty ? "Otro" : cat, est_min: est)); nota = nil; tarea = ""; estimado = "" }
        catch { nota = "No pude empezar. Revisa la tarea y el estimado." }
    }

    private func exportar() async {
        guard let api = session.api, let d = try? await api.data("/api/time/export") else { return }
        UIPasteboard.general.string = String(decoding: d, as: UTF8.self); copiado = true
    }
}
