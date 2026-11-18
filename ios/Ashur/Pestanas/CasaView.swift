import SwiftUI
import Charts

/// Pestaña Casa, nativa (etapa 6 de docs/ios/NATIVO.md): casas, dispositivos por área con control, organizar
/// (favoritos, orden, ocultos) y Energía. Las cámaras siguen en el panel web hasta decidir el paquete WebRTC.
struct CasaView: View {
    @Environment(Session.self) private var session
    let web: PanelWeb

    struct Casa: Decodable {
        let casa: String?; let casas: [Una]; let ha: String; let ha_causa: String?; let visto: Double?
        let favoritos: [Disp]; let areas: [Area]; let ocultos: [Disp]; let energia: Energia?
        struct Una: Decodable, Identifiable { let id: String; let nombre: String }
        struct Area: Decodable, Identifiable { let nombre: String; let dispositivos: [Disp]; var id: String { nombre } }
        struct Energia: Decodable { let ahora_w: Double?; let hoy_kwh: Double?; let estadisticas: String? }
    }
    struct Disp: Decodable, Identifiable {
        let entity_id: String; let nombre: String; let dominio: String; let estado: String?; let potencia_w: Double?
        let controlable: Bool?; let favorito: Bool?; let orden: Int?
        var id: String { entity_id }
    }
    private struct Error409: Decodable { let detail: String?; let texto: String? }
    private struct Accion: Encodable { let entity_id: String; let accion: String; var confirm: Bool? = nil }

    static let haTexto = ["sin_respuesta": "Home Assistant no responde", "token_invalido": "El token de Home Assistant no es válido",
                          "sin_configurar": "Home Assistant no está configurado", "error": "Home Assistant devolvió un error"]
    static let es = ["on": "encendida", "off": "apagada", "locked": "bloqueada", "unlocked": "desbloqueada", "open": "abierta", "closed": "cerrada",
                     "opening": "abriendo", "closing": "cerrando", "unavailable": "no disponible", "unknown": "sin dato"]
    static let statCorto = ["pendiente": "pendiente", "sin_estadisticas": "sin estadísticas", "error": "con error"]

    @State private var datos: Casa?
    @State private var casaId: String?
    @State private var organizar = false
    @State private var errores: [String: String] = [:]
    @State private var confirmar: (texto: String, seguir: () async -> Void)?
    @State private var verCamaras = false

    static func w(_ w: Double) -> String {
        w >= 1000 ? String(format: "%.2f kW", w / 1000).replacingOccurrences(of: ".", with: ",") : String(format: "%.1f W", w).replacingOccurrences(of: ".", with: ",").replacingOccurrences(of: ",0 W", with: " W")
    }
    static func kwh(_ v: Double) -> String { String(format: v >= 10 ? "%.1f kWh" : "%.2f kWh", v).replacingOccurrences(of: ".", with: ",") }

    /// casaEstado() del panel.
    static func estado(_ d: Disp) -> String {
        if let p = d.potencia_w { return w(p) }
        if d.dominio == "scene" { return "" }
        if d.dominio == "alarm_control_panel" {
            if d.estado == "disarmed" { return "desarmada" }
            if d.estado == "triggered" { return "disparada" }
            if (d.estado ?? "").hasPrefix("armed_") { return "armada" }
        }
        if d.dominio == "switch" { if d.estado == "on" { return "encendido" }; if d.estado == "off" { return "apagado" } }
        return es[d.estado ?? ""] ?? (d.estado ?? "")
    }
    static func accion(_ d: Disp) -> (String, String)? {
        if d.dominio == "lock" { return d.estado == "locked" ? ("desbloquear", "Desbloquear") : ("bloquear", "Bloquear") }
        if d.dominio == "cover" { return d.estado == "closed" ? ("abrir", "Abrir") : ("cerrar", "Cerrar") }
        return d.dominio == "scene" ? ("activar", "Activar") : nil
    }

    var body: some View {
        List {
            if let d = datos {
                if d.casas.count > 1 {
                    Picker("Casa", selection: Binding(get: { casaId ?? d.casa ?? "" }, set: { casaId = $0; Task { await cargar() } })) {
                        ForEach(d.casas) { Text($0.nombre).tag($0.id) }
                    }.pickerStyle(.segmented)
                }
                if d.ha != "ok" {
                    Text(haLinea(d)).font(.footnote).foregroundStyle(.orange)
                }
                if let e = d.energia, d.casa != nil {
                    NavigationLink { EnergiaView(casa: d.casa ?? "") } label: {
                        HStack {
                            tarjeta("Ahora", e.ahora_w.map { Self.w($0) } ?? "–", aviso: false)
                            tarjeta("Hoy", e.estadisticas != "ok" ? (Self.statCorto[e.estadisticas ?? "error"] ?? "con error") : (e.hoy_kwh.map { Self.kwh($0) } ?? "–"), aviso: e.estadisticas != "ok")
                        }
                    }
                }
                Section("Cámaras") {
                    Button("Ver cámaras (panel web)") { verCamaras = true }
                }
                if !d.favoritos.isEmpty { Section("Favoritos") { lista(d.favoritos, oculto: false) } }
                ForEach(d.areas) { a in Section(a.nombre) { lista(a.dispositivos, oculto: false) } }
                if organizar && !d.ocultos.isEmpty { Section("Ocultos (\(d.ocultos.count))") { lista(d.ocultos, oculto: true) } }
                if d.favoritos.isEmpty && d.areas.isEmpty && d.ha == "ok" { Text("Home Assistant no tiene dispositivos que mostrar.") }
                Button(organizar ? "Listo" : "Organizar") { organizar.toggle() }
            } else {
                ProgressView()
            }
        }
        .task {
            while !Task.isCancelled { await cargar(); try? await Task.sleep(for: .seconds(5)) }       // cada 5 s, como el panel
        }
        .sheet(isPresented: $verCamaras) {
            NavigationStack {
                WebAqui(web: web, ruta: "t=casa")
                    .navigationTitle("Cámaras").navigationBarTitleDisplayMode(.inline)
                    .toolbar { ToolbarItem(placement: .topBarLeading) { Button("Cerrar") { verCamaras = false } } }
            }
        }
        .confirmationDialog(confirmar?.texto ?? "", isPresented: Binding(get: { confirmar != nil }, set: { if !$0 { confirmar = nil } }), titleVisibility: .visible) {
            Button("Sí") { if let c = confirmar { Task { await c.seguir() } }; confirmar = nil }
            Button("No", role: .cancel) { confirmar = nil }
        }
    }

    private func haLinea(_ d: Casa) -> String {
        var t = Self.haTexto[d.ha] ?? Self.haTexto["error"]!
        if let c = d.ha_causa { t += " · " + c }
        if let v = d.visto { let m = max(0, Int((Date().timeIntervalSince1970 - v) / 60)); t += " · visto " + (m < 1 ? "hace menos de 1 min" : "hace \(m) min") }
        return t
    }

    private func tarjeta(_ k: String, _ v: String, aviso: Bool) -> some View {
        VStack(alignment: .leading) {
            Text(k).font(.caption).foregroundStyle(.secondary)
            Text(v).font(.title3.weight(.semibold)).foregroundStyle(aviso ? .orange : .primary)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func lista(_ l: [Disp], oculto: Bool) -> some View {
        ForEach(Array(l.enumerated()), id: \.element.id) { i, d in
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(d.nombre)
                    Spacer()
                    Text(Self.estado(d)).font(.caption).foregroundStyle(.secondary)
                    if !organizar && d.controlable == true {
                        if d.dominio == "light" || d.dominio == "switch" {
                            Toggle(d.nombre, isOn: Binding(get: { d.estado == "on" }, set: { v in Task { await control(d, v ? "on" : "off") } })).labelsHidden()
                        } else if let a = Self.accion(d) {
                            Button(a.1) { Task { await control(d, a.0) } }.buttonStyle(.bordered)
                        }
                    }
                }
                if organizar {
                    HStack {
                        if oculto { Button("Volver a mostrar") { Task { await poner(d, ["oculto": false]) } } }
                        else {
                            Button(d.favorito == true ? "★ Favorito" : "☆ Favorito") { Task { await poner(d, ["favorito": !(d.favorito ?? false)]) } }
                            Button("Subir", systemImage: "arrow.up") { Task { await mover(l, i, -1) } }.labelStyle(.iconOnly)
                            Button("Bajar", systemImage: "arrow.down") { Task { await mover(l, i, 1) } }.labelStyle(.iconOnly)
                            Button("Quitar", role: .destructive) { Task { await poner(d, ["oculto": true]) } }
                        }
                    }.buttonStyle(.borderless).font(.caption)
                }
                if let e = errores[d.entity_id] { Text(e).font(.caption).foregroundStyle(.red) }
            }
        }
    }

    private func cargar() async {
        guard let api = session.api else { return }
        if let d: Casa = try? await api.get("/api/casa", query: casaId.map { [URLQueryItem(name: "casa", value: $0)] } ?? []) { datos = d; casaId = d.casa }
    }

    /// casaSend(): un 409 «confirmar» pregunta en pantalla y reenvía con confirm: true.
    private func enviar<B: Encodable>(_ metodo: String, _ ruta: String, _ body: B, _ eid: String, conConfirm: @escaping () -> B) async {
        guard let api = session.api else { return }
        errores[eid] = nil
        do {
            let (code, data) = try await api.crudo(metodo, ruta, body: body)
            if code == 409, let j = try? JSONDecoder().decode(Error409.self, from: data), j.detail == "confirmar" {
                confirmar = (j.texto ?? "¿Seguro?", {
                    if let (c2, d2) = try? await api.crudo(metodo, ruta, body: conConfirm()), !(200..<300).contains(c2) {
                        errores[eid] = (try? JSONDecoder().decode(Error409.self, from: d2))?.detail ?? "No se pudo (HTTP \(c2))"
                    }
                    await cargar()
                })
                return
            }
            if !(200..<300).contains(code) { errores[eid] = (try? JSONDecoder().decode(Error409.self, from: data))?.detail ?? "No se pudo (HTTP \(code))" }
        } catch { errores[eid] = "Sin conexión con el núcleo" }
        await cargar()
    }

    private func control(_ d: Disp, _ a: String) async {
        guard let c = datos?.casa else { return }
        await enviar("POST", "/api/casa/\(c)/control", Accion(entity_id: d.entity_id, accion: a), d.entity_id) { Accion(entity_id: d.entity_id, accion: a, confirm: true) }
    }

    private func poner(_ d: Disp, _ cambios: [String: Bool]) async {
        guard let c = datos?.casa else { return }
        var b: [String: JSONCodable] = ["entity_id": .s(d.entity_id)]
        for (k, v) in cambios { b[k] = .b(v) }
        let conf = b.merging(["confirm": .b(true)]) { a, _ in a }
        await enviar("PUT", "/api/casa/\(c)/dispositivo", b, d.entity_id) { conf }
    }

    /// casaMove(): renumera la lista de diez en diez.
    private func mover(_ l: [Disp], _ i: Int, _ delta: Int) async {
        guard let api = session.api, let c = datos?.casa else { return }
        var arr = l; let j = i + delta
        guard j >= 0, j < arr.count else { return }
        arr.swapAt(i, j)
        for (k, x) in arr.enumerated() where x.orden != k * 10 {
            let body: [String: JSONCodable] = ["entity_id": .s(x.entity_id), "orden": .n(Double(k * 10))]
            if let (code, _) = try? await api.crudo("PUT", "/api/casa/\(c)/dispositivo", body: body), !(200..<300).contains(code) {
                errores[x.entity_id] = "No se pudo (HTTP \(code))"; break
            }
        }
        await cargar()
    }
}

/// Casa › Energía, nativa: total del periodo con su gráfica, por área (con sus sensores), sin medición, fuera del total,
/// nivel de la casa y medidor de tablero. Falta el nivel por sensor y sus mini gráficas (quedan en el panel web).
struct EnergiaView: View {
    @Environment(Session.self) private var session
    let casa: String

    struct Energia: Decodable {
        let tz: String?; let resolucion: Int; let total: [Punto]?; let no_medido: [Punto]?; let total_kwh: Double?; let no_medido_kwh: Double?
        let areas: [Area]; let sin_medicion: [Nombre]?; let fuera_del_total: [Nombre]?; let tablero: String?; let tablero_nombre: String?; let nivel_casa: Int?
        struct Punto: Decodable { let inicio: Double; let kwh: Double? }
        struct Area: Decodable, Identifiable { let nombre: String; let kwh: Double; let dispositivos: [Disp]; var id: String { nombre } }
        struct Disp: Decodable, Identifiable { let entity_id: String; let nombre: String; let nivel: Int?; let fuente: String?; let kwh: Double?; let w_max: Double?; var id: String { entity_id } }
        struct Nombre: Decodable, Identifiable { let entity_id: String; let nombre: String; var id: String { entity_id } }
    }
    static let periodos = [("hoy", "Hoy"), ("7", "7 días"), ("30", "30 días"), ("12m", "12 meses")]

    @State private var per = "hoy"
    @State private var d: Energia?
    @State private var error: String?
    @State private var abiertas: Set<String> = []

    var body: some View {
        List {
            Picker("Periodo", selection: $per) { ForEach(Self.periodos, id: \.0) { Text($0.1).tag($0.0) } }.pickerStyle(.segmented)
            if let error { Text(error).font(.footnote).foregroundStyle(.orange) }
            if let d {
                let suma = d.total_kwh ?? (d.total ?? []).reduce(0) { $0 + ($1.kwh ?? 0) }
                Section {
                    LabeledContent("Total · " + (Self.periodos.first { $0.0 == per }?.1 ?? ""), value: CasaView.kwh(suma))
                    Chart {
                        ForEach(Array((d.total ?? []).enumerated()), id: \.offset) { _, p in
                            BarMark(x: .value("Cuándo", Date(timeIntervalSince1970: p.inicio), unit: unidad(d.resolucion)), y: .value("kWh", p.kwh ?? 0))
                        }
                        ForEach(Array((d.no_medido ?? []).enumerated()), id: \.offset) { _, p in
                            BarMark(x: .value("Cuándo", Date(timeIntervalSince1970: p.inicio), unit: unidad(d.resolucion)), y: .value("kWh", p.kwh ?? 0))
                                .foregroundStyle(.gray)
                        }
                    }
                    .frame(height: 140)
                    if d.no_medido != nil { Nota(texto: "En gris: no medido (\(CasaView.kwh(d.no_medido_kwh ?? 0))), lo que el tablero lee y los sensores no explican.") }
                }
                Section("Por área") {
                    if d.areas.isEmpty { Text("Todavía no hay mediciones para este periodo.") }
                    let tope = max(d.areas.map(\.kwh).max() ?? 1, 0.0001)
                    ForEach(d.areas) { a in
                        DisclosureGroup(isExpanded: Binding(get: { abiertas.contains(a.nombre) }, set: { if $0 { abiertas.insert(a.nombre) } else { abiertas.remove(a.nombre) } })) {
                            ForEach(a.dispositivos) { x in
                                VStack(alignment: .leading) {
                                    HStack { Text(x.nombre); Spacer(); Text(CasaView.kwh(x.kwh ?? 0)).font(.caption) }
                                    Text((x.fuente ?? "") + (x.w_max.map { " · máx " + CasaView.w($0) } ?? "")).font(.caption2).foregroundStyle(.secondary)
                                    DosToques(titulo: "Quitar del total", armado: "Toca otra vez para quitarlo") { await contar(x.entity_id, false) }
                                        .font(.caption).buttonStyle(.borderless)
                                }
                            }
                        } label: {
                            VStack(alignment: .leading) {
                                HStack { Text(a.nombre); Spacer(); Text(CasaView.kwh(a.kwh)).font(.caption) }
                                ProgressView(value: a.kwh / tope)
                            }
                        }
                    }
                }
                if let sin = d.sin_medicion, !sin.isEmpty {
                    Section("Sin medición (\(sin.count))") { ForEach(sin) { Text($0.nombre) } }
                }
                if let fu = d.fuera_del_total, !fu.isEmpty {
                    Section("Fuera del total (\(fu.count))") {
                        ForEach(fu) { x in HStack { Text(x.nombre); Spacer(); Button("Volver a contar") { Task { await contar(x.entity_id, true) } }.buttonStyle(.borderless) } }
                    }
                }
                Section {
                    Picker("Nivel de la casa", selection: Binding(get: { d.nivel_casa ?? 1 }, set: { k in Task { await nivel(k) } })) {
                        ForEach(1...3, id: \.self) { Text("\($0)").tag($0) }
                    }.pickerStyle(.segmented)
                    Picker("Medidor de tablero", selection: Binding(get: { d.tablero ?? "" }, set: { v in Task { await tablero(v) } })) {
                        Text("Ninguno").tag("")
                        ForEach(d.areas.flatMap(\.dispositivos)) { x in Text(x.nombre + " (" + (x.fuente ?? "") + ")").tag(x.entity_id) }
                        if let t = d.tablero, !d.areas.flatMap(\.dispositivos).contains(where: { $0.entity_id == t }) { Text(d.tablero_nombre ?? t).tag(t) }
                    }
                } footer: { Text("Si eliges uno, el total es su lectura y la diferencia aparece como «no medido».") }
            } else if error == nil { ProgressView() }
        }
        .navigationTitle("Energía")
        .onChange(of: per) { Task { await cargar() } }
        .task { while !Task.isCancelled { await cargar(); try? await Task.sleep(for: .seconds(60)) } }
    }

    private func unidad(_ res: Int) -> Calendar.Component { per == "12m" ? .month : (per == "hoy" && res >= 2) ? .hour : .day }

    /// epRange() del panel, en la zona horaria de la casa.
    private func rango(_ tz: TimeZone) -> (Int, Int) {
        var cal = Calendar(identifier: .gregorian); cal.timeZone = tz
        let ahora = Date(), hoy = cal.startOfDay(for: ahora)
        let desde: Date
        switch per {
        case "7": desde = cal.date(byAdding: .day, value: -6, to: hoy)!
        case "30": desde = cal.date(byAdding: .day, value: -29, to: hoy)!
        case "12m": desde = cal.date(byAdding: .month, value: -11, to: cal.date(from: cal.dateComponents([.year, .month], from: ahora))!)!
        default: desde = hoy
        }
        return (Int(desde.timeIntervalSince1970), Int(ahora.timeIntervalSince1970))
    }

    private func cargar() async {
        guard let api = session.api else { return }
        let tz = d?.tz.flatMap(TimeZone.init(identifier:)) ?? .current
        let (desde, hasta) = rango(tz)
        var res = 1
        for _ in 0..<2 {                                           // la primera respuesta dice los niveles; «Hoy» pide horas si hay
            do {
                let r: Energia = try await api.get("/api/casa/\(casa)/energia", query: [
                    URLQueryItem(name: "resolucion", value: String(res)), URLQueryItem(name: "desde", value: String(desde)), URLQueryItem(name: "hasta", value: String(hasta))])
                d = r; error = nil
                let maxNivel = r.areas.flatMap(\.dispositivos).compactMap(\.nivel).max() ?? 1
                let quiere = per == "hoy" && maxNivel >= 2 ? 2 : 1
                if quiere == res { return }
                res = quiere
            } catch { self.error = detalle(error, "No se pudo leer la energía"); return }
        }
    }

    private func nivel(_ k: Int) async {
        guard let api = session.api else { return }
        do { let _: Ignorado = try await api.put("/api/casa/\(casa)/energia/nivel", body: ["nivel": k]) } catch { self.error = detalle(error, "No se pudo") }
        await cargar()
    }

    private func tablero(_ v: String) async {
        guard let api = session.api else { return }
        let body: [String: JSONCodable] = ["entity_id": v.isEmpty ? .null : .s(v)]
        do { let _: Ignorado = try await api.put("/api/casa/\(casa)/energia/total", body: body) } catch { self.error = detalle(error, "No se pudo") }
        await cargar()
    }

    private func contar(_ eid: String, _ si: Bool) async {
        guard let api = session.api else { return }
        let body: [String: JSONCodable] = ["entity_id": .s(eid), "oculto": .b(!si), "confirm": .b(true)]
        if let (code, data) = try? await api.crudo("PUT", "/api/casa/\(casa)/dispositivo", body: body), !(200..<300).contains(code) {
            self.error = String(data: data, encoding: .utf8) ?? "No se pudo (HTTP \(code))"
        }
        await cargar()
    }
}
