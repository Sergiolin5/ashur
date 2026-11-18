import SwiftUI

/// Ajustes › Actualización y seguridad (data-page="update"), nativo: versión del núcleo y de los nodos, actualizar
/// (con código del autenticador desde fuera de la tailnet) y el autenticador. Etapa 2 de docs/ios/NATIVO.md.
struct ActualizacionView: View {
    @Environment(Session.self) private var session
    @State private var info: UpdateInfo?
    @State private var soloTailnet = false
    @State private var codigo = ""
    @State private var actualizandoA: String?
    @State private var nota: String?
    @State private var totp: TotpState?
    @State private var afuera = true
    @State private var setup: TotpSetup?
    @State private var codigoNuevo = ""
    @State private var totpNota: String?

    var body: some View {
        List {
            Section {
                if soloTailnet {
                    VStack(alignment: .leading) {
                        Text("Solo desde la tailnet")
                        Text("Por seguridad, las actualizaciones no se hacen desde la dirección pública.").font(.caption).foregroundStyle(.secondary)
                    }
                } else if let u = info { actualizacion(u) }
                if let nota { Nota(texto: nota) }
            } header: { Text("Actualización") }
            Section {
                autenticador()
                if let totpNota { Nota(texto: totpNota) }
            } header: { Text("Autenticador") } footer: {
                Text("Para actualizar desde fuera de la tailnet hacen falta tu clave de administrador y un código de esta app (Google Authenticator, Microsoft Authenticator, Contraseñas del iPhone…). Se configura solo estando en la tailnet.")
            }
        }
        .refreshable { await cargar() }
        .task { await cargar() }
    }

    @ViewBuilder private func actualizacion(_ u: UpdateInfo) -> some View {
        let afuera = u.door == "public", last = u.updater?.last
        VStack(alignment: .leading) {
            Text("Núcleo v\(u.current)")
            Text(u.latest.map { u.available == true ? "Hay una versión nueva: v\($0)" : "Es la última publicada" } ?? "No pude consultar la última versión")
                .font(.caption).foregroundStyle(.secondary)
        }
        if let ns = u.nodes, !ns.isEmpty {
            let atras = ns.filter { !$0.current }.map { $0.id + ($0.version.map { " (v\($0))" } ?? " (versión ?)") }
            VStack(alignment: .leading) {
                Text("Nodos: código v\(u.nodes_version ?? "?")")
                Text(atras.isEmpty ? "Todos al día. Se actualizan solos cuando cambia su código."
                     : "Al día: \(ns.count - atras.count) de \(ns.count). Pendientes: \(atras.joined(separator: ", ")). Se actualizan solos; zima-server con este botón.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        if actualizandoA != nil || u.updater?.busy == true {
            VStack(alignment: .leading) {
                Text("Actualizando a v\(actualizandoA ?? "…")")
                Text("El panel se desconecta un momento y vuelve solo.").font(.caption).foregroundStyle(.secondary)
            }
        } else if u.available == true, u.updater != nil, afuera, u.totp != true, let latest = u.latest {
            VStack(alignment: .leading) {
                Text("Desde fuera de la tailnet")
                Text("Hay una versión nueva (v\(latest)). Para instalarla desde aquí, primero configura el autenticador estando en la tailnet.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        } else if u.available == true, u.updater != nil, let latest = u.latest {
            if afuera {
                TextField("Código del autenticador", text: $codigo).keyboardType(.numberPad).textContentType(.oneTimeCode)
            }
            Button("Actualizar a v\(latest)") { Task { await actualizar(latest, afuera) } }
        }
        if let p = u.updater_problem { Nota(texto: p) }
        else if let last, let v = last.version {
            Nota(texto: (last.ok == true ? "Última actualización: v\(v) en \(Int(last.seconds ?? 0)) s. " : "La última actualización FALLÓ: ")
                 + (last.steps ?? []).joined(separator: " · "))
        }
    }

    @ViewBuilder private func autenticador() -> some View {
        if let t = totp {
            if t.active {
                HStack {
                    VStack(alignment: .leading) {
                        Text("Activo")
                        Text("Se pide un código para actualizar desde fuera de la tailnet.").font(.caption).foregroundStyle(.secondary)
                    }
                    if !afuera { Spacer(); Button("Quitar", role: .destructive) { Task { await quitarTotp() } } }
                }
            } else if afuera {
                VStack(alignment: .leading) {
                    Text("Sin configurar")
                    Text("Se configura desde la tailnet.").font(.caption).foregroundStyle(.secondary)
                }
            } else if let s = setup {
                Text("1. Agrégalo a tu app").font(.headline)
                if let url = URL(string: s.uri) { Link("Abrir en el autenticador", destination: url) }
                Text("O escribe esta clave a mano: " + stride(from: 0, to: s.secret.count, by: 4).map { i -> String in
                    let a = s.secret.index(s.secret.startIndex, offsetBy: i), b = s.secret.index(a, offsetBy: min(4, s.secret.count - i))
                    return String(s.secret[a..<b])
                }.joined(separator: " ")).font(.caption).textSelection(.enabled)
                Text("2. Escribe el código que te muestra").font(.headline)
                TextField("000000", text: $codigoNuevo).keyboardType(.numberPad).textContentType(.oneTimeCode)
                Button("Confirmar") { Task { await confirmarTotp() } }
            } else {
                Button("Configurar el autenticador") { Task { await empezarTotp() } }
            }
        }
    }

    private func cargar() async {
        guard let api = session.api else { return }
        do {
            let u: UpdateInfo = try await api.get("/api/update")
            info = u; soloTailnet = false
            if let a = actualizandoA, u.current == a || (u.updater?.busy != true && u.updater?.last?.version == a) { actualizandoA = nil }
        } catch APIClient.Failure.server(403, _) {
            soloTailnet = true
        } catch {
            if actualizandoA == nil { nota = detalle(error, "No pude consultar la actualización.") }
        }
        if let h: Health = try? await api.get("/health") { afuera = h.door == "public" }
        totp = try? await api.get("/api/totp")
        if actualizandoA != nil || info?.updater?.busy == true {          // el núcleo reinicia: seguir preguntando
            try? await Task.sleep(for: .seconds(5))
            await cargar()
        }
    }

    private func actualizar(_ v: String, _ afuera: Bool) async {
        guard let api = session.api else { return }
        do {
            let _: Ignorado = try await api.post("/api/update", body: ["version": v],
                                                 headers: afuera ? ["X-Totp": codigo.trimmingCharacters(in: .whitespaces)] : [:])
            actualizandoA = v; nota = nil
        } catch { nota = detalle(error, "No se pudo iniciar.") }
        await cargar()
    }

    private func empezarTotp() async {
        guard let api = session.api else { return }
        do { setup = try await api.post("/api/totp/setup") } catch { totpNota = detalle(error, "No pude empezar.") }
    }

    private func confirmarTotp() async {
        guard let api = session.api else { return }
        do {
            let _: Ignorado = try await api.post("/api/totp/confirm", body: ["code": codigoNuevo.trimmingCharacters(in: .whitespaces)])
            setup = nil; totpNota = nil
            await cargar()
        } catch { totpNota = detalle(error, "Ese código no sirvió.") }
    }

    private func quitarTotp() async {
        guard let api = session.api else { return }
        let _: Ignorado? = try? await api.delete("/api/totp")
        await cargar()
    }
}
