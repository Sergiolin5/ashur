import SwiftUI

struct ChatView: View {
    @Bindable var model: ChatModel
    @Environment(Session.self) private var session
    @FocusState private var focused: Bool

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if model.loading && model.lines.isEmpty {
                        ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
                    }
                    ForEach(model.lines) { line in
                        Bubble(line: line).id(line.id)
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
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: model.lines.count) {
                withAnimation { proxy.scrollTo(model.lines.last?.id, anchor: .bottom) }
            }
        }
        .safeAreaInset(edge: .bottom) { composer }
        .navigationTitle("Ashur")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button("Recargar historial", systemImage: "arrow.clockwise") {
                        Task { await model.loadHistory() }
                    }
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
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField("Escribe a Ashur", text: $model.draft, axis: .vertical)
                .lineLimit(1...5)
                .focused($focused)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(.fill.tertiary, in: RoundedRectangle(cornerRadius: 18))
            Button {
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
