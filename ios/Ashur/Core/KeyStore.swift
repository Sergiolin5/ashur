import Foundation
import LocalAuthentication
import Security

/// La clave admin vive solo en el Keychain de este teléfono, protegida con biometría (.biometryCurrentSet):
/// leerla dispara Touch ID en el Duo y Face ID en el 15 Pro. Si cambian las huellas o caras registradas,
/// el ítem queda inválido y Sergio vuelve a escribir la clave.
enum KeyStore {
    private static let service = "com.sergiolin5.ashur"
    private static let account = "admin-key"

    enum Failure: LocalizedError {
        case cancelled
        case noBiometry(String)
        case status(OSStatus)

        var errorDescription: String? {
            switch self {
            case .cancelled:
                return nil
            case .noBiometry(let why):
                return "No puedo proteger la clave con biometría: \(why)"
            case .status(let s):
                return (SecCopyErrorMessageString(s, nil) as String?) ?? "Error del Keychain (\(s))."
            }
        }
    }

    private static var baseQuery: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    /// ¿Hay una clave guardada? Lee solo los atributos, que no están protegidos: no pide biometría.
    static func exists() -> Bool {
        var q = baseQuery
        q[kSecReturnAttributes as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        return SecItemCopyMatching(q as CFDictionary, nil) == errSecSuccess
    }

    static func save(_ key: String) throws {
        let context = LAContext()
        var laError: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &laError) else {
            throw Failure.noBiometry(laError?.localizedDescription ?? "este teléfono no tiene Touch ID ni Face ID configurado.")
        }
        var cfError: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly,
                                                           .biometryCurrentSet, &cfError) else {
            throw cfError!.takeRetainedValue() as Error
        }
        delete()
        var q = baseQuery
        q[kSecValueData as String] = Data(key.utf8)
        q[kSecAttrAccessControl as String] = access
        let s = SecItemAdd(q as CFDictionary, nil)
        guard s == errSecSuccess else { throw Failure.status(s) }
    }

    /// Lee la clave tras pedir biometría con `reason` (el texto que ve Sergio bajo Touch ID).
    /// Cada llamada crea un LAContext nuevo y sin reutilización: cada aprobación pide el dedo otra vez.
    /// Devuelve nil si no hay clave guardada (o si quedó inválida porque cambiaron las huellas).
    static func read(reason: String) async throws -> String? {
        let context = LAContext()
        context.touchIDAuthenticationAllowableReuseDuration = 0
        context.localizedCancelTitle = "Cancelar"
        do {
            try await context.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason)
        } catch let e as LAError where [.userCancel, .systemCancel, .appCancel].contains(e.code) {
            throw Failure.cancelled
        } catch let e as LAError where [.biometryNotEnrolled, .biometryNotAvailable].contains(e.code) {
            throw Failure.noBiometry(e.localizedDescription)
        }
        var q = baseQuery
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        q[kSecUseAuthenticationContext as String] = context      // ya autenticado: no vuelve a preguntar
        var out: CFTypeRef?
        let s = SecItemCopyMatching(q as CFDictionary, &out)
        if s == errSecItemNotFound { return nil }
        if s == errSecUserCanceled { throw Failure.cancelled }
        guard s == errSecSuccess, let data = out as? Data else { throw Failure.status(s) }
        return String(decoding: data, as: UTF8.self)
    }

    static func delete() {
        SecItemDelete(baseQuery as CFDictionary)
    }
}
