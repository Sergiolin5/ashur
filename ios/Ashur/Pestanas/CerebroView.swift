import SwiftUI
import SceneKit

/// Pestaña Cerebro, nativa (etapa 5 de docs/ios/NATIVO.md): el mismo grafo de /api/cerebro en 3D con SceneKit, la
/// vista «Núcleo» (el mismo acomodo que cbTargets() del panel), los filtros, la ficha al tocar un punto y la actividad
/// real (lo que Ashur usa al pensar se enciende). La vista «Red» (fuerzas) queda en el panel web.
struct CerebroView: View {
    @Environment(Session.self) private var session

    struct Grafo: Decodable {
        let nodos: [Nodo]; let aristas: [[String]]; let totales: Totales
        struct Totales: Decodable { let recuerdos: Int; let personas: Int; let conversaciones: Int }
    }
    struct Nodo: Decodable, Identifiable {
        let id: String; let tipo: String; let grupo: String?; let texto: String
        let fecha: Double?; let tier: String?; let fuente: String?
        let dueno: String?
        enum CodingKeys: String, CodingKey { case id, tipo, grupo, texto, fecha, tier, fuente, dueno = "dueño" }
    }
    struct Actividad: Decodable { let eventos: [Evento]; let hasta: Int?; struct Evento: Decodable { let ts: Double; let tipo: String; let quien: String?; let recuerdos: [Int]? } }

    @State private var grafo: Grafo?
    @State private var filtro = "todo"
    @State private var elegido: Nodo?
    @State private var problema: String?
    @State private var escena = Escena()
    @State private var hasta: Int?
    @State private var pensoEn: Double?

    var body: some View {
        VStack(spacing: 8) {
            Picker("Filtro", selection: $filtro) {
                Text("Todo").tag("todo"); Text("Tuyos").tag("yo"); Text("Invitados").tag("inv"); Text("Hoy").tag("hoy")
            }
            .pickerStyle(.segmented).padding(.horizontal)
            ZStack(alignment: .top) {
                VistaEscena(escena: escena) { id in elegido = grafo?.nodos.first { $0.id == id } }
                if let t = grafo?.totales {
                    Text("\(t.recuerdos) recuerdos · \(t.personas) " + (t.personas == 1 ? "persona" : "personas") + " · \(t.conversaciones) conversaciones"
                         + (pensoEn.map { " · pensó " + Textos.hace(Date().timeIntervalSince1970 - $0) } ?? ""))
                        .font(.caption).padding(6).background(.ultraThinMaterial, in: Capsule()).padding(.top, 6)
                }
                if let problema { Text(problema).font(.footnote).padding().background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 10)).padding(.top, 60) }
                else if grafo == nil { ProgressView("Cargando el cerebro…").padding(.top, 60) }
            }
            leyenda
            if let n = elegido { ficha(n) }
        }
        .onChange(of: filtro) { dibujar() }
        .task {
            await cargar()
            if let a: Actividad = try? await session.api?.get("/api/cerebro/actividad") { hasta = a.hasta }
            while !Task.isCancelled {                         // cada 2 s, como el panel: solo eventos reales
                try? await Task.sleep(for: .seconds(2))
                await actividad()
            }
        }
    }

    static let colores: [String: UIColor] = [
        "core": .init(hex: 0xc9c6ff), "trabajo": .init(hex: 0x8f8cff), "gustos": .init(hex: 0x4fd1b5), "ashur": .init(hex: 0xc6c2ff),
        "privado": .init(hex: 0x6f6e86), "sin tema": .init(hex: 0x55546c), "persona": .init(hex: 0xff9e7a), "conversacion": .init(hex: 0x7cb8ff),
        "trato": .init(hex: 0xf48fb8), "correccion": .init(hex: 0xffb86b), "rasgo": .init(hex: 0xb6a6ff), "guestmem": .init(hex: 0xffc2a8)]
    static let clases = ["core": "Ashur", "persona": "Persona", "conversacion": "Conversación", "recuerdo": "Recuerdo",
                         "trato": "Trato aprendido", "correccion": "Corrección", "rasgo": "Rasgo de su persona"]

    static func color(_ n: Nodo) -> UIColor {
        if n.tipo == "recuerdo" { return (n.dueno != nil && n.dueno != "yo") ? colores["guestmem"]! : (colores[n.grupo ?? ""] ?? colores["sin tema"]!) }
        return colores[n.tipo] ?? .white
    }

    private var leyenda: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 10) {
                ForEach([("trabajo", "trabajo"), ("gustos", "gustos"), ("ashur", "Ashur"), ("privado", "privado"), ("persona", "personas"),
                         ("conversacion", "conversaciones"), ("trato", "aprendido")], id: \.0) { par in
                    HStack(spacing: 4) { Circle().fill(Color(Self.colores[par.0]!)).frame(width: 8, height: 8); Text(par.1).font(.caption2) }
                }
            }.padding(.horizontal)
        }
    }

    private func ficha(_ n: Nodo) -> some View {
        let enlaces = (grafo?.aristas ?? []).filter { $0.count >= 2 && ($0[0] == n.id || $0[1] == n.id) }.count
        let clase = n.tipo == "recuerdo"
            ? "Recuerdo · " + ((n.dueno ?? "yo") == "yo" ? (n.grupo ?? "") : "de " + (grafo?.nodos.first { $0.id == n.dueno }?.texto ?? "un invitado"))
            : (Self.clases[n.tipo] ?? n.tipo)
        var bits: [String] = []
        if let f = n.fecha { bits.append(Date(timeIntervalSince1970: f).formatted(.dateTime.day().month(.abbreviated).year())) }
        if let f = n.fuente, !f.isEmpty { bits.append("de " + f) }
        if enlaces > 0 { bits.append("\(enlaces) " + (enlaces == 1 ? "conexión" : "conexiones")) }
        return VStack(alignment: .leading, spacing: 4) {
            Text(clase + (n.tier == "local" ? " · solo local" : "")).font(.caption).foregroundStyle(.secondary)
            Text(n.texto)
            Text(bits.joined(separator: " · ")).font(.caption2).foregroundStyle(.secondary)
            Button("Cerrar") { elegido = nil }.font(.caption)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding().background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12)).padding(.horizontal)
    }

    /// cbVisible() del panel.
    private func visible(_ n: Nodo) -> Bool {
        switch filtro {
        case "yo": return n.tipo == "core" || n.id == "yo" || n.dueno == "yo" || (n.tipo == "conversacion" && n.grupo == "yo") || ["trato", "correccion", "rasgo"].contains(n.tipo)
        case "inv": return n.tipo == "core" || (n.tipo == "persona" && n.id != "yo") || (n.dueno != nil && n.dueno != "yo") || (n.tipo == "conversacion" && n.grupo != "yo")
        case "hoy": return n.tipo == "core" || (n.fecha ?? 0) > Date().timeIntervalSince1970 - 86400 || n.tipo == "persona"
        default: return true
        }
    }

    private func cargar() async {
        guard let api = session.api else { return }
        do { grafo = try await api.get("/api/cerebro"); problema = nil; dibujar() }
        catch {
            if grafo == nil { problema = "No pude cargar el cerebro. Lo intento de nuevo en unos segundos."; try? await Task.sleep(for: .seconds(5)); await cargar() }
        }
    }

    private func dibujar() {
        guard let g = grafo else { return }
        let ns = g.nodos.filter(visible)
        escena.construir(ns, Set(ns.map(\.id)), g.aristas)
    }

    private func actividad() async {
        guard let api = session.api, let h = hasta,
              let a: Actividad = try? await api.get("/api/cerebro/actividad", query: [URLQueryItem(name: "desde", value: String(h))]) else { return }
        hasta = a.hasta ?? h
        var recargar = false
        for ev in a.eventos {
            switch ev.tipo {
            case "pensó":
                pensoEn = ev.ts
                escena.encender([ev.quien ?? "yo", "core"] + (ev.recuerdos ?? []).map { "m\($0)" })
            case "aprendió", "cerró", "corrigió", "persona": recargar = true
            default: break
            }
        }
        if recargar { await cargar() }
    }
}

/// La escena: un punto por nodo en la posición de cbTargets() y líneas tenues entre los conectados.
@Observable
final class Escena {
    let scene = SCNScene()
    private let raiz = SCNNode()
    private var puntos: [String: SCNNode] = [:]

    init() {
        scene.background.contents = UIColor.clear
        scene.rootNode.addChildNode(raiz)
        let cam = SCNNode(); cam.camera = SCNCamera(); cam.camera?.zFar = 200; cam.position = SCNVector3(0, 0, 22)
        scene.rootNode.addChildNode(cam)
    }

    private static func fib(_ i: Int, _ n: Int) -> SIMD3<Float> {
        let y = 1 - (Float(i) + 0.5) / Float(n) * 2, r = (1 - y * y).squareRoot(), th = Float(i) * 2.39996
        return SIMD3(cos(th) * r, y, sin(th) * r)
    }

    func construir(_ ns: [CerebroView.Nodo], _ ids: Set<String>, _ aristas: [[String]]) {
        raiz.childNodes.forEach { $0.removeFromParentNode() }
        puntos = [:]
        let temas: [String: SIMD3<Float>] = ["trabajo": [1, 0.2, 0.2], "gustos": [-1, 0.2, 0.3], "ashur": [0.1, 1, -0.2], "privado": [0, -1, 0.2], "sin tema": [0.2, -0.3, -1]]
        let personas = ns.filter { $0.tipo == "persona" }
        var pdir: [String: SIMD3<Float>] = [:]
        for (i, p) in personas.enumerated() { pdir[p.id] = p.id == "yo" ? [0, 0, 1] : Self.fib(i, personas.count + 1) }
        var grupos: [String: [CerebroView.Nodo]] = [:], orden: [String] = []
        for n in ns {
            let k = n.tipo + "|" + (n.tipo == "recuerdo" ? ((n.dueno ?? "yo") == "yo" ? (n.grupo ?? "") : (n.dueno ?? "")) : n.tipo == "conversacion" ? (n.grupo ?? "") : "")
            if grupos[k] == nil { orden.append(k) }
            grupos[k, default: []].append(n)
        }
        var pos: [String: SCNVector3] = [:]
        for k in orden {
            let lista = grupos[k]!
            for (i, n) in lista.enumerated() {
                if n.tipo == "core" { pos[n.id] = SCNVector3(0, 0, 0); continue }
                var dir: SIMD3<Float>, R: Float, spread: Float
                if ["trato", "correccion", "rasgo"].contains(n.tipo) { dir = Self.fib(i, lista.count); R = 1.9; spread = 0 }
                else if n.tipo == "persona" { dir = pdir[n.id] ?? [0, 0, 1]; R = 3.3; spread = 0 }
                else if n.tipo == "conversacion" { dir = pdir[n.grupo ?? ""] ?? [0, 0, 1]; R = 4.6; spread = 0.9 }
                else {
                    dir = (n.dueno ?? "yo") == "yo" ? (temas[n.grupo ?? ""] ?? temas["sin tema"]!) : (pdir[n.dueno ?? ""] ?? [0, 0, -1])
                    R = 6.6 + Float(i % 5) * 0.35; spread = min(1.25, 0.35 + Float(lista.count) / 160)
                }
                var v = dir + Self.fib(i, max(1, lista.count)) * spread
                let L = (v.x * v.x + v.y * v.y + v.z * v.z).squareRoot(); if L > 0 { v /= L }
                pos[n.id] = SCNVector3(v.x * R, v.y * R, v.z * R)
            }
        }
        // aristas: una sola geometría de líneas
        var verts: [SCNVector3] = [], idx: [Int32] = []
        for a in aristas where a.count >= 2 && ids.contains(a[0]) && ids.contains(a[1]) {
            guard let p = pos[a[0]], let q = pos[a[1]] else { continue }
            idx.append(Int32(verts.count)); verts.append(p); idx.append(Int32(verts.count)); verts.append(q)
        }
        if !verts.isEmpty {
            let geo = SCNGeometry(sources: [SCNGeometrySource(vertices: verts)], elements: [SCNGeometryElement(indices: idx, primitiveType: .line)])
            let m = SCNMaterial(); m.diffuse.contents = UIColor.white.withAlphaComponent(0.12); m.lightingModel = .constant
            geo.materials = [m]
            raiz.addChildNode(SCNNode(geometry: geo))
        }
        for n in ns {
            let r: CGFloat = n.tipo == "core" ? 0.42 : n.tipo == "persona" ? 0.25 : n.tipo == "conversacion" ? 0.16 : ["trato", "correccion", "rasgo"].contains(n.tipo) ? 0.15 : 0.12
            let s = SCNSphere(radius: r); s.segmentCount = 10
            let m = SCNMaterial(); m.diffuse.contents = CerebroView.color(n); m.emission.contents = CerebroView.color(n).withAlphaComponent(0.35); m.lightingModel = .constant
            s.materials = [m]
            let nodo = SCNNode(geometry: s); nodo.position = pos[n.id] ?? SCNVector3Zero; nodo.name = n.id
            raiz.addChildNode(nodo); puntos[n.id] = nodo
        }
    }

    /// Lo que Ashur usó al pensar se agranda un momento y vuelve.
    func encender(_ ids: [String]) {
        for id in ids {
            guard let n = puntos[id] else { continue }
            n.removeAllActions()
            n.runAction(.sequence([.scale(to: 2.2, duration: 0.25), .scale(to: 1, duration: 1.6)]))
        }
    }
}

/// SCNView con giro y zoom de la cámara, y toque para elegir un punto.
private struct VistaEscena: UIViewRepresentable {
    let escena: Escena
    let alTocar: (String) -> Void

    func makeCoordinator() -> Coord { Coord(alTocar) }

    func makeUIView(context: Context) -> SCNView {
        let v = SCNView()
        v.scene = escena.scene
        v.backgroundColor = .clear
        v.allowsCameraControl = true
        v.autoenablesDefaultLighting = false
        v.antialiasingMode = .multisampling2X
        v.addGestureRecognizer(UITapGestureRecognizer(target: context.coordinator, action: #selector(Coord.toque(_:))))
        return v
    }

    func updateUIView(_ v: SCNView, context: Context) { context.coordinator.alTocar = alTocar }

    final class Coord: NSObject {
        var alTocar: (String) -> Void
        init(_ f: @escaping (String) -> Void) { alTocar = f }
        @objc func toque(_ g: UITapGestureRecognizer) {
            guard let v = g.view as? SCNView else { return }
            if let id = v.hitTest(g.location(in: v), options: [.searchMode: SCNHitTestSearchMode.closest.rawValue]).first?.node.name { alTocar(id) }
        }
    }
}

extension UIColor {
    convenience init(hex: Int) {
        self.init(red: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255, blue: CGFloat(hex & 0xff) / 255, alpha: 1)
    }
}
