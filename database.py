import os
import re
import json
import math
import time
import logging
import threading
import asyncio
import libsql
from dotenv import load_dotenv
from passlib.context import CryptContext

load_dotenv()
TURSO_URL = os.getenv("TURSO_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "").strip()
DB_LOCAL = os.getenv("DB_LOCAL", "kovaq_local.db")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
log = logging.getLogger("kovaq.db")
ADMIN_PASSWORD_MIN = 12

_FECHA_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COLUMNAS_FECHA_VALIDAS = {"creado"}
_SEED_VERSION = "v4_limpio_coherente"

# Caché breve de metadatos estáticos (sucursales, cámaras, perchas, catálogo, promociones)
_META_CACHE_TTL = 30.0
_meta_cache_lock = threading.Lock()
_meta_cache: dict = {}


def invalidar_cache_metadatos():
    with _meta_cache_lock:
        _meta_cache.clear()

# Polígonos en perspectiva calibrados al video demo_supermercado.mp4 (16:9)
POLY_PER_A1 = [[0.000, 0.285], [0.432, 0.285], [0.432, 0.548], [0.205, 1.000], [0.000, 1.000]]
POLY_PER_A2 = [[0.665, 0.290], [1.000, 0.305], [1.000, 1.000], [0.752, 1.000], [0.655, 0.530]]

# Reutiliza conexión por hilo en Turso para evitar handshakes TLS repetidos en Render.
_tls = threading.local()


def _abrir():
    if TURSO_URL:
        cached = getattr(_tls, "conn", None)
        cached_url = getattr(_tls, "url", None)
        if cached is not None and cached_url == TURSO_URL:
            return cached
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        _tls.conn = conn
        _tls.url = TURSO_URL
        return conn

    conn = libsql.connect(database=DB_LOCAL)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _cerrar_si_local(conn):
    if not TURSO_URL:
        try:
            conn.close()
        except Exception:
            pass


def _es_error_integridad(err: Exception) -> bool:
    msg = str(err).lower()
    return any(tok in msg for tok in ("unique", "constraint", "foreign key", "syntax", "no such"))


def _asegurar_columna(conn, tabla: str, columna: str, definicion: str):
    """Agrega columna de forma idempotente si una base existente aún no la tiene."""
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tabla})").fetchall()]
        if columna not in cols:
            conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}")
    except Exception as err:
        log.debug("Aviso migración %s.%s: %s", tabla, columna, err)


def _insertar_en_lotes(conn, sql_insert_prefix: str, filas: list, chunk_size: int = 60):
    """Ejecuta INSERT multi-fila en lotes para evitar miles de round-trips HTTP en Turso."""
    if not filas:
        return
    num_cols = len(filas[0])
    placeholder_fila = "(" + ",".join(["?"] * num_cols) + ")"
    for i in range(0, len(filas), chunk_size):
        lote = filas[i:i + chunk_size]
        placeholders = ",".join([placeholder_fila] * len(lote))
        flat_params = [val for fila in lote for val in fila]
        conn.execute(f"{sql_insert_prefix} VALUES {placeholders}", flat_params)


def leer(query: str, params: tuple = (), uno: bool = False):
    conn = _abrir()
    try:
        cur = conn.execute(query, params)
        return cur.fetchone() if uno else cur.fetchall()
    except Exception as err:
        if TURSO_URL and not _es_error_integridad(err):
            _tls.conn = None
            conn = _abrir()
            cur = conn.execute(query, params)
            return cur.fetchone() if uno else cur.fetchall()
        raise
    finally:
        _cerrar_si_local(conn)


def escribir(query: str, params: tuple = ()):
    conn = _abrir()
    try:
        conn.execute(query, params)
        conn.commit()
    except Exception as err:
        if TURSO_URL and not _es_error_integridad(err):
            _tls.conn = None
            conn = _abrir()
            conn.execute(query, params)
            conn.commit()
            return
        raise
    finally:
        _cerrar_si_local(conn)


def init_db_sync():
    conn = _abrir()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS roles (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT UNIQUE)")
        conn.execute("""CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password_hash TEXT,
            nombre_completo TEXT,
            rol_id INTEGER,
            sucursal_id INTEGER,
            estado TEXT DEFAULT 'activo',
            FOREIGN KEY(rol_id) REFERENCES roles(id))""")

        conn.execute("""CREATE TABLE IF NOT EXISTS eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT,
            detalle TEXT,
            valor REAL,
            sucursal_id TEXT DEFAULT 'SUC-NORTE',
            creado TEXT DEFAULT CURRENT_TIMESTAMP)""")
        _asegurar_columna(conn, "eventos", "sucursal_id", "TEXT DEFAULT 'SUC-NORTE'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_eventos_tipo_creado ON eventos(tipo, creado)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_eventos_creado ON eventos(creado)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_eventos_suc_tipo_creado ON eventos(sucursal_id, tipo, creado)")

        for rol in ("admin", "admin_tecnico", "dueno", "encargado", "vendedor"):
            conn.execute("INSERT OR IGNORE INTO roles (nombre) VALUES (?)", (rol,))
        conn.commit()

        if conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
            _crear_admin_inicial(conn)
    finally:
        _cerrar_si_local(conn)


def _crear_admin_inicial(conn):
    """Crea el primer admin solo si KOVAQ_ADMIN_USER y KOVAQ_ADMIN_PASSWORD están definidas."""
    usuario = os.getenv("KOVAQ_ADMIN_USER", "").strip()
    clave = os.getenv("KOVAQ_ADMIN_PASSWORD", "")
    if not usuario or not clave:
        log.warning(
            "No hay usuarios y faltan KOVAQ_ADMIN_USER / KOVAQ_ADMIN_PASSWORD: no se creó ningún admin. "
            "Defínelas en .env (o en Render) o usa 'python crear_usuario.py'."
        )
        return
    if len(clave) < ADMIN_PASSWORD_MIN:
        log.warning(
            "KOVAQ_ADMIN_PASSWORD debe tener al menos %d caracteres: no se creó ningún admin.",
            ADMIN_PASSWORD_MIN,
        )
        return
    rol_admin = conn.execute("SELECT id FROM roles WHERE nombre = 'admin'").fetchone()[0]
    conn.execute(
        "INSERT INTO usuarios (username, password_hash, nombre_completo, rol_id, estado) VALUES (?, ?, ?, ?, 'activo')",
        (usuario, pwd_context.hash(clave), "Administrador Maestro", rol_admin)
    )
    conn.commit()
    log.warning("Admin inicial '%s' creado desde KOVAQ_ADMIN_USER.", usuario)


def guardar_trafico(sucursal_id: str = "SUC-NORTE"):
    escribir("INSERT INTO eventos (tipo, sucursal_id) VALUES ('trafico', ?)", (sucursal_id or "SUC-NORTE",))


def guardar_interaccion(producto: str, sucursal_id: str = "SUC-NORTE"):
    escribir("INSERT INTO eventos (tipo, detalle, sucursal_id) VALUES ('interaccion', ?, ?)", (producto, sucursal_id or "SUC-NORTE"))


def guardar_producto_tomado(producto: str, sesion_id: int = None, percha_nombre: str = None, sucursal_id: str = "SUC-NORTE"):
    """Registra toma de producto con sesión anónima efímera (una sola fila canónica)."""
    etiqueta_percha = f" [{percha_nombre}]" if percha_nombre else ""
    detalle = f"{producto}{etiqueta_percha} (Vector S#{sesion_id})" if sesion_id else f"{producto}{etiqueta_percha}"
    escribir(
        "INSERT INTO eventos (tipo, detalle, valor, sucursal_id) VALUES ('producto_tomado', ?, ?, ?)",
        (detalle, float(sesion_id or 0), sucursal_id or "SUC-NORTE"),
    )


def guardar_producto_devuelto(producto: str, sesion_id: int = None, percha_nombre: str = None, sucursal_id: str = "SUC-NORTE"):
    """Registra devolución de producto (cambio de decisión en percha) de forma 100% anónima."""
    etiqueta_percha = f" [{percha_nombre}]" if percha_nombre else ""
    detalle = f"{producto}{etiqueta_percha} (Vector S#{sesion_id})" if sesion_id else f"{producto}{etiqueta_percha}"
    escribir(
        "INSERT INTO eventos (tipo, detalle, valor, sucursal_id) VALUES ('producto_devuelto', ?, ?, ?)",
        (detalle, float(sesion_id or 0), sucursal_id or "SUC-NORTE"),
    )


def guardar_permanencia(segundos: float, sucursal_id: str = "SUC-NORTE"):
    escribir("INSERT INTO eventos (tipo, valor, sucursal_id) VALUES ('permanencia', ?, ?)", (segundos, sucursal_id or "SUC-NORTE"))


def guardar_handoff_camara(sesion_id: int, cam_origen: str, cam_destino: str, continuidad: float, sucursal_id: str = "SUC-NORTE"):
    detalle = f"Vector Anónimo S#{sesion_id}: {cam_origen} -> {cam_destino}"
    escribir(
        "INSERT INTO eventos (tipo, detalle, valor, sucursal_id) VALUES ('handoff_camara', ?, ?, ?)",
        (detalle, round(float(continuidad), 3), sucursal_id or "SUC-NORTE"),
    )


def guardar_telemetria_coordenada(
    sesion_anonima: str,
    es_cliente_valido: int,
    x_norm: float,
    y_norm: float,
    percha_id: str,
    nivel_percha: str,
    postura_cabeza: str,
    comportamiento: str,
    producto: str,
    dwell_sec: float,
    camara_id: str = "CAM-01",
    sucursal_id: str = "SUC-NORTE",
):
    """Guarda únicamente coordenadas espaciales (x, y) y vectores de comportamiento anónimo."""
    escribir("""
        INSERT INTO telemetria_coordenadas
        (sesion_anonima, es_cliente_valido, x_norm, y_norm, percha_id, nivel_percha,
         postura_cabeza, comportamiento, producto, dwell_sec, camara_id, sucursal_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sesion_anonima, int(es_cliente_valido), round(float(x_norm), 4), round(float(y_norm), 4),
        percha_id or "", nivel_percha or "", postura_cabeza or "frente",
        comportamiento or "transito", producto or "", round(float(dwell_sec), 2),
        camara_id, sucursal_id or "SUC-NORTE",
    ))


async def init_db():
    await asyncio.to_thread(init_db_sync)


# Infraestructura arquitectónica por sucursal (paredes, pasillos, bodega, columnas, cajas, accesos)

def _plano_arquitectonico_norte():
    return {
        "nombre_plano": "Planta Baja • Formato Hipermercado (1,450 m²)",
        "dimensiones_m": [42, 24],
        "bodega": {"x": 0.03, "y": 0.04, "w": 0.94, "h": 0.10, "label": "BODEGA / REPOSICIÓN (SOLO PERSONAL)"},
        "puerta_entrada": {"x": 0.025, "y": 0.30, "w": 0.055, "h": 0.40, "nombre": "ACCESO PRINCIPAL"},
        "linea_cajas": {"x": 0.835, "y": 0.46, "w": 0.125, "h": 0.46, "nombre": "CAJAS (4 POS)", "cajas": 4},
        "zona_atencion": {"x": 0.835, "y": 0.18, "w": 0.125, "h": 0.24, "label": "ATENCIÓN / RETIRO"},
        "columnas": [[0.365, 0.50], [0.645, 0.50], [0.365, 0.86], [0.645, 0.86]],
        "pasillos": [
            {"label": "PASILLO 1 • DESAYUNO Y CONSERVAS", "x": 0.11, "y": 0.50},
            {"label": "PASILLO 2 • BEBIDAS Y LÁCTEOS", "x": 0.41, "y": 0.50},
            {"label": "CORREDOR CAJAS Y HOGAR", "x": 0.66, "y": 0.88},
        ],
        "cabeceras": [
            {"x": 0.345, "y": 0.25, "w": 0.032, "h": 0.14, "label": "POP"},
            {"x": 0.345, "y": 0.62, "w": 0.032, "h": 0.14, "label": "POP"},
        ],
    }


def _plano_arquitectonico_centro():
    return {
        "nombre_plano": "Planta Urbana • Formato Supermercado Express (980 m²)",
        "dimensiones_m": [32, 20],
        "bodega": {"x": 0.03, "y": 0.04, "w": 0.60, "h": 0.10, "label": "TRABAJADORES / ALMACÉN RÁPIDO"},
        "puerta_entrada": {"x": 0.025, "y": 0.34, "w": 0.055, "h": 0.36, "nombre": "ACCESO PEATONAL"},
        "linea_cajas": {"x": 0.835, "y": 0.46, "w": 0.125, "h": 0.46, "nombre": "CAJAS (3 POS)", "cajas": 3},
        "zona_atencion": {"x": 0.835, "y": 0.18, "w": 0.125, "h": 0.24, "label": "PICKUP / DELIVERY"},
        "columnas": [[0.38, 0.50], [0.68, 0.50]],
        "pasillos": [
            {"label": "PASILLO 1 • SNACKS Y DESAYUNO", "x": 0.12, "y": 0.50},
            {"label": "PASILLO 2 • BEBIDAS FRÍAS", "x": 0.44, "y": 0.50},
            {"label": "ZONA CHECKOUT", "x": 0.69, "y": 0.88},
        ],
        "cabeceras": [
            {"x": 0.355, "y": 0.25, "w": 0.032, "h": 0.14, "label": "POP"},
        ],
    }


def _plano_arquitectonico_sur():
    return {
        "nombre_plano": "Nave Comercial • Formato Hiper Familiar (1,820 m²)",
        "dimensiones_m": [48, 26],
        "bodega": {"x": 0.03, "y": 0.04, "w": 0.94, "h": 0.10, "label": "CENTRO DE CARGA Y BODEGA SUR"},
        "puerta_entrada": {"x": 0.025, "y": 0.28, "w": 0.055, "h": 0.44, "nombre": "ACCESO PARQUEADERO"},
        "linea_cajas": {"x": 0.835, "y": 0.44, "w": 0.125, "h": 0.48, "nombre": "CAJAS (5 POS)", "cajas": 5},
        "zona_atencion": {"x": 0.835, "y": 0.18, "w": 0.125, "h": 0.22, "label": "SERVICIO CLIENTE"},
        "columnas": [[0.365, 0.50], [0.645, 0.50], [0.365, 0.86], [0.645, 0.86]],
        "pasillos": [
            {"label": "PASILLO 1 • CANASTA BÁSICA", "x": 0.11, "y": 0.50},
            {"label": "PASILLO 2 • REFRIGERADOS Y BEBIDAS", "x": 0.41, "y": 0.50},
            {"label": "PASILLO 3 • HOGAR MAYORISTA", "x": 0.66, "y": 0.88},
        ],
        "cabeceras": [
            {"x": 0.345, "y": 0.25, "w": 0.032, "h": 0.14, "label": "PACK"},
            {"x": 0.635, "y": 0.25, "w": 0.032, "h": 0.14, "label": "POP"},
        ],
    }


# Analítica avanzada, sucursales, planograma, promociones y telemetría anónima

def init_analytics_sync():
    """Inicializa tablas para sucursales, cámaras, planograma de perchas, promociones POP y coordenadas anónimas."""
    conn = _abrir()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS meta_semilla (
            clave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS sucursales (
            id TEXT PRIMARY KEY,
            nombre TEXT NOT NULL,
            ciudad TEXT DEFAULT 'Quito',
            direccion TEXT DEFAULT '',
            formato TEXT DEFAULT 'Hipermercado',
            area_m2 INTEGER DEFAULT 1200,
            plano_json TEXT DEFAULT '{}',
            estado TEXT DEFAULT 'Activa'
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS categorias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT UNIQUE)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS producto_categoria (
            producto TEXT,
            categoria_id INTEGER,
            FOREIGN KEY(categoria_id) REFERENCES categorias(id),
            UNIQUE(producto, categoria_id))""")

        conn.execute("""CREATE TABLE IF NOT EXISTS camaras_mapa (
            id TEXT PRIMARY KEY,
            nombre TEXT NOT NULL,
            fuente TEXT DEFAULT 'video',
            ruta TEXT DEFAULT '',
            estado TEXT DEFAULT 'Activa',
            pos_x REAL DEFAULT 0.5,
            pos_y REAL DEFAULT 0.5,
            angulo REAL DEFAULT 0.0,
            fov REAL DEFAULT 70.0,
            alcance REAL DEFAULT 0.35,
            homografia_json TEXT DEFAULT '[]',
            sucursal_id TEXT DEFAULT 'SUC-NORTE'
        )""")
        _asegurar_columna(conn, "camaras_mapa", "sucursal_id", "TEXT DEFAULT 'SUC-NORTE'")

        conn.execute("""CREATE TABLE IF NOT EXISTS perchas_mapa (
            id TEXT PRIMARY KEY,
            nombre TEXT NOT NULL,
            categoria TEXT NOT NULL,
            camara_id TEXT NOT NULL,
            map_x REAL NOT NULL,
            map_y REAL NOT NULL,
            map_w REAL NOT NULL,
            map_h REAL NOT NULL,
            roi_json TEXT NOT NULL,
            niveles_json TEXT DEFAULT '[]',
            color TEXT DEFAULT '#FE5000',
            sucursal_id TEXT DEFAULT 'SUC-NORTE'
        )""")
        _asegurar_columna(conn, "perchas_mapa", "sucursal_id", "TEXT DEFAULT 'SUC-NORTE'")

        conn.execute("""CREATE TABLE IF NOT EXISTS productos_catalogo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT UNIQUE,
            nombre TEXT NOT NULL,
            categoria TEXT NOT NULL,
            percha_id TEXT NOT NULL,
            nivel TEXT DEFAULT 'Medio',
            stock INTEGER DEFAULT 20,
            tomas_base INTEGER DEFAULT 0,
            devoluciones_base INTEGER DEFAULT 0,
            sucursal_id TEXT DEFAULT 'SUC-NORTE'
        )""")
        _asegurar_columna(conn, "productos_catalogo", "sucursal_id", "TEXT DEFAULT 'SUC-NORTE'")

        conn.execute("""CREATE TABLE IF NOT EXISTS promociones_percha (
            id TEXT PRIMARY KEY,
            percha_id TEXT NOT NULL,
            producto TEXT NOT NULL,
            titulo TEXT NOT NULL,
            tipo_publicidad TEXT NOT NULL,
            mensaje_pop TEXT DEFAULT '',
            descuento_pct INTEGER DEFAULT 0,
            activa INTEGER DEFAULT 1,
            sucursal_id TEXT DEFAULT 'SUC-NORTE',
            creado TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        _asegurar_columna(conn, "promociones_percha", "sucursal_id", "TEXT DEFAULT 'SUC-NORTE'")

        conn.execute("""CREATE TABLE IF NOT EXISTS telemetria_coordenadas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sesion_anonima TEXT NOT NULL,
            es_cliente_valido INTEGER DEFAULT 1,
            x_norm REAL NOT NULL,
            y_norm REAL NOT NULL,
            percha_id TEXT DEFAULT '',
            nivel_percha TEXT DEFAULT '',
            postura_cabeza TEXT DEFAULT 'frente',
            comportamiento TEXT DEFAULT 'transito',
            producto TEXT DEFAULT '',
            dwell_sec REAL DEFAULT 0.0,
            camara_id TEXT DEFAULT 'CAM-01',
            sucursal_id TEXT DEFAULT 'SUC-NORTE',
            creado TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        _asegurar_columna(conn, "telemetria_coordenadas", "sucursal_id", "TEXT DEFAULT 'SUC-NORTE'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tc_valido_creado ON telemetria_coordenadas(es_cliente_valido, creado)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tc_percha_comp ON telemetria_coordenadas(percha_id, comportamiento, creado)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tc_suc_valido_creado ON telemetria_coordenadas(sucursal_id, es_cliente_valido, creado)")

        # Semilla de sucursales corporativas (INSERT multi-fila en 1 solo round-trip)
        sucursales_default = [
            ("SUC-NORTE", "Matriz Norte • Av. Amazonas", "Quito", "Av. Amazonas N34-120", "Hipermercado", 1450, json.dumps(_plano_arquitectonico_norte()), "Activa"),
            ("SUC-CENTRO", "Plaza Centro • Mall del Sol", "Guayaquil", "Av. Juan Tanca Marengo", "Supermercado Express", 980, json.dumps(_plano_arquitectonico_centro()), "Activa"),
            ("SUC-SUR", "Hiper Sur • Av. Maldonado", "Cuenca", "Av. Remigio Crespo 4-80", "Hiper Familiar", 1820, json.dumps(_plano_arquitectonico_sur()), "Activa"),
        ]
        _insertar_en_lotes(
            conn,
            "INSERT OR IGNORE INTO sucursales (id, nombre, ciudad, direccion, formato, area_m2, plano_json, estado)",
            sucursales_default,
            chunk_size=20,
        )

        cats = {
            "Cereales": ["Cereales Kellogg's 500g", "Avena Crujiente 400g", "Barras de Cereal Mix"],
            "Conservas": ["Salsa de Tomate Artesanal", "Pasta Italiana 500g", "Aceitunas y Encurtidos"],
            "Bebidas": ["bottle", "cup", "wine glass", "Coca Cola Zero 500ml", "Agua Mineral 600ml"],
            "Lácteos": ["Leche Entera Tetra 1L", "Yogurt Griego Natural"],
            "Snacks": ["cake", "donut", "sandwich", "Papas Artesanales 150g"],
            "Limpieza": ["Detergente Líquido 2L"]
        }
        _insertar_en_lotes(conn, "INSERT OR IGNORE INTO categorias (nombre)", [(c,) for c in cats.keys()], chunk_size=20)
        cat_ids = dict(conn.execute("SELECT nombre, id FROM categorias").fetchall())
        prod_cat_rows = [(p, cat_ids[c]) for c, prods in cats.items() if c in cat_ids for p in prods]
        _insertar_en_lotes(conn, "INSERT OR IGNORE INTO producto_categoria (producto, categoria_id)", prod_cat_rows, chunk_size=50)

        # Semilla de Cámaras (con cobertura por sucursal, despejadas debajo de la bodega)
        camaras_default = [
            ("CAM-01", "Cámara 12 • Pasillo Central y Puerta", "video", "static/demo_supermercado.mp4", "Activa",
             0.16, 0.21, 72.0, 74.0, 0.38,
             json.dumps({"x_min": 0.08, "x_max": 0.40, "y_min": 0.20, "y_max": 0.84, "altura_m": 3.4, "pitch_deg": 38}), "SUC-NORTE"),
            ("CAM-02", "Cámara 14 • Góndolas Lácteos y Bebidas", "video", "static/demo_supermercado.mp4", "Activa",
             0.48, 0.21, 90.0, 74.0, 0.38,
             json.dumps({"x_min": 0.38, "x_max": 0.68, "y_min": 0.20, "y_max": 0.84, "altura_m": 3.5, "pitch_deg": 40}), "SUC-NORTE"),
            ("CAM-03", "Cámara 16 • Hogar, Cajas y Salida", "video", "static/demo_supermercado.mp4", "Activa",
             0.80, 0.48, 178.0, 70.0, 0.34,
             json.dumps({"x_min": 0.60, "x_max": 0.84, "y_min": 0.20, "y_max": 0.88, "altura_m": 3.4, "pitch_deg": 36}), "SUC-NORTE"),
            ("CAM-C1", "Cámara Express 01 • Acceso y Snacks", "video", "static/demo_supermercado.mp4", "Activa",
             0.18, 0.21, 68.0, 74.0, 0.38,
             json.dumps({"x_min": 0.08, "x_max": 0.44, "y_min": 0.20, "y_max": 0.84, "altura_m": 3.2, "pitch_deg": 36}), "SUC-CENTRO"),
            ("CAM-C2", "Cámara Express 02 • Bebidas Frías y Cajas", "video", "static/demo_supermercado.mp4", "Activa",
             0.56, 0.21, 105.0, 74.0, 0.40,
             json.dumps({"x_min": 0.40, "x_max": 0.84, "y_min": 0.20, "y_max": 0.86, "altura_m": 3.2, "pitch_deg": 38}), "SUC-CENTRO"),
            ("CAM-S1", "Cámara Sur 01 • Pasillo Canasta Básica", "video", "static/demo_supermercado.mp4", "Activa",
             0.16, 0.21, 74.0, 74.0, 0.38,
             json.dumps({"x_min": 0.08, "x_max": 0.40, "y_min": 0.20, "y_max": 0.84, "altura_m": 3.8, "pitch_deg": 40}), "SUC-SUR"),
            ("CAM-S2", "Cámara Sur 02 • Refrigerados y Mayorista", "video", "static/demo_supermercado.mp4", "Activa",
             0.48, 0.21, 92.0, 74.0, 0.38,
             json.dumps({"x_min": 0.38, "x_max": 0.68, "y_min": 0.20, "y_max": 0.86, "altura_m": 3.8, "pitch_deg": 40}), "SUC-SUR"),
            ("CAM-S3", "Cámara Sur 03 • Cajas y Parqueadero", "video", "static/demo_supermercado.mp4", "Activa",
             0.80, 0.48, 180.0, 70.0, 0.34,
             json.dumps({"x_min": 0.62, "x_max": 0.84, "y_min": 0.20, "y_max": 0.88, "altura_m": 3.8, "pitch_deg": 36}), "SUC-SUR"),
        ]
        _insertar_en_lotes(
            conn,
            "INSERT OR IGNORE INTO camaras_mapa (id, nombre, fuente, ruta, estado, pos_x, pos_y, angulo, fov, alcance, homografia_json, sucursal_id)",
            camaras_default,
            chunk_size=20,
        )

        # Semilla de Perchas mapeadas con polígonos de perspectiva reales y sin choques con cajas
        perchas_default = [
            ("PER-A1", "Percha A1 (Izq.) • Cereales y Desayuno", "Cereales", "CAM-01",
             0.11, 0.25, 0.22, 0.15,
             json.dumps({"x1": 0.0, "y1": 0.285, "x2": 0.432, "y2": 1.0, "poly": POLY_PER_A1}),
             json.dumps([
                 {"nivel": "Superior (Subir Cabeza)", "y1": 0.28, "y2": 0.46, "producto": "Avena Crujiente 400g"},
                 {"nivel": "Medio (Altura Ojos)", "y1": 0.46, "y2": 0.70, "producto": "Cereales Kellogg's 500g"},
                 {"nivel": "Inferior (Agacharse)", "y1": 0.70, "y2": 0.96, "producto": "Barras de Cereal Mix"}
             ]), "#FE5000", "SUC-NORTE"),
            ("PER-A2", "Percha A2 (Der.) • Salsas, Pastas y Conservas", "Conservas", "CAM-01",
             0.11, 0.62, 0.22, 0.15,
             json.dumps({"x1": 0.655, "y1": 0.29, "x2": 1.0, "y2": 1.0, "poly": POLY_PER_A2}),
             json.dumps([
                 {"nivel": "Superior (Subir Cabeza)", "y1": 0.29, "y2": 0.48, "producto": "Pasta Italiana 500g"},
                 {"nivel": "Medio (Altura Ojos)", "y1": 0.48, "y2": 0.72, "producto": "Salsa de Tomate Artesanal"},
                 {"nivel": "Inferior (Agacharse)", "y1": 0.72, "y2": 0.96, "producto": "Aceitunas y Encurtidos"}
             ]), "#F59E0B", "SUC-NORTE"),
            ("PER-B1", "Percha B1 • Bebidas Frías y Energizantes", "Bebidas", "CAM-02",
             0.41, 0.25, 0.21, 0.15,
             json.dumps({"x1": 0.0, "y1": 0.285, "x2": 0.432, "y2": 1.0, "poly": POLY_PER_A1}),
             json.dumps([
                 {"nivel": "Superior (Subir Cabeza)", "y1": 0.28, "y2": 0.48, "producto": "Coca Cola Zero 500ml"},
                 {"nivel": "Medio (Altura Ojos)", "y1": 0.48, "y2": 0.72, "producto": "Agua Mineral 600ml"},
                 {"nivel": "Inferior (Agacharse)", "y1": 0.72, "y2": 0.96, "producto": "Jugo Naranja 1L"}
             ]), "#2563EB", "SUC-NORTE"),
            ("PER-B2", "Percha B2 • Lácteos y Refrigerados", "Lácteos", "CAM-02",
             0.41, 0.62, 0.21, 0.15,
             json.dumps({"x1": 0.655, "y1": 0.29, "x2": 1.0, "y2": 1.0, "poly": POLY_PER_A2}),
             json.dumps([
                 {"nivel": "Superior (Subir Cabeza)", "y1": 0.29, "y2": 0.56, "producto": "Leche Entera Tetra 1L"},
                 {"nivel": "Medio (Altura Ojos)", "y1": 0.56, "y2": 0.90, "producto": "Yogurt Griego Natural"}
             ]), "#10B981", "SUC-NORTE"),
            ("PER-C1", "Percha C1 • Snacks, Hogar y Cajas", "Snacks", "CAM-03",
             0.67, 0.26, 0.115, 0.48,
             json.dumps({"x1": 0.0, "y1": 0.285, "x2": 0.432, "y2": 1.0, "poly": POLY_PER_A1}),
             json.dumps([
                 {"nivel": "Superior (Subir Cabeza)", "y1": 0.28, "y2": 0.54, "producto": "Papas Artesanales 150g"},
                 {"nivel": "Inferior (Agacharse)", "y1": 0.54, "y2": 0.94, "producto": "Detergente Líquido 2L"}
             ]), "#8B5CF6", "SUC-NORTE"),
            ("PER-CEN1", "Percha Centro 1 • Desayuno Express", "Cereales", "CAM-C1",
             0.13, 0.24, 0.22, 0.14,
             json.dumps({"x1": 0.0, "y1": 0.285, "x2": 0.432, "y2": 1.0, "poly": POLY_PER_A1}),
             json.dumps([
                 {"nivel": "Medio (Altura Ojos)", "y1": 0.44, "y2": 0.72, "producto": "Cereales Kellogg's 500g"},
                 {"nivel": "Inferior (Agacharse)", "y1": 0.72, "y2": 0.95, "producto": "Avena Crujiente 400g"},
             ]), "#FE5000", "SUC-CENTRO"),
            ("PER-CEN2", "Percha Centro 2 • Snacks y Almuerzo Rápido", "Snacks", "CAM-C1",
             0.13, 0.62, 0.22, 0.14,
             json.dumps({"x1": 0.655, "y1": 0.29, "x2": 1.0, "y2": 1.0, "poly": POLY_PER_A2}),
             json.dumps([
                 {"nivel": "Medio (Altura Ojos)", "y1": 0.44, "y2": 0.72, "producto": "Papas Artesanales 150g"},
                 {"nivel": "Superior (Subir Cabeza)", "y1": 0.29, "y2": 0.44, "producto": "Salsa de Tomate Artesanal"},
             ]), "#F59E0B", "SUC-CENTRO"),
            ("PER-CEN3", "Percha Centro 3 • Bebidas Frías Oficina", "Bebidas", "CAM-C2",
             0.45, 0.24, 0.24, 0.15,
             json.dumps({"x1": 0.0, "y1": 0.285, "x2": 0.432, "y2": 1.0, "poly": POLY_PER_A1}),
             json.dumps([
                 {"nivel": "Medio (Altura Ojos)", "y1": 0.44, "y2": 0.72, "producto": "Coca Cola Zero 500ml"},
                 {"nivel": "Inferior (Agacharse)", "y1": 0.72, "y2": 0.95, "producto": "Agua Mineral 600ml"},
             ]), "#2563EB", "SUC-CENTRO"),
            ("PER-CEN4", "Percha Centro 4 • Lácteos Grab & Go", "Lácteos", "CAM-C2",
             0.45, 0.62, 0.24, 0.15,
             json.dumps({"x1": 0.655, "y1": 0.29, "x2": 1.0, "y2": 1.0, "poly": POLY_PER_A2}),
             json.dumps([
                 {"nivel": "Medio (Altura Ojos)", "y1": 0.44, "y2": 0.74, "producto": "Yogurt Griego Natural"},
                 {"nivel": "Superior (Subir Cabeza)", "y1": 0.29, "y2": 0.44, "producto": "Leche Entera Tetra 1L"},
             ]), "#10B981", "SUC-CENTRO"),
            ("PER-SUR1", "Percha Sur 1 • Cereales Familiares", "Cereales", "CAM-S1",
             0.12, 0.24, 0.23, 0.14,
             json.dumps({"x1": 0.0, "y1": 0.285, "x2": 0.432, "y2": 1.0, "poly": POLY_PER_A1}),
             json.dumps([
                 {"nivel": "Medio (Altura Ojos)", "y1": 0.44, "y2": 0.72, "producto": "Cereales Kellogg's 500g"},
                 {"nivel": "Superior (Subir Cabeza)", "y1": 0.28, "y2": 0.44, "producto": "Avena Crujiente 400g"},
             ]), "#FE5000", "SUC-SUR"),
            ("PER-SUR2", "Percha Sur 2 • Pastas y Conservas Pack", "Conservas", "CAM-S1",
             0.12, 0.62, 0.23, 0.14,
             json.dumps({"x1": 0.655, "y1": 0.29, "x2": 1.0, "y2": 1.0, "poly": POLY_PER_A2}),
             json.dumps([
                 {"nivel": "Medio (Altura Ojos)", "y1": 0.44, "y2": 0.72, "producto": "Pasta Italiana 500g"},
                 {"nivel": "Inferior (Agacharse)", "y1": 0.72, "y2": 0.95, "producto": "Salsa de Tomate Artesanal"},
             ]), "#F59E0B", "SUC-SUR"),
            ("PER-SUR3", "Percha Sur 3 • Lácteos y Bebidas Pack", "Lácteos", "CAM-S2",
             0.43, 0.24, 0.21, 0.14,
             json.dumps({"x1": 0.0, "y1": 0.285, "x2": 0.432, "y2": 1.0, "poly": POLY_PER_A1}),
             json.dumps([
                 {"nivel": "Medio (Altura Ojos)", "y1": 0.44, "y2": 0.72, "producto": "Leche Entera Tetra 1L"},
                 {"nivel": "Superior (Subir Cabeza)", "y1": 0.28, "y2": 0.44, "producto": "Coca Cola Zero 500ml"},
             ]), "#10B981", "SUC-SUR"),
            ("PER-SUR4", "Percha Sur 4 • Limpieza Mayorista", "Limpieza", "CAM-S3",
             0.70, 0.25, 0.12, 0.48,
             json.dumps({"x1": 0.655, "y1": 0.29, "x2": 1.0, "y2": 1.0, "poly": POLY_PER_A2}),
             json.dumps([
                 {"nivel": "Inferior (Agacharse)", "y1": 0.56, "y2": 0.95, "producto": "Detergente Líquido 2L"},
                 {"nivel": "Superior (Subir Cabeza)", "y1": 0.29, "y2": 0.56, "producto": "Papas Artesanales 150g"},
             ]), "#8B5CF6", "SUC-SUR"),
        ]
        _insertar_en_lotes(
            conn,
            "INSERT OR IGNORE INTO perchas_mapa (id, nombre, categoria, camara_id, map_x, map_y, map_w, map_h, roi_json, niveles_json, color, sucursal_id)",
            perchas_default,
            chunk_size=20,
        )

        conn.execute(
            "UPDATE perchas_mapa SET roi_json = ? WHERE id = 'PER-A1' AND roi_json NOT LIKE '%\"poly\"%'",
            (json.dumps({"x1": 0.0, "y1": 0.285, "x2": 0.432, "y2": 1.0, "poly": POLY_PER_A1}),),
        )
        conn.execute(
            "UPDATE perchas_mapa SET roi_json = ? WHERE id = 'PER-A2' AND roi_json NOT LIKE '%\"poly\"%'",
            (json.dumps({"x1": 0.655, "y1": 0.29, "x2": 1.0, "y2": 1.0, "poly": POLY_PER_A2}),),
        )

        catalogo_default = [
            ("CK-500", "Cereales Kellogg's 500g", "Cereales", "PER-A1", "Medio (Altura Ojos)", 48, 19, 6, "SUC-NORTE"),
            ("AC-400", "Avena Crujiente 400g", "Cereales", "PER-A1", "Superior (Subir Cabeza)", 36, 11, 1, "SUC-NORTE"),
            ("ST-350", "Salsa de Tomate Artesanal", "Conservas", "PER-A2", "Medio (Altura Ojos)", 54, 24, 2, "SUC-NORTE"),
            ("PI-500", "Pasta Italiana 500g", "Conservas", "PER-A2", "Superior (Subir Cabeza)", 42, 16, 2, "SUC-NORTE"),
            ("CC-500", "Coca Cola Zero 500ml", "Bebidas", "PER-B1", "Superior (Subir Cabeza)", 60, 21, 3, "SUC-NORTE"),
            ("LE-100", "Leche Entera Tetra 1L", "Lácteos", "PER-B2", "Superior (Subir Cabeza)", 52, 18, 2, "SUC-NORTE"),
            ("PA-150", "Papas Artesanales 150g", "Snacks", "PER-C1", "Superior (Subir Cabeza)", 38, 14, 1, "SUC-NORTE"),
            ("DL-200", "Detergente Líquido 2L", "Limpieza", "PER-C1", "Inferior (Agacharse)", 28, 5, 2, "SUC-NORTE"),
            ("CEN-CK", "Cereales Kellogg's 500g", "Cereales", "PER-CEN1", "Medio (Altura Ojos)", 32, 12, 2, "SUC-CENTRO"),
            ("CEN-PA", "Papas Artesanales 150g", "Snacks", "PER-CEN2", "Medio (Altura Ojos)", 44, 18, 2, "SUC-CENTRO"),
            ("CEN-CC", "Coca Cola Zero 500ml", "Bebidas", "PER-CEN3", "Medio (Altura Ojos)", 65, 28, 1, "SUC-CENTRO"),
            ("CEN-YG", "Yogurt Griego Natural", "Lácteos", "PER-CEN4", "Medio (Altura Ojos)", 38, 15, 2, "SUC-CENTRO"),
            ("SUR-CK", "Cereales Kellogg's 500g", "Cereales", "PER-SUR1", "Medio (Altura Ojos)", 58, 22, 4, "SUC-SUR"),
            ("SUR-PI", "Pasta Italiana 500g", "Conservas", "PER-SUR2", "Medio (Altura Ojos)", 62, 25, 2, "SUC-SUR"),
            ("SUR-LE", "Leche Entera Tetra 1L", "Lácteos", "PER-SUR3", "Medio (Altura Ojos)", 70, 29, 2, "SUC-SUR"),
            ("SUR-DL", "Detergente Líquido 2L", "Limpieza", "PER-SUR4", "Inferior (Agacharse)", 40, 14, 3, "SUC-SUR"),
        ]
        _insertar_en_lotes(
            conn,
            "INSERT OR IGNORE INTO productos_catalogo (sku, nombre, categoria, percha_id, nivel, stock, tomas_base, devoluciones_base, sucursal_id)",
            catalogo_default,
            chunk_size=30,
        )

        promos_default = [
            ("PROMO-01", "PER-A1", "Cereales Kellogg's 500g", "2x1 en Cereales de Desayuno", "Cenefa Naranja + Stopper Lateral", "¡Lleva 2 al precio de 1!", 50, 1, "SUC-NORTE"),
            ("PROMO-02", "PER-A2", "Salsa de Tomate Artesanal", "Combo Pasta + Salsa -25%", "Exhibidor de Cabecera Amarilla", "Descuento directo llevando combo", 25, 1, "SUC-NORTE"),
            ("PROMO-03", "PER-B1", "Coca Cola Zero 500ml", "Pantalla Digital Bebidas Frías", "Pantalla LED en Góndola Superior", "Precio Especial $1.20", 15, 1, "SUC-NORTE"),
            ("PROMO-04", "PER-C1", "Detergente Líquido 2L", "Etiqueta Roja Ahorro Hogar", "Etiqueta de Precio Nivel Inferior", "Ahorro familiar 10%", 10, 1, "SUC-NORTE"),
            ("PROMO-C1", "PER-CEN3", "Coca Cola Zero 500ml", "Combo Mediodía Bebida + Snack", "Stopper Refrigerador Express", "Ideal hora de almuerzo", 20, 1, "SUC-CENTRO"),
            ("PROMO-C2", "PER-CEN4", "Yogurt Griego Natural", "Desayuno Saludable -15%", "Cenefa Verde Grab & Go", "Mañanas activas", 15, 1, "SUC-CENTRO"),
            ("PROMO-S1", "PER-SUR2", "Pasta Italiana 500g", "Pack Familiar 3x2 Pastas", "Cabecera Mayorista Sur", "Ahorro familiar fin de semana", 33, 1, "SUC-SUR"),
            ("PROMO-S2", "PER-SUR4", "Detergente Líquido 2L", "Mega Bidón Hogar -20%", "Cartel Aéreo Pasillo 3", "Precio mayorista", 20, 1, "SUC-SUR"),
        ]
        _insertar_en_lotes(
            conn,
            "INSERT OR IGNORE INTO promociones_percha (id, percha_id, producto, titulo, tipo_publicidad, mensaje_pop, descuento_pct, activa, sucursal_id)",
            promos_default,
            chunk_size=20,
        )

        sem_row = conn.execute("SELECT valor FROM meta_semilla WHERE clave = 'seed_version'").fetchone()
        cnt_reciente = conn.execute(
            "SELECT COUNT(*) FROM telemetria_coordenadas WHERE creado >= datetime('now', '-24 hours')"
        ).fetchone()[0]
        necesita_semilla = (not sem_row or sem_row[0] != _SEED_VERSION) or cnt_reciente < 18
        limpiar_prev = not sem_row or sem_row[0] != _SEED_VERSION

        # Si se actualiza versión en una base existente (ej. Turso), sincronizar coordenadas del plano 2D
        if limpiar_prev:
            for s_id, plano_fn in (
                ("SUC-NORTE", _plano_arquitectonico_norte),
                ("SUC-CENTRO", _plano_arquitectonico_centro),
                ("SUC-SUR", _plano_arquitectonico_sur),
            ):
                conn.execute("UPDATE sucursales SET plano_json = ? WHERE id = ?", (json.dumps(plano_fn()), s_id))
            for cid, cnom, cx, cy, cang, cfov, calc in (
                ("CAM-01", "Cámara 12 • Pasillo Central y Puerta", 0.16, 0.21, 72.0, 74.0, 0.38),
                ("CAM-02", "Cámara 14 • Góndolas Lácteos y Bebidas", 0.48, 0.21, 90.0, 74.0, 0.38),
                ("CAM-03", "Cámara 16 • Hogar, Cajas y Salida", 0.80, 0.48, 178.0, 70.0, 0.34),
            ):
                conn.execute(
                    "UPDATE camaras_mapa SET nombre = ?, pos_x = ?, pos_y = ?, angulo = ?, fov = ?, alcance = ? WHERE id = ?",
                    (cnom, cx, cy, cang, cfov, calc, cid),
                )
            for pid, mx, my, mw, mh in (
                ("PER-A1", 0.11, 0.25, 0.22, 0.15),
                ("PER-A2", 0.11, 0.62, 0.22, 0.15),
                ("PER-B1", 0.41, 0.25, 0.21, 0.15),
                ("PER-B2", 0.41, 0.62, 0.21, 0.15),
                ("PER-C1", 0.67, 0.26, 0.115, 0.48),
            ):
                conn.execute(
                    "UPDATE perchas_mapa SET map_x = ?, map_y = ?, map_w = ?, map_h = ? WHERE id = ?",
                    (mx, my, mw, mh, pid),
                )

        conn.commit()
    finally:
        _cerrar_si_local(conn)
    invalidar_cache_metadatos()

    if necesita_semilla:
        if TURSO_URL:
            # En Render + Turso remoto, sembrar en segundo plano para abrir $PORT en < 2 segundos sin timeout.
            threading.Thread(
                target=_sembrar_en_segundo_plano,
                args=(limpiar_prev,),
                daemon=True,
                name="KovaqTursoSeeder",
            ).start()
        else:
            conn_seed = _abrir()
            try:
                _sembrar_historico_muestra(conn_seed, limpiar_previos=limpiar_prev)
                conn_seed.execute("INSERT OR REPLACE INTO meta_semilla (clave, valor) VALUES ('seed_version', ?)", (_SEED_VERSION,))
                conn_seed.commit()
            finally:
                _cerrar_si_local(conn_seed)


def _sembrar_en_segundo_plano(limpiar_previos: bool):
    try:
        conn = _abrir()
        _sembrar_historico_muestra(conn, limpiar_previos=limpiar_previos)
        conn.execute("INSERT OR REPLACE INTO meta_semilla (clave, valor) VALUES ('seed_version', ?)", (_SEED_VERSION,))
        conn.commit()
        log.info("Semilla histórica multi-sucursal (%s) completada en segundo plano.", _SEED_VERSION)
    except Exception as err:
        log.warning("Aviso ejecutando semilla en segundo plano: %s", err)


def _sembrar_historico_muestra(conn, limpiar_previos: bool = False):
    """
    Genera una muestra histórica realista de los últimos 90 días (incluyendo densidad horaria
    en las últimas 24 horas y última hora) distribuida en las 3 sucursales corporativas.
    """
    from datetime import datetime, timedelta, timezone
    ahora = datetime.now(timezone.utc)

    if limpiar_previos:
        # Limpia eventos y coordenadas heredadas/sintéticas para garantizar coherencia 1-a-1 (tomados <= trafico y devueltos exactos)
        conn.execute("DELETE FROM telemetria_coordenadas")
        conn.execute("DELETE FROM eventos")

    perchas_por_suc = {
        "SUC-NORTE": [
            ("PER-A1", "Medio (Altura Ojos)", "giro_percha_izq", "ultimo_momento_devuelve", "Cereales Kellogg's 500g", 4.4, "CAM-01", 0.22, 0.30),
            ("PER-A1", "Medio (Altura Ojos)", "giro_percha_izq", "mira_y_compra", "Cereales Kellogg's 500g", 3.8, "CAM-01", 0.24, 0.32),
            ("PER-A1", "Superior (Subir Cabeza)", "sube_cabeza", "solo_mira", "Avena Crujiente 400g", 2.6, "CAM-01", 0.27, 0.31),
            ("PER-A2", "Medio (Altura Ojos)", "giro_percha_der", "mira_y_compra", "Salsa de Tomate Artesanal", 4.8, "CAM-01", 0.22, 0.64),
            ("PER-A2", "Superior (Subir Cabeza)", "sube_cabeza", "mira_y_compra", "Pasta Italiana 500g", 3.4, "CAM-01", 0.26, 0.62),
            ("PER-B1", "Superior (Subir Cabeza)", "sube_cabeza", "no_mira_y_compra", "Coca Cola Zero 500ml", 1.3, "CAM-02", 0.50, 0.30),
            ("PER-B1", "Medio (Altura Ojos)", "giro_percha_izq", "mira_y_compra", "Agua Mineral 600ml", 2.9, "CAM-02", 0.53, 0.33),
            ("PER-B2", "Superior (Subir Cabeza)", "giro_percha_der", "mira_y_compra", "Leche Entera Tetra 1L", 3.6, "CAM-02", 0.51, 0.64),
            ("PER-B2", "Medio (Altura Ojos)", "giro_percha_der", "ultimo_momento_devuelve", "Yogurt Griego Natural", 4.1, "CAM-02", 0.54, 0.63),
            ("PER-C1", "Inferior (Agacharse)", "se_agacha", "solo_mira", "Detergente Líquido 2L", 3.9, "CAM-03", 0.74, 0.52),
            ("PER-C1", "Superior (Subir Cabeza)", "sube_cabeza", "no_mira_y_compra", "Papas Artesanales 150g", 1.5, "CAM-03", 0.74, 0.34),
        ],
        "SUC-CENTRO": [
            ("PER-CEN3", "Medio (Altura Ojos)", "giro_percha_izq", "mira_y_compra", "Coca Cola Zero 500ml", 2.8, "CAM-C2", 0.54, 0.32),
            ("PER-CEN3", "Medio (Altura Ojos)", "sube_cabeza", "no_mira_y_compra", "Coca Cola Zero 500ml", 1.1, "CAM-C2", 0.58, 0.34),
            ("PER-CEN2", "Medio (Altura Ojos)", "giro_percha_der", "mira_y_compra", "Papas Artesanales 150g", 3.2, "CAM-C1", 0.24, 0.64),
            ("PER-CEN4", "Medio (Altura Ojos)", "giro_percha_der", "mira_y_compra", "Yogurt Griego Natural", 3.5, "CAM-C2", 0.56, 0.64),
            ("PER-CEN1", "Medio (Altura Ojos)", "giro_percha_izq", "ultimo_momento_devuelve", "Cereales Kellogg's 500g", 4.0, "CAM-C1", 0.22, 0.31),
            ("PER-CEN1", "Inferior (Agacharse)", "se_agacha", "solo_mira", "Avena Crujiente 400g", 2.4, "CAM-C1", 0.26, 0.33),
        ],
        "SUC-SUR": [
            ("PER-SUR2", "Medio (Altura Ojos)", "giro_percha_der", "mira_y_compra", "Pasta Italiana 500g", 4.5, "CAM-S1", 0.22, 0.64),
            ("PER-SUR3", "Medio (Altura Ojos)", "giro_percha_izq", "mira_y_compra", "Leche Entera Tetra 1L", 3.7, "CAM-S2", 0.51, 0.32),
            ("PER-SUR1", "Medio (Altura Ojos)", "giro_percha_izq", "ultimo_momento_devuelve", "Cereales Kellogg's 500g", 4.6, "CAM-S1", 0.23, 0.31),
            ("PER-SUR1", "Superior (Subir Cabeza)", "sube_cabeza", "mira_y_compra", "Cereales Kellogg's 500g", 3.1, "CAM-S1", 0.26, 0.32),
            ("PER-SUR4", "Inferior (Agacharse)", "se_agacha", "solo_mira", "Detergente Líquido 2L", 4.2, "CAM-S3", 0.74, 0.54),
            ("PER-SUR4", "Superior (Subir Cabeza)", "sube_cabeza", "no_mira_y_compra", "Papas Artesanales 150g", 1.6, "CAM-S3", 0.74, 0.36),
        ],
    }

    horas_por_suc = {
        "SUC-NORTE": [9, 10, 11, 12, 15, 16, 17, 18, 19, 20],
        "SUC-CENTRO": [8, 9, 11, 12, 13, 14, 17, 18, 19],
        "SUC-SUR": [8, 9, 10, 11, 14, 15, 16, 17, 18, 19, 20, 21],
    }

    eventos_batch = []
    coords_batch = []

    # 1. Histórico 90 días atrás hasta ayer
    for suc_idx, (suc_id, prods_list) in enumerate(perchas_por_suc.items()):
        horas_suc = horas_por_suc[suc_id]
        for d_back in range(90, 0, -1):
            dia_dt = ahora - timedelta(days=d_back)
            es_finde = dia_dt.weekday() in (4, 5, 6)
            base_vis = 5 if suc_id == "SUC-NORTE" else (4 if suc_id == "SUC-SUR" else 3)
            visitas_dia = base_vis + (2 if es_finde else 0)

            for v in range(visitas_dia):
                h = horas_suc[(d_back * 3 + v * 5 + suc_idx * 2) % len(horas_suc)]
                m = (d_back * 7 + v * 13 + suc_idx * 11) % 58
                ts_str = dia_dt.replace(hour=h, minute=m, second=15).strftime("%Y-%m-%d %H:%M:%S")
                sesion_id = f"VEC-{suc_idx}{d_back:02d}-{v:02d}"

                eventos_batch.append(("trafico", None, None, suc_id, ts_str))
                p_info = prods_list[(d_back + v * 2 + suc_idx) % len(prods_list)]
                pid, niv, post, comp, prod, dwell, cam_id, mx, my = p_info
                # Pequeña dispersión realista alrededor de la percha para mapa de calor orgánico
                jx = round(max(0.08, min(0.84, mx + (((d_back + v) % 5) - 2) * 0.018)), 4)
                jy = round(max(0.18, min(0.86, my + (((d_back * 2 + v) % 5) - 2) * 0.022)), 4)

                eventos_batch.append(("permanencia", pid, dwell, suc_id, ts_str))
                coords_batch.append((sesion_id, 1, jx, jy, pid, niv, post, comp, prod, dwell, cam_id, suc_id, ts_str))

                if comp in ("mira_y_compra", "no_mira_y_compra"):
                    eventos_batch.append(("producto_tomado", f"{prod} [{pid}] ({sesion_id})", float(v + 1), suc_id, ts_str))
                elif comp == "ultimo_momento_devuelve":
                    eventos_batch.append(("producto_tomado", f"{prod} [{pid}] ({sesion_id})", float(v + 1), suc_id, ts_str))
                    eventos_batch.append(("producto_devuelto", f"{prod} [{pid}] ({sesion_id})", float(v + 1), suc_id, ts_str))

            if d_back % 4 == 0:
                ts_staff = dia_dt.replace(hour=14, minute=20, second=0).strftime("%Y-%m-%d %H:%M:%S")
                coords_batch.append((
                    f"STAFF-{suc_idx}-{d_back:02d}", 0, 0.28, 0.14, prods_list[0][0],
                    "Medio (Altura Ojos)", "frente", "empleado_excluido", "", 6.5, prods_list[0][6], suc_id, ts_staff
                ))

        # 2. Densidad horaria de las últimas 24 horas (incluyendo la última hora!) para que 1h/6h/12h/Hoy siempre muestren curvas ricas
        for h_back in range(23, -1, -1):
            hora_dt = ahora - timedelta(hours=h_back)
            # Curva de afluencia por hora (mayor en horario comercial y última hora)
            vis_h = 3 if (h_back <= 2 or 11 <= hora_dt.hour <= 20) else 1
            for v in range(vis_h):
                min_off = max(1, min(55, (v * 17 + suc_idx * 9) % 54))
                ts_dt = ahora - timedelta(minutes=(h_back * 60 + (min_off if h_back > 0 else (v * 14 + 3))))
                ts_str = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
                sesion_id = f"VEC-H{suc_idx}-{h_back:02d}-{v}"

                eventos_batch.append(("trafico", None, None, suc_id, ts_str))
                p_info = prods_list[(h_back + v * 2 + suc_idx) % len(prods_list)]
                pid, niv, post, comp, prod, dwell, cam_id, mx, my = p_info
                jx = round(max(0.08, min(0.84, mx + (((h_back + v) % 5) - 2) * 0.016)), 4)
                jy = round(max(0.18, min(0.86, my + (((h_back * 3 + v) % 5) - 2) * 0.020)), 4)

                eventos_batch.append(("permanencia", pid, dwell, suc_id, ts_str))
                coords_batch.append((sesion_id, 1, jx, jy, pid, niv, post, comp, prod, dwell, cam_id, suc_id, ts_str))

                if comp in ("mira_y_compra", "no_mira_y_compra"):
                    eventos_batch.append(("producto_tomado", f"{prod} [{pid}] ({sesion_id})", float(v + 1), suc_id, ts_str))
                elif comp == "ultimo_momento_devuelve":
                    eventos_batch.append(("producto_tomado", f"{prod} [{pid}] ({sesion_id})", float(v + 1), suc_id, ts_str))
                    eventos_batch.append(("producto_devuelto", f"{prod} [{pid}] ({sesion_id})", float(v + 1), suc_id, ts_str))

        # 1 staff excluido reciente por sucursal
        ts_st_now = (ahora - timedelta(minutes=22)).strftime("%Y-%m-%d %H:%M:%S")
        coords_batch.append((
            f"STAFF-H{suc_idx}", 0, 0.30, 0.14, prods_list[0][0],
            "Medio (Altura Ojos)", "frente", "empleado_excluido", "", 5.2, prods_list[0][6], suc_id, ts_st_now
        ))

    _insertar_en_lotes(
        conn,
        "INSERT INTO eventos (tipo, detalle, valor, sucursal_id, creado)",
        eventos_batch,
        chunk_size=140,
    )
    _insertar_en_lotes(
        conn,
        """INSERT INTO telemetria_coordenadas
        (sesion_anonima, es_cliente_valido, x_norm, y_norm, percha_id, nivel_percha,
         postura_cabeza, comportamiento, producto, dwell_sec, camara_id, sucursal_id, creado)""",
        coords_batch,
        chunk_size=60,
    )


def regenerar_simulacion_muestra():
    """Permite regenerar datos ficticios frescos desde la UI o mantenimiento."""
    conn = _abrir()
    try:
        _sembrar_historico_muestra(conn, limpiar_previos=True)
        conn.execute("INSERT OR REPLACE INTO meta_semilla (clave, valor) VALUES ('seed_version', ?)", (_SEED_VERSION,))
        conn.commit()
    finally:
        _cerrar_si_local(conn)
    invalidar_cache_metadatos()


def _construir_filtro_fecha(
    dias: int = 30,
    fecha_inicio: str = None,
    fecha_fin: str = None,
    col: str = "creado",
    horas: int = None,
    hora_inicio: int = None,
    hora_fin: int = None,
    sucursal_id: str = None,
):
    """Construye cláusula SQL segura para rango de fechas, ventana de horas, franja horaria y sucursal."""
    if col not in _COLUMNAS_FECHA_VALIDAS:
        raise ValueError("Columna de fecha no permitida")

    clausulas = []
    params = []

    fi = fecha_inicio.strip() if fecha_inicio else None
    ff = fecha_fin.strip() if fecha_fin else None
    if fi and not _FECHA_ISO_RE.match(fi):
        raise ValueError("fecha_inicio debe tener formato YYYY-MM-DD")
    if ff and not _FECHA_ISO_RE.match(ff):
        raise ValueError("fecha_fin debe tener formato YYYY-MM-DD")

    if horas is not None:
        horas_val = max(1, min(168, int(horas)))
        clausulas.append(f"{col} >= datetime('now', ?)")
        params.append(f"-{horas_val} hours")
    elif fi and ff:
        clausulas.append(f"{col} >= datetime(?) AND {col} <= datetime(?, '+23 hours', '+59 minutes', '+59 seconds')")
        params.extend([fi, ff])
    elif fi:
        clausulas.append(f"{col} >= datetime(?)")
        params.append(fi)
    else:
        dias_val = max(1, min(365, int(dias or 30)))
        clausulas.append(f"{col} >= datetime('now', ?)")
        params.append(f"-{dias_val} days")

    if hora_inicio is not None or hora_fin is not None:
        hi = max(0, min(23, int(hora_inicio if hora_inicio is not None else 0)))
        hf = max(0, min(23, int(hora_fin if hora_fin is not None else 23)))
        if hi <= hf:
            clausulas.append(f"CAST(strftime('%H', {col}) AS INTEGER) BETWEEN ? AND ?")
            params.extend([hi, hf])
        else:
            clausulas.append(f"(CAST(strftime('%H', {col}) AS INTEGER) >= ? OR CAST(strftime('%H', {col}) AS INTEGER) <= ?)")
            params.extend([hi, hf])

    suc = (sucursal_id or "").strip()
    if suc and suc.lower() != "todas":
        clausulas.append("sucursal_id = ?")
        params.append(suc)

    return " AND ".join(clausulas), tuple(params)


async def init_analytics():
    await asyncio.to_thread(init_analytics_sync)


# --- Gestión de Sucursales ---
def obtener_sucursales():
    filas = leer("""
        SELECT id, nombre, ciudad, direccion, formato, area_m2, plano_json, estado
        FROM sucursales ORDER BY id
    """)
    res = []
    for f in filas:
        try:
            plano = json.loads(f[6] or "{}")
        except Exception:
            plano = _plano_arquitectonico_norte()
        res.append({
            "id": f[0],
            "nombre": f[1],
            "ciudad": f[2],
            "direccion": f[3],
            "formato": f[4],
            "area_m2": int(f[5] or 1200),
            "plano": plano,
            "estado": f[7],
        })
    return res


def registrar_sucursal(suc_id: str, nombre: str, ciudad: str = "Quito", direccion: str = "", formato: str = "Supermercado", area_m2: int = 1200):
    plano = _plano_arquitectonico_norte()
    plano["nombre_plano"] = f"Planta Comercial • {formato} ({area_m2:,} m²)"
    escribir("""
        INSERT OR REPLACE INTO sucursales
        (id, nombre, ciudad, direccion, formato, area_m2, plano_json, estado)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'Activa')
    """, (suc_id, nombre, ciudad, direccion, formato, int(area_m2 or 1200), json.dumps(plano)))


# --- Consultas de Mapeo de Cámaras, Planograma de Perchas, Promociones y Catálogo ---
def obtener_camaras_mapa(sucursal_id: str = None):
    suc = (sucursal_id or "").strip()
    if suc and suc.lower() != "todas":
        filas = leer("""
            SELECT id, nombre, fuente, ruta, estado, pos_x, pos_y, angulo, fov, alcance, homografia_json, sucursal_id
            FROM camaras_mapa WHERE sucursal_id = ? ORDER BY id
        """, (suc,))
    else:
        filas = leer("""
            SELECT id, nombre, fuente, ruta, estado, pos_x, pos_y, angulo, fov, alcance, homografia_json, sucursal_id
            FROM camaras_mapa ORDER BY id
        """)
    res = []
    for f in filas:
        try:
            hom = json.loads(f[10] or "{}")
        except Exception:
            hom = {}
        res.append({
            "id": f[0], "nombre": f[1], "fuente": f[2], "ruta": f[3], "estado": f[4],
            "pos_x": float(f[5]), "pos_y": float(f[6]), "angulo": float(f[7]),
            "fov": float(f[8]), "alcance": float(f[9]), "zona_2d": hom,
            "sucursal_id": f[11] or "SUC-NORTE",
        })
    return res


def registrar_o_actualizar_camara_mapa(cam_id: str, nombre: str, ruta: str, estado: str = "Activa",
                                       pos_x: float = 0.5, pos_y: float = 0.5, angulo: float = 45.0,
                                       sucursal_id: str = "SUC-NORTE"):
    fuente = "rtsp" if str(ruta).startswith("rtsp") else ("webcam" if str(ruta).isdigit() else "video")
    zona_2d = json.dumps({
        "x_min": max(0.05, round(pos_x - 0.18, 2)),
        "x_max": min(0.95, round(pos_x + 0.18, 2)),
        "y_min": max(0.08, round(pos_y - 0.25, 2)),
        "y_max": min(0.92, round(pos_y + 0.25, 2)),
        "altura_m": 3.4,
        "pitch_deg": 38
    })
    escribir("""
        INSERT OR REPLACE INTO camaras_mapa
        (id, nombre, fuente, ruta, estado, pos_x, pos_y, angulo, fov, alcance, homografia_json, sucursal_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 72.0, 0.42, ?, ?)
    """, (cam_id, nombre, fuente, ruta, estado, float(pos_x), float(pos_y), float(angulo), zona_2d, sucursal_id or "SUC-NORTE"))


def eliminar_camara_mapa(cam_id: str):
    escribir("DELETE FROM camaras_mapa WHERE id = ?", (cam_id,))


def obtener_promociones_percha(sucursal_id: str = None):
    suc = (sucursal_id or "").strip()
    if suc and suc.lower() != "todas":
        filas = leer("""
            SELECT pr.id, pr.percha_id, COALESCE(pm.nombre, pr.percha_id), pr.producto,
                   pr.titulo, pr.tipo_publicidad, pr.mensaje_pop, pr.descuento_pct, pr.activa, pr.sucursal_id
            FROM promociones_percha pr
            LEFT JOIN perchas_mapa pm ON pr.percha_id = pm.id
            WHERE pr.sucursal_id = ?
            ORDER BY pr.id
        """, (suc,))
    else:
        filas = leer("""
            SELECT pr.id, pr.percha_id, COALESCE(pm.nombre, pr.percha_id), pr.producto,
                   pr.titulo, pr.tipo_publicidad, pr.mensaje_pop, pr.descuento_pct, pr.activa, pr.sucursal_id
            FROM promociones_percha pr
            LEFT JOIN perchas_mapa pm ON pr.percha_id = pm.id
            ORDER BY pr.id
        """)
    res = []
    for f in filas:
        res.append({
            "id": f[0],
            "percha_id": f[1],
            "percha_nombre": f[2],
            "producto": f[3],
            "titulo": f[4],
            "tipo_publicidad": f[5],
            "mensaje_pop": f[6],
            "descuento_pct": int(f[7] or 0),
            "activa": bool(f[8]),
            "sucursal_id": f[9] or "SUC-NORTE",
        })
    return res


def registrar_promocion_percha(promo_id: str, percha_id: str, producto: str, titulo: str,
                               tipo_publicidad: str, mensaje_pop: str = "", descuento_pct: int = 0,
                               sucursal_id: str = "SUC-NORTE"):
    escribir("""
        INSERT OR REPLACE INTO promociones_percha
        (id, percha_id, producto, titulo, tipo_publicidad, mensaje_pop, descuento_pct, activa, sucursal_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
    """, (promo_id, percha_id, producto, titulo, tipo_publicidad, mensaje_pop, int(descuento_pct or 0), sucursal_id or "SUC-NORTE"))


def eliminar_promocion_percha(promo_id: str):
    escribir("DELETE FROM promociones_percha WHERE id = ?", (promo_id,))


def obtener_perchas_mapa(
    dias: int = 30,
    fecha_inicio: str = None,
    fecha_fin: str = None,
    promos: list = None,
    horas: int = None,
    hora_inicio: int = None,
    hora_fin: int = None,
    sucursal_id: str = None,
):
    suc = (sucursal_id or "").strip()
    if suc and suc.lower() != "todas":
        filas = leer("""
            SELECT id, nombre, categoria, camara_id, map_x, map_y, map_w, map_h, roi_json, niveles_json, color, sucursal_id
            FROM perchas_mapa WHERE sucursal_id = ? ORDER BY id
        """, (suc,))
    else:
        filas = leer("""
            SELECT id, nombre, categoria, camara_id, map_x, map_y, map_w, map_h, roi_json, niveles_json, color, sucursal_id
            FROM perchas_mapa ORDER BY id
        """)

    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    tomas_rows = leer(f"SELECT detalle, COUNT(*) FROM eventos WHERE tipo = 'producto_tomado' AND {where_ev} GROUP BY detalle", params_ev)
    dev_rows = leer(f"SELECT detalle, COUNT(*) FROM eventos WHERE tipo = 'producto_devuelto' AND {where_ev} GROUP BY detalle", params_ev)
    coords_rows = leer(f"""
        SELECT percha_id, comportamiento, COUNT(*), COALESCE(SUM(dwell_sec), 0)
        FROM telemetria_coordenadas
        WHERE es_cliente_valido = 1 AND {where_ev}
        GROUP BY percha_id, comportamiento
    """, params_ev)
    promos = promos if promos is not None else obtener_promociones_percha(sucursal_id=sucursal_id)
    promos_por_percha = {}
    for pr in promos:
        if pr["activa"]:
            promos_por_percha.setdefault(pr["percha_id"], []).append(pr)

    stats_percha = {}
    dwell_acc = {}
    for pid_c, comp, cnt, sum_d in coords_rows:
        if not pid_c:
            continue
        d = stats_percha.setdefault(pid_c, {
            "mira_y_compra": 0, "no_mira_y_compra": 0, "solo_mira": 0,
            "ultimo_momento_devuelve": 0, "dwell_prom": 0.0
        })
        if comp in d:
            d[comp] += int(cnt)
        acc = dwell_acc.setdefault(pid_c, [0.0, 0])
        acc[0] += float(sum_d or 0.0)
        acc[1] += int(cnt or 0)
    for pid_c, (tot_d, tot_n) in dwell_acc.items():
        if tot_n > 0:
            stats_percha[pid_c]["dwell_prom"] = round(tot_d / tot_n, 1)

    bruto = []
    for f in filas:
        pid = f[0]
        pnombre = f[1]
        try:
            roi = json.loads(f[8] or "{}")
        except Exception:
            roi = {"x1": 0.15, "y1": 0.15, "x2": 0.85, "y2": 0.85}
        try:
            niveles = json.loads(f[9] or "[]")
        except Exception:
            niveles = []

        tomas_ev = sum(cnt for det, cnt in tomas_rows if det and (pid in det or pnombre in det))
        dev_ev = sum(cnt for det, cnt in dev_rows if det and (pid in det or pnombre in det))
        st = stats_percha.get(pid, {
            "mira_y_compra": 0, "no_mira_y_compra": 0, "solo_mira": 0,
            "ultimo_momento_devuelve": 0, "dwell_prom": 0.0
        })
        tomas_coords = st["mira_y_compra"] + st["no_mira_y_compra"] + st["ultimo_momento_devuelve"]
        total_tomas = max(tomas_ev, tomas_coords)
        total_dev = max(dev_ev, st["ultimo_momento_devuelve"])
        interacciones_tot = total_tomas + st["solo_mira"]
        bruto.append((f, roi, niveles, st, total_tomas, total_dev, interacciones_tot))

    # Umbrales relativos al período seleccionado para que 1h, 1d, 7d y 30d distingan zonas calientes/templadas/frías
    max_tomas = max((item[4] for item in bruto), default=1)
    res = []
    for f, roi, niveles, st, total_tomas, total_dev, interacciones_tot in bruto:
        pid, pnombre = f[0], f[1]
        ratio_calor = (total_tomas / max(1, max_tomas)) if max_tomas > 0 else 0.0
        if total_tomas > 0 and ratio_calor >= 0.65:
            temperatura = "caliente"
        elif total_tomas > 0 and ratio_calor >= 0.35:
            temperatura = "templada"
        else:
            temperatura = "fria"

        res.append({
            "id": pid,
            "nombre": pnombre,
            "categoria": f[2],
            "camara_id": f[3],
            "map_x": float(f[4]),
            "map_y": float(f[5]),
            "map_w": float(f[6]),
            "map_h": float(f[7]),
            "roi": roi,
            "niveles": niveles,
            "color": f[10] or "#FE5000",
            "sucursal_id": f[11] or "SUC-NORTE",
            "tomas": total_tomas,
            "devoluciones": total_dev,
            "interacciones": interacciones_tot,
            "temperatura": temperatura,
            "intensidad_calor": round(ratio_calor, 2),
            "comportamiento": st,
            "promociones": promos_por_percha.get(pid, [])
        })
    return res


def obtener_mapa_calor_espacial(
    dias: int = 30,
    fecha_inicio: str = None,
    fecha_fin: str = None,
    horas: int = None,
    hora_inicio: int = None,
    hora_fin: int = None,
    sucursal_id: str = None,
    cols: int = 24,
    rows: int = 12,
):
    """Calcula la matriz de calor espacial 2D directamente desde telemetria_coordenadas del período y sucursal."""
    where_tc, params_tc = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    filas = leer(f"""
        SELECT x_norm, y_norm, dwell_sec, comportamiento
        FROM telemetria_coordenadas
        WHERE es_cliente_valido = 1 AND {where_tc}
    """, params_tc)

    grid = [[0.0 for _ in range(cols)] for _ in range(rows)]
    puntos = []
    for x_n, y_n, dwell, comp in filas:
        x = max(0.0, min(0.999, float(x_n or 0.0)))
        y = max(0.0, min(0.999, float(y_n or 0.0)))
        peso = 1.0 + min(4.0, float(dwell or 0.0) / 2.0)
        if comp in ("mira_y_compra", "no_mira_y_compra"):
            peso *= 1.35
        elif comp == "ultimo_momento_devuelve":
            peso *= 1.15
        c = int(x * cols)
        r = int(y * rows)
        grid[r][c] += peso
        # Suavizado espacial inmediato 3x3
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                rr, cc = r + dr, c + dc
                if 0 <= rr < rows and 0 <= cc < cols:
                    grid[rr][cc] += peso * (0.45 if (dr == 0 or dc == 0) else 0.22)
        if len(puntos) < 250:
            puntos.append({"x": round(x, 3), "y": round(y, 3), "w": round(peso, 2)})

    grid_round = [[round(v, 2) for v in fila] for fila in grid]
    max_val = max((max(fila) for fila in grid_round), default=0.0)
    return {
        "cols": cols,
        "rows": rows,
        "max_intensidad": round(max_val, 2),
        "total_muestras": len(filas),
        "grid": grid_round,
        "puntos": puntos,
    }


def registrar_o_actualizar_percha_mapa(percha_id: str, nombre: str, categoria: str, camara_id: str,
                                       map_x: float, map_y: float, map_w: float, map_h: float,
                                       roi: dict, niveles: list = None, color: str = "#FE5000",
                                       sucursal_id: str = "SUC-NORTE"):
    escribir("""
        INSERT OR REPLACE INTO perchas_mapa
        (id, nombre, categoria, camara_id, map_x, map_y, map_w, map_h, roi_json, niveles_json, color, sucursal_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        percha_id, nombre, categoria, camara_id,
        float(map_x), float(map_y), float(map_w), float(map_h),
        json.dumps(roi), json.dumps(niveles or []), color, sucursal_id or "SUC-NORTE",
    ))
    for idx, niv in enumerate(niveles or []):
        prod_nom = (niv.get("producto") or "").strip()
        niv_nom = (niv.get("nivel") or "Medio").strip()
        if prod_nom:
            sku = f"{percha_id.replace('PER-', '')}-N{idx+1}"
            escribir("""
                INSERT OR IGNORE INTO productos_catalogo
                (sku, nombre, categoria, percha_id, nivel, stock, tomas_base, devoluciones_base, sucursal_id)
                VALUES (?, ?, ?, ?, ?, 24, 8, 1, ?)
            """, (sku, prod_nom, categoria, percha_id, niv_nom, sucursal_id or "SUC-NORTE"))


def obtener_inventario_real(
    dias: int = 30,
    fecha_inicio: str = None,
    fecha_fin: str = None,
    promos: list = None,
    horas: int = None,
    hora_inicio: int = None,
    hora_fin: int = None,
    sucursal_id: str = None,
):
    """Devuelve el catálogo enriquecido con planograma, promociones activas y tomas reales 1x del período."""
    suc = (sucursal_id or "").strip()
    if suc and suc.lower() != "todas":
        filas = leer("""
            SELECT c.sku, c.nombre, c.categoria, c.percha_id, COALESCE(p.nombre, c.percha_id),
                   c.nivel, c.stock, c.tomas_base, c.devoluciones_base, c.sucursal_id
            FROM productos_catalogo c
            LEFT JOIN perchas_mapa p ON c.percha_id = p.id
            WHERE c.sucursal_id = ?
            ORDER BY c.id
        """, (suc,))
    else:
        filas = leer("""
            SELECT c.sku, c.nombre, c.categoria, c.percha_id, COALESCE(p.nombre, c.percha_id),
                   c.nivel, c.stock, c.tomas_base, c.devoluciones_base, c.sucursal_id
            FROM productos_catalogo c
            LEFT JOIN perchas_mapa p ON c.percha_id = p.id
            ORDER BY c.id
        """)

    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    tomas_db = dict(leer(f"""
        SELECT detalle, COUNT(*) FROM eventos
        WHERE tipo = 'producto_tomado' AND {where_ev} GROUP BY detalle
    """, params_ev))
    dev_db = dict(leer(f"""
        SELECT detalle, COUNT(*) FROM eventos
        WHERE tipo = 'producto_devuelto' AND {where_ev} GROUP BY detalle
    """, params_ev))
    promos = promos if promos is not None else obtener_promociones_percha(sucursal_id=sucursal_id)
    promo_por_prod = {(pr["producto"].lower(), pr["percha_id"]): pr for pr in promos if pr["activa"]}
    promo_por_percha = {pr["percha_id"]: pr for pr in promos if pr["activa"]}

    trafico_total = max(1, (leer(f"SELECT COUNT(*) FROM eventos WHERE tipo = 'trafico' AND {where_ev}", params_ev, uno=True) or [0])[0])

    items = []
    for sku, nombre, cat, pid, pnombre, nivel, stock, _t_base, _d_base, suc_row in filas:
        total_tomas = int(sum(
            cnt for det, cnt in tomas_db.items()
            if det and (pid in det or (nombre.lower() in det.lower() and "[" not in det))
            and nombre.lower() in det.lower()
        ))
        total_dev = int(sum(
            cnt for det, cnt in dev_db.items()
            if det and (pid in det or (nombre.lower() in det.lower() and "[" not in det))
            and nombre.lower() in det.lower()
        ))
        netos = max(0, total_tomas - total_dev)
        stock_actual = max(0, int(stock) - min(int(stock), netos // 2))
        evacuacion = round(min(100.0, (netos / max(1, trafico_total)) * 100.0), 1)
        promo = promo_por_prod.get((nombre.lower(), pid)) or promo_por_percha.get(pid)

        if total_dev >= 2 and (total_dev / max(1, total_tomas)) >= 0.22:
            estado = "Indecisión / Cambio Último Momento"
        elif stock_actual <= 5:
            estado = "Crítico (Reponer)"
        elif evacuacion >= 12.0:
            estado = "Zona Caliente (Alta Rotación)"
        elif evacuacion < 4.0:
            estado = "Zona Fría (Lento)"
        else:
            estado = "Óptimo"

        items.append({
            "sku": sku,
            "nombre": nombre,
            "categoria": cat,
            "percha_id": pid,
            "percha_nombre": pnombre,
            "nivel": nivel,
            "sucursal_id": suc_row or "SUC-NORTE",
            "stock": stock_actual,
            "tomas": total_tomas,
            "devueltos": total_dev,
            "llevados_netos": netos,
            "tasa_evacuacion": evacuacion,
            "promocion_activa": promo["titulo"] if promo else "Sin promo activa",
            "tipo_publicidad": promo["tipo_publicidad"] if promo else "Orgánico",
            "estado": estado
        })
    return items


def obtener_resumen_ejecutivo_publicidad(
    dias: int = 30,
    fecha_inicio: str = None,
    fecha_fin: str = None,
    horas: int = None,
    hora_inicio: int = None,
    hora_fin: int = None,
    sucursal_id: str = None,
):
    """Cruza coordenadas anónimas, planograma y promociones activas del período y sucursal."""
    promos = obtener_promociones_percha(sucursal_id=sucursal_id)
    perchas = obtener_perchas_mapa(
        dias, fecha_inicio, fecha_fin, promos=promos,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    inventario = obtener_inventario_real(
        dias, fecha_inicio, fecha_fin, promos=promos,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    where_tc, params_tc = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )

    filas_comp = leer(f"""
        SELECT comportamiento, COUNT(*), COALESCE(AVG(dwell_sec), 0)
        FROM telemetria_coordenadas
        WHERE es_cliente_valido = 1 AND {where_tc}
        GROUP BY comportamiento
    """, params_tc)
    comp_map = {
        "mira_y_compra": {"casos": 0, "dwell": 0.0},
        "no_mira_y_compra": {"casos": 0, "dwell": 0.0},
        "solo_mira": {"casos": 0, "dwell": 0.0},
        "ultimo_momento_devuelve": {"casos": 0, "dwell": 0.0},
    }
    for comp, cnt, avg_d in filas_comp:
        if comp in comp_map:
            comp_map[comp]["casos"] += int(cnt)
            comp_map[comp]["dwell"] = round(float(avg_d or 0.0), 1)

    filas_postura = leer(f"""
        SELECT postura_cabeza, COUNT(*)
        FROM telemetria_coordenadas
        WHERE es_cliente_valido = 1 AND {where_tc}
        GROUP BY postura_cabeza
    """, params_tc)
    postura_map = {
        "giro_percha": 0,
        "sube_cabeza": 0,
        "se_agacha": 0,
        "frente_pasillo": 0
    }
    for post, cnt in filas_postura:
        if post in ("giro_percha_izq", "giro_percha_der", "giro_percha"):
            postura_map["giro_percha"] += int(cnt)
        elif post == "sube_cabeza":
            postura_map["sube_cabeza"] += int(cnt)
        elif post == "se_agacha":
            postura_map["se_agacha"] += int(cnt)
        else:
            postura_map["frente_pasillo"] += int(cnt)

    fila_priv = leer(f"""
        SELECT
            COALESCE(SUM(CASE WHEN es_cliente_valido = 0 THEN 1 ELSE 0 END), 0),
            COUNT(DISTINCT CASE WHEN es_cliente_valido = 1 THEN sesion_anonima END)
        FROM telemetria_coordenadas
        WHERE {where_tc}
    """, params_tc, uno=True) or (0, 0)
    empleados_excluidos = int(fila_priv[0] or 0)
    clientes_validados = int(fila_priv[1] or 0)

    perchas_ordenadas = sorted(perchas, key=lambda p: p["tomas"], reverse=True)
    calientes_filtradas = [p for p in perchas_ordenadas if p.get("temperatura") == "caliente"] or perchas_ordenadas[:2]
    ids_calientes = {p["id"] for p in calientes_filtradas[:3]}
    zonas_calientes = [
        {
            "id": p["id"],
            "nombre": p["nombre"],
            "tomas": p["tomas"],
            "devoluciones": p["devoluciones"],
            "sucursal_id": p.get("sucursal_id", "SUC-NORTE"),
            "promocion": p["promociones"][0]["titulo"] if p.get("promociones") else "Tráfico Orgánico"
        }
        for p in calientes_filtradas[:3]
    ]
    candidatas_frias = [
        p for p in reversed(perchas_ordenadas)
        if p["id"] not in ids_calientes and p.get("temperatura") != "caliente"
    ]
    zonas_frias = [
        {
            "id": p["id"],
            "nombre": p["nombre"],
            "tomas": p["tomas"],
            "devoluciones": p["devoluciones"],
            "sucursal_id": p.get("sucursal_id", "SUC-NORTE"),
            "promocion": p["promociones"][0]["titulo"] if p.get("promociones") else "Sin Pauta POP"
        }
        for p in candidatas_frias[:2]
    ]

    mas_llevados = sorted(inventario, key=lambda x: x["llevados_netos"], reverse=True)[:5]
    mayor_indecision = sorted(inventario, key=lambda x: x["devueltos"], reverse=True)[:4]

    evaluacion_promos = []
    for pr in promos:
        prod_match = next(
            (it for it in inventario if it["percha_id"] == pr["percha_id"] and it["nombre"].lower() == pr["producto"].lower()),
            next((it for it in inventario if it["nombre"].lower() == pr["producto"].lower()), None),
        )
        tomas_p = prod_match["tomas"] if prod_match else 0
        dev_p = prod_match["devueltos"] if prod_match else 0
        nivel_p = (prod_match["nivel"] if prod_match else "").lower()
        netos_p = max(0, tomas_p - dev_p)
        retencion_pct = round((netos_p / max(1, tomas_p)) * 100.0, 1) if tomas_p > 0 else 0.0
        ratio_dev = (dev_p / tomas_p) if tomas_p > 0 else 0.0

        if dev_p >= 2 and ratio_dev >= 0.22:
            diagnostico = "Capta la atención inicial, pero presenta devoluciones de último momento por precio o comparación con el competidor."
            estado_efecto = "Fricción de Precio / Sustitución"
        elif "inferior" in nivel_p or tomas_p < 3:
            diagnostico = "Baja visibilidad en el rango consultado (zona fría o nivel inferior). Conviene reubicar a altura de los ojos o reforzar stopper."
            estado_efecto = "Baja Visibilidad (Zona Fría)"
        else:
            diagnostico = "Atracción visual consistente y cierre efectivo en percha dentro del período analizado."
            estado_efecto = "Alta Conversión POP"

        evaluacion_promos.append({
            "id": pr["id"],
            "percha_id": pr["percha_id"],
            "percha_nombre": pr["percha_nombre"],
            "sucursal_id": pr.get("sucursal_id", "SUC-NORTE"),
            "producto": pr["producto"],
            "titulo": pr["titulo"],
            "tipo_publicidad": pr["tipo_publicidad"],
            "descuento_pct": pr["descuento_pct"],
            "tomas": tomas_p,
            "devueltos": dev_p,
            "llevados_netos": netos_p,
            "efectividad_pct": retencion_pct,
            "estado_efecto": estado_efecto,
            "diagnostico": diagnostico
        })

    return {
        "sucursal_id": sucursal_id or "todas",
        "privacidad": {
            "modo": "Anonimización Coordenadas 2D Activa (Cero Biometría / GDPR y LOPDP)",
            "clientes_validados_puerta_caja": clientes_validados,
            "empleados_excluidos": empleados_excluidos
        },
        "comportamiento_visual": comp_map,
        "postura_cabeza": postura_map,
        "zonas_calientes": zonas_calientes,
        "zonas_frias": zonas_frias,
        "productos_mas_llevados": mas_llevados,
        "productos_cambio_ultimo_momento": mayor_indecision,
        "evaluacion_promociones": evaluacion_promos
    }


def obtener_serie_temporal_periodo(
    agrupacion: str = "dia",
    dias: int = 30,
    fecha_inicio: str = None,
    fecha_fin: str = None,
    horas: int = None,
    hora_inicio: int = None,
    hora_fin: int = None,
    sucursal_id: str = None,
):
    """
    Agrupa los datos históricos por:
    - 'hora'   -> YYYY-MM-DD HH:00 (o HH:00 en ventanas <= 24h)
    - 'dia'    -> YYYY-MM-DD
    - 'semana' -> Año-Semana (YYYY-Snn)
    - 'mes'    -> Año-Mes (YYYY-MM)
    """
    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    if agrupacion == "hora":
        expr_periodo = "strftime('%Y-%m-%d %H:00', creado)"
    elif agrupacion == "semana":
        expr_periodo = "strftime('%Y-S%W', creado)"
    elif agrupacion == "mes":
        expr_periodo = "strftime('%Y-%m', creado)"
    else:
        expr_periodo = "date(creado)"

    filas = leer(f"""
        SELECT
            {expr_periodo} AS periodo,
            SUM(CASE WHEN tipo = 'trafico' THEN 1 ELSE 0 END) AS trafico,
            SUM(CASE WHEN tipo = 'producto_tomado' THEN 1 ELSE 0 END) AS tomados,
            SUM(CASE WHEN tipo = 'producto_devuelto' THEN 1 ELSE 0 END) AS devueltos,
            ROUND(COALESCE(AVG(CASE WHEN tipo = 'permanencia' THEN valor END), 0), 1) AS permanencia
        FROM eventos
        WHERE {where_ev}
        GROUP BY periodo
        ORDER BY periodo ASC
    """, params_ev)

    labels = []
    for r in filas:
        if not r[0]:
            continue
        lbl = str(r[0])
        if agrupacion == "hora" and ((horas is not None and horas <= 24) or (dias == 1 and not fecha_inicio)):
            lbl = lbl[11:16]
        labels.append(lbl)

    validas = [r for r in filas if r[0]]
    trafico_list = [int(r[1] or 0) for r in validas]
    tomados_list = [int(r[2] or 0) for r in validas]
    devueltos_list = [int(r[3] or 0) for r in validas]
    return {
        "agrupacion": agrupacion,
        "labels": labels,
        "trafico": trafico_list,
        "interacciones": tomados_list,
        "tomados": tomados_list,
        "llevados": tomados_list,
        "devueltos": devueltos_list,
        "permanencia": [float(r[4] or 0.0) for r in validas],
    }


def obtener_heatmap_hora(dias=7, fecha_inicio: str = None, fecha_fin: str = None,
                         horas: int = None, hora_inicio: int = None, hora_fin: int = None, sucursal_id: str = None):
    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    return leer(f"""
        SELECT strftime('%H', creado) as hora, COUNT(*) as total
        FROM eventos WHERE tipo = 'trafico' AND {where_ev}
        GROUP BY hora ORDER BY hora
    """, params_ev)


def obtener_heatmap_dia_hora(dias=30, fecha_inicio: str = None, fecha_fin: str = None,
                             horas: int = None, hora_inicio: int = None, hora_fin: int = None, sucursal_id: str = None):
    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    return leer(f"""
        SELECT strftime('%w', creado) as dia, strftime('%H', creado) as hora, COUNT(*) as total
        FROM eventos WHERE tipo = 'trafico' AND {where_ev}
        GROUP BY dia, hora
    """, params_ev)


def obtener_interacciones_por_hora(dias=7, fecha_inicio: str = None, fecha_fin: str = None,
                                   horas: int = None, hora_inicio: int = None, hora_fin: int = None, sucursal_id: str = None):
    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    return leer(f"""
        SELECT strftime('%H', creado) as hora, detalle as producto, COUNT(*) as total
        FROM eventos WHERE tipo IN ('producto_tomado', 'interaccion') AND {where_ev}
        GROUP BY hora, producto ORDER BY hora, total DESC
    """, params_ev)


def obtener_top_productos(limite=10, dias=30, fecha_inicio: str = None, fecha_fin: str = None,
                          horas: int = None, hora_inicio: int = None, hora_fin: int = None, sucursal_id: str = None):
    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    expr_sin_par = "TRIM(CASE WHEN instr(detalle, ' (') > 0 THEN substr(detalle, 1, instr(detalle, ' (') - 1) ELSE detalle END)"
    expr_prod = f"TRIM(CASE WHEN instr({expr_sin_par}, ' [') > 0 THEN substr({expr_sin_par}, 1, instr({expr_sin_par}, ' [') - 1) ELSE {expr_sin_par} END)"
    return leer(f"""
        SELECT {expr_prod} as producto, COUNT(*) as interacciones
        FROM eventos WHERE tipo IN ('producto_tomado', 'interaccion') AND {where_ev}
        GROUP BY producto ORDER BY interacciones DESC LIMIT ?
    """, (*params_ev, limite))


def buscar_productos(termino):
    limpio = str(termino or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return leer("""
        SELECT detalle as producto, COUNT(*) as interacciones
        FROM eventos
        WHERE tipo IN ('producto_tomado', 'interaccion')
          AND LOWER(detalle) LIKE LOWER(?) ESCAPE '\\'
        GROUP BY producto ORDER BY interacciones DESC LIMIT 50
    """, (f"%{limpio}%",))


def obtener_categorias_con_productos(dias=90, sucursal_id: str = None):
    where_ev, params_ev = _construir_filtro_fecha(dias, None, None, "creado", sucursal_id=sucursal_id)
    return leer(f"""
        SELECT c.nombre as categoria, pc.producto,
               COALESCE(cnt.total, 0) as interacciones
        FROM categorias c
        LEFT JOIN producto_categoria pc ON c.id = pc.categoria_id
        LEFT JOIN (
            SELECT detalle, COUNT(*) as total
            FROM eventos WHERE tipo IN ('producto_tomado', 'interaccion') AND {where_ev} GROUP BY detalle
        ) cnt ON pc.producto = cnt.detalle
        ORDER BY c.nombre, interacciones DESC
    """, params_ev)


def obtener_stats_por_franja(dias=7, fecha_inicio: str = None, fecha_fin: str = None,
                             horas: int = None, hora_inicio: int = None, hora_fin: int = None, sucursal_id: str = None):
    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    return leer(f"""
        SELECT
            CASE
                WHEN CAST(strftime('%H', creado) AS INTEGER) BETWEEN 6 AND 11  THEN 'manana'
                WHEN CAST(strftime('%H', creado) AS INTEGER) BETWEEN 12 AND 17 THEN 'tarde'
                ELSE 'noche'
            END as franja,
            CASE WHEN tipo = 'producto_tomado' THEN 'interaccion' ELSE tipo END as tipo,
            COUNT(*) as total,
            COALESCE(AVG(CASE WHEN tipo = 'permanencia' THEN valor END), 0) as prom
        FROM eventos WHERE {where_ev}
        GROUP BY franja, CASE WHEN tipo = 'producto_tomado' THEN 'interaccion' ELSE tipo END
    """, params_ev)


def obtener_productos_por_franja(dias=7, fecha_inicio: str = None, fecha_fin: str = None,
                                 horas: int = None, hora_inicio: int = None, hora_fin: int = None, sucursal_id: str = None):
    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    return leer(f"""
        SELECT
            CASE
                WHEN CAST(strftime('%H', creado) AS INTEGER) BETWEEN 6 AND 11  THEN 'manana'
                WHEN CAST(strftime('%H', creado) AS INTEGER) BETWEEN 12 AND 17 THEN 'tarde'
                ELSE 'noche'
            END as franja,
            detalle as producto, COUNT(*) as interacciones
        FROM eventos
        WHERE tipo IN ('producto_tomado', 'interaccion') AND {where_ev}
        GROUP BY franja, producto ORDER BY franja, interacciones DESC
    """, params_ev)


def obtener_perfil_permanencia(dias=7, fecha_inicio: str = None, fecha_fin: str = None,
                               horas: int = None, hora_inicio: int = None, hora_fin: int = None, sucursal_id: str = None):
    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    return leer(f"""
        SELECT
            CASE
                WHEN valor < 2.5 THEN 'Express (<2.5s)'
                WHEN valor < 3.8 THEN 'Explorador (2.5-3.8s)'
                WHEN valor < 4.5 THEN 'Interesado (3.8-4.5s)'
                ELSE 'Decisor (4.5s+)'
            END as perfil,
            COUNT(*) as cantidad,
            ROUND(AVG(valor), 1) as promedio_seg
        FROM eventos
        WHERE tipo = 'permanencia' AND {where_ev}
        GROUP BY perfil ORDER BY promedio_seg
    """, params_ev)


def obtener_tendencia_diaria(dias=14, fecha_inicio: str = None, fecha_fin: str = None,
                             horas: int = None, hora_inicio: int = None, hora_fin: int = None, sucursal_id: str = None):
    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    return leer(f"""
        SELECT date(creado) as fecha,
            SUM(CASE WHEN tipo = 'trafico' THEN 1 ELSE 0 END) as trafico,
            SUM(CASE WHEN tipo IN ('producto_tomado', 'interaccion') THEN 1 ELSE 0 END) as interacciones,
            ROUND(AVG(CASE WHEN tipo = 'permanencia' THEN valor END), 1) as prom
        FROM eventos WHERE {where_ev}
        GROUP BY fecha ORDER BY fecha
    """, params_ev)


def _obtener_metadatos_cacheados():
    """Lee tablas de configuración (sucursales, cámaras, perchas, catálogo, promos) con caché breve en RAM."""
    ahora = time.time()
    if TURSO_URL:
        with _meta_cache_lock:
            cached = _meta_cache.get("all")
            if cached and (ahora - cached["t"]) < _META_CACHE_TTL:
                return cached["d"]

    sucursales = obtener_sucursales()
    camaras = obtener_camaras_mapa()
    promos = obtener_promociones_percha()
    perchas_raw = leer("""
        SELECT id, nombre, categoria, camara_id, map_x, map_y, map_w, map_h, roi_json, niveles_json, color, sucursal_id
        FROM perchas_mapa ORDER BY id
    """)
    catalogo_raw = leer("""
        SELECT c.sku, c.nombre, c.categoria, c.percha_id, COALESCE(p.nombre, c.percha_id),
               c.nivel, c.stock, c.tomas_base, c.devoluciones_base, c.sucursal_id
        FROM productos_catalogo c
        LEFT JOIN perchas_mapa p ON c.percha_id = p.id
        ORDER BY c.id
    """)
    d = {
        "sucursales": sucursales,
        "camaras": camaras,
        "promos": promos,
        "perchas_raw": perchas_raw,
        "catalogo_raw": catalogo_raw,
    }
    if TURSO_URL:
        with _meta_cache_lock:
            _meta_cache["all"] = {"t": ahora, "d": d}
    return d


def _normalizar_nombre_producto(detalle: str) -> str:
    txt = str(detalle or "").strip()
    if " (" in txt:
        txt = txt.split(" (", 1)[0].strip()
    if " [" in txt:
        txt = txt.split(" [", 1)[0].strip()
    return txt


def obtener_bundle_dashboard(
    dias: int = 30,
    fecha_inicio: str = None,
    fecha_fin: str = None,
    horas: int = None,
    hora_inicio: int = None,
    hora_fin: int = None,
    sucursal_id: str = None,
):
    """
    Calcula KPIs (/api/stats), Resumen Ejecutivo de Publicidad y Estado Espacial del Mapa 2D
    en solo 3 consultas SQL + metadatos en RAM (reduce >22 round-trips en Turso a 3).
    """
    meta = _obtener_metadatos_cacheados()
    suc_clean = (sucursal_id or "").strip()
    es_todas = not suc_clean or suc_clean.lower() == "todas"
    suc_mapa = "SUC-NORTE" if es_todas else suc_clean

    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado",
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )

    # Query 1: Resumen agregado de eventos
    fila_st = leer(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN tipo = 'trafico' THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN tipo = 'permanencia' THEN 1 ELSE 0 END), 0),
            COALESCE(AVG(CASE WHEN tipo = 'permanencia' THEN valor END), 0),
            COALESCE(SUM(CASE WHEN tipo IN ('producto_tomado', 'interaccion') THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN tipo = 'producto_tomado' THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN tipo = 'producto_devuelto' THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN tipo = 'handoff_camara' THEN 1 ELSE 0 END), 0)
        FROM eventos WHERE {where_ev}
        """,
        params_ev,
        uno=True,
    ) or (0, 0, 0, 0, 0, 0, 0)

    # Query 2: Tomas y devoluciones agrupadas por detalle (para perchas, inventario y top productos)
    ev_prod_rows = leer(
        f"""
        SELECT tipo, detalle, COUNT(*)
        FROM eventos
        WHERE tipo IN ('producto_tomado', 'producto_devuelto', 'interaccion') AND {where_ev}
        GROUP BY tipo, detalle
        """,
        params_ev,
    )

    # Query 3: Telemetría de coordenadas en 1 solo escaneo (comportamiento, postura, privacidad, perchas y heatmap 24x12)
    tc_rows = leer(
        f"""
        SELECT sesion_anonima, es_cliente_valido, x_norm, y_norm, percha_id, postura_cabeza, comportamiento, dwell_sec
        FROM telemetria_coordenadas
        WHERE {where_ev}
        """,
        params_ev,
    )

    tomas_det = {}
    dev_det = {}
    top_prod_acc = {}
    for tipo, det, cnt in ev_prod_rows:
        c_int = int(cnt or 0)
        if tipo in ("producto_tomado", "interaccion"):
            tomas_det[det] = tomas_det.get(det, 0) + c_int
            nom_clean = _normalizar_nombre_producto(det)
            if nom_clean:
                top_prod_acc[nom_clean] = top_prod_acc.get(nom_clean, 0) + c_int
        elif tipo == "producto_devuelto":
            dev_det[det] = dev_det.get(det, 0) + c_int

    top_productos = [
        {"producto": k, "veces": v}
        for k, v in sorted(top_prod_acc.items(), key=lambda kv: kv[1], reverse=True)[:5]
    ]

    stats = {
        "sucursal_id": sucursal_id or "todas",
        "trafico": int(fila_st[0] or 0),
        "permanencia_eventos": int(fila_st[1] or 0),
        "permanencia_promedio_seg": round(float(fila_st[2] or 0.0), 1),
        "interacciones": int(fila_st[3] or 0),
        "tomados": int(fila_st[4] or 0),
        "devueltos": int(fila_st[5] or 0),
        "handoffs": int(fila_st[6] or 0),
        "top_productos": top_productos,
    }

    # Procesar telemetría en una sola pasada en memoria
    cols, rows = 24, 12
    grid = [[0.0 for _ in range(cols)] for _ in range(rows)]
    comp_acc = {
        "mira_y_compra": [0, 0.0],
        "no_mira_y_compra": [0, 0.0],
        "solo_mira": [0, 0.0],
        "ultimo_momento_devuelve": [0, 0.0],
    }
    postura_map = {"giro_percha": 0, "sube_cabeza": 0, "se_agacha": 0, "frente_pasillo": 0}
    stats_percha = {}
    dwell_percha = {}
    empleados_excluidos = 0
    sesiones_validas = set()
    muestras_termicas = 0

    for ses, valido, x_n, y_n, pid_c, post, comp, dwell in tc_rows:
        if int(valido or 0) == 0:
            empleados_excluidos += 1
            continue
        muestras_termicas += 1
        if ses:
            sesiones_validas.add(ses)
        d_val = float(dwell or 0.0)

        if comp in comp_acc:
            comp_acc[comp][0] += 1
            comp_acc[comp][1] += d_val

        if post in ("giro_percha_izq", "giro_percha_der", "giro_percha"):
            postura_map["giro_percha"] += 1
        elif post == "sube_cabeza":
            postura_map["sube_cabeza"] += 1
        elif post == "se_agacha":
            postura_map["se_agacha"] += 1
        else:
            postura_map["frente_pasillo"] += 1

        if pid_c:
            sp = stats_percha.setdefault(pid_c, {
                "mira_y_compra": 0, "no_mira_y_compra": 0, "solo_mira": 0,
                "ultimo_momento_devuelve": 0, "dwell_prom": 0.0
            })
            if comp in sp:
                sp[comp] += 1
            acc = dwell_percha.setdefault(pid_c, [0.0, 0])
            acc[0] += d_val
            acc[1] += 1

        x = max(0.0, min(0.999, float(x_n or 0.0)))
        y = max(0.0, min(0.999, float(y_n or 0.0)))
        peso = 1.0 + min(4.0, d_val / 2.0)
        if comp in ("mira_y_compra", "no_mira_y_compra"):
            peso *= 1.35
        elif comp == "ultimo_momento_devuelve":
            peso *= 1.15
        c_idx = int(x * cols)
        r_idx = int(y * rows)
        grid[r_idx][c_idx] += peso
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                rr, cc = r_idx + dr, c_idx + dc
                if 0 <= rr < rows and 0 <= cc < cols:
                    grid[rr][cc] += peso * (0.45 if (dr == 0 or dc == 0) else 0.22)

    for pid_c, (tot_d, tot_n) in dwell_percha.items():
        if tot_n > 0:
            stats_percha[pid_c]["dwell_prom"] = round(tot_d / tot_n, 1)

    comp_map = {
        k: {"casos": v[0], "dwell": round(v[1] / v[0], 1) if v[0] > 0 else 0.0}
        for k, v in comp_acc.items()
    }

    promos_filtradas = [pr for pr in meta["promos"] if es_todas or pr.get("sucursal_id") == suc_clean]
    promos_por_percha = {}
    for pr in promos_filtradas:
        if pr["activa"]:
            promos_por_percha.setdefault(pr["percha_id"], []).append(pr)

    def _construir_perchas(raw_rows):
        bruto = []
        for f in raw_rows:
            pid, pnombre = f[0], f[1]
            try:
                roi = json.loads(f[8] or "{}")
            except Exception:
                roi = {"x1": 0.15, "y1": 0.15, "x2": 0.85, "y2": 0.85}
            try:
                niveles = json.loads(f[9] or "[]")
            except Exception:
                niveles = []
            tomas_ev = sum(cnt for det, cnt in tomas_det.items() if det and (pid in det or pnombre in det))
            dev_ev = sum(cnt for det, cnt in dev_det.items() if det and (pid in det or pnombre in det))
            st = stats_percha.get(pid, {
                "mira_y_compra": 0, "no_mira_y_compra": 0, "solo_mira": 0,
                "ultimo_momento_devuelve": 0, "dwell_prom": 0.0
            })
            tomas_coords = st["mira_y_compra"] + st["no_mira_y_compra"] + st["ultimo_momento_devuelve"]
            total_tomas = max(tomas_ev, tomas_coords)
            total_dev = max(dev_ev, st["ultimo_momento_devuelve"])
            bruto.append((f, roi, niveles, st, total_tomas, total_dev, total_tomas + st["solo_mira"]))

        max_tomas = max((it[4] for it in bruto), default=1)
        out = []
        for f, roi, niveles, st, total_tomas, total_dev, inter_tot in bruto:
            ratio = (total_tomas / max(1, max_tomas)) if max_tomas > 0 else 0.0
            temp = "caliente" if (total_tomas > 0 and ratio >= 0.65) else ("templada" if (total_tomas > 0 and ratio >= 0.35) else "fria")
            out.append({
                "id": f[0], "nombre": f[1], "categoria": f[2], "camara_id": f[3],
                "map_x": float(f[4]), "map_y": float(f[5]), "map_w": float(f[6]), "map_h": float(f[7]),
                "roi": roi, "niveles": niveles, "color": f[10] or "#FE5000", "sucursal_id": f[11] or "SUC-NORTE",
                "tomas": total_tomas, "devoluciones": total_dev, "interacciones": inter_tot,
                "temperatura": temp, "intensidad_calor": round(ratio, 2),
                "comportamiento": st, "promociones": promos_por_percha.get(f[0], []),
            })
        return out

    perchas_todas_filtro = _construir_perchas([f for f in meta["perchas_raw"] if es_todas or (f[11] or "SUC-NORTE") == suc_clean])
    perchas_mapa_suc = (
        perchas_todas_filtro if not es_todas
        else _construir_perchas([f for f in meta["perchas_raw"] if (f[11] or "SUC-NORTE") == suc_mapa])
    )

    # Inventario consolidado sin round-trips extra
    promo_por_prod = {(pr["producto"].lower(), pr["percha_id"]): pr for pr in promos_filtradas if pr["activa"]}
    promo_por_percha = {pr["percha_id"]: pr for pr in promos_filtradas if pr["activa"]}
    traf_base = max(1, stats["trafico"])
    inventario = []
    for sku, nombre, cat, pid, pnombre, nivel, stock, _tb, _db, suc_row in meta["catalogo_raw"]:
        if not es_todas and (suc_row or "SUC-NORTE") != suc_clean:
            continue
        t_prod = int(sum(
            cnt for det, cnt in tomas_det.items()
            if det and (pid in det or (nombre.lower() in det.lower() and "[" not in det)) and nombre.lower() in det.lower()
        ))
        d_prod = int(sum(
            cnt for det, cnt in dev_det.items()
            if det and (pid in det or (nombre.lower() in det.lower() and "[" not in det)) and nombre.lower() in det.lower()
        ))
        netos = max(0, t_prod - d_prod)
        stock_act = max(0, int(stock) - min(int(stock), netos // 2))
        evac = round(min(100.0, (netos / traf_base) * 100.0), 1)
        pr = promo_por_prod.get((nombre.lower(), pid)) or promo_por_percha.get(pid)
        if d_prod >= 2 and (d_prod / max(1, t_prod)) >= 0.22:
            est = "Indecisión / Cambio Último Momento"
        elif stock_act <= 5:
            est = "Crítico (Reponer)"
        elif evac >= 12.0:
            est = "Zona Caliente (Alta Rotación)"
        elif evac < 4.0:
            est = "Zona Fría (Lento)"
        else:
            est = "Óptimo"
        inventario.append({
            "sku": sku, "nombre": nombre, "categoria": cat, "percha_id": pid, "percha_nombre": pnombre,
            "nivel": nivel, "sucursal_id": suc_row or "SUC-NORTE", "stock": stock_act,
            "tomas": t_prod, "devueltos": d_prod, "llevados_netos": netos, "tasa_evacuacion": evac,
            "promocion_activa": pr["titulo"] if pr else "Sin promo activa",
            "tipo_publicidad": pr["tipo_publicidad"] if pr else "Orgánico",
            "estado": est,
        })

    perchas_ord = sorted(perchas_todas_filtro, key=lambda p: p["tomas"], reverse=True)
    calientes_f = [p for p in perchas_ord if p.get("temperatura") == "caliente"] or perchas_ord[:2]
    ids_cal = {p["id"] for p in calientes_f[:3]}
    zonas_calientes = [
        {"id": p["id"], "nombre": p["nombre"], "tomas": p["tomas"], "devoluciones": p["devoluciones"],
         "sucursal_id": p.get("sucursal_id", "SUC-NORTE"),
         "promocion": p["promociones"][0]["titulo"] if p.get("promociones") else "Tráfico Orgánico"}
        for p in calientes_f[:3]
    ]
    candidatas_fr = [p for p in reversed(perchas_ord) if p["id"] not in ids_cal and p.get("temperatura") != "caliente"]
    zonas_frias = [
        {"id": p["id"], "nombre": p["nombre"], "tomas": p["tomas"], "devoluciones": p["devoluciones"],
         "sucursal_id": p.get("sucursal_id", "SUC-NORTE"),
         "promocion": p["promociones"][0]["titulo"] if p.get("promociones") else "Sin Pauta POP"}
        for p in candidatas_fr[:2]
    ]

    evaluacion_promos = []
    for pr in promos_filtradas:
        pm = next(
            (it for it in inventario if it["percha_id"] == pr["percha_id"] and it["nombre"].lower() == pr["producto"].lower()),
            next((it for it in inventario if it["nombre"].lower() == pr["producto"].lower()), None),
        )
        tp = pm["tomas"] if pm else 0
        dp = pm["devueltos"] if pm else 0
        np_ = max(0, tp - dp)
        ret = round((np_ / max(1, tp)) * 100.0, 1) if tp > 0 else 0.0
        rdev = (dp / tp) if tp > 0 else 0.0
        niv_p = (pm["nivel"] if pm else "").lower()
        if dp >= 2 and rdev >= 0.22:
            diag_txt = "Capta la atención inicial, pero presenta devoluciones de último momento por precio o comparación con el competidor."
            est_ef = "Fricción de Precio / Sustitución"
        elif "inferior" in niv_p or tp < 3:
            diag_txt = "Baja visibilidad en el rango consultado (zona fría o nivel inferior). Conviene reubicar a altura de los ojos o reforzar stopper."
            est_ef = "Baja Visibilidad (Zona Fría)"
        else:
            diag_txt = "Atracción visual consistente y cierre efectivo en percha dentro del período analizado."
            est_ef = "Alta Conversión POP"
        evaluacion_promos.append({
            "id": pr["id"], "percha_id": pr["percha_id"], "percha_nombre": pr["percha_nombre"],
            "sucursal_id": pr.get("sucursal_id", "SUC-NORTE"), "producto": pr["producto"],
            "titulo": pr["titulo"], "tipo_publicidad": pr["tipo_publicidad"], "descuento_pct": pr["descuento_pct"],
            "tomas": tp, "devueltos": dp, "llevados_netos": np_, "efectividad_pct": ret,
            "estado_efecto": est_ef, "diagnostico": diag_txt,
        })

    resumen_pub = {
        "sucursal_id": sucursal_id or "todas",
        "privacidad": {
            "modo": "Anonimización Coordenadas 2D Activa (Cero Biometría / GDPR y LOPDP)",
            "clientes_validados_puerta_caja": len(sesiones_validas),
            "empleados_excluidos": empleados_excluidos,
        },
        "comportamiento_visual": comp_map,
        "postura_cabeza": postura_map,
        "zonas_calientes": zonas_calientes,
        "zonas_frias": zonas_frias,
        "productos_mas_llevados": sorted(inventario, key=lambda x: x["llevados_netos"], reverse=True)[:5],
        "productos_cambio_ultimo_momento": sorted(inventario, key=lambda x: x["devueltos"], reverse=True)[:4],
        "evaluacion_promociones": evaluacion_promos,
    }

    suc_obj = next((s for s in meta["sucursales"] if s["id"] == suc_mapa), meta["sucursales"][0] if meta["sucursales"] else None)
    plano_arq = (suc_obj or {}).get("plano") or _plano_arquitectonico_norte()
    camaras_suc = [c for c in meta["camaras"] if (c.get("sucursal_id") or "SUC-NORTE") == suc_mapa]
    grid_round = [[round(v, 2) for v in fila] for fila in grid]
    max_int = max((max(fila) for fila in grid_round), default=0.0)

    return {
        "stats": stats,
        "resumen_publicidad": resumen_pub,
        "inventario": inventario,
        "mapa_base": {
            "sucursal_id": suc_obj["id"] if suc_obj else "SUC-NORTE",
            "sucursal_nombre": suc_obj["nombre"] if suc_obj else "Matriz Norte",
            "plano_arquitectura": plano_arq,
            "zonas_arquitectura": {
                "puerta_entrada": plano_arq.get("puerta_entrada"),
                "linea_cajas": plano_arq.get("linea_cajas"),
            },
            "heatmap_grid": grid_round,
            "heatmap_meta": {
                "cols": cols, "rows": rows,
                "max_intensidad": round(max_int, 2),
                "total_muestras": muestras_termicas,
            },
            "camaras": camaras_suc,
            "perchas": perchas_mapa_suc,
        },
    }


def obtener_bundle_analitica(
    agrupacion: str = "semana",
    dias: int = 30,
    fecha_inicio: str = None,
    fecha_fin: str = None,
    horas: int = None,
    hora_inicio: int = None,
    hora_fin: int = None,
    sucursal_id: str = None,
):
    """Calcula toda la pestaña 2 (serie temporal, curva horaria, matriz, franjas, perfiles y top productos) en 1 escaneo SQL."""
    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado",
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )
    if agrupacion == "hora":
        expr_periodo = "strftime('%Y-%m-%d %H:00', creado)"
    elif agrupacion == "semana":
        expr_periodo = "strftime('%Y-S%W', creado)"
    elif agrupacion == "mes":
        expr_periodo = "strftime('%Y-%m', creado)"
    else:
        expr_periodo = "date(creado)"

    rows = leer(
        f"""
        SELECT tipo, detalle, valor, {expr_periodo}, strftime('%w', creado), strftime('%H', creado)
        FROM eventos
        WHERE {where_ev}
        ORDER BY creado ASC
        """,
        params_ev,
    )

    serie_acc = {}
    horas_map = {str(i).zfill(2): 0 for i in range(24)}
    matriz = [[0] * 24 for _ in range(7)]
    franjas = {
        "manana": {"trafico": 0, "interacciones": 0, "perm_sum": 0.0, "perm_cnt": 0},
        "tarde": {"trafico": 0, "interacciones": 0, "perm_sum": 0.0, "perm_cnt": 0},
        "noche": {"trafico": 0, "interacciones": 0, "perm_sum": 0.0, "perm_cnt": 0},
    }
    perfiles_acc = {}
    top_acc = {}

    for tipo, det, val, per, dow_s, hr_s in rows:
        h_int = int(hr_s) if hr_s is not None else 0
        d_int = int(dow_s) if dow_s is not None else 0
        fr_key = "manana" if 6 <= h_int <= 11 else ("tarde" if 12 <= h_int <= 17 else "noche")

        if per:
            sb = serie_acc.setdefault(per, {"t": 0, "tom": 0, "dev": 0, "ps": 0.0, "pc": 0})
        else:
            sb = None

        if tipo == "trafico":
            if sb:
                sb["t"] += 1
            horas_map[f"{h_int:02d}"] = horas_map.get(f"{h_int:02d}", 0) + 1
            if 0 <= d_int < 7 and 0 <= h_int < 24:
                matriz[d_int][h_int] += 1
            franjas[fr_key]["trafico"] += 1
        elif tipo in ("producto_tomado", "interaccion"):
            if sb and tipo == "producto_tomado":
                sb["tom"] += 1
            franjas[fr_key]["interacciones"] += 1
            nom = _normalizar_nombre_producto(det)
            if nom:
                top_acc[nom] = top_acc.get(nom, 0) + 1
        elif tipo == "producto_devuelto":
            if sb:
                sb["dev"] += 1
        elif tipo == "permanencia" and val is not None:
            fv = float(val)
            if sb:
                sb["ps"] += fv
                sb["pc"] += 1
            franjas[fr_key]["perm_sum"] += fv
            franjas[fr_key]["perm_cnt"] += 1
            if fv < 2.5:
                pf_k = "Express (<2.5s)"
            elif fv < 3.8:
                pf_k = "Explorador (2.5-3.8s)"
            elif fv < 4.5:
                pf_k = "Interesado (3.8-4.5s)"
            else:
                pf_k = "Decisor (4.5s+)"
            pa = perfiles_acc.setdefault(pf_k, [0, 0.0])
            pa[0] += 1
            pa[1] += fv

    # Formatear serie temporal
    periodos_ord = sorted(serie_acc.keys())
    recortar_hora = agrupacion == "hora" and ((horas is not None and horas <= 24) or (dias == 1 and not fecha_inicio))
    serie = {
        "agrupacion": agrupacion,
        "labels": [p[11:16] if recortar_hora else p for p in periodos_ord],
        "trafico": [serie_acc[p]["t"] for p in periodos_ord],
        "tomados": [serie_acc[p]["tom"] for p in periodos_ord],
        "interacciones": [serie_acc[p]["tom"] for p in periodos_ord],
        "llevados": [serie_acc[p]["tom"] for p in periodos_ord],
        "devueltos": [serie_acc[p]["dev"] for p in periodos_ord],
        "permanencia": [round(serie_acc[p]["ps"] / serie_acc[p]["pc"], 1) if serie_acc[p]["pc"] else 0.0 for p in periodos_ord],
    }

    # Formatear matriz semanal
    nombres_dias = ["Dom", "Lun", "Mar", "Mié", "Jue", "Vie", "Sáb"]
    total_semanal = 0
    max_hv, max_hd, max_ht = -1, "", ""
    resumen_franjas = []
    totales_dia = []
    for d in range(7):
        for h in range(24):
            v = matriz[d][h]
            total_semanal += v
            if v > max_hv:
                max_hv, max_hd, max_ht = v, nombres_dias[d], f"{h:02d}:00"
        m_v = sum(matriz[d][h] for h in range(6, 11))
        md_v = sum(matriz[d][h] for h in range(11, 14))
        t_v = sum(matriz[d][h] for h in range(14, 18))
        n_v = sum(matriz[d][h] for h in range(18, 23))
        mad_v = sum(matriz[d][h] for h in [23, 0, 1, 2, 3, 4, 5])
        tot_d = sum(matriz[d])
        totales_dia.append(tot_d)
        resumen_franjas.append({
            "dia": nombres_dias[d], "manana": m_v, "mediodia": md_v,
            "tarde": t_v, "noche": n_v, "madrugada": mad_v, "total": tot_d,
        })
    max_di = max(range(7), key=lambda i: totales_dia[i]) if totales_dia else 0
    heatmap_matriz = {
        "dias": nombres_dias,
        "horas": [f"{h:02d}:00" for h in range(24)],
        "matriz": matriz,
        "total_semanal": total_semanal,
        "dia_pico": {"dia": nombres_dias[max_di], "total": totales_dia[max_di] if totales_dia else 0},
        "hora_pico": {"hora": max_ht or "00:00", "dia": max_hd or "Sin datos", "total": max(0, max_hv)},
        "resumen_franjas": resumen_franjas,
    }

    # Formatear franjas (conversión acotada a 0..100%)
    tot_traf = sum(v["trafico"] for v in franjas.values())
    franjas_out = {}
    for fk, fv in franjas.items():
        conv = min(100.0, round((fv["interacciones"] / fv["trafico"]) * 100.0, 1)) if fv["trafico"] > 0 else 0.0
        franjas_out[fk] = {
            "trafico": fv["trafico"],
            "interacciones": fv["interacciones"],
            "permanencia": round(fv["perm_sum"] / fv["perm_cnt"], 1) if fv["perm_cnt"] else 0.0,
            "conversion": conv,
        }
    franjas_out["porcentajes"] = {
        "Mañana": round((franjas["manana"]["trafico"] / tot_traf) * 100, 1) if tot_traf else 0.0,
        "Tarde": round((franjas["tarde"]["trafico"] / tot_traf) * 100, 1) if tot_traf else 0.0,
        "Noche": round((franjas["noche"]["trafico"] / tot_traf) * 100, 1) if tot_traf else 0.0,
    }

    orden_perf = ["Express (<2.5s)", "Explorador (2.5-3.8s)", "Interesado (3.8-4.5s)", "Decisor (4.5s+)"]
    perfiles_out = [
        {"tipo": k, "cantidad": perfiles_acc[k][0], "promedio_seg": round(perfiles_acc[k][1] / perfiles_acc[k][0], 1)}
        for k in orden_perf if k in perfiles_acc
    ]
    top_out = [
        {"nombre": k, "interacciones": v}
        for k, v in sorted(top_acc.items(), key=lambda kv: kv[1], reverse=True)[:6]
    ]

    return {
        "serie_temporal": serie,
        "heatmap_hora": {"labels": list(horas_map.keys()), "data": list(horas_map.values())},
        "heatmap_matriz": heatmap_matriz,
        "franjas": franjas_out,
        "perfiles": perfiles_out,
        "top_productos": top_out,
    }


def obtener_datos_publicidad(franja=None, dias=7, fecha_inicio: str = None, fecha_fin: str = None,
                             horas: int = None, hora_inicio: int = None, hora_fin: int = None, sucursal_id: str = None):
    where_ev, params_ev = _construir_filtro_fecha(
        dias, fecha_inicio, fecha_fin, "creado", horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id
    )
    franja_sql = {
        'manana': "AND CAST(strftime('%H', creado) AS INTEGER) BETWEEN 6 AND 11",
        'tarde':  "AND CAST(strftime('%H', creado) AS INTEGER) BETWEEN 12 AND 17",
        'noche':  "AND (CAST(strftime('%H', creado) AS INTEGER) >= 18 OR CAST(strftime('%H', creado) AS INTEGER) < 6)"
    }
    extra = franja_sql.get(franja, "")

    resumen = leer(f"""
        SELECT
            COALESCE(SUM(CASE WHEN tipo = 'trafico' THEN 1 ELSE 0 END), 0),
            COALESCE(AVG(CASE WHEN tipo = 'permanencia' THEN valor END), 0),
            COALESCE(SUM(CASE WHEN tipo = 'permanencia' THEN 1 ELSE 0 END), 0)
        FROM eventos WHERE {where_ev} {extra}
    """, params_ev, uno=True) or (0, 0, 0)
    top = leer(
        f"SELECT detalle, COUNT(*) FROM eventos WHERE tipo IN ('producto_tomado', 'interaccion') AND {where_ev} {extra} GROUP BY detalle ORDER BY COUNT(*) DESC LIMIT 5",
        params_ev
    )
    return {
        "trafico": int(resumen[0] or 0),
        "permanencia_promedio": round(float(resumen[1] or 0), 1),
        "permanencia_eventos": int(resumen[2] or 0),
        "top_productos": [{"producto": p[0], "interacciones": p[1]} for p in top] if top else [],
        "franja": franja or "todas"
    }
