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
