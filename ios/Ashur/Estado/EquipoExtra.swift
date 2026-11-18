import SwiftUI

/// Estado completo (etapa 7 de docs/ios/NATIVO.md): bajo cada equipo, sus discos (Incluido / Fuera, renombrar) y
/// «Configurar» (modelo de chat, módulos, política y límites, que el equipo aplica en su próximo latido).
struct EquipoExtra: View {
    @Environment(Session.self) private var session
    let nodo: String
    let discos: [Disco]?
    let nombres: [String: String]
    let recargar: () async -> Void

    struct Disco: Decodable, Identifiable {
        let key: String; let mount: String; let total: Double?; let used: Double?; let present: Bool?; let kind: String?; let scan: Bool?
        let connected_s: Double?; let sessions: Int?
        var id: String { key }
    }
    struct Config: Decodable {
        let values: [String: JSONCodable]; let aplicada: Bool; let politicas: [String]; let limites: [String: [Double]]; let modulos: [String]
    }
    private static let limES = ["idle_for_full_s": "Sin uso para modo completo (s)", "other_load_off_pct": "Tu carga máxima (%)", "battery_off_pct": "Batería mínima (%)"]

    @State private var config: Config?
    @State private var modelo = ""
    @State private var cambiarMods = false
    @State private var mods: Set<String> = []
    @State private var politica = ""
    @State private var limites: [String: String] = [:]
    @State private var estado = ""
    @State private var renombrar: (clave: String, actual: String)?
    @State private var nuevoNombre = ""

    private func gbs(_ b: Double?) -> String {
        let v = b ?? 0
        return v >= 1e12 ? String(format: "%.1f", v / 1e12).replacingOccurrences(of: ".0", with: "") + " TB" : "\(Int((v / 1e9).rounded())) GB"
    }

    var body: some View {
        Group {
            if let ds = discos {
                DisclosureGroup("Discos · \(ds.count)") {
                    Button("Renombrar el equipo") { renombrar = ("node:" + nodo, nombres["node:" + nodo] ?? ""); nuevoNombre = renombrar?.actual ?? "" }.font(.caption)
                    ForEach(ds) { x in
                        HStack {
                            if x.present == true { Circle().fill(x.scan == true ? Color.green : .secondary).frame(width: 7, height: 7) }
                            VStack(alignment: .leading) {
                                Button(nombres["disk:\(nodo):\(x.key)"] ?? EspacioView.nombreDisco(x.mount)) {
                                    renombrar = ("disk:\(nodo):\(x.key)", nombres["disk:\(nodo):\(x.key)"] ?? ""); nuevoNombre = renombrar?.actual ?? ""
                                }.buttonStyle(.plain)
                                Text(detalleDisco(x)).font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button(x.scan == true ? "Incluido" : "Fuera") { Task { await escanear(x) } }
                                .buttonStyle(.bordered).tint(x.scan == true ? .accentColor : .secondary).font(.caption)
                        }
                    }
                }
            }
            DisclosureGroup("Configurar") { configurar() }
                .onAppear { if config == nil { Task { await cargarConfig() } } }
        }
        .font(.subheadline)
        .alert("Nombre nuevo (déjalo vacío para volver al original):", isPresented: Binding(get: { renombrar != nil }, set: { if !$0 { renombrar = nil } })) {
            TextField("Nombre", text: $nuevoNombre)
            Button("Guardar") { if let r = renombrar { Task { let _: Ignorado? = try? await session.api?.post("/api/names", body: ["key": r.clave, "name": nuevoNombre]); await recargar() } } }
            Button("Cancelar", role: .cancel) {}
        }
    }

    /// diskDetail() del panel.
    private func detalleDisco(_ x: Disco) -> String {
        var b = [x.present == true ? "\(gbs(x.used)) usados de \(gbs(x.total))" : "ausente ahora · \(gbs(x.total))"]
        if let k = x.kind, k != "nuevo" { b.append(k) }
        if let c = x.connected_s, c >= 360, x.kind != "fijo" {
            b.append("\((c / 360).rounded() / 10) h conectado en \(x.sessions ?? 1) " + ((x.sessions ?? 1) == 1 ? "vez" : "veces"))
        }
        return b.joined(separator: " · ")
    }

    @ViewBuilder private func configurar() -> some View {
        if let c = config {
            TextField("Modelo de chat (automático: el mejor que tenga instalado)", text: $modelo).textInputAutocapitalization(.never)
            Toggle("Cambiar módulos", isOn: $cambiarMods)
            if cambiarMods {
                ForEach(c.modulos, id: \.self) { m in
                    Toggle(m, isOn: Binding(get: { mods.contains(m) }, set: { if $0 { mods.insert(m) } else { mods.remove(m) } })).font(.caption)
                }
            }
            Picker("Política", selection: $politica) {
                Text("la de su node.yaml").tag("")
                ForEach(c.politicas, id: \.self) { Text($0).tag($0) }
            }
            ForEach(c.limites.keys.sorted(), id: \.self) { k in
                let r = c.limites[k] ?? [0, 0]
                HStack {
                    Text(Self.limES[k] ?? k).font(.caption)
                    TextField("\(Int(r.first ?? 0))–\(Int(r.last ?? 0))", text: Binding(get: { limites[k] ?? "" }, set: { limites[k] = $0 })).keyboardType(.numberPad).multilineTextAlignment(.trailing)
                }
            }
            HStack {
                Button("Guardar") { Task { await guardar(vacio: false) } }
                Button("Volver a su node.yaml") { Task { await guardar(vacio: true) } }
            }.buttonStyle(.bordered)
            Text(estado.isEmpty ? (c.aplicada ? "Aplicada en el equipo." : "Pendiente: el equipo la aplica en su próximo latido.") : estado).font(.caption).foregroundStyle(.secondary)
        } else { Text("Cargando…").font(.caption) }
    }

    private func escanear(_ x: Disco) async {
        let body: [String: JSONCodable] = ["node_id": .s(nodo), "key": .s(x.key), "scan": .b(!(x.scan ?? false))]
        let _: Ignorado? = try? await session.api?.post("/api/disks/scan", body: body)
        await recargar()
    }

    private func ruta() -> String { "/api/nodes/\(nodo.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? nodo)/config" }

    private func cargarConfig() async {
        guard let api = session.api else { return }
        do {
            let c: Config = try await api.get(ruta())
            config = c
            if case .s(let m) = c.values["llm_model"] { modelo = m }
            if case .a(let l) = c.values["modules"] { cambiarMods = true; mods = Set(l.compactMap { if case .s(let s) = $0 { return s }; return nil }) }
            if case .s(let p) = c.values["yield_policy"] { politica = p }
            if case .o(let t) = c.values["thresholds"] { for (k, v) in t { if case .n(let n) = v { limites[k] = String(Int(n)) } } }
        } catch { estado = "No pude leer la configuración." }
    }

    private func guardar(vacio: Bool) async {
        guard let api = session.api else { return }
        var v: [String: JSONCodable] = [:]
        if !vacio {
            let m = modelo.trimmingCharacters(in: .whitespaces)
            if !m.isEmpty { v["llm_model"] = .s(m) }
            if cambiarMods { v["modules"] = .a(mods.sorted().map { .s($0) }) }
            if !politica.isEmpty { v["yield_policy"] = .s(politica) }
            var t: [String: JSONCodable] = [:]
            for (k, s) in limites { if let n = Double(s) { t[k] = .n(n) } }
            if !t.isEmpty { v["thresholds"] = .o(t) }
        }
        do { let _: Ignorado = try await api.put(ruta(), body: ["values": JSONCodable.o(v)]); estado = "Guardada. El equipo la aplica en su próximo latido (15 s)." }
        catch { estado = detalle(error, "No se pudo guardar.") }
        if vacio { modelo = ""; cambiarMods = false; mods = []; politica = ""; limites = [:] }
    }
}
