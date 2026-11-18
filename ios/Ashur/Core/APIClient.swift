import Foundation

/// Cliente HTTP del núcleo de Ashur. La app no guarda verdad propia: todo sale de aquí.
struct APIClient {
    /// URL pública de Funnel (HTTPS, sin VPN). La de la tailnet exigiría una excepción de ATS.
    static let baseURL = URL(string: "https://zimaos.tailf478cc.ts.net")!

    let key: String
    var session: URLSession = .shared

    enum Failure: LocalizedError {
        case wrongKey                 // 401: no reintentar (10 errores bloquean la puerta 15 min)
        case server(Int, String)
        case offline(String)

        var errorDescription: String? {
            switch self {
            case .wrongKey:
                return "El núcleo no aceptó la clave. No reintento solo: diez claves erróneas bloquean la puerta 15 minutos."
            case .server(let code, let detail):
                return detail.isEmpty ? "El núcleo respondió con error \(code)." : detail
            case .offline(let why):
                return "No pude hablar con el núcleo: \(why)"
            }
        }
    }

    func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        try await send(request(path, method: "GET", query: query))
    }

    func post<T: Decodable>(_ path: String, body: some Encodable) async throws -> T {
        var req = request(path, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)
        return try await send(req)
    }

    func post<T: Decodable>(_ path: String) async throws -> T {
        try await send(request(path, method: "POST"))
    }

    func post<T: Decodable>(_ path: String, query: [URLQueryItem]) async throws -> T {
        try await send(request(path, method: "POST", query: query))
    }

    func put<T: Decodable>(_ path: String, body: some Encodable) async throws -> T {
        var req = request(path, method: "PUT")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)
        return try await send(req)
    }

    /// POST con cabeceras extra (p. ej. X-Totp al actualizar desde fuera de la tailnet).
    func post<T: Decodable>(_ path: String, body: some Encodable, headers: [String: String]) async throws -> T {
        var req = request(path, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }
        req.httpBody = try JSONEncoder().encode(body)
        return try await send(req)
    }

    /// La respuesta cruda (código y cuerpo), para flujos que leen el cuerpo de un 409 (Casa: «confirmar»).
    func crudo(_ method: String, _ path: String, body: some Encodable) async throws -> (Int, Data) {
        var req = request(path, method: method)
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)
        let (d, r) = try await session.data(for: req)
        let code = (r as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw Failure.wrongKey }
        return (code, d)
    }

    /// Sube bytes tal cual (audio del diario: POST /api/audio?kind=…).
    func upload<T: Decodable>(_ path: String, query: [URLQueryItem], data: Data) async throws -> T {
        var req = request(path, method: "POST", query: query)
        req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        req.httpBody = data
        req.timeoutInterval = 600
        return try await send(req)
    }

    /// Bytes tal cual (imágenes de las notas, que piden la clave en la cabecera).
    func data(_ path: String) async throws -> Data {
        let req = request(path, method: "GET")
        let (d, r) = try await session.data(for: req)
        let code = (r as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw Failure.wrongKey }
        guard (200..<300).contains(code) else { throw Failure.server(code, "") }
        return d
    }

    func delete<T: Decodable>(_ path: String) async throws -> T {
        try await send(request(path, method: "DELETE"))
    }

    private func request(_ path: String, method: String, query: [URLQueryItem] = []) -> URLRequest {
        var comps = URLComponents(url: Self.baseURL.appending(path: path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty { comps.queryItems = query }
        var req = URLRequest(url: comps.url!)
        req.httpMethod = method
        req.timeoutInterval = 120          // el modelo local puede tardar en contestar
        req.setValue(key, forHTTPHeaderField: "X-Admin-Key")
        return req
    }

    private func send<T: Decodable>(_ req: URLRequest) async throws -> T {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: req)
        } catch {
            throw Failure.offline(error.localizedDescription)
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw Failure.wrongKey }
        guard (200..<300).contains(code) else {
            let detail = (try? JSONDecoder().decode(ErrorBody.self, from: data))?.detail ?? ""
            throw Failure.server(code, detail)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    private struct ErrorBody: Decodable { let detail: String }
}
