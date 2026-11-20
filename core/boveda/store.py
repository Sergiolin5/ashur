"""The two vault tables (notion_map, vault_fuentes), on the core's DB connection and lock."""
from typing import Optional


def notion_all(db) -> dict:
    with db.lock:
        rows = db.conn.execute("SELECT page_id, kind, ruta, editado, hash, sincronizado FROM notion_map").fetchall()
    return {r["page_id"]: dict(r) for r in rows}


def notion_put(db, page_id: str, kind: str, ruta: str, editado: str, hash_: str, now: float) -> None:
    with db.lock:
        db.conn.execute("INSERT OR REPLACE INTO notion_map (page_id, kind, ruta, editado, hash, sincronizado) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (page_id, kind, ruta, editado, hash_, now))
        db.conn.commit()


def fuente_get(db, origen: str) -> Optional[dict]:
    with db.lock:
        r = db.conn.execute("SELECT origen, ruta, hash, ts FROM vault_fuentes WHERE origen = ?", (origen,)).fetchone()
    return dict(r) if r else None


def fuentes_prefijo(db, prefijo: str) -> dict:
    with db.lock:
        rows = db.conn.execute("SELECT origen, ruta, hash, ts FROM vault_fuentes WHERE origen >= ? AND origen < ?",
                               (prefijo, prefijo + "￿")).fetchall()
    return {r["origen"]: dict(r) for r in rows}


def fuente_put(db, origen: str, ruta: str, hash_: str, now: float) -> None:
    with db.lock:
        db.conn.execute("INSERT OR REPLACE INTO vault_fuentes (origen, ruta, hash, ts) VALUES (?, ?, ?, ?)",
                        (origen, ruta, hash_, now))
        db.conn.commit()
