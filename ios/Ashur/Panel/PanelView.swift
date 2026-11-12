import SwiftUI

struct PanelView: View {
    @Bindable var model: PanelModel

    var body: some View {
        List {
            if let error = model.error {
                Text(error).foregroundStyle(.red).font(.footnote)
            }
            switch model.tab {
            case .memoria: memoria
            case .habitos: habitos
            case .pedidos: pedidos
            }
        }
        .overlay {
            if model.loading && isEmpty { ProgressView() }
        }
        .refreshable { await model.load(model.tab) }
        .toolbar {
            ToolbarItem(placement: .principal) {
                Picker("Sección", selection: $model.tab) {
                    ForEach(PanelModel.Tab.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .fixedSize()
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .alert("Pedido", isPresented: Binding(get: { model.notice != nil }, set: { if !$0 { model.notice = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(model.notice ?? "")
        }
        .task(id: model.tab) { await model.load(model.tab) }
    }

    private var isEmpty: Bool {
        switch model.tab {
        case .memoria: model.memories.isEmpty
        case .habitos: model.habits.isEmpty
        case .pedidos: model.pedidos.isEmpty
        }
    }

    @ViewBuilder private var memoria: some View {
        if model.memories.isEmpty && !model.loading {
            ContentUnavailableView("Sin recuerdos", systemImage: "brain")
        }
        ForEach(model.memories) { m in
            VStack(alignment: .leading, spacing: 4) {
                Text(verbatim: m.text)
                if let tier = m.tier {
                    Text(tier).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    @ViewBuilder private var habitos: some View {
        if model.habits.isEmpty && !model.loading {
            ContentUnavailableView("Sin hábitos todavía", systemImage: "repeat")
        }
        ForEach(model.habits) { h in
            VStack(alignment: .leading, spacing: 4) {
                Text(verbatim: h.descripcion ?? h.accion)
                Text("\(h.veces) veces en \(h.dias) días · \(h.estado)")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder private var pedidos: some View {
        if model.pedidos.isEmpty && !model.loading {
            ContentUnavailableView("Sin pedidos", systemImage: "checkmark.seal")
        }
        ForEach(model.pedidos) { p in
            PedidoRow(pedido: p, busy: model.deciding == p.id,
                      approve: { Task { await model.approve(p) } },
                      reject: { Task { await model.reject(p) } })
        }
    }
}

struct PedidoRow: View {
    let pedido: Pedido
    var busy = false
    var approve: (() -> Void)?
    var reject: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(verbatim: pedido.label ?? pedido.action).font(.subheadline.weight(.semibold))
                Spacer()
                Text(statusText).font(.caption)
                    .foregroundStyle(pedido.isPending ? Color.accentColor : .secondary)
            }
            if !pedido.reason.isEmpty {
                Text(verbatim: pedido.reason).font(.footnote).foregroundStyle(.secondary)
            }
            if let result = pedido.result, !result.isEmpty {
                Text(verbatim: result).font(.caption).foregroundStyle(.secondary)
            }
            if pedido.isPending, let approve, let reject {
                HStack {
                    Button(action: approve) {
                        Label("Aprobar", systemImage: "touchid")
                    }
                    .buttonStyle(.borderedProminent)
                    Button("Rechazar", role: .destructive, action: reject)
                        .buttonStyle(.bordered)
                    if busy { ProgressView() }
                }
                .disabled(busy)
                .padding(.top, 4)
            }
        }
    }

    private var statusText: String {
        switch pedido.status {
        case "pending": "Pendiente"
        case "running": "En curso"
        case "rejected": "Rechazado"
        case "approved": "Aprobado"
        case "not_done": "No se hizo"
        default: pedido.status
        }
    }
}
