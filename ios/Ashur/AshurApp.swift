import SwiftUI

@main
struct AshurApp: App {
    @State private var session = Session()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(session)
                .tint(Color.accentColor)
        }
    }
}

/// Estado de la sesión: la clave (leída del Keychain) y el cliente del núcleo.
/// Los modelos viven aquí y no en las vistas: al abrir o cerrar el teléfono cambia la jerarquía de vistas,
/// pero la conversación (y lo que Sergio estaba escribiendo) no se pierde.
@Observable
final class Session {
    private(set) var api: APIClient? {
        didSet {
            chat = api.map(ChatModel.init(api:))
            panel = api.map(PanelModel.init(api:))
        }
    }
    private(set) var chat: ChatModel?
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
        } catch {
            problem = error.localizedDescription      // nil si Sergio canceló: se queda en la pantalla de desbloqueo
        }
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

    func forgetKey() {
        KeyStore.delete()
        api = nil
        locked = false
    }
}

struct RootView: View {
    @Environment(Session.self) private var session

    var body: some View {
        if let chat = session.chat, let panel = session.panel {
            AdaptiveRoot(chat: chat, panel: panel)
        } else if session.locked {
            UnlockView()
        } else {
            KeySetupView()
        }
    }
}

/// Funcionalidad (b). Se decide por size class (guía de Apple), no por orientación ni por el tamaño de pantalla:
/// pantalla externa del Duo (cerrado) → compacto, solo conversación;
/// pantalla interna (abierto) → regular, dos paneles: conversación | memoria, hábitos y pedidos.
/// No se usa la bisagra: con size classes basta.
struct AdaptiveRoot: View {
    let chat: ChatModel
    let panel: PanelModel
    @Environment(\.horizontalSizeClass) private var sizeClass
    @State private var columns: NavigationSplitViewVisibility = .all

    var body: some View {
        if sizeClass == .regular {
            NavigationSplitView(columnVisibility: $columns) {
                ChatView(model: chat)
                    .navigationSplitViewColumnWidth(min: 320, ideal: 420)
            } detail: {
                NavigationStack { PanelView(model: panel) }
            }
            .navigationSplitViewStyle(.balanced)
        } else {
            NavigationStack {
                ChatView(model: chat)
            }
        }
    }
}
