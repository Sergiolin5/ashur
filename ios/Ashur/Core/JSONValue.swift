import Foundation

/// Un valor JSON cualquiera: para los `detail` de la actividad, que cambian según el evento.
enum JSONValue: Decodable {
    case string(String), number(Double), bool(Bool), array([JSONValue]), object([String: JSONValue]), null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else if let v = try? c.decode([String: JSONValue].self) { self = .object(v) }
        else { self = .null }
    }

    var text: String? {
        switch self {
        case .string(let v): return v
        case .number(let v): return v == v.rounded() ? String(Int(v)) : String(v)
        case .bool(let v): return v ? "true" : "false"
        default: return nil
        }
    }
    var bool: Bool { if case .bool(let v) = self { return v }; return false }
    var list: [String] { if case .array(let v) = self { return v.compactMap(\.text) }; return [] }
}

/// Para respuestas cuyo contenido no se usa.
struct Ignorado: Decodable { init(from decoder: Decoder) throws {} }
