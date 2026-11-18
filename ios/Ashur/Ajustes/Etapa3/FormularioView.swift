import SwiftUI

/// Ajustes › Formulario (data-page="formulario"), nativo: preguntas de Ashur y secciones. Cada respuesta se guarda
/// al salir del campo; «Guardar sección» hace que Ashur aprenda de ella.
struct FormularioView: View {
    @Environment(Session.self) private var session

    struct Pregunta: Decodable, Identifiable { let id: String; let pregunta: String; let respuesta: String? }
    struct Seccion: Decodable, Identifiable { let id: String; let titulo: String; let respondidas: Int; let preguntas: [Pregunta] }
    struct Form: Decodable { let secciones: [Seccion]; let ashur: [Pregunta] }

    @State private var form: Form?
    @State private var guardadas: [String: String] = [:]

    var body: some View {
        List {
            if let f = form {
                let abiertas = f.ashur.filter { ($0.respuesta ?? "").isEmpty }
                if !abiertas.isEmpty {
                    Section("Preguntas de Ashur") { ForEach(abiertas) { CampoPregunta(q: $0) } }
                }
                ForEach(f.secciones) { s in
                    Section {
                        DisclosureGroup {
                            ForEach(s.preguntas) { CampoPregunta(q: $0) }
                            Button(guardadas[s.id] ?? "Guardar sección") { Task { await aprender(s) } }
                        } label: {
                            HStack { Text(s.titulo); Spacer(); Text("\(s.respondidas) de \(s.preguntas.count)").font(.caption).foregroundStyle(.secondary) }
                        }
                    }
                }
                let hechas = f.secciones.reduce(0) { $0 + $1.respondidas }, total = f.secciones.reduce(0) { $0 + $1.preguntas.count }
                Section {
                    Nota(texto: "\(hechas) de \(total) respondidas. Se guarda cada respuesta al salir del campo; puedes dejarlo a medias. «Guardar sección» hace que Ashur aprenda de esas respuestas con el modelo local (lo sensible va a la bandeja).")
                }
            }
        }
        .task { await cargar() }
    }

    private func cargar() async { form = try? await session.api?.get("/api/formulario") }

    private func aprender(_ s: Seccion) async {
        guard let api = session.api else { return }
        try? await Task.sleep(for: .milliseconds(300))              // deja terminar el guardado del último campo
        do { let _: Ignorado = try await api.post("/api/formulario/\(s.id)/aprender"); guardadas[s.id] = "Guardada: Ashur aprenderá de ella" }
        catch { guardadas[s.id] = "No se pudo guardar" }
        try? await Task.sleep(for: .seconds(2))
        guardadas[s.id] = nil
        await cargar()
    }
}

/// Una pregunta: se guarda al perder el foco, si cambió.
private struct CampoPregunta: View {
    @Environment(Session.self) private var session
    let q: FormularioView.Pregunta
    @State private var texto = ""
    @State private var guardado = ""
    @FocusState private var foco: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(q.pregunta).font(.subheadline)
            TextField("", text: $texto, axis: .vertical).lineLimit(2...8).focused($foco)
                .padding(6).background(.quaternary, in: RoundedRectangle(cornerRadius: 6))
        }
        .onAppear { texto = q.respuesta ?? ""; guardado = texto }
        .onChange(of: foco) {
            guard !foco, texto != guardado, let api = session.api else { return }
            let t = texto
            Task {
                if (try? await api.put("/api/formulario/\(q.id)", body: ["respuesta": t]) as Ignorado) != nil { guardado = t }
            }
        }
    }
}
