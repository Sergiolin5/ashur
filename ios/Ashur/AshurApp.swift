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
@Observable
final class Session {
    private(set) var api: APIClient?
    var problem: String?

    init() {
        do {
            if let key = try KeyStore.read() { api = APIClient(key: key) }
        } catch {
            problem = error.localizedDescription
        }
    }

    /// Comprueba la clave una sola vez con /api/whoami y, si es la admin, la guarda.
    func connect(with key: String) async -> String? {
        let candidate = APIClient(key: key)
        do {
            let who: WhoAmI = try await candidate.get("/api/whoami")
            guard who.role == "admin" else {
                return "Esa clave es de rol «\(who.role)». La app necesita la clave admin para aprobar pedidos."
            }
            try KeyStore.save(key)
            api = candidate
            return nil
        } catch {
            return error.localizedDescription
        }
    }

    func forgetKey() {
        KeyStore.delete()
        api = nil
    }
}

struct RootView: View {
    @Environment(Session.self) private var session

    var body: some View {
        if let api = session.api {
            NavigationStack {
                ChatView(model: ChatModel(api: api))
            }
        } else {
            KeySetupView()
        }
    }
}
