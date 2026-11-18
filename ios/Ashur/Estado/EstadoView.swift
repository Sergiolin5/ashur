import SwiftUI

/// Pestaña Estado, nativa (docs/ios/NATIVO.md, etapa 1). Mismo contenido y orden que el panel web:
/// cifras, en curso, cola, equipos, inventario y actividad. Faltan los discos y «Configurar» de cada equipo (parcial).
struct EstadoView: View {
    let model: EstadoModel

    var body: some View {
        List {
            if let problem = model.problem {
                Text(problem).font(.footnote).foregroundStyle(.red)
            }
            if let d = model.data {
                cifras(d)
                enCurso(d)
                cola(d)
                equipos(d)
                inventario(d)
                actividad(d)
                Section {
                    Text(subtitulo(d)).font(.caption).foregroundStyle(.secondary)
                }
            } else if model.problem == nil {
                ProgressView("Cargando…")
            }
        }
        .listStyle(.insetGrouped)
        .refreshable { await model.load() }
        .task {                                          // cada 5 s mientras se ve, como el panel
            while !Task.isCancelled {
                await model.load()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    private func subtitulo(_ d: Overview) -> String {
        var s = "Núcleo v\(d.version) · " + (d.status.online == 1 ? "1 dispositivo conectado" : "\(d.status.online) dispositivos conectados")
        if let ms = model.ms {
            s += " · " + (ms >= 2000 ? "lento: " : "") + (ms < 1000 ? "\(Int(ms)) ms" : String(format: "%.1f s", ms / 1000).replacingOccurrences(of: ".", with: ","))
        }
        return s
    }

    // MARK: cifras (kpi-run, kpi-queue, kpi-online, kpi-inv)
    private func cifras(_ d: Overview) -> some View {
        let enMarcha = max(d.running.count, d.lote?.status == "running" ? 1 : 0)
        let inv: (String, String) = {
            guard let i = d.inventory else { return ("…", "calculando") }
            let e = i.reduce(0) { $0 + $1.entries }, b = i.reduce(0) { $0 + $1.bytes }
            return (Textos.n(e), i.isEmpty ? "sin repaso" : "elementos · " + Textos.bytes(b))
        }()
        return Section {
            Grid(horizontalSpacing: 12, verticalSpacing: 4) {
                GridRow {
                    cifra(Textos.n(enMarcha), "en marcha")
                    cifra(Textos.n(d.queued.count), "en cola")
                    cifra("\(d.status.online)/\(d.status.nodes.count)", "conectados")
                    cifra(inv.0, inv.1)
                }
            }
        }
    }

    private func cifra(_ v: String, _ l: String) -> some View {
        VStack(spacing: 2) {
            Text(v).font(.title3.monospacedDigit().weight(.semibold))
            Text(l).font(.caption2).foregroundStyle(.secondary).lineLimit(2).multilineTextAlignment(.center)
        }.frame(maxWidth: .infinity)
    }

    // MARK: en curso
    @ViewBuilder private func enCurso(_ d: Overview) -> some View {
        Section("En curso") {
            if d.lote == nil && d.running.isEmpty {
                Text("Todo tranquilo. Nada en curso.").foregroundStyle(.secondary)
            }
            if let L = d.lote {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Lote #\(L.id) · \(L.label)").font(.subheadline)
                    Text("\(Textos.n(L.done)) de \(Textos.n(L.total)) documentos").font(.headline.monospacedDigit())
                    if L.failed > 0 { Text("\(Textos.n(L.failed)) fallidos").font(.caption).foregroundStyle(.orange) }
                    if let eta = L.eta_s { Text("Faltan unos \(Textos.duracion(eta))").font(.caption).foregroundStyle(.secondary) }
                    ProgressView(value: Double(min(L.done + L.failed + L.skipped, L.total)), total: Double(max(L.total, 1)))
                }
            }
            ForEach(d.running) { t in
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(Textos.modulo[t.module] ?? t.module) en \(t.node_id ?? "?"), \(Textos.duracion(t.elapsed_s))").font(.subheadline)
                    Text(conteo(t)).font(.headline.monospacedDigit())
                    if let f = t.folder { Text(f).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.head) }
                    ProgressView()
                }
            }
        }
    }

    private func conteo(_ t: Running) -> String {
        if t.module == "docs" {
            if t.phase == "read", let found = t.found, found > 0 { return "\(Textos.n(t.read ?? 0)) de \(Textos.n(found)) documentos leídos" }
            if t.phase == "scan" { return "\(Textos.n(t.found ?? 0)) documentos encontrados, buscando más" }
            return "Empezando"
        }
        if let e = t.entries { return "\(Textos.n(e)) " + (e == 1 ? "elemento" : "elementos") }
        return t.module == "llm" ? "Respondiendo" : "Empezando"
    }

    // MARK: cola: subir, bajar, cancelar (dos toques) y pausar
    @ViewBuilder private func cola(_ d: Overview) -> some View {
        let byId = Dictionary(uniqueKeysWithValues: d.status.nodes.map { ($0.device_id, $0) })
        let notas: [String] = {
            var n: [String] = []
            if d.queue_paused { n.append("Cola en pausa: los equipos no reciben tareas nuevas (las respuestas del chat sí siguen). Lo que ya corre termina solo.") }
            if d.queued.contains(where: { $0.weight == "heavy" && $0.target.flatMap { byId[$0] }.map { $0.mode != "full" } == true }) {
                n.append("Las pesadas empiezan cuando el equipo esté libre: 10 minutos sin uso y, si es portátil, conectado a la corriente.")
            }
            if let x = model.note { n.append(x) }
            return n
        }()
        if !d.queued.isEmpty || d.queue_paused || !notas.isEmpty {
            Section("En cola") {
                ForEach(notas, id: \.self) { Text($0).font(.footnote).foregroundStyle(.secondary) }
                ForEach(Array(d.queued.enumerated()), id: \.element.id) { i, q in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(Textos.moduloCola[q.module] ?? q.module)
                            Text((q.target.map { "en " + $0 } ?? "en cualquier equipo libre") + " · \(i + 1)º en la fila")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if i > 0 { Button("Subir", systemImage: "arrow.up") { Task { await model.move(q.id, "up") } }.labelStyle(.iconOnly) }
                        if i < d.queued.count - 1 { Button("Bajar", systemImage: "arrow.down") { Task { await model.move(q.id, "down") } }.labelStyle(.iconOnly) }
                        Button(model.armed.contains(q.id) ? "Toca otra vez" : "Cancelar", role: .destructive) {
                            Task { await model.cancel(q.id) }
                        }
                    }
                    .buttonStyle(.borderless)
                }
                if !d.queued.isEmpty || d.queue_paused {
                    Button(d.queue_paused ? "Reanudar la cola" : "Pausar la cola") {
                        Task { await model.setPaused(!d.queue_paused) }
                    }
                }
            }
        }
    }

    // MARK: equipos
    private func equipos(_ d: Overview) -> some View {
        let nodos = d.status.nodes.sorted { a, b in
            (a.mode == "offline" ? 1 : 0, a.device_id) < (b.mode == "offline" ? 1 : 0, b.device_id)
        }
        let inv = Dictionary(uniqueKeysWithValues: (d.inventory ?? []).map { ($0.node_id, $0) })
        return Section("Equipos") {
            if nodos.isEmpty { Text("Aún no hay dispositivos. Se irán sumando cuando los conectes.").foregroundStyle(.secondary) }
            ForEach(nodos) { n in
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(n.device_id).font(.headline)
                        Spacer()
                        Text(Textos.modo[n.mode] ?? n.mode).font(.caption.weight(.medium))
                            .padding(.horizontal, 8).padding(.vertical, 2)
                            .background(n.mode == "offline" ? Color.secondary.opacity(0.15) : Color.accentColor.opacity(0.18), in: Capsule())
                    }
                    Text(sub(n, d)).font(.caption).foregroundStyle(.secondary)
                    HStack(spacing: 10) {
                        medidor("CPU", n.cpu_pct, n)
                        medidor("RAM", n.ram_pct, n)
                        medidor("GPU", n.gpu_pct, n)
                        medidor(n.battery_pct == nil ? "Bat." : n.on_power == true ? "Carga" : "Bat.", n.battery_pct, n, bajo: n.on_power != true)
                    }
                    HStack {
                        if d.inventory == nil { Text("calculando…") }
                        else if let it = inv[n.device_id] {
                            Text("\(Textos.n(it.entries)) · \(Textos.bytes(it.bytes)) · \(Textos.n(it.runnable ?? 0)) " + ((it.runnable ?? 0) == 1 ? "ejecutable" : "ejecutables"))
                        } else { Text("sin repaso") }
                        Spacer()
                        Text(Textos.hace(n.last_seen_s_ago))
                    }.font(.caption2).foregroundStyle(.secondary)
                }
                .opacity(n.mode == "offline" ? 0.6 : 1)
            }
        }
    }

    private func sub(_ n: Nodo, _ d: Overview) -> String {
        var s = Textos.os[n.os ?? ""] ?? (n.os ?? "")
        if let want = d.node_code_version, let have = n.code_version {
            s += have == want ? " · v\(n.code_label ?? d.node_code_label ?? "?")"
                : " · " + (n.code_label.map { "v" + $0 } ?? "versión anterior") + " → v\(d.node_code_label ?? "?") pendiente"
        }
        if let r = n.reason, n.mode != "full" { s += " · " + r }
        return s
    }

    private func medidor(_ l: String, _ v: Double?, _ n: Nodo, bajo: Bool = false) -> some View {
        let na = v == nil || n.mode == "offline"
        let x = min(100, max(0, v ?? 0))
        let caliente = !na && (bajo ? x <= 20 : x >= 90)
        return VStack(alignment: .leading, spacing: 2) {
            Text(l).font(.caption2).foregroundStyle(.secondary)
            Text(na ? "—" : "\(Int(x.rounded()))%").font(.caption.monospacedDigit()).foregroundStyle(caliente ? .red : .primary)
            ProgressView(value: na ? 0 : x, total: 100).tint(caliente ? .red : .accentColor)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: inventario y actividad
    private func inventario(_ d: Overview) -> some View {
        Section("Inventario") {
            if let inv = d.inventory {
                if inv.isEmpty { Text("Todavía no hay inventario. Aparecerá cuando un equipo termine su primer repaso.").foregroundStyle(.secondary) }
                ForEach(inv) { i in
                    LabeledContent {
                        Text(Textos.bytes(i.bytes))
                    } label: {
                        Text(i.node_id)
                        Text("\(Textos.n(i.entries)) " + (i.entries == 1 ? "elemento, " : "elementos, ")
                             + "\(Textos.n(i.runnable ?? 0)) " + ((i.runnable ?? 0) == 1 ? "ejecutable" : "ejecutables"))
                    }
                }
            } else {
                Text("Calculando el inventario…").foregroundStyle(.secondary)
            }
        }
    }

    private func actividad(_ d: Overview) -> some View {
        Section("Actividad") {
            if d.activity.isEmpty { Text("Todo en calma por ahora.").foregroundStyle(.secondary) }
            ForEach(d.activity.prefix(5)) { ev in
                VStack(alignment: .leading) {
                    Text(Textos.describe(ev))
                    Text((ev.node_id ?? "Núcleo") + " · " + Date(timeIntervalSince1970: ev.ts).formatted(date: .omitted, time: .shortened))
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }
}
