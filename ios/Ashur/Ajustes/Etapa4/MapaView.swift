import SwiftUI

/// Ajustes › Mapa de archivos (data-page="map"), nativo: equipos, carpetas con tamaño, «Mía» / «Ruido», marcar como
/// ruido lo sugerido, mudar una carpeta al servidor y las mudanzas.
struct MapaView: View {
    @Environment(Session.self) private var session
    let inicio: (nodo: String, ruta: String)?

    init(inicio: (nodo: String, ruta: String)? = nil) { self.inicio = inicio }

    struct Raiz: Decodable { let nodes: [Equipo]; let counting: Bool?; struct Equipo: Decodable, Identifiable { let node_id: String; let entries: Int?; let bytes: Double?; var id: String { node_id } } }
    struct Nivel: Decodable {
        let building: Bool?; let built: Double?; let path: String?; let parent: String?; let total_bytes: Double?; let protected: Int?
        let children: [Hija]?; let mark_here: String?; let files_here: Int?; let bytes_here: Double?; let more: Int?
        struct Hija: Decodable, Identifiable {
            let name: String; let path: String; let bytes: Double; let files: Int?; let kinds: [String: Double]?
            let mark: String?; let own: Bool?; let suggest: Bool?; let summary: Bool?
            var id: String { path }
        }
    }
    struct Mudanza: Decodable, Identifiable {
        let id: Int; let node_id: String; let path: String; let bytes: Double?; let files: Int?; let modo: String?; let dest: String?
        let estado: String; let done_files: Int?; let done_bytes: Double?; let error: String?; let volumen: Bool?
    }
    struct Marcadas: Decodable { let marked: Int }
    private struct Marca: Encodable { let node: String; let path: String; let mark: String?
        func encode(to e: Encoder) throws { var c = e.container(keyedBy: K.self); try c.encode(node, forKey: .node); try c.encode(path, forKey: .path); try c.encode(mark, forKey: .mark) }
        enum K: String, CodingKey { case node, path, mark } }

    private static let estadoMudanza = ["propuesta": "propuesta", "aprobada": "aprobada · en cola, empieza cuando el equipo esté libre", "en_curso": "copiando",
                                        "hecha": "hecha", "fallida": "falló", "cancelada": "cancelada"]

    @State private var raiz: Raiz?
    @State private var nodo: String?
    @State private var nivel: Nivel?
    @State private var cabecera = ""
    @State private var mudanzas: [Mudanza] = []
    @State private var verMudanzas = false

    var body: some View {
        List {
            Section {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack {
                        ForEach((raiz?.nodes ?? []).filter { ($0.entries ?? 0) > 0 }) { n in
                            Button("\(n.node_id) · \(Textos.bytes(n.bytes ?? 0))") { Task { await abrir(n.node_id, "") } }
                                .buttonStyle(.bordered).tint(nodo == n.node_id ? .accentColor : .secondary)
                        }
                    }
                }
                if !cabecera.isEmpty { Text(cabecera).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
                if nodo == nil { Text(raiz?.counting == true ? "Contando el inventario…" : (raiz?.nodes.contains { ($0.entries ?? 0) > 0 } == true ? "Elige un equipo." : "Todavía no hay inventarios.")) }
                if let n = nodo, let d = nivel, d.building != true { carpetas(n, d) }
            } footer: {
                Text("Tamaño por carpeta, según el último inventario. Toca una carpeta para entrar. «Mía» o «Ruido» vale para todo lo que tiene adentro; lo que no marques no entra al consolidado. Ashur no mueve ni borra nada desde aquí.")
            }
            Section {
                if let m = mudanzas.first, m.volumen == false {
                    VStack(alignment: .leading) {
                        Text("Falta el volumen /consolidado en ashur-core")
                        Text("Sin él no se copia nada: agrega /DATA/Consolidado → /consolidado en los ajustes de la app en ZimaOS.").font(.caption).foregroundStyle(.secondary)
                    }
                }
                if mudanzas.isEmpty { Text("Ninguna todavía.") }
                ForEach(verMudanzas ? mudanzas : Array(mudanzas.prefix(6))) { mudanza($0) }
                if mudanzas.count > 6 { Button(verMudanzas ? "Ver menos" : "Ver las \(mudanzas.count)") { verMudanzas.toggle() } }
            } header: { Text("Mudanzas al servidor") } footer: {
                Text("Cada carpeta «Mía» que mandes al servidor queda aquí como propuesta hasta que la apruebes. «Copiar» deja el original donde está; «Mover» lo manda a _cuarentena_ashur en su mismo disco cuando la copia esté verificada. Al aprobarla, el equipo copia cuando esté libre y el servidor verifica cada archivo con SHA-256. «Mover» se puede proponer pero todavía no aprobar: mandar el original a cuarentena llega en la siguiente etapa.")
            }
        }
        .task {
            await cargarRaiz()
            if let i = inicio { await abrir(i.nodo, i.ruta) }
            await cargarMudanzas()
        }
        .refreshable { if let n = nodo { await abrir(n, nivel?.path ?? "") } else { await cargarRaiz() }; await cargarMudanzas() }
    }

    @ViewBuilder private func carpetas(_ n: String, _ d: Nivel) -> some View {
        if let p = d.path, !p.isEmpty { Button("‹ Subir") { Task { await abrir(n, d.parent ?? "") } } }
        let hijas = d.children ?? []
        let tope = Swift.max(hijas.first?.bytes ?? 1, 1)
        ForEach(hijas) { k in
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(k.name).lineLimit(2)
                    Spacer()
                    Text(Textos.bytes(k.bytes)).font(.caption.monospacedDigit())
                }
                let estado = k.mark.map { ($0 == "mia" ? "mía" : "ruido") + (k.own == true ? "" : " (por la carpeta de arriba)") }
                    ?? (k.suggest == true ? "sin revisar · sugerido: ruido" : "sin revisar")
                let tipos = (k.kinds ?? [:]).sorted { $0.value > $1.value }.map { "\($0.key) \(Textos.bytes($0.value))" }.joined(separator: " · ")
                Text(estado + " · " + (k.summary == true ? "resumida · " : "\(Textos.n(k.files ?? 0)) " + ((k.files ?? 0) == 1 ? "archivo · " : "archivos · ")) + tipos)
                    .font(.caption2).foregroundStyle(.secondary)
                ProgressView(value: Swift.max(0.02, k.bytes / tope))
                HStack {
                    ForEach([("mia", "Mía"), ("ruido", "Ruido")], id: \.0) { par in
                        let mk = par.0, puesta = k.mark == par.0 && k.own == true
                        Button(par.1) { Task { await marcar(n, k.path, puesta ? nil : mk, d.path ?? "") } }
                            .buttonStyle(.bordered).tint(puesta ? .accentColor : .secondary)
                    }
                    Spacer()
                    if k.summary != true { Button("Entrar") { Task { await abrir(n, k.path) } }.buttonStyle(.borderless) }
                }
            }
        }
        if d.mark_here == "mia", let p = d.path, !p.isEmpty {
            VStack(alignment: .leading) {
                Text("Mudar esta carpeta al servidor")
                Text("Destino: Consolidado/\(n)/…  Se crea una propuesta; nada viaja hasta que la apruebes.").font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Copiar") { Task { await mudar(n, p, "copiar") } }
                    Button("Mover") { Task { await mudar(n, p, "mover") } }
                }.buttonStyle(.bordered)
            }
        }
        if !hijas.isEmpty && d.mark_here != "mia" {
            DosToques(titulo: "Marcar como ruido todo lo sugerido aquí adentro", armado: "Toca otra vez para marcar") { await aceptar(n, d.path ?? "") }
        }
        if let f = d.files_here, f > 0 { LabeledContent("Archivos sueltos aquí", value: "\(Textos.n(f)) archivos · \(Textos.bytes(d.bytes_here ?? 0))") }
        if let m = d.more, m > 0 { Text("y \(m) carpetas más pequeñas").foregroundStyle(.secondary) }
        if hijas.isEmpty && (d.files_here ?? 0) == 0 { Text("Carpeta vacía.") }
    }

    private func mudanza(_ m: Mudanza) -> some View {
        let prog = ["en_curso", "hecha", "fallida"].contains(m.estado) && (m.files ?? 0) > 0
            ? " · \(Textos.n(m.done_files ?? 0)) de \(Textos.n(m.files ?? 0)) archivos (\(Textos.bytes(m.done_bytes ?? 0)))" : ""
        return VStack(alignment: .leading, spacing: 4) {
            HStack { Text("\(m.node_id)  \(m.path)").lineLimit(2); Spacer(); Text(Textos.bytes(m.bytes ?? 0)).font(.caption) }
            Text((m.modo == "mover" ? "mover" : "copiar") + " · \(Textos.n(m.files ?? 0)) " + ((m.files ?? 0) == 1 ? "archivo" : "archivos")
                 + " · → \(m.dest ?? "") · " + (Self.estadoMudanza[m.estado] ?? m.estado) + prog + (m.error.map { " · " + $0 } ?? ""))
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                if m.estado == "propuesta" { DosToques(titulo: "Aprobar", armado: "¿Seguro?") { await accionMudanza(m.id, "aprobar") } }
                if m.estado == "propuesta" || m.estado == "aprobada" { DosToques(titulo: "Cancelar", armado: "¿Seguro?", rol: .destructive) { await accionMudanza(m.id, "cancelar") } }
            }.buttonStyle(.bordered)
        }
    }

    private func cargarRaiz() async {
        guard let api = session.api else { return }
        do {
            raiz = try await api.get("/api/map")
            if raiz?.counting == true && nodo == nil { try? await Task.sleep(for: .seconds(4)); await cargarRaiz() }
        } catch { cabecera = "No pude leer el mapa (\(detalle(error, "error"))). Toca otra vez en un momento." }
    }

    private func abrir(_ n: String, _ ruta: String) async {
        guard let api = session.api else { return }
        nodo = n
        cabecera = "\(n)  \(ruta) · leyendo…"
        do {
            let d: Nivel = try await api.get("/api/map", query: [URLQueryItem(name: "node", value: n), URLQueryItem(name: "path", value: ruta)])
            if d.building == true {
                cabecera = "\(n) · preparando el mapa (solo la primera vez)…"; nivel = d
                try? await Task.sleep(for: .seconds(5))
                if nodo == n { await abrir(n, ruta) }
                return
            }
            nivel = d
            let cuando = d.built.map { " · inventario del " + Date(timeIntervalSince1970: $0).formatted(.dateTime.day().month(.abbreviated).hour().minute()) } ?? ""
            let prot = (d.protected ?? 0) > 0 ? " · \(d.protected ?? 0) " + (d.protected == 1 ? "elemento protegido" : "elementos protegidos") + " (no se miden)" : ""
            cabecera = "\(n)  \(d.path ?? "") · \(Textos.bytes(d.total_bytes ?? 0))" + cuando + prot
        } catch { cabecera = "No pude leer el mapa (\(detalle(error, "error"))). Toca otra vez en un momento." }
    }

    private func marcar(_ n: String, _ ruta: String, _ mk: String?, _ aqui: String) async {
        guard let api = session.api else { return }
        do { let _: Ignorado = try await api.put("/api/map/mark", body: Marca(node: n, path: ruta, mark: mk)); await abrir(n, aqui) }
        catch { cabecera = "No pude guardar la marca (\(detalle(error, "error")))." }
    }

    private func aceptar(_ n: String, _ ruta: String) async {
        guard let api = session.api else { return }
        do {
            let r: Marcadas = try await api.post("/api/map/accept", body: ["node": n, "path": ruta])
            await abrir(n, ruta)
            cabecera += " · " + (r.marked > 0 ? "\(Textos.n(r.marked)) " + (r.marked == 1 ? "carpeta marcada ruido" : "carpetas marcadas ruido") : "nada sugerido sin marcar")
        } catch { cabecera = "No pude marcar (\(detalle(error, "error")))." }
    }

    private func mudar(_ n: String, _ ruta: String, _ modo: String) async {
        guard let api = session.api else { return }
        do { let _: Ignorado = try await api.post("/api/mudanzas", body: ["node": n, "path": ruta, "modo": modo]); await cargarMudanzas() }
        catch { cabecera = "No pude crear la propuesta: " + detalle(error, "error") }
    }

    private func cargarMudanzas() async {
        guard let api = session.api else { return }
        mudanzas = (try? await api.get("/api/mudanzas")) ?? []
        if mudanzas.contains(where: { $0.estado == "en_curso" || $0.estado == "aprobada" }) {
            try? await Task.sleep(for: .seconds(10))
            await cargarMudanzas()
        }
    }

    private func accionMudanza(_ id: Int, _ que: String) async {
        guard let api = session.api else { return }
        do { let _: Ignorado = try await api.post("/api/mudanzas/\(id)/\(que)") } catch { cabecera = detalle(error, "error") }
        await cargarMudanzas()
    }
}
