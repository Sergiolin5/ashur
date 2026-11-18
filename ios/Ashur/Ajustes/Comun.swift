import SwiftUI

/// Piezas compartidas por las pantallas nativas del panel.

/// «Toca otra vez para …»: el primer toque arma, el segundo hace; a los 3 s se desarma (twice() del panel).
struct DosToques: View {
    let titulo: String
    let armado: String
    var rol: ButtonRole? = nil
    let accion: () async -> Void
    @State private var listo = false
    @State private var ocupado = false

    var body: some View {
        Button(listo ? armado : titulo, role: rol) {
            if !listo {
                listo = true
                Task { try? await Task.sleep(for: .seconds(3)); listo = false }
                return
            }
            listo = false
            ocupado = true
            Task { await accion(); ocupado = false }
        }
        .disabled(ocupado)
    }
}

enum Fechas {
    /// whenText() del panel: «Hoy, 4:37 p. m.», «Ayer, …», «12 nov, …».
    static func cuando(_ ts: Double?) -> String {
        guard let ts else { return "" }
        let d = Date(timeIntervalSince1970: ts), cal = Calendar.current
        let hora = d.formatted(date: .omitted, time: .shortened)
        if cal.isDateInToday(d) { return "Hoy, " + hora }
        if cal.isDateInYesterday(d) { return "Ayer, " + hora }
        return d.formatted(.dateTime.day().month(.abbreviated)) + ", " + hora
    }
}

extension String {
    /// cap() del panel.
    var cap: String { prefix(1).uppercased() + dropFirst() }
}

/// Un aviso bajo una sección (las «note» del panel).
struct Nota: View {
    let texto: String
    var body: some View { Text(texto).font(.footnote).foregroundStyle(.secondary) }
}

/// El cuerpo de cada error: «detail» del núcleo si lo trae.
func detalle(_ error: Error, _ siNo: String) -> String {
    if case APIClient.Failure.server(_, let d) = error, !d.isEmpty { return d }
    return error.localizedDescription.isEmpty ? siNo : error.localizedDescription
}
