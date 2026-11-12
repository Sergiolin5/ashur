import Foundation
import Security

/// La clave admin vive solo en el Keychain de este teléfono (nunca en el código ni en UserDefaults).
/// Funcionalidad (a): ítem simple, solo en este dispositivo. La (c) lo protegerá con biometría.
enum KeyStore {
    private static let service = "com.sergiolin5.ashur"
    private static let account = "admin-key"

    enum Failure: LocalizedError {
        case status(OSStatus)
        var errorDescription: String? {
            if case .status(let s) = self {
                return (SecCopyErrorMessageString(s, nil) as String?) ?? "Error del Keychain (\(s))."
            }
            return nil
        }
    }

    private static var baseQuery: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    static func save(_ key: String) throws {
        delete()
        var q = baseQuery
        q[kSecValueData as String] = Data(key.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        let s = SecItemAdd(q as CFDictionary, nil)
        guard s == errSecSuccess else { throw Failure.status(s) }
    }

    static func read() throws -> String? {
        var q = baseQuery
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        let s = SecItemCopyMatching(q as CFDictionary, &out)
        if s == errSecItemNotFound { return nil }
        guard s == errSecSuccess, let data = out as? Data else { throw Failure.status(s) }
        return String(decoding: data, as: UTF8.self)
    }

    static func delete() {
        SecItemDelete(baseQuery as CFDictionary)
    }
}
