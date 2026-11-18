import SwiftUI

/// Ajustes › Archivos y espacio (data-page="space"), nativo: ¿está respaldado?, duplicados y los más grandes.
struct EspacioView: View {
    @Environment(Session.self) private var session

    struct Backup: Decodable {
        let pending: Bool?
        let disks: [Disco]?
        struct Donde: Decodable { let node_id: String; let key: String?; let mount: String? }
        struct Disco: Decodable, Identifiable {
            let node_id: String; let key: String?; let mount: String
            let files_fp: Int?; let small_files: Int?; let unknown_files: Int?
            let bytes_fp: Double?; let small_bytes: Double?; let copied_bytes: Double?; let probable_files: Int?; let unknown_bytes: Double?
            let `where`: [Donde]?
            var id: String { node_id + ":" + (key ?? mount) }
        }
    }
    struct Reporte: Decodable {
        let pending: Bool?
        let duplicate_groups: Int?; let wasted: Double?
        let duplicates: [Grupo]?; let largest: [Grande]?
        struct Archivo: Decodable { let node_id: String; let path: String }
        struct Grupo: Decodable, Identifiable { let files: [Archivo]; let count: Int; let size: Double; let wasted: Double; var id: String { files.map(\.path).joined() } }
        struct Grande: Decodable, Identifiable { let path: String; let node_id: String; let size: Double; var id: String { node_id + path } }
    }
    struct Pedido: Decodable { let queued: [String] }

    @State private var backup: Backup?
    @State private var reporte: Reporte?
    @State private var invNota: String?
    @State private var verDups = false
    @State private var verGrandes = false

    static func nombreDisco(_ mount: String) -> String {
        if mount.range(of: #"^[A-Za-z]:[\\/]?$"#, options: .regularExpression) != nil { return String(mount.prefix(2)) }
        if mount == "/" { return "Disco principal" }
        return (mount.split(separator: "/").last.map(String.init) ?? mount).replacingOccurrences(of: "_", with: " ")
    }

    var body: some View {
        List {
            Section {
                if backup?.pending == true { Text("Calculando…") }
                else if let ds = backup?.disks {
                    let filas = ds.filter { ($0.files_fp ?? 0) + ($0.small_files ?? 0) + ($0.unknown_files ?? 0) > 0 }
                        .sorted { a, b in
                            if (a.node_id == "zima-server") != (b.node_id == "zima-server") { return a.node_id == "zima-server" }
                            return total(a) > total(b)
                        }
                    if filas.isEmpty { Text("Aún no hay inventarios con discos.") }
                    ForEach(filas) { x in disco(x, ds) }
                }
                Button("Recalcular") { Task { let _: Ignorado? = try? await session.api?.post("/api/backup/refresh"); backup = nil; try? await Task.sleep(for: .seconds(3)); await cargar() } }
            } header: { Text("¿Está respaldado?") } footer: {
                Text("Por disco: cuánto de lo que tiene existe también en otro disco, de este u otro equipo. Archivos de 1 MB o más, por huella. Los más pequeños solo por nombre y tamaño: es una copia probable, nunca suficiente para borrar. Ashur no mueve ni borra nada aquí.")
            }
            Section {
                if reporte?.pending == true { Text("Calculando… vuelve a abrir en un momento.") }
                else if let r = reporte {
                    if let g = r.duplicate_groups, g > 0 {
                        LabeledContent("Podrías recuperar " + Textos.bytes(r.wasted ?? 0), value: "\(Textos.n(g)) " + (g == 1 ? "archivo repetido" : "archivos repetidos"))
                    }
                    let dups = r.duplicates ?? []
                    if dups.isEmpty { Text("No encontré copias repetidas.") }
                    ForEach(verDups ? dups : Array(dups.prefix(6))) { g in
                        VStack(alignment: .leading) {
                            Text(g.files.map { "\($0.node_id): \($0.path)" }.joined(separator: "\n")).font(.caption)
                            Text("\(g.count) copias de \(Textos.bytes(g.size)) · sobran \(Textos.bytes(g.wasted))").font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                    if dups.count > 6 { Button(verDups ? "Ver menos" : "Ver los \(dups.count)") { verDups.toggle() } }
                }
                Button("Repasar inventario ahora") { Task { await repasar() } }
                if let invNota { Nota(texto: invNota) }
            } header: { Text("Archivos duplicados") } footer: {
                Text("Copias idénticas de 1 MB o más, entre todos tus equipos. Ashur no borra nada: solo te muestra dónde están.")
            }
            Section("Archivos más grandes") {
                let g = reporte?.largest ?? []
                if g.isEmpty && reporte?.pending != true { Text("Aún no hay inventarios.") }
                ForEach(verGrandes ? g : Array(g.prefix(5))) { f in
                    LabeledContent { Text(Textos.bytes(f.size)) } label: { Text(f.path).font(.caption); Text(f.node_id) }
                }
                if g.count > 5 { Button(verGrandes ? "Ver menos" : "Ver los \(g.count)") { verGrandes.toggle() } }
            }
        }
        .refreshable { await cargar() }
        .task { await cargar() }
    }

    private func total(_ x: Backup.Disco) -> Double { (x.bytes_fp ?? 0) + (x.small_bytes ?? 0) }

    private func disco(_ x: Backup.Disco, _ todos: [Backup.Disco]) -> some View {
        let donde = (x.where ?? []).map { w in w.node_id + " " + Self.nombreDisco(todos.first { $0.node_id == w.node_id && $0.key == w.key }?.mount ?? w.mount ?? "") }.joined(separator: ", ")
        var lineas = ["\(Textos.bytes(x.copied_bytes ?? 0)) de \(Textos.bytes(x.bytes_fp ?? 0)) con copia en otro disco" + (donde.isEmpty ? "" : " · sobre todo en " + donde)]
        if let s = x.small_files, s > 0 { lineas.append("Pequeños: \(Textos.n(x.probable_files ?? 0)) de \(Textos.n(s)) con copia probable") }
        if let u = x.unknown_files, u > 0 { lineas.append("\(Textos.n(u)) sin huella (\(Textos.bytes(x.unknown_bytes ?? 0)))") }
        let fp = x.bytes_fp ?? 0
        return LabeledContent {
            Text(fp > 0 ? "\(Int((100 * (x.copied_bytes ?? 0) / fp).rounded())) %" : "—")
        } label: {
            Text("\(x.node_id) · \(Self.nombreDisco(x.mount))")
            Text(lineas.joined(separator: "\n"))
        }
    }

    private func cargar() async {
        guard let api = session.api else { return }
        backup = try? await api.get("/api/backup")
        reporte = try? await api.get("/api/inventory/report")
        if backup?.pending == true || reporte?.pending == true {
            try? await Task.sleep(for: .seconds(15))
            await cargar()
        }
    }

    private func repasar() async {
        guard let api = session.api else { return }
        invNota = "Pidiéndolo…"
        do {
            let d: Pedido = try await api.post("/api/inventory/refresh")
            invNota = d.queued.isEmpty ? "Nada que pedir: ya tienen un repaso esperando o en curso."
                : "Pedido para \(d.queued.joined(separator: ", ")). Cada equipo empieza cuando esté libre (10 minutos sin uso y, si es portátil, enchufado). Los duplicados aparecen cuando terminen."
        } catch { invNota = "No pude pedirlo." }
    }
}
