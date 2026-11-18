import SwiftUI

struct ChatView: View {
    @Bindable var model: ChatModel
    @Environment(Session.self) private var session
    @FocusState private var focused: Bool
    @State private var verConversaciones = false

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if model.loading && model.lines.isEmpty {
                        ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
                    }
                    ForEach(model.lines) { line in
                        Bubble(line: line, readAloud: { model.speaker.say(line.text) }).id(line.id)
                    }
                    if model.sending {
                        HStack(spacing: 6) {
                            ProgressView()
                            Text("Ashur está pensando…").font(.footnote).foregroundStyle(.secondary)
                        }
                        .id("typing")
                    }
                    if let p = model.pendingApproval {
                        ApprovalCard(approval: p) {
                            Task { await model.approveClaude() }
                        } decline: {
                            model.pendingApproval = nil
                        }
                        .id("approval")
                    }
                }
                .padding()
            }
            // Al reconstruirse la vista (desbloquear, abrir o cerrar el teléfono) los mensajes no cambian y
            // onChange no salta: sin esto el chat aparecía arriba del todo en vez de en el último mensaje.
            .defaultScrollAnchor(.bottom, for: .initialOffset)
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: model.lines.count) {
                withAnimation { proxy.scrollTo(model.lines.last?.id, anchor: .bottom) }
            }
        }
        .safeAreaInset(edge: .bottom) { composer }
        .navigationTitle("Ashur")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button("Conversaciones", systemImage: "list.bullet") { verConversaciones = true }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button("＋ Nueva") { model.nueva() }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Toggle("Leer respuestas en voz alta", systemImage: "speaker.wave.2",
                           isOn: Binding(get: { model.speaker.readAloud }, set: { model.speaker.readAloud = $0 }))
                    Toggle("Compartir documentos con Claude en este mensaje", systemImage: "doc.text",
                           isOn: Binding(get: { model.compartirDocs }, set: { model.compartirDocs = $0 }))
                    Toggle("Compartir el diario con Claude en este mensaje", systemImage: "book",
                           isOn: Binding(get: { model.compartirDiario }, set: { model.compartirDiario = $0 }))
                    Button("Olvidar la clave", systemImage: "key.slash", role: .destructive) {
                        session.forgetKey()
                    }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
            }
        }
        .alert("Algo falló", isPresented: Binding(get: { model.error != nil }, set: { if !$0 { model.error = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(model.error ?? "")
        }
        .task { if model.lines.isEmpty { await model.loadHistory() } }
        .sheet(isPresented: $verConversaciones) {
            ListaConversaciones(model: model) { verConversaciones = false }
        }
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 8) {
            let listening = model.dictation.listening
            Button {
                focused = false
                Task { await model.toggleDictation() }
            } label: {
                Image(systemName: listening ? "stop.circle.fill" : "mic.circle")
                    .font(.system(size: 32))
                    .foregroundStyle(listening ? Color.red : Color.accentColor)
            }
            .disabled(model.sending)
            .accessibilityLabel(listening ? "Dejar de dictar" : "Dictar")
            TextField(listening ? "Escuchando…" : "Escribe a Ashur", text: $model.draft, axis: .vertical)
                .lineLimit(1...5)
                .focused($focused)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(.fill.tertiary, in: RoundedRectangle(cornerRadius: 18))
            Button {
                model.dictation.stop()
                let text = model.draft
                model.draft = ""
                Task { await model.send(text) }
            } label: {
                Image(systemName: "arrow.up.circle.fill").font(.system(size: 32))
            }
            .disabled(model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.sending)
            .accessibilityLabel("Enviar")
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.bar)
    }
}

/// Texto plano: Ashur no responde con markdown, así que se muestra tal cual (sin interpretar asteriscos).
private struct Bubble: View {
    let line: ChatLine
    let readAloud: () -> Void

    var body: some View {
        switch line.role {
        case .aviso:
            Text(verbatim: line.text)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .center)
                .multilineTextAlignment(.center)
        case .sergio, .ashur:
            let mine = line.role == .sergio
            HStack {
                if mine { Spacer(minLength: 40) }
                Text(verbatim: line.text)
                    .textSelection(.enabled)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .foregroundStyle(mine ? Color.white : Color.primary)
                    .background(mine ? Color.accentColor : Color(.secondarySystemBackground),
                                in: RoundedRectangle(cornerRadius: 18))
                    .contextMenu {
                        if !mine { Button("Leer en voz alta", systemImage: "speaker.wave.2", action: readAloud) }
                        Button("Copiar", systemImage: "doc.on.doc") { UIPasteboard.general.string = line.text }
                    }
                if !mine { Spacer(minLength: 40) }
            }
        }
    }
}

private struct ApprovalCard: View {
    let approval: ClaudeApproval
    let approve: () -> Void
    let decline: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("¿Autorizas a Claude para este mensaje?", systemImage: "cloud")
                .font(.subheadline.weight(.semibold))
            if let usd = approval.estimateUSD {
                Text("Costo estimado: \(usd, format: .currency(code: "USD").precision(.fractionLength(2...4)))")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            HStack {
                Button("Autorizar", action: approve).buttonStyle(.borderedProminent)
                Button("No", role: .cancel, action: decline).buttonStyle(.bordered)
            }
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 16))
    }
}


/// La lista de conversaciones (0.62, como el panel): Hoy, Ayer, Anteriores y el Archivo; tocar una la retoma.
private struct ListaConversaciones: View {
    let model: ChatModel
    let cerrar: () -> Void
    @State private var lista: [Conversacion] = []
    @State private var archivo: Archivo?
    @State private var cargado = false

    private func grupo(_ c: Conversacion) -> String {
        if c.archived == true { return "Archivo" }
        let cal = Calendar.current, d = Date(timeIntervalSince1970: c.last_msg ?? c.started)
        if cal.isDateInToday(d) { return "Hoy" }
        if cal.isDateInYesterday(d) { return "Ayer" }
        return "Anteriores"
    }

    var body: some View {
        NavigationStack {
            List {
                if let a = archivo, (a.total ?? 0) > 0, a.terminado != true, a.aprobado == true {
                    Text("Aprendiendo del archivo: \(a.done ?? 0) de \(a.total ?? 0) mensajes").font(.caption).foregroundStyle(.secondary)
                }
                if cargado && lista.isEmpty { Text("Todavía no hay conversaciones.") }
                let grupos = lista.reduce(into: [String]()) { if $0.last != grupo($1) { $0.append(grupo($1)) } }
                ForEach(grupos, id: \.self) { g in
                    Section(g) {
                        ForEach(lista.filter { grupo($0) == g }) { c in
                            Button(c.title ?? Date(timeIntervalSince1970: c.started).formatted(.dateTime.day().month(.abbreviated).hour().minute())) {
                                Task { await model.abrir(c.id); cerrar() }
                            }
                            .fontWeight(c.id == model.convId ? .semibold : .regular)
                        }
                    }
                }
            }
            .navigationTitle("Conversaciones").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarLeading) { Button("Cerrar", action: cerrar) } }
            .task { (lista, archivo) = await model.conversaciones(); cargado = true }
        }
    }
}
