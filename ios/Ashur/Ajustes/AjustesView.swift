import SwiftUI

/// Ajustes, nativo: el mismo menú del panel (mismo orden y textos). Las páginas nativas abren su versión nativa con
/// su propio interruptor Nativo / Web; las demás abren el panel web en esa página. docs/ios/NATIVO.md.
struct AjustesView: View {
    let web: PanelWeb

    struct Pagina: Identifiable {
        let id: String, titulo: String, sub: String, icono: String
    }
    static let paginas: [Pagina] = [
        .init(id: "claude-requests", titulo: "Por aprobar", sub: "Todo lo que espera tu sí: pedidos de Claude, encargos, lotes y hábitos", icono: "checkmark.bubble"),
        .init(id: "encargos", titulo: "Trabajo en curso", sub: "Encargos y lotes que corren en tus equipos", icono: "list.bullet.rectangle"),
        .init(id: "habitos", titulo: "Hábitos", sub: "Lo que sueles hacer y lo que Ashur te sugiere", icono: "repeat"),
        .init(id: "space", titulo: "Archivos y espacio", sub: "Duplicados y los archivos más grandes", icono: "externaldrive"),
        .init(id: "map", titulo: "Mapa de archivos", sub: "Dónde se va el espacio, carpeta por carpeta", icono: "map"),
        .init(id: "docs", titulo: "Documentos", sub: "Buscar y releer lo que Ashur ha leído", icono: "doc.text.magnifyingglass"),
        .init(id: "memory", titulo: "Memoria", sub: "Recuerdos, resúmenes e importar", icono: "brain.head.profile"),
        .init(id: "estilo", titulo: "Tu estilo", sub: "Tus chats de WhatsApp y cómo escribes", icono: "text.quote"),
        .init(id: "formulario", titulo: "Formulario", sub: "Para que Ashur te conozca", icono: "list.clipboard"),
        .init(id: "time", titulo: "Mi tiempo", sub: "Empiezo, termino y en qué se va la semana", icono: "clock"),
        .init(id: "voices", titulo: "Personas", sub: "Cómo te habla Ashur en cada dispositivo", icono: "person.2"),
        .init(id: "access", titulo: "Acceso", sub: "Invitados y claves de chat", icono: "key"),
        .init(id: "connect", titulo: "Conectar un equipo", sub: "Paso a paso, y ves cuando queda listo", icono: "link"),
        .init(id: "cmds", titulo: "Comandos", sub: "Para pegar en cada equipo, con un toque", icono: "terminal"),
        .init(id: "update", titulo: "Actualización y seguridad", sub: "Versiones y autenticador", icono: "checkmark.shield"),
        .init(id: "diag", titulo: "Diagnóstico", sub: "Errores y por qué un equipo no está al día", icono: "waveform.path.ecg"),
        .init(id: "notes", titulo: "Notas del panel", sub: "Problemas e ideas que anotaste", icono: "flag"),
    ]

    var body: some View {
        List(Self.paginas) { p in
            NavigationLink {
                SubPagina(pagina: p, web: web)
            } label: {
                Label {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(p.titulo)
                        Text(p.sub).font(.caption).foregroundStyle(.secondary)
                    }
                } icon: { Image(systemName: p.icono) }
            }
        }
    }

    /// La vista nativa de una página, si ya existe (etapa 2: Pedidos, Trabajo en curso, Actualización, Diagnóstico).
    @ViewBuilder static func nativa(_ id: String) -> some View {
        switch id {
        case "claude-requests": PedidosView()
        case "encargos": TrabajoView()
        case "update": ActualizacionView()
        case "diag": DiagnosticoView()
        default: EmptyView()
        }
    }
    static let nativas: Set<String> = ["claude-requests", "encargos", "update", "diag"]
}

/// Una página de Ajustes con su interruptor Nativo / Web (recordado por página).
private struct SubPagina: View {
    let pagina: AjustesView.Pagina
    let web: PanelWeb
    @AppStorage private var modo: String

    init(pagina: AjustesView.Pagina, web: PanelWeb) {
        self.pagina = pagina; self.web = web
        _modo = AppStorage(wrappedValue: "nativo", "modo.t=settings&p=" + pagina.id)
    }

    private var nativa: Bool { AjustesView.nativas.contains(pagina.id) }

    var body: some View {
        Group {
            if nativa && modo == "nativo" {
                AjustesView.nativa(pagina.id)
            } else {
                WebAqui(web: web, ruta: "t=settings&p=" + pagina.id)
            }
        }
        .navigationTitle(pagina.titulo)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                if nativa {
                    Picker("Versión", selection: $modo) {
                        Text("Nativo").tag("nativo")
                        Text("Web").tag("web")
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 130)
                } else {
                    Text("solo web").font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }
}
