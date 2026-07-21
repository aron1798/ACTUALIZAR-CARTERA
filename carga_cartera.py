# carga_cartera.py — genera la base de cartera y la sube a Supabase (GitHub Actions)
# Lee el mapa de campañas desde la tabla 'campanias' de Supabase (no hardcodeado).
# Normaliza programa/sede/asesor con la tabla 'alias_normalizacion' antes de subir.
import os
from datetime import datetime, date

# ── Credenciales desde variables de entorno (GitHub Secrets) ─────────────
PG_HOST = os.environ["PG_HOST"]
PG_DATABASE = os.environ["PG_DATABASE"]
PG_USER = os.environ["PG_USER"]
PG_PASSWORD = os.environ["PG_PASSWORD"]
PG_PORT = os.environ.get("PG_PORT", "5432")

SUPABASE_URL = os.environ["SUPABASE_URL"]          # origen: datos_unificados
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_TABLA = "datos_unificados"

CARTERA_URL = os.environ["CARTERA_URL"]            # destino: cartera_junta + campanias
CARTERA_KEY = os.environ["CARTERA_KEY"]
CARTERA_TABLA = "cartera_junta"
CAMPANIAS_TABLA = "campanias"
ALIAS_TABLA = "alias_normalizacion"


def _norm_tel(v):
    s = str(v or "")
    for x in ("+51", "+", " ", "-", "(", ")"):
        s = s.replace(x, "")
    s = s.strip()
    # Descartar: vacíos, solo ceros, y números de 6 dígitos o menos
    if not s or set(s) == {"0"}:
        return ""
    # Celular peruano con "51" pegado adelante (51900000000 a 51999999999)
    # -> quitar el "51" para dejarlo en 9 dígitos (9XXXXXXXX).
    if s.isdigit() and 51900000000 <= int(s) <= 51999999999:
        s = s[2:]
    if s.isdigit() and len(s) <= 6:
        return ""
    return s


def _txt(v):
    s = str(v or "").strip()
    if s == "-":
        s = ""
    return s.upper()


def _to_fecha(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except Exception:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except Exception:
            pass
    return None


def traer_campanias():
    """Lee el mapa de campañas desde la tabla 'campanias' de Supabase (proyecto CARTERA)."""
    from supabase import create_client
    sb = create_client(CARTERA_URL, CARTERA_KEY)
    res = sb.table(CAMPANIAS_TABLA).select(
        "frase_busqueda,cargo,codigo,sede,dia,origen,fecha_inicio,fecha_fin"
    ).execute()
    filas = res.data or []
    print(f"  Campañas leídas de Supabase: {len(filas)}")
    return filas


def traer_alias():
    """Lee el diccionario alias_normalizacion → dicts programa, sede y asesor."""
    from supabase import create_client
    sb = create_client(CARTERA_URL, CARTERA_KEY)
    res = sb.table(ALIAS_TABLA).select("tipo,alias,correcto").execute()
    prog, sede, asesor = {}, {}, {}
    for r in (res.data or []):
        tipo = str(r.get("tipo", "")).upper()
        alias = " ".join(str(r.get("alias", "")).upper().split())
        corr = str(r.get("correcto", ""))
        if tipo == "PROGRAMA":
            prog[alias] = corr
        elif tipo == "SEDE":
            sede[alias] = corr
        elif tipo == "ASESOR":
            asesor[alias] = corr
    print(f"  Alias: {len(prog)} programa, {len(sede)} sede, {len(asesor)} asesor")
    return prog, sede, asesor


def _norm_valor(v, mapa):
    """MAYÚSCULAS + colapsar espacios (mantiene acentos); aplica el diccionario.
    Si no está en el diccionario, deja el valor tal cual (será 'no identificado')."""
    s = " ".join(str(v or "").upper().split())
    return mapa.get(s, s)


def _values_sql(campanias):
    """Arma el bloque VALUES (...) para el JOIN del query de Chatwoot."""
    out = []
    for c in campanias:
        frase = str(c.get("frase_busqueda", "")).replace("'", "''")
        cargo = str(c.get("cargo", "")).replace("'", "''")
        codigo = str(c.get("codigo", "")).replace("'", "''")
        sede = str(c.get("sede", "")).replace("'", "''")
        dia = str(c.get("dia", "")).replace("'", "''")
        origen = str(c.get("origen", "")).replace("'", "''")
        fi = str(c.get("fecha_inicio", ""))[:10]
        ff = str(c.get("fecha_fin", ""))[:10]
        out.append(f"('{frase}','{cargo}','{codigo}','{sede}','{dia}','{origen}','{fi}','{ff}')")
    return ",\n".join(out)


def _pg_connect():
    """Conexión a Postgre con reintentos (por timeout momentáneo)."""
    import psycopg2
    import time
    INTENTOS = 3
    ESPERA = 10
    for intento in range(1, INTENTOS + 1):
        try:
            return psycopg2.connect(host=PG_HOST, dbname=PG_DATABASE, user=PG_USER,
                                    password=PG_PASSWORD, port=PG_PORT,
                                    connect_timeout=30)
        except psycopg2.OperationalError as e:
            print(f"  ⚠️ Conexión Postgre falló (intento {intento}/{INTENTOS}): {e}")
            if intento == INTENTOS:
                raise
            time.sleep(ESPERA)


def traer_postgre(campanias):
    """Leads de Chatwoot que hacen MATCH con una frase de campaña.
    fecha_creada = fecha del mensaje de campaña (messages.created_at).
    Son filas REALES (solo_contacto=False)."""
    conn = _pg_connect()
    cur = conn.cursor()
    sql = f"""
    SELECT DISTINCT ON (m.id)
        REPLACE(REPLACE(c.phone_number, '+51', ''), '+', '') AS telefono,
        m.created_at AS fecha_creada,
        map.cargo AS programa, map.sede AS sede,
        map.codigo AS codigo, map.origen AS origen,
        u.name AS asesor
    FROM messages m
    JOIN conversations cv ON m.conversation_id = cv.id
    JOIN contacts c ON cv.contact_id = c.id
    LEFT JOIN users u ON cv.assignee_id = u.id
    JOIN (VALUES
{_values_sql(campanias)}
    ) AS map(frase_busqueda, cargo, codigo, sede, dia, origen, fecha_inicio, fecha_fin)
        ON m.content LIKE '%' || map.frase_busqueda || '%'
        AND m.created_at >= CAST(map.fecha_inicio AS TIMESTAMP)
        AND m.created_at <= CAST(map.fecha_fin AS TIMESTAMP) + INTERVAL '1 day'
    WHERE m.sender_type = 'Contact'
    ORDER BY m.id ASC
    """
    cur.execute(sql)
    out = []
    for tel, fecha, programa, sede, codigo, origen, asesor in cur.fetchall():
        t = _norm_tel(tel)
        if not t:
            continue
        out.append({"telefono": t, "fecha": _to_fecha(fecha),
                    "canal": _txt(origen), "sede": _txt(sede),
                    "programa": _txt(programa), "codigo": _txt(codigo),
                    "asesor": _txt(asesor),
                    "origen_base": "POSTGRE",
                    "solo_contacto": False})
    conn.close()
    return out


def traer_postgre_sin_campania(tels_con_campania):
    """TODOS los contactos de Chatwoot que NO tienen mensaje de campaña.
    Se meten con fecha_creada = contacts.created_at, canal COPITO, y su asesor
    (users.name del assignee) si tiene. Se excluyen los que YA salieron por
    campaña (tels_con_campania) para no duplicar: la campaña siempre gana.
    Son filas SOLO-CONTACTO (solo_contacto=True): solo seran es_origen=SI si el
    telefono NO tiene ninguna otra fila real (campaña/Excel)."""
    conn = _pg_connect()
    cur = conn.cursor()
    sql = """
    SELECT DISTINCT ON (c.id)
        REPLACE(REPLACE(c.phone_number, '+51', ''), '+', '') AS telefono,
        c.created_at AS fecha_creada,
        u.name AS asesor
    FROM contacts c
    LEFT JOIN conversations cv ON cv.contact_id = c.id
    LEFT JOIN users u ON cv.assignee_id = u.id
    ORDER BY c.id ASC, cv.id DESC
    """
    cur.execute(sql)
    out = []
    for tel, fecha, asesor in cur.fetchall():
        t = _norm_tel(tel)
        if not t:
            continue
        if t in tels_con_campania:
            continue   # ya entró por campaña -> NO duplicar (campaña gana)
        out.append({"telefono": t, "fecha": _to_fecha(fecha),
                    "canal": "COPITO", "sede": "", "programa": "",
                    "codigo": "", "asesor": _txt(asesor),
                    "origen_base": "POSTGRE",
                    "solo_contacto": True})
    conn.close()
    return out


def traer_supabase():
    """Lee TODAS las filas de datos_unificados paginando.
    FIX: parar SOLO cuando Supabase devuelve 0 filas (antes cortaba si un
    bloque venía con < 1000, lo que dejaba miles de filas sin leer y hacía
    que el total 'bailara' entre corridas). Son filas REALES (solo_contacto=False)."""
    from supabase import create_client
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    out = []
    paso, desde = 1000, 0
    while True:
        res = sb.table(SUPABASE_TABLA).select(
            "Telefono,Fechacreada,Canal,Sede,Programa,Codigo,Ejecutivo"
        ).order("Telefono").range(desde, desde + paso - 1).execute()   # orden estable
        data = res.data or []
        if not data:
            break                        # SOLO parar cuando NO devuelve NADA
        for r in data:
            t = _norm_tel(r.get("Telefono"))
            if not t:
                continue
            out.append({
                "telefono": t, "fecha": _to_fecha(r.get("Fechacreada")),
                "canal": _txt(r.get("Canal")), "sede": _txt(r.get("Sede")),
                "programa": _txt(r.get("Programa")), "codigo": _txt(r.get("Codigo")),
                "asesor": _txt(r.get("Ejecutivo")),
                "origen_base": "SUPABASE",
                "solo_contacto": False,
            })
        desde += len(data)               # avanzar por lo REALMENTE recibido
    print(f"  Supabase leídas (crudas, incl. telefonos filtrados): {desde}")
    return out


def _rango_canal(canal):
    if canal == "PAUTA_WSP":
        return 0
    if canal == "PAUTA_WSP_FACE":
        return 1
    return 2


def _clave_orden(r):
    """Orden para elegir es_origen. Las filas SOLO-CONTACTO van SIEMPRE al final
    (prioridad 1), asi una fila REAL (0) gana el origen aunque el contacto sea
    mas antiguo. Dentro de cada grupo: mas antigua + desempate por canal."""
    FUTURO = date(9999, 1, 1)
    solo = 1 if r.get("solo_contacto") else 0
    return (solo, r["fecha"] or FUTURO, _rango_canal(r["canal"]), r["canal"])


def subir_a_supabase(filas):
    from supabase import create_client
    dest = create_client(CARTERA_URL, CARTERA_KEY)
    dest.table(CARTERA_TABLA).delete().gte("id", 0).execute()   # vaciar
    lote = 500
    for i in range(0, len(filas), lote):
        dest.table(CARTERA_TABLA).insert(filas[i:i + lote]).execute()
        print(f"  subidas {min(i + lote, len(filas))}/{len(filas)}")


def main():
    print("Leyendo campañas de Supabase...")
    campanias = traer_campanias()
    if not campanias:
        print("❌ No hay campañas en la tabla. Aborto.")
        return

    print("Leyendo diccionario de normalización...")
    mapa_prog, mapa_sede, mapa_ase = traer_alias()

    print("Trayendo Postgre (leads con mensaje de campaña)...")
    pg = traer_postgre(campanias)
    print(f"  Postgre (campaña): {len(pg)} filas")

    # Set de telefonos que YA tienen campaña -> para no duplicarlos abajo
    tels_campania = {r["telefono"] for r in pg}

    print("Trayendo Postgre (contactos SIN mensaje de campaña)...")
    pg_sin = traer_postgre_sin_campania(tels_campania)
    print(f"  Postgre (sin campaña, created_at): {len(pg_sin)} filas")

    print("Trayendo Supabase (datos_unificados)...")
    sup = traer_supabase()
    print(f"  Supabase: {len(sup)} filas")

    todos = pg + pg_sin + sup
    print(f"Total combinado (con repetidos): {len(todos)}")

    # SEGURIDAD: si la carga vino muy chica, NO vaciar cartera_junta (evita
    # destruir la cartera buena con una corrida incompleta). Ajusta el mínimo.
    MINIMO_ESPERADO = 80000
    if len(todos) < MINIMO_ESPERADO:
        raise SystemExit(
            f"❌ ABORTADO: solo {len(todos)} filas combinadas (< {MINIMO_ESPERADO}). "
            f"NO se vacía {CARTERA_TABLA} para no perder datos. "
            f"Revisa Postgre ({len(pg)}) y datos_unificados ({len(sup)})."
        )

    # Marcar ES_ORIGEN: SI a la ganadora por teléfono. Las filas REALES (campaña/Excel)
    # tienen prioridad sobre las SOLO-CONTACTO. Dentro: mas antigua + desempate canal.
    # -> un contacto sin campaña SOLO es origen si el telefono no tiene fila real.
    orden = sorted(range(len(todos)), key=lambda i: _clave_orden(todos[i]))
    ya_origen = set()
    for i in orden:
        tel = todos[i]["telefono"]
        if tel not in ya_origen:
            todos[i]["es_origen"] = "SI"
            ya_origen.add(tel)
        else:
            todos[i]["es_origen"] = "NO"

    filas = []
    for r in todos:
        f = r["fecha"]
        filas.append({
            "telefono": r["telefono"],
            "canal": r["canal"],
            "fecha_creada": f.isoformat() if f else None,
            "sede": _norm_valor(r["sede"], mapa_sede),
            "programa": _norm_valor(r["programa"], mapa_prog),
            "codigo": r["codigo"],
            "asesor": _norm_valor(r.get("asesor", ""), mapa_ase),
            "es_origen": r["es_origen"],
            "origen_base": r["origen_base"],
        })

    print(f"Subiendo {len(filas)} filas a {CARTERA_TABLA}...")
    subir_a_supabase(filas)
    print("✅ Cartera actualizada.")


if __name__ == "__main__":
    main()
