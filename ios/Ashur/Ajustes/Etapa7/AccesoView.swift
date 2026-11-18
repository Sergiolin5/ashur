import SwiftUI

/// Ajustes › Acceso (data-page="access"), nativo: tu nombre, invitados (temas, estilo, perfil, revocar), límites y el
/// conector de Claude.
struct AccesoView: View {
    @Environment(Session.self) private var session

    struct Invitado: Decodable, Identifiable {
        let id: Int; let name: String; let is_self: Bool?; let spent_usd: Double?; let monthly_cap_usd: Double?
        let temas: [String]?; let estilo: Bool?; let perfil: Int?
    }
    struct Limites: Decodable { let messages_per_hour: Int; let default_cap_usd: Double }
    struct Creado: Decodable { let name: String; let key: String }
    struct Clave: Decodable { let active: Bool; let created: Double? }
    struct NuevaClave: Decodable { let key: String? }
    struct Dueno: Decodable { let owner: String }
    struct Who: Decodable { let owner: String? }
    struct PerfilItem: Decodable, Identifiable { let id: Int; let text: String; let tier: String? }
    private struct NuevoInvitado: Encodable { let name: String; let monthly_cap_usd: Double; let is_self: Bool }
    private struct Lims: Encodable { let messages_per_hour: Int; let default_cap_usd: Double }
    private static let temas = [("trabajo", "Trabajo y proyectos"), ("gustos", "Gustos y aficiones"), ("ashur", "Ashur mismo")]

    @State private var dueno = ""
    @State private var duenoNota: String?
    @State private var invitados: [Invitado] = []
    @State private var msgs = ""
    @State private var tope = ""
    @State private var limNota: String?
    @State private var nombre = ""
    @State private var topeNuevo = "1"
    @State private var esMio = false
    @State private var creado: String?
    @State private var clave: Clave?
    @State private var claveNota: String?
    @State private var claveNueva: String?
    @State private var perfiles: [Int: [PerfilItem]] = [:]

    var body: some View {
        List {
            Section("Tu nombre") {
                Text("Así te nombra Ashur ante tus invitados: «Ashur, el asistente de …».").font(.footnote)
                HStack { TextField("Tu nombre", text: $dueno); Button("Guardar") { Task { await guardarDueno() } } }
                if let duenoNota { Nota(texto: duenoNota) }
            }
            Section {
                if invitados.isEmpty { Text("Nadie más puede chatear con Ashur.") }
                ForEach(invitados) { invitado($0) }
            } header: { Text("Invitados") }
            Section {
                Text("Un invitado solo puede chatear con Ashur, en su propia conversación. No ve tus conversaciones ni tus dispositivos; de tus recuerdos, Ashur solo le cuenta los de los temas que marques en cada invitado (nunca salud ni lo «solo local»). Ashur recuerda lo que cada invitado le cuenta en su perfil, que solo ves tú, y lo que te enseñe a ti o a Ashur llega a «Por aprobar». Tiene un tope de gasto propio y un máximo de mensajes por hora; ambos los cambias aquí abajo.")
                    .font(.footnote)
                HStack {
                    TextField("Mensajes/hora", text: $msgs).keyboardType(.numberPad)
                    TextField("Tope US$ por defecto", text: $tope).keyboardType(.decimalPad)
                    Button("Guardar") { Task { await guardarLimites() } }
                }
                if let limNota { Nota(texto: limNota) }
                TextField("Nombre", text: $nombre)
                TextField("Tope US$ al mes", text: $topeNuevo).keyboardType(.decimalPad)
                Toggle("Es para mí (Atajo del iPhone): usa mis recuerdos y mi chat, pero solo puede chatear", isOn: $esMio).font(.footnote)
                Button("Crear invitado") { Task { await crear() } }
                if let creado {
                    Text(creado).font(.callout).textSelection(.enabled)
                    Button("Copiar la clave") { UIPasteboard.general.string = creado.components(separatedBy: "\n").dropFirst().first }
                }
            }
            Section("Conector de Claude") {
                if let c = clave {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(c.active ? "Activa desde " + Fechas.cuando(c.created) : "Sin clave")
                            Text(c.active ? "Claude puede usar el conector" : "Claude no puede entrar").font(.caption).foregroundStyle(.secondary)
                        }
                        if c.active { Spacer(); DosToques(titulo: "Revocar", armado: "Toca otra vez", rol: .destructive) { await cambiarClave("DELETE") }.buttonStyle(.borderless) }
                    }
                }
                Text("Con esta clave, Claude puede ver el estado de Ashur, los diagnósticos, buscar archivos por nombre y preguntarle al modelo local. No lee tu diario, tus documentos, tus recuerdos locales ni tus pendientes. Lo que quiera hacer te llega como pedido en Ajustes → Pedidos de Claude.")
                    .font(.footnote)
                if clave?.active == true {
                    DosToques(titulo: "Crear otra clave", armado: "Toca otra vez: la anterior deja de funcionar") { await cambiarClave("POST") }
                } else {
                    Button("Crear clave") { Task { await cambiarClave("POST") } }
                }
                if let claveNota { Text(claveNota).font(.callout).textSelection(.enabled) }
                if let k = claveNueva { Button("Copiar valor") { UIPasteboard.general.string = "Bearer " + k } }
            }
        }
        .refreshable { await cargar() }
        .task { await cargar() }
    }

    private func invitado(_ g: Invitado) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(g.name).font(.headline)
            Text(g.is_self == true ? "Clave de chat tuya: solo chat, con tus recuerdos"
                 : String(format: "US$%.2f de US$%.2f este mes", g.spent_usd ?? 0, g.monthly_cap_usd ?? 0)).font(.caption).foregroundStyle(.secondary)
            if g.is_self != true {
                Text("Puede contarle de ti:").font(.caption)
                HStack {
                    ForEach(Self.temas, id: \.0) { t in
                        let on = (g.temas ?? []).contains(t.0)
                        Button(t.1) { Task { await temas(g, t.0, !on) } }.buttonStyle(.bordered).tint(on ? .accentColor : .secondary).font(.caption)
                    }
                }
                Toggle("Con su estilo (siempre como Ashur): responde con mi estilo", isOn: Binding(get: { g.estilo == true }, set: { v in Task { await estilo(g, v) } })).font(.caption)
                DisclosureGroup("Perfil (\(g.perfil ?? 0))") {
                    Text("Lo que Ashur recuerda de \(g.name). Solo tú lo ves.").font(.caption)
                    if (perfiles[g.id] ?? []).isEmpty { Text("Todavía nada.").font(.caption) }
                    ForEach(perfiles[g.id] ?? []) { m in
                        HStack {
                            Text(m.text + (m.tier == "local" ? " · solo local" : "")).font(.caption)
                            Spacer()
                            Button("Olvidar", role: .destructive) { Task { let _: Ignorado? = try? await session.api?.delete("/api/memory/\(m.id)"); await perfil(g.id) } }
                                .buttonStyle(.borderless).font(.caption)
                        }
                    }
                }
                .onAppear { Task { await perfil(g.id) } }
            }
            Button("Revocar", role: .destructive) { Task { let _: Ignorado? = try? await session.api?.delete("/api/guests/\(g.id)"); await cargar() } }
                .buttonStyle(.borderless)
        }
    }

    private func cargar() async {
        guard let api = session.api else { return }
        if let w: Who = try? await api.get("/api/whoami"), let o = w.owner, dueno.isEmpty { dueno = o }
        invitados = (try? await api.get("/api/guests")) ?? []
        if let l: Limites = try? await api.get("/api/guests/limits") {
            msgs = String(l.messages_per_hour); tope = String(l.default_cap_usd); topeNuevo = String(l.default_cap_usd)
        }
        clave = try? await api.get("/api/mcp/key")
    }

    private func perfil(_ id: Int) async { perfiles[id] = (try? await session.api?.get("/api/guests/\(id)/perfil")) ?? [] }

    private func guardarDueno() async {
        guard let api = session.api else { return }
        do { let r: Dueno = try await api.put("/api/owner", body: ["name": dueno]); dueno = r.owner; duenoNota = "Listo: tus invitados verán «Ashur, el asistente de \(r.owner)»." }
        catch { duenoNota = detalle(error, "No pude guardarlo.") }
    }

    private func guardarLimites() async {
        guard let api = session.api else { return }
        do {
            let _: Ignorado = try await api.put("/api/guests/limits", body: Lims(messages_per_hour: Int(msgs) ?? 0, default_cap_usd: Double(tope.replacingOccurrences(of: ",", with: ".")) ?? 0))
            limNota = "Límites guardados. Aplican desde el próximo mensaje; el tope de gasto por defecto solo a invitados nuevos."
            await cargar()
        } catch { limNota = "Valores no válidos: mensajes entre 1 y 1000, tope entre 0 y 100." }
    }

    private func crear() async {
        guard let api = session.api else { return }
        let n = nombre.trimmingCharacters(in: .whitespaces)
        guard !n.isEmpty else { return }
        do {
            let g: Creado = try await api.post("/api/guests", body: NuevoInvitado(name: n, monthly_cap_usd: Double(topeNuevo.replacingOccurrences(of: ",", with: ".")) ?? 0, is_self: esMio))
            creado = "Clave de \(g.name) (cópiala ahora, no se vuelve a mostrar):\n\(g.key)\n\nEntra en la misma dirección del panel y pega esta clave."
            nombre = ""; esMio = false
            await cargar()
        } catch { creado = "No pude crear el invitado." }
    }

    private func temas(_ g: Invitado, _ t: String, _ on: Bool) async {
        var s = Set(g.temas ?? []); if on { s.insert(t) } else { s.remove(t) }
        let _: Ignorado? = try? await session.api?.put("/api/guests/\(g.id)/temas", body: ["temas": Array(s)])
        await cargar()
    }

    private func estilo(_ g: Invitado, _ on: Bool) async {
        let _: Ignorado? = try? await session.api?.put("/api/guests/\(g.id)/estilo", body: ["estilo": on])
        await cargar()
    }

    private func cambiarClave(_ metodo: String) async {
        guard let api = session.api else { return }
        claveNueva = nil
        do {
            if metodo == "DELETE" {
                let _: Ignorado = try await api.delete("/api/mcp/key")
                claveNota = "Clave revocada. Claude ya no puede usar el conector."
            } else {
                let r: NuevaClave = try await api.post("/api/mcp/key")
                if let k = r.key {
                    claveNueva = k
                    claveNota = "Clave del conector (cópiala ahora, no se vuelve a mostrar):\n\(k)\n\nDirección: \(APIClient.baseURL.absoluteString)/mcp\nNombre: Authorization · Valor: Bearer \(k)"
                }
            }
        } catch { claveNota = detalle(error, metodo == "DELETE" ? "No pude revocar la clave." : "No pude crear la clave.") }
        await cargar()
    }
}
