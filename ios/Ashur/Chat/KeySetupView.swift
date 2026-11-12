import SwiftUI

/// Sergio escribe la clave admin una vez; se comprueba una sola vez y se guarda en el Keychain.
struct KeySetupView: View {
    @Environment(Session.self) private var session
    @State private var key = ""
    @State private var checking = false
    @State private var message: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SecureField("Clave admin", text: $key)
                        .textContentType(.password)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                } footer: {
                    Text("Se guarda solo en el Keychain de este teléfono. Diez claves erróneas bloquean la puerta del núcleo 15 minutos.")
                }
                if let message = message ?? session.problem {
                    Section { Text(message).foregroundStyle(.red) }
                }
                Section {
                    Button {
                        Task {
                            checking = true
                            message = await session.connect(with: key.trimmingCharacters(in: .whitespacesAndNewlines))
                            checking = false
                        }
                    } label: {
                        HStack {
                            Text("Conectar con Ashur")
                            if checking { Spacer(); ProgressView() }
                        }
                    }
                    .disabled(key.isEmpty || checking)
                }
            }
            .navigationTitle("Ashur")
        }
    }
}
