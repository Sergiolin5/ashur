import Foundation

/// La pestaña Estado en nativo. Vive en Session, como el chat: abrir o cerrar el Duo no la vacía.
@Observable
final class EstadoModel {
    var api: APIClient?
    private(set) var data: Overview?
    private(set) var ms: Double?                 // cuánto tardó el núcleo, como el subtítulo del panel
    private(set) var problem: String?
    private(set) var discos: [String: [EquipoExtra.Disco]] = [:]   // /api/disks, por equipo (etapa 7)
    private(set) var nombres: [String: String] = [:]               // /api/names: «node:…», «disk:…:…»
    private(set) var sinRespaldo: (bytes: Double, detalle: String)?  // loadHook() del panel, cada 5 min como mucho
    private var respaldoEn = Date.distantPast
    var note: String?                            // avisos de la cola (p. ej. «esa tarea ya no está en espera»)
    var armed: Set<Int> = []                     // «Cancelar» pide un segundo toque, como en el panel

    init(api: APIClient) { self.api = api }

    struct DiscosResp: Decodable { let nodes: [N]; struct N: Decodable { let node_id: String; let disks: [EquipoExtra.Disco] } }

    func load() async {
        guard let api else { return }
        let t0 = Date()
        do {
            data = try await api.get("/api/overview")
            if let d: DiscosResp = try? await api.get("/api/disks") { discos = Dictionary(uniqueKeysWithValues: d.nodes.map { ($0.node_id, $0.disks) }) }
            if let n: [String: String] = try? await api.get("/api/names") { nombres = n }
            if Date().timeIntervalSince(respaldoEn) > 300, let b: EspacioView.Backup = try? await api.get("/api/backup"), b.pending != true, let ds = b.disks {
                respaldoEn = Date()
                let solos = ds.map { max(0, ($0.bytes_fp ?? 0) - ($0.copied_bytes ?? 0)) }
                let total = solos.reduce(0, +), discos = solos.filter { $0 > 1e9 }.count
                sinRespaldo = (total, "de tus archivos existen en una sola copia" + (discos > 1 ? ", repartidos en \(discos) discos. Si uno falla, lo suyo se pierde." : ". Si ese disco falla, se pierden."))
            }
            ms = Date().timeIntervalSince(t0) * 1000
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
    }

    func move(_ id: Int, _ to: String) async {
        guard let api else { return }
        let _: Ignorado? = try? await api.post("/api/tasks/\(id)/move", body: ["to": to])
        await load()
    }

    /// Primer toque arma, segundo cancela; a los 3 s se desarma solo.
    func cancel(_ id: Int) async {
        guard let api else { return }
        if !armed.contains(id) {
            armed.insert(id)
            try? await Task.sleep(for: .seconds(3))
            armed.remove(id)
            return
        }
        armed.remove(id)
        do {
            let _: Ignorado = try await api.delete("/api/tasks/\(id)")
        } catch APIClient.Failure.server(409, let detail) {
            note = detail.isEmpty ? "Esa tarea ya no está en espera." : detail
        } catch {
            note = error.localizedDescription
        }
        await load()
    }

    func setPaused(_ paused: Bool) async {
        guard let api else { return }
        let _: Ignorado? = try? await api.post("/api/queue/pause", body: ["paused": paused])
        await load()
    }
}
