import LocalAuthentication
import SwiftUI

/// Pantalla de inicio cuando ya hay clave: se abre con Touch ID (Duo) o Face ID (15 Pro).
struct UnlockView: View {
    @Environment(Session.self) private var session
    @Environment(\.scenePhase) private var phase

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            Image(systemName: biometryIcon)
                .font(.system(size: 64))
                .foregroundStyle(Color.accentColor)
            Text("Ashur").font(.largeTitle.weight(.semibold))
            if let problem = session.problem {
                Text(problem).font(.footnote).foregroundStyle(.red).multilineTextAlignment(.center)
            }
            Spacer()
            Button {
                Task { await session.unlock() }
            } label: {
                Text("Abrir con \(biometryName)").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(session.unlocking)
            Button("Usar otra clave", role: .destructive) { session.forgetKey() }
                .font(.footnote)
        }
        .padding(32)
        // Pide la biometría solo con la app en primer plano (al volver de segundo plano, no antes).
        .task(id: phase) { if phase == .active { await session.unlock() } }
    }

    private var biometryType: LABiometryType {
        let c = LAContext()
        _ = c.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil)
        return c.biometryType
    }

    private var biometryName: String {
        switch biometryType {
        case .faceID: "Face ID"
        case .touchID: "Touch ID"
        case .opticID: "Optic ID"
        default: "biometría"
        }
    }

    private var biometryIcon: String {
        biometryType == .faceID ? "faceid" : "touchid"
    }
}
