import SwiftUI

/// Ajustes › Comandos (data-page="cmds") y Conectar un equipo (data-page="connect"), nativos. Los comandos son textos
/// fijos del panel (CMDS y CONNECT en core/ui/index.html): la app los lee del propio panel, no los copia, así nunca se
/// desfasan (docs/ios/NATIVO.md).
enum PanelTextos {
    /// [(título, dónde, [(para qué, comando)])]
    static func comandos(_ html: String) -> [(String, String, [(String, String)])] {
        guard let i = html.range(of: "  const CMDS = "), let j = html.range(of: "\n  ];", range: i.upperBound..<html.endIndex) else { return [] }
        var lit = String(html[i.upperBound..<j.upperBound]).trimmingCharacters(in: .whitespacesAndNewlines)
        if lit.hasSuffix(";") { lit.removeLast() }
        lit = lit.replacingOccurrences(of: #",\s*\]"#, with: "]", options: .regularExpression)
        guard let d = lit.data(using: .utf8), let arr = try? JSONSerialization.jsonObject(with: d) as? [[Any]] else { return [] }
        return arr.compactMap { g in
            guard g.count >= 3, let t = g[0] as? String, let w = g[1] as? String, let items = g[2] as? [[String]] else { return nil }
            return (t, w, items.compactMap { $0.count >= 2 ? ($0[0], $0[1]) : nil })
        }
    }

    /// CONNECT: [(clave, nombre, dónde, comando)]
    static func conectar(_ html: String) -> [(String, String, String, String)] {
        guard let i = html.range(of: "  const CONNECT = {"), let j = html.range(of: "\n  };", range: i.upperBound..<html.endIndex) else { return [] }
        let cuerpo = String(html[i.upperBound..<j.lowerBound])
        let re = try! NSRegularExpression(pattern: #"(\w+):\s*\[\s*"((?:[^"\\]|\\.)*)",\s*"((?:[^"\\]|\\.)*)",\s*"((?:[^"\\]|\\.)*)"\s*\]"#)
        return re.matches(in: cuerpo, range: NSRange(cuerpo.startIndex..., in: cuerpo)).compactMap { m in
            let g = (1...4).compactMap { Range(m.range(at: $0), in: cuerpo).map { String(cuerpo[$0]).replacingOccurrences(of: "\\\\", with: "\\") } }
            return g.count == 4 ? (g[0], g[1], g[2], g[3]) : nil
        }
    }
}

struct ComandosView: View {
    @Environment(Session.self) private var session
    @State private var grupos: [(String, String, [(String, String)])] = []
    @State private var copiado: String?
    @State private var cargado = false

    var body: some View {
        List {
            if cargado && grupos.isEmpty { Text("No pude leer los comandos del panel.") }
            ForEach(grupos.indices, id: \.self) { gi in
                let g = grupos[gi]
                Section {
                    DisclosureGroup(g.0) {
                        ForEach(g.2.indices, id: \.self) { ii in
                            let c = g.2[ii]
                            VStack(alignment: .leading, spacing: 4) {
                                Text(c.0).font(.subheadline)
                                Text(g.1).font(.caption2).foregroundStyle(.secondary)
                                HStack(alignment: .top) {
                                    Text(c.1).font(.caption.monospaced()).textSelection(.enabled)
                                    Spacer()
                                    Button(copiado == c.1 ? "Copiado ✓" : "Copiar") { UIPasteboard.general.string = c.1; copiado = c.1 }.buttonStyle(.bordered).font(.caption)
                                }
                            }
                        }
                    }
                }
            }
            Section { Nota(texto: "Los instaladores solo funcionan dentro de tu tailnet. Ninguno de estos comandos lleva claves. Pégalos de uno en uno.") }
        }
        .task {
            if let d = try? await session.api?.data("/") { grupos = PanelTextos.comandos(String(decoding: d, as: UTF8.self)) }
            cargado = true
        }
    }
}

struct ConectarView: View {
    @Environment(Session.self) private var session
    @State private var sistemas: [(String, String, String, String)] = []
    @State private var os = "windows"
    @State private var vistos: Set<String>?
    @State private var nodos: [Nodo] = []
    @State private var copiado = false

    var body: some View {
        let actual = sistemas.first { $0.0 == os }
        let nuevos = vistos.map { v in nodos.filter { !v.contains($0.device_id) } } ?? []
        List {
            Section {
                Text("1. Elige qué equipo vas a conectar").font(.headline)
                Picker("Sistema", selection: $os) { ForEach(sistemas, id: \.0) { Text($0.1).tag($0.0) } }.pickerStyle(.segmented)
                Text("2. Pega esto en el equipo").font(.headline)
                if let a = actual {
                    Text(a.2).font(.caption).foregroundStyle(.secondary)
                    HStack(alignment: .top) {
                        Text(a.3).font(.caption.monospaced()).textSelection(.enabled)
                        Spacer()
                        Button(copiado ? "Copiado ✓" : "Copiar") { UIPasteboard.general.string = a.3; copiado = true }.buttonStyle(.bordered)
                    }
                }
                Text("El equipo tiene que estar en tu red de Tailscale. Instala lo que falte y arranca solo.").font(.caption).foregroundStyle(.secondary)
                Text(nuevos.isEmpty ? "3. Espera aquí: lo verás aparecer" : nuevos.map(\.device_id).joined(separator: ", ") + " ya está conectado").font(.headline)
                    .foregroundStyle(nuevos.isEmpty ? .primary : Color.green)
                Text(nuevos.isEmpty ? "Reviso cada 10 segundos." : "Siguiente paso: enciende sus discos.").font(.caption).foregroundStyle(.secondary)
                Text("4. Enciende sus discos").font(.headline)
                Text("Ningún disco se escanea hasta que lo marques «Incluido» en Estado → Dispositivos. Tras el primer inventario verás cuánto de lo suyo existe en una sola copia.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Conectados ahora") {
                if nodos.isEmpty { Text("Aún no hay equipos.") }
                ForEach(nodos.sorted { ($0.mode == "offline" ? 1 : 0) < ($1.mode == "offline" ? 1 : 0) }) { n in
                    LabeledContent {
                        Text(Textos.modo[n.mode] ?? n.mode)
                    } label: {
                        Text(n.device_id)
                        Text((Textos.os[n.os ?? ""] ?? (n.os ?? "")) + ((vistos.map { !$0.contains(n.device_id) } ?? false) ? " · nuevo" : ""))
                    }
                }
            }
        }
        .onChange(of: os) { copiado = false }
        .task {
            if let d = try? await session.api?.data("/") { sistemas = PanelTextos.conectar(String(decoding: d, as: UTF8.self)) }
            while !Task.isCancelled {
                if let o: Overview = try? await session.api?.get("/api/overview") {
                    nodos = o.status.nodes
                    if vistos == nil { vistos = Set(nodos.map(\.device_id)) }
                }
                try? await Task.sleep(for: .seconds(10))
            }
        }
    }
}
