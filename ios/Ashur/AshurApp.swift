import SwiftUI

@main
struct AshurApp: App {
    @State private var session = Session()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(session)
                .tint(Color.accentColor)
        }
        .onChange(of: phase) {
            if phase == .background { session.lock() }
        }
    }
}

/// Estado de la sesión: la clave (leída del Keychain) y el cliente del núcleo.
/// Los modelos viven aquí y no en las vistas: al abrir o cerrar el teléfono cambia la jerarquía de vistas,
/// pero la conversación (y lo que Sergio estaba escribiendo) no se pierde.
/// Tampoco se pierden al bloquear: solo sueltan el cliente (y con él la clave) hasta el siguiente desbloqueo.
@Observable
final class Session {
    private(set) var api: APIClient? {
        didSet {
            web?.close()
            web = nil
            guard let api else { return }
            web = PanelWeb(key: api.key) { [weak self] detail in
                Task { await self?.panelRejectedKey(detail) }
            }
            if let chat, let panel {
                chat.api = api
                panel.api = api
            } else {
                chat = ChatModel(api: api)
                panel = PanelModel(api: api)
            }
        }
    }
    private(set) var chat: ChatModel?
    /// El panel web (docs/ios/HIBRIDO.md). Uno por desbloqueo: se crea con el cliente y se suelta con él.
    private(set) var web: PanelWeb?
    private(set) var panel: PanelModel?
    /// Hay clave en el Keychain pero todavía no se abrió con biometría en esta ejecución.
    private(set) var locked: Bool
    private(set) var unlocking = false
    var problem: String?

    init() {
        locked = KeyStore.exists()
    }

    /// Lee la clave una vez con Touch ID al abrir la app y la deja solo en memoria para el chat.
    /// Aprobar un pedido vuelve a pedir biometría (PanelModel.approve).
    func unlock() async {
        guard locked, !unlocking else { return }
        unlocking = true
        defer { unlocking = false }
        do {
            if let key = try await KeyStore.read(reason: "Abrir Ashur") {
                api = APIClient(key: key)
            } else {
                KeyStore.delete()
                problem = "La clave guardada ya no es válida (¿cambiaron las huellas?). Escríbela otra vez."
            }
            locked = false
            problem = api == nil ? problem : nil
        } catch KeyStore.Failure.cancelled {
            problem = nil                             // Sergio canceló: se queda en la pantalla de desbloqueo, sin aviso
        } catch {
            problem = error.localizedDescription
        }
    }

    /// Al pasar a segundo plano: la clave sale de la memoria y la app vuelve a la pantalla de desbloqueo.
    /// La conversación y el borrador se conservan.
    func lock() {
        guard api != nil else { return }
        chat?.dictation.stop()
        chat?.speaker.stop()
        chat?.api = nil
        panel?.api = nil
        api = nil
        problem = nil
        locked = true
    }

    /// Comprueba la clave una sola vez con /api/whoami y, si es la admin, la guarda protegida con biometría.
    func connect(with key: String) async -> String? {
        let candidate = APIClient(key: key)
        do {
            let who: WhoAmI = try await candidate.get("/api/whoami")
            guard who.role == "admin" else {
                return "Esa clave es de rol «\(who.role)». La app necesita la clave admin para aprobar pedidos."
            }
            try KeyStore.save(key)
            api = candidate
            problem = nil
            return nil
        } catch {
            return error.localizedDescription
        }
    }

    /// El panel avisó un 401 o un 429. Se comprueba la clave una vez: solo si el núcleo de verdad ya no la acepta
    /// se olvida (un 429 es la puerta bloqueada por intentos, no una clave mala).
    func panelRejectedKey(_ detail: String) async {
        guard let api else { return }
        do {
            let _: WhoAmI = try await api.get("/api/whoami")
            problem = detail.isEmpty ? nil : detail
        } catch APIClient.Failure.wrongKey {
            forgetKey()
            problem = "El núcleo ya no acepta la clave guardada. Escríbela otra vez."
        } catch {
            problem = error.localizedDescription
        }
    }

    func forgetKey() {
        KeyStore.delete()
        api = nil
        chat = nil
        panel = nil
        locked = false
    }
}

struct RootView: View {
    @Environment(Session.self) private var session

    var body: some View {
        if session.locked {
            UnlockView()
        } else if session.api != nil, let chat = session.chat, let web = session.web {
            AdaptiveRoot(chat: chat, web: web)
        } else {
            KeySetupView()
        }
    }
}

/// Funcionalidad (b), híbrida desde docs/ios/HIBRIDO.md. Se decide por size class, no por orientación:
/// pantalla externa del Duo (cerrado) → compacto: el chat, y «Panel» abre el panel web a pantalla completa;
/// pantalla interna (abierto) → regular: chat nativo | panel web.
struct AdaptiveRoot: View {
    let chat: ChatModel
    let web: PanelWeb
    @Environment(\.horizontalSizeClass) private var sizeClass
    @State private var columns: NavigationSplitViewVisibility = .all
    @State private var panelOpen = false

    var body: some View {
        if sizeClass == .regular {
            NavigationSplitView(columnVisibility: $columns) {
                ChatView(model: chat)
                    .navigationSplitViewColumnWidth(min: 320, ideal: 420)
            } detail: {
                PanelWebView(web: web).ignoresSafeArea(edges: .bottom)
            }
            .navigationSplitViewStyle(.balanced)
        } else {
            NavigationStack {
                ChatView(model: chat)
                    .toolbar {
                        ToolbarItem(placement: .topBarLeading) {
                            Button("Panel", systemImage: "square.grid.2x2") { panelOpen = true }
                        }
                    }
            }
            .fullScreenCover(isPresented: $panelOpen) {
                NavigationStack {
                    PanelWebView(web: web)
                        .ignoresSafeArea(edges: .bottom)
                        .navigationTitle("Panel")
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar {
                            ToolbarItem(placement: .topBarLeading) {
                                Button("Chat", systemImage: "chevron.left") { panelOpen = false }
                            }
                        }
                }
            }
        }
    }
}
