import SwiftUI
import WebKit

/// docs/ios/HIBRIDO.md, paso 2: el panel web del núcleo dentro de la app («modo app» del panel, núcleo 0.63.2).
/// Hay uno por desbloqueo y vive en Session: abrir o cerrar el Duo no lo recarga. Al bloquear se suelta, y con él
/// su almacén no persistente: la clave solo existe en memoria, nunca en el disco del teléfono.
final class PanelWeb: NSObject, WKNavigationDelegate {
    let view: WKWebView
    private let handler = MessageHandler()
    private let onKeyRejected: (String) -> Void

    init(key: String, onKeyRejected: @escaping (String) -> Void) {
        self.onKeyRejected = onKeyRejected
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        // La clave va como literal JSON, nunca interpolada a mano.
        let literal = (try? JSONEncoder().encode(key)).flatMap { String(data: $0, encoding: .utf8) } ?? "null"
        config.userContentController.addUserScript(
            WKUserScript(source: "window.__ASHUR_KEY = \(literal);", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        config.userContentController.add(handler, name: "ashur")
        view = WKWebView(frame: .zero, configuration: config)
        super.init()
        handler.target = self
        view.navigationDelegate = self
        #if DEBUG
        view.isInspectable = true                    // depurar con Safari › Desarrollo en la Mac
        #endif
        view.load(URLRequest(url: APIClient.baseURL))
    }

    /// Al bloquear: corta la carga y suelta el puente, para que la vista (y su almacén) se libere.
    func close() {
        view.stopLoading()
        view.configuration.userContentController.removeScriptMessageHandler(forName: "ashur")
    }

    fileprivate func receive(_ body: Any) {
        guard let m = body as? [String: Any], let tipo = m["tipo"] as? String else { return }
        switch tipo {
        case "clave_rechazada":
            onKeyRejected(m["detalle"] as? String ?? "")
        case "abrir":
            if let s = m["url"] as? String, let url = URL(string: s), url.scheme == "https" {
                UIApplication.shared.open(url)
            }
        default:
            break
        }
    }

    /// Solo se navega dentro del núcleo; cualquier otra dirección https se abre en Safari.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction) async -> WKNavigationActionPolicy {
        guard let url = action.request.url else { return .cancel }
        if url.scheme == "about" { return .allow }
        if url.scheme == "https", url.host == APIClient.baseURL.host { return .allow }
        if url.scheme == "https" { _ = await UIApplication.shared.open(url) }
        return .cancel
    }
}

/// Puente débil: WKUserContentController retiene a su handler, y así no retiene al PanelWeb.
private final class MessageHandler: NSObject, WKScriptMessageHandler {
    weak var target: PanelWeb?

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.receive(message.body)
    }
}

/// La vista SwiftUI. Envuelve el WKWebView en un contenedor para que pueda pasar de la columna (Duo abierto)
/// a la pantalla completa (Duo cerrado) sin crearse de nuevo.
struct PanelWebView: UIViewRepresentable {
    let web: PanelWeb

    func makeUIView(context: Context) -> UIView {
        let box = UIView()
        web.view.frame = box.bounds
        web.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        box.addSubview(web.view)
        return box
    }

    func updateUIView(_ box: UIView, context: Context) {
        if web.view.superview !== box {
            web.view.frame = box.bounds
            box.addSubview(web.view)
        }
    }
}
