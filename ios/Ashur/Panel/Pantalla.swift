import SwiftUI

/// docs/ios/NATIVO.md: cada pantalla del panel con su interruptor Nativo / Web, para comparar la misma pantalla
/// al instante. La elección se recuerda por pantalla. Las que aún no son nativas se ven solo en web.
struct Pantalla: View {
    let titulo: String
    let ruta: String                       // «t=status», como lo entiende el panel (núcleo 0.67.3)
    let web: PanelWeb
    let activa: Bool                       // solo la pestaña visible muestra el WKWebView (hay uno solo)
    let nativa: AnyView?
    @AppStorage private var modo: String

    init(_ titulo: String, ruta: String, web: PanelWeb, activa: Bool, nativa: AnyView? = nil) {
        self.titulo = titulo; self.ruta = ruta; self.web = web; self.activa = activa; self.nativa = nativa
        _modo = AppStorage(wrappedValue: "nativo", "modo." + ruta)
    }

    private var enWeb: Bool { nativa == nil || modo == "web" }

    var body: some View {
        NavigationStack {
            Group {
                if enWeb {
                    if activa { WebAqui(web: web, ruta: ruta) } else { Color.clear }
                } else if let nativa {
                    nativa
                }
            }
            .navigationTitle(titulo)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    if nativa != nil {
                        Picker("Versión", selection: $modo) {
                            Text("Nativo").tag("nativo")
                            Text("Web").tag("web")
                        }
                        .pickerStyle(.segmented)
                        .frame(maxWidth: 180)
                    } else {
                        Text("\(titulo) · solo web").font(.subheadline.weight(.semibold))
                    }
                }
            }
        }
    }
}

/// Las pestañas del panel, en su orden (núcleo: diary, cerebro, casa, status, settings).
enum PestañaPanel: String, CaseIterable, Identifiable {
    case chat, diary, cerebro, casa, status, settings
    var id: String { rawValue }
    var titulo: String {
        switch self {
        case .chat: "Chat"
        case .diary: "Diario"
        case .cerebro: "Cerebro"
        case .casa: "Casa"
        case .status: "Estado"
        case .settings: "Ajustes"
        }
    }
    var icono: String {
        switch self {
        case .chat: "bubble.left.and.bubble.right"
        case .diary: "book"
        case .cerebro: "brain"
        case .casa: "house"
        case .status: "gauge.with.dots.needle.50percent"
        case .settings: "gearshape"
        }
    }
}

/// Las pestañas del panel. Con `chat`, el chat nativo va como primera pestaña (Duo cerrado); sin él, a su lado.
struct PanelTabs: View {
    let session: Session
    let web: PanelWeb
    let conChat: Bool
    @AppStorage("pestaña") private var sel: PestañaPanel = .status

    var body: some View {
        TabView(selection: $sel) {
            ForEach(PestañaPanel.allCases.filter { conChat || $0 != .chat }) { p in
                vista(p)
                    .tabItem { Label(p.titulo, systemImage: p.icono) }
                    .tag(p)
            }
        }
        .onAppear { if !conChat && sel == .chat { sel = .status } }
    }

    @ViewBuilder private func vista(_ p: PestañaPanel) -> some View {
        switch p {
        case .chat:
            if let chat = session.chat { NavigationStack { ChatView(model: chat) } }
        case .status:
            Pantalla(p.titulo, ruta: "t=status", web: web, activa: sel == p,
                     nativa: session.estado.map { AnyView(EstadoView(model: $0)) })
        case .diary:
            Pantalla(p.titulo, ruta: "t=diary", web: web, activa: sel == p, nativa: AnyView(DiarioView()))
        case .cerebro:
            Pantalla(p.titulo, ruta: "t=cerebro", web: web, activa: sel == p, nativa: AnyView(CerebroView()))
        case .casa:
            Pantalla(p.titulo, ruta: "t=casa", web: web, activa: sel == p, nativa: AnyView(NavigationStack { CasaView(web: web) }))
        case .settings:
            Pantalla(p.titulo, ruta: "t=settings", web: web, activa: sel == p, nativa: AnyView(AjustesView(web: web)))
        default:
            Pantalla(p.titulo, ruta: "t=" + p.rawValue, web: web, activa: sel == p)
        }
    }
}
