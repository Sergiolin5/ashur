import SwiftUI

/// Ajustes › Hábitos (data-page="habitos"), nativo: sugerencias («Hazlo» deja un pedido), lo que Ashur notó
/// (corregir con día o franja, borrar) y el conteo de los últimos 60 días.
struct HabitosView: View {
    @Environment(Session.self) private var session

    struct Sugerencia: Decodable, Identifiable { let habito: Int; let texto: String; let puede_hacer: Bool?; var id: Int { habito } }
    struct Habito: Decodable, Identifiable {
        let id: Int; let descripcion: String?; let nota: String?; let veces: Int; let dias: Int; let tipo: String?; let estado: String; let patron: [String: JSONValue]?
    }
    struct Conteo: Decodable { let texto: String; let veces: Int; let dias: Int }
    struct Propuesto: Decodable { let pedido: Int }

    @State private var sugerencias: [Sugerencia] = []
    @State private var habitos: [Habito] = []
    @State private var conteo: String?
    @State private var nota: String?
    @State private var editando: Int?
    @State private var texto = ""
    @State private var eleccion = 0

    private static let dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

    var body: some View {
        List {
            Section {
                if sugerencias.isEmpty { Text("Nada que sugerir ahora.") }
                ForEach(sugerencias) { x in
                    VStack(alignment: .leading) {
                        Text(x.texto)
                        if x.puede_hacer == true { Button("Hazlo") { Task { await hazlo(x) } }.buttonStyle(.bordered) }
                    }
                }
                if let nota { Nota(texto: nota) }
            } header: { Text("Sugerencias ahora") } footer: {
                Text("Ashur solo sugiere. «Hazlo» deja un pedido en Pedidos, y nada corre hasta que lo apruebas ahí.")
            }
            Section {
                if habitos.isEmpty { Text("Todavía no hay hábitos: Ashur necesita verte repetir algo unas semanas.") }
                ForEach(habitos) { h in habito(h) }
                Button("Volver a contar ahora") { Task { await cargar(refrescar: true) } }
                if let conteo { Nota(texto: conteo) }
            } header: { Text("Lo que Ashur notó") } footer: {
                Text("Cuenta en tu servidor, sin modelo ni Claude: la misma acción el mismo día de la semana en 3 semanas, o en la misma franja de 2 horas o el mismo lugar en 5 días. Corregir guarda tu versión; borrar hace que no vuelva.")
            }
        }
        .refreshable { await cargar() }
        .task { await cargar() }
    }

    private func habito(_ h: Habito) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(h.nota ?? h.descripcion ?? "")
            Text("\(h.veces) veces en \(h.dias)" + (h.tipo == "semana" ? " semanas" : " días") + " · " + (h.estado == "corregido" ? "corregido por ti" : "notado por Ashur"))
                .font(.caption).foregroundStyle(.secondary)
            if editando == h.id {
                TextField("Hábito", text: $texto)
                if h.tipo == "semana" {
                    Picker("Día", selection: $eleccion) { ForEach(0..<7, id: \.self) { Text(Self.dias[$0]).tag($0) } }
                } else if h.tipo == "hora" {
                    Picker("Franja", selection: $eleccion) { ForEach(0..<12, id: \.self) { i in Text("\(i * 2):00 a \(i * 2 + 2):00").tag(i * 2) } }
                }
                Button("Guardar") { Task { await corregir(h) } }.buttonStyle(.borderedProminent)
            } else {
                HStack {
                    Button("Corregir") {
                        editando = h.id; texto = h.nota ?? h.descripcion ?? ""
                        let clave = h.tipo == "semana" ? "dia" : "desde"
                        if case .number(let v) = h.patron?[clave] { eleccion = Int(v) } else { eleccion = 0 }
                    }
                    DosToques(titulo: "Borrar", armado: "Toca otra vez para borrar", rol: .destructive) {
                        let _: Ignorado? = try? await session.api?.delete("/api/habits/\(h.id)")
                        await cargar()
                    }
                }.buttonStyle(.borderless)
            }
        }
    }

    private func cargar(refrescar: Bool = false) async {
        guard let api = session.api else { return }
        if let s: [Sugerencia] = try? await api.get("/api/habits/sugerencias") { sugerencias = s }
        do {
            habitos = refrescar ? try await api.post("/api/habits/refrescar") : try await api.get("/api/habits")
        } catch { nota = "No pude cargar los hábitos." }
        if let c: [Conteo] = try? await api.get("/api/habits/conteo") {
            conteo = c.isEmpty ? "En los últimos 60 días Ashur no vio ninguna acción tuya."
                : "En los últimos 60 días Ashur vio: " + c.map { "\($0.texto) \($0.veces) veces en \($0.dias) " + ($0.dias == 1 ? "día" : "días") }.joined(separator: " · ") + "."
        }
    }

    private func hazlo(_ x: Sugerencia) async {
        guard let api = session.api else { return }
        do {
            let r: Propuesto = try await api.post("/api/habits/\(x.habito)/proponer")
            nota = "Pedido #\(r.pedido) listo para aprobar en Pedidos."
        } catch { nota = detalle(error, "No pude proponerlo.") }
    }

    private struct Correccion: Encodable { let nota: String; let dia: Int?; let desde: Int? }

    private func corregir(_ h: Habito) async {
        guard let api = session.api else { return }
        let body = Correccion(nota: texto, dia: h.tipo == "semana" ? eleccion : nil, desde: h.tipo == "hora" ? eleccion : nil)
        do { let _: Ignorado = try await api.put("/api/habits/\(h.id)", body: body); nota = "Hábito corregido." }
        catch { nota = detalle(error, "No pude corregirlo.") }
        editando = nil
        await cargar()
    }
}
