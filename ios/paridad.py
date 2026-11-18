"""Paridad panel web ↔ app iOS (docs/ios/NATIVO.md). Lee el index.html del panel en main y compara sus pestañas,
páginas de Ajustes y endpoints con ios/paridad.json. Falla si el panel tiene algo que la lista no tiene: cada cosa nueva
del panel entra a la lista (como «web» hasta portarla), así la app nunca se queda atrás sin que se note.
Uso: python3 ios/paridad.py <index.html> [ios/paridad.json]"""
import json, re, sys

html = open(sys.argv[1], encoding="utf-8").read()
lista = json.load(open(sys.argv[2] if len(sys.argv) > 2 else "ios/paridad.json", encoding="utf-8"))
ESTADOS = {"nativo", "parcial", "web"}

panel = {
    "pestañas": set(re.findall(r'id="tab-([a-z]+)"', html)),
    "paginas": set(re.findall(r'data-page="([a-z-]+)"', html)),
    "endpoints": {m.rstrip("/") for m in re.findall(r'api\(\s*["`](api/[A-Za-z0-9/_.-]*)', html)} - {"api"},
}
falta, sobra, mal = [], [], []
for grupo, items in panel.items():
    tiene = lista.get(grupo, {})
    falta += [f"{grupo}: {x}" for x in sorted(items - tiene.keys())]
    sobra += [f"{grupo}: {x}" for x in sorted(tiene.keys() - items)]
    mal += [f"{grupo}: {x} = {v.get('estado') if isinstance(v, dict) else v}" for x, v in tiene.items()
            if (v.get("estado") if isinstance(v, dict) else v) not in ESTADOS]

total = {g: len(panel[g]) for g in panel}
nativas = {g: sum(1 for x, v in lista.get(g, {}).items() if x in panel[g] and (v.get("estado") if isinstance(v, dict) else v) == "nativo") for g in panel}
print("Paridad: " + ", ".join(f"{g} {nativas[g]}/{total[g]} nativas" for g in panel))
for x in sobra:
    print("  ya no está en el panel (se puede quitar de la lista):", x)
for x in mal:
    print("  estado inválido:", x)
for x in falta:
    print("  FALTA en ios/paridad.json:", x)
sys.exit(1 if falta or mal else 0)
