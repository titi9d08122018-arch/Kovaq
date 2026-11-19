import asyncio
import ipaddress
import os
import secrets
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

import jwt
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ai_agent import preguntar_a_los_datos
from analytics import router as analytics_router
from auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    ROLES_GESTION,
    SECRET_KEY,
    obtener_usuario_actual,
    requerir_roles,
    validar_jwt_secret,
)
from camera import camara
from database import (
    _construir_filtro_fecha,
    eliminar_camara_mapa,
    eliminar_promocion_percha,
    escribir,
    init_analytics,
    init_db,
    leer,
    obtener_camaras_mapa,
    obtener_perchas_mapa,
    obtener_promociones_percha,
    pwd_context,
    registrar_o_actualizar_camara_mapa,
    registrar_o_actualizar_percha_mapa,
    registrar_promocion_percha,
)

MODO = os.getenv("MODO", "local").lower()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
DEMO_VIDEO = os.path.join(STATIC_DIR, "demo_supermercado.mp4")

_FECHA_PAT = r"^\d{4}-\d{2}-\d{2}$"
_exigir_gestion = requerir_roles(*ROLES_GESTION)


class CamaraIn(BaseModel):
    id: str | None = Field(default=None, max_length=32)
    nombre: str = Field(default="Nueva Cámara", min_length=1, max_length=120)
    ruta: str = Field(default="0", min_length=1, max_length=300)
    estado: str = Field(default="Activa", pattern="^(Activa|Pausada|Inactiva)$")
    pos_x: float = Field(default=0.5, ge=0.0, le=1.0)
    pos_y: float = Field(default=0.5, ge=0.0, le=1.0)
    angulo: float = Field(default=45.0, ge=-360.0, le=360.0)


class PerchaIn(BaseModel):
    id: str | None = Field(default=None, max_length=32)
    nombre: str = Field(default="Nueva Percha", min_length=1, max_length=120)
    categoria: str = Field(default="General", min_length=1, max_length=60)
    camara_id: str = Field(default="CAM-01", min_length=1, max_length=32)
    map_x: float = Field(default=0.3, ge=0.0, le=1.0)
    map_y: float = Field(default=0.3, ge=0.0, le=1.0)
    map_w: float = Field(default=0.2, ge=0.02, le=1.0)
    map_h: float = Field(default=0.14, ge=0.02, le=1.0)
    roi: dict | None = None
    niveles: list[dict] | None = None
    color: str = Field(default="#FE5000", max_length=16)


class PromocionIn(BaseModel):
    id: str | None = Field(default=None, max_length=32)
    percha_id: str = Field(default="PER-A1", min_length=1, max_length=32)
    producto: str = Field(default="Producto en Góndola", min_length=1, max_length=120)
    titulo: str = Field(default="Promoción Punto de Venta", min_length=1, max_length=140)
    tipo_publicidad: str = Field(default="Cenefa / Cartel POP", min_length=1, max_length=80)
    mensaje_pop: str = Field(default="", max_length=240)
    descuento_pct: int = Field(default=0, ge=0, le=100)


class ChatIn(BaseModel):
    mensaje: str = Field(min_length=1, max_length=600)
    modo: str = Field(default="datos", max_length=32)


class ControlIn(BaseModel):
    accion: str = Field(pattern="^(pausa|parar)$")


class FuenteIn(BaseModel):
    tipo: str = Field(default="video", pattern="^(webcam|rtsp|video|imagen|apagado)$")
    ruta: str | None = Field(default=None, max_length=300)
    camara_id: str | None = Field(default=None, max_length=32)


def _redactar_ruta_rtsp(ruta: str) -> str:
    """Oculta usuario y contraseña en URLs RTSP antes de exponerlas a la UI."""
    if not isinstance(ruta, str) or "://" not in ruta:
        return ruta
    try:
        parsed = urlparse(ruta)
        if parsed.username or parsed.password:
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            netloc = f"***@{host}{port}"
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass
    return ruta


def _validar_ruta_media(tipo: str, ruta: str) -> str:
    """Bloquea SSRF en RTSP y traversal fuera de static/ o uploads/ en archivos."""
    ruta_limpia = (ruta or "").strip()
    if tipo == "apagado":
        return ""
    if tipo == "webcam":
        if ruta_limpia == "" or (ruta_limpia.isdigit() and int(ruta_limpia) <= 8):
            return ruta_limpia or "0"
        raise HTTPException(status_code=400, detail="Índice de cámara USB inválido")
    if tipo == "rtsp":
        parsed = urlparse(ruta_limpia)
        if parsed.scheme not in ("rtsp", "rtsps") or not parsed.hostname:
            raise HTTPException(status_code=400, detail="Solo se permiten URLs rtsp:// o rtsps:// válidas")
        host = parsed.hostname.lower()
        if host in ("localhost", "0.0.0.0", "::1"):
            raise HTTPException(status_code=400, detail="Host RTSP no permitido")
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                raise HTTPException(status_code=400, detail="Dirección IP RTSP no permitida")
        except ValueError:
            pass
        return ruta_limpia
    # video o imagen: restringir estrictamente a static/ o uploads/
    real = os.path.realpath(ruta_limpia if os.path.isabs(ruta_limpia) else os.path.join(BASE_DIR, ruta_limpia))
    permitidos = (os.path.realpath(STATIC_DIR), os.path.realpath(UPLOADS_DIR))
    if not any(real == base or real.startswith(base + os.sep) for base in permitidos):
        raise HTTPException(status_code=400, detail="Ruta de archivo fuera de directorios permitidos")
    return real


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_analytics()
    if MODO == "local":
        camara.iniciar(modo_nube=False)
        if os.path.exists(DEMO_VIDEO):
            camara.cambiar_fuente("video", DEMO_VIDEO)
        else:
            camara.cambiar_fuente("webcam")
    else:
        camara.iniciar(modo_nube=True)
    try:
        yield
    finally:
        camara.detener()
        camara.flush_cola_db()


_ocultar_docs = MODO == "nube"
app = FastAPI(
    title="Kovaq Retail Analytics",
    lifespan=lifespan,
    docs_url=None if _ocultar_docs else "/docs",
    redoc_url=None if _ocultar_docs else "/redoc",
    openapi_url=None if _ocultar_docs else "/openapi.json",
)
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def agregar_cabeceras_seguridad(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


app.include_router(analytics_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

MENU = [
    {"url": "/dashboard", "titulo": "Dashboard General", "icono": "bi bi-speedometer2", "admin_only": False},
    {"url": "/usuarios", "titulo": "Usuarios del Sistema", "icono": "bi bi-people", "admin_only": True},
]


def _buscar_usuario_login(user_clean: str):
    return leer(
        "SELECT u.id, u.username, u.password_hash, u.nombre_completo, r.nombre, u.estado "
        "FROM usuarios u JOIN roles r ON u.rol_id = r.id WHERE LOWER(u.username) = LOWER(?)",
        (user_clean,),
        uno=True,
    )


@app.get("/")
async def root():
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_action(request: Request, username: str = Form(...), password: str = Form(...)):
    user_clean = (username or "").strip()
    usuario = await asyncio.to_thread(_buscar_usuario_login, user_clean)
    if not usuario or not pwd_context.verify(password, usuario[2]) or usuario[5] != "activo":
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Credenciales inválidas. Verifique su usuario y contraseña."},
        )

    vence = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = jwt.encode({"sub": usuario[1], "rol": usuario[4], "exp": vence}, SECRET_KEY, algorithm=ALGORITHM)
    response = RedirectResponse(url="/dashboard", status_code=303)
    es_https = (request.url.scheme == "https") or (request.headers.get("x-forwarded-proto") == "https")
    response.set_cookie(
        key="access_token",
        value=f"Bearer {token}",
        httponly=True,
        samesite="lax",
        secure=(MODO == "nube" and es_https),
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return response


@app.post("/logout")
async def logout_post():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("access_token")
    return response


# Vistas web

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    try:
        user = await obtener_usuario_actual(request)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"usuario_actual": user, "menu": MENU, "modo": MODO, "roles_gestion": ROLES_GESTION},
    )


@app.get("/productos")
async def pagina_productos(request: Request):
    try:
        await obtener_usuario_actual(request)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=303)
    return RedirectResponse(url="/dashboard#tab-inventario", status_code=303)


@app.get("/usuarios", response_class=HTMLResponse)
async def listar_usuarios(request: Request):
    try:
        user = await obtener_usuario_actual(request)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=303)
    if user["rol"] not in ROLES_GESTION:
        raise HTTPException(status_code=403, detail="Acceso denegado. Permisos insuficientes.")

    usuarios = await asyncio.to_thread(
        leer,
        "SELECT u.id, u.username, u.nombre_completo, r.nombre, u.estado "
        "FROM usuarios u JOIN roles r ON u.rol_id = r.id",
    )
    roles = await asyncio.to_thread(leer, "SELECT id, nombre FROM roles")
    return templates.TemplateResponse(
        request,
        "usuarios.html",
        {"usuarios": usuarios, "roles": roles, "usuario_actual": user, "menu": MENU, "modo": MODO, "roles_gestion": ROLES_GESTION},
    )


# Gestión de usuarios

@app.post("/usuarios/crear")
async def crear_usuario(
    request: Request,
    nombre: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    rol_id: int = Form(...),
    user: dict = Depends(_exigir_gestion),
):
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 8 caracteres")
    rol_row = await asyncio.to_thread(leer, "SELECT nombre FROM roles WHERE id = ?", (rol_id,), True)
    if not rol_row:
        raise HTTPException(status_code=400, detail="Rol no válido")
    if rol_row[0] == "admin" and user["rol"] != "admin":
        raise HTTPException(status_code=403, detail="Solo un administrador puede crear otro administrador")
    try:
        await asyncio.to_thread(
            escribir,
            "INSERT INTO usuarios (username, password_hash, nombre_completo, rol_id, estado) VALUES (?, ?, ?, ?, 'activo')",
            (username.strip(), pwd_context.hash(password), nombre.strip(), rol_id),
        )
    except Exception:
        raise HTTPException(status_code=409, detail="El usuario ya existe")
    return RedirectResponse(url="/usuarios", status_code=303)


@app.post("/usuarios/eliminar/{usuario_id}")
async def eliminar_usuario(usuario_id: int, user: dict = Depends(_exigir_gestion)):
    if user["id"] == usuario_id:
        raise HTTPException(status_code=400, detail="No puedes eliminar tu propio usuario")
    objetivo = await asyncio.to_thread(
        leer,
        "SELECT r.nombre FROM usuarios u JOIN roles r ON u.rol_id = r.id WHERE u.id = ?",
        (usuario_id,),
        True,
    )
    if objetivo and objetivo[0] == "admin" and user["rol"] != "admin":
        raise HTTPException(status_code=403, detail="Solo un administrador puede eliminar a otro administrador")
    await asyncio.to_thread(escribir, "DELETE FROM usuarios WHERE id = ?", (usuario_id,))
    return RedirectResponse(url="/usuarios", status_code=303)


# Mapeo 2D, planograma y promociones

@app.get("/api/map_state")
async def api_map_state(request: Request):
    await obtener_usuario_actual(request)
    estado = await asyncio.to_thread(camara.obtener_estado_mapa_2d)
    cams_seguras = [{**c, "ruta": _redactar_ruta_rtsp(c.get("ruta", ""))} for c in estado.get("camaras", [])]
    return {**estado, "camaras": cams_seguras}


@app.post("/api/simulation/toggle")
async def api_simulation_toggle(request: Request, _user: dict = Depends(_exigir_gestion)):
    try:
        datos = await request.json()
    except Exception:
        datos = {}
    estado = camara.toggle_simulacion_papel(datos.get("activar"))
    return {"ok": True, "simulacion_activa": estado}


@app.post("/api/cameras/select")
async def api_select_camera(request: Request, _user: dict = Depends(_exigir_gestion)):
    datos = await request.json()
    cam_id = (datos.get("camara_id") or "CAM-01").strip()
    actual = await asyncio.to_thread(camara.cambiar_camara_activa, cam_id)
    return {"ok": True, "camara_activa": actual}


@app.get("/api/cameras")
async def api_listar_camaras(request: Request):
    await obtener_usuario_actual(request)
    cams = await asyncio.to_thread(obtener_camaras_mapa)
    return {"camaras": [{**c, "ruta": _redactar_ruta_rtsp(c.get("ruta", ""))} for c in cams]}


@app.post("/api/cameras")
async def api_guardar_camara(payload: CamaraIn, _user: dict = Depends(_exigir_gestion)):
    cam_id = (payload.id or f"CAM-{uuid.uuid4().hex[:6].upper()}").strip()
    tipo_ruta = "rtsp" if payload.ruta.startswith("rtsp") else ("webcam" if payload.ruta.isdigit() else "video")
    ruta_segura = _validar_ruta_media(tipo_ruta, payload.ruta)
    await asyncio.to_thread(
        registrar_o_actualizar_camara_mapa,
        cam_id,
        payload.nombre.strip(),
        ruta_segura,
        payload.estado,
        payload.pos_x,
        payload.pos_y,
        payload.angulo,
    )
    await asyncio.to_thread(camara.recargar_mapa_tienda)
    cams = await asyncio.to_thread(obtener_camaras_mapa)
    return {"ok": True, "camaras": [{**c, "ruta": _redactar_ruta_rtsp(c.get("ruta", ""))} for c in cams]}


@app.delete("/api/cameras/{cam_id}")
async def api_borrar_camara(cam_id: str, _user: dict = Depends(_exigir_gestion)):
    await asyncio.to_thread(eliminar_camara_mapa, cam_id)
    await asyncio.to_thread(camara.recargar_mapa_tienda)
    cams = await asyncio.to_thread(obtener_camaras_mapa)
    return {"ok": True, "camaras": [{**c, "ruta": _redactar_ruta_rtsp(c.get("ruta", ""))} for c in cams]}


@app.get("/api/shelves")
async def api_listar_perchas(request: Request):
    await obtener_usuario_actual(request)
    perchas = await asyncio.to_thread(obtener_perchas_mapa)
    return {"perchas": perchas}


@app.post("/api/shelves")
async def api_guardar_percha(payload: PerchaIn, _user: dict = Depends(_exigir_gestion)):
    percha_id = (payload.id or f"PER-{uuid.uuid4().hex[:6].upper()}").strip()
    roi = payload.roi or {"x1": 0.15, "y1": 0.15, "x2": 0.85, "y2": 0.85}
    niveles = payload.niveles or [{"nivel": "Medio", "y1": 0.15, "y2": 0.85, "producto": payload.nombre}]
    await asyncio.to_thread(
        registrar_o_actualizar_percha_mapa,
        percha_id,
        payload.nombre.strip(),
        payload.categoria.strip(),
        payload.camara_id.strip(),
        payload.map_x,
        payload.map_y,
        payload.map_w,
        payload.map_h,
        roi,
        niveles,
        payload.color,
    )
    await asyncio.to_thread(camara.recargar_mapa_tienda)
    perchas = await asyncio.to_thread(obtener_perchas_mapa)
    return {"ok": True, "perchas": perchas}


@app.get("/api/promotions")
async def api_listar_promociones(request: Request):
    await obtener_usuario_actual(request)
    promos = await asyncio.to_thread(obtener_promociones_percha)
    return {"promociones": promos}


@app.post("/api/promotions")
async def api_guardar_promocion(payload: PromocionIn, _user: dict = Depends(_exigir_gestion)):
    promo_id = (payload.id or f"PROMO-{uuid.uuid4().hex[:6].upper()}").strip()
    await asyncio.to_thread(
        registrar_promocion_percha,
        promo_id,
        payload.percha_id.strip(),
        payload.producto.strip(),
        payload.titulo.strip(),
        payload.tipo_publicidad.strip(),
        payload.mensaje_pop.strip(),
        payload.descuento_pct,
    )
    await asyncio.to_thread(camara.recargar_mapa_tienda)
    promos = await asyncio.to_thread(obtener_promociones_percha)
    return {"ok": True, "promociones": promos}


@app.delete("/api/promotions/{promo_id}")
async def api_borrar_promocion(promo_id: str, _user: dict = Depends(_exigir_gestion)):
    await asyncio.to_thread(eliminar_promocion_percha, promo_id)
    await asyncio.to_thread(camara.recargar_mapa_tienda)
    promos = await asyncio.to_thread(obtener_promociones_percha)
    return {"ok": True, "promociones": promos}


# API de fuentes, IA, subida y estadísticas

@app.get("/api/sources")
async def listar_fuentes_camara(request: Request):
    await obtener_usuario_actual(request)
    return {
        "fuentes": [
            {"id": "webcam", "nombre": "Cámara Web Predeterminada (USB 0)"},
            {"id": "rtsp", "nombre": "Cámara IP Percha Principal (RTSP)"},
            {"id": "demo", "nombre": "Video Demostración Supermercado"},
        ]
    }


@app.post("/api/chat")
async def api_chat(payload: ChatIn, request: Request):
    await obtener_usuario_actual(request)
    mensaje = payload.mensaje.strip()
    if not mensaje:
        raise HTTPException(status_code=400, detail="El mensaje está vacío")
    respuesta_ia = await preguntar_a_los_datos(mensaje, modo=payload.modo)
    return {"respuesta": respuesta_ia}


@app.post("/api/upload")
async def api_upload(archivo: UploadFile = File(...), _user: dict = Depends(_exigir_gestion)):
    ext = os.path.splitext(archivo.filename or "")[1].lower()
    if ext in {".mp4", ".avi", ".mov", ".mkv"}:
        tipo = "video"
    elif ext in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
        tipo = "imagen"
    else:
        raise HTTPException(status_code=400, detail="Formato no soportado")

    max_bytes = (50 if MODO == "nube" else 200) * 1024 * 1024
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    ruta = os.path.join(UPLOADS_DIR, f"{uuid.uuid4().hex}{ext}")
    escritos = 0
    try:
        with open(ruta, "wb") as f:
            while chunk := await archivo.read(1024 * 1024):
                escritos += len(chunk)
                if escritos > max_bytes:
                    raise HTTPException(status_code=413, detail=f"El archivo supera {max_bytes // (1024 * 1024)} MB")
                f.write(chunk)
    except HTTPException:
        if os.path.exists(ruta):
            os.remove(ruta)
        raise

    try:
        resultado_analisis = await asyncio.to_thread(camara.procesar_archivo_subido_inmediato, tipo, ruta)
    finally:
        if MODO == "nube" and os.path.exists(ruta):
            try:
                os.remove(ruta)
            except OSError:
                pass
    return resultado_analisis


@app.post("/api/control")
async def api_control(payload: ControlIn, _user: dict = Depends(_exigir_gestion)):
    if payload.accion == "pausa":
        return {"ok": True, "pausado": camara.toggle_pausa()}
    camara.apagar()
    return {"ok": True, "pausado": False}


@app.post("/api/fuente")
async def cambiar_fuente_api(payload: FuenteIn, _user: dict = Depends(_exigir_gestion)):
    ruta = payload.ruta
    if not ruta:
        rutas = {
            "video": DEMO_VIDEO if os.path.exists(DEMO_VIDEO) else os.path.join(STATIC_DIR, "demo_supermercado.mp4"),
            "imagen": os.path.join(STATIC_DIR, "demo.jpg"),
            "webcam": "0",
            "apagado": "",
        }
        ruta = rutas.get(payload.tipo, "")
    ruta_validada = _validar_ruta_media(payload.tipo, ruta)
    if MODO == "local":
        camara.cambiar_fuente(payload.tipo, ruta_validada, cam_id=payload.camara_id)
    elif payload.camara_id:
        await asyncio.to_thread(camara.cambiar_camara_activa, payload.camara_id)
    return {"ok": True, "tipo": payload.tipo, "ruta": _redactar_ruta_rtsp(ruta_validada), "camara_activa": camara.camara_activa_id}


def _obtener_stats(dias: int = 30, fecha_inicio: str = None, fecha_fin: str = None):
    where_ev, params_ev = _construir_filtro_fecha(dias, fecha_inicio, fecha_fin, "creado")
    fila = leer(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN tipo = 'trafico' THEN 1 ELSE 0 END), 0) AS trafico,
            COALESCE(SUM(CASE WHEN tipo = 'permanencia' THEN 1 ELSE 0 END), 0) AS perm_eventos,
            COALESCE(AVG(CASE WHEN tipo = 'permanencia' THEN valor END), 0) AS perm_prom,
            COALESCE(SUM(CASE WHEN tipo IN ('producto_tomado', 'interaccion') THEN 1 ELSE 0 END), 0) AS inter,
            COALESCE(SUM(CASE WHEN tipo = 'producto_tomado' THEN 1 ELSE 0 END), 0) AS tomados,
            COALESCE(SUM(CASE WHEN tipo = 'producto_devuelto' THEN 1 ELSE 0 END), 0) AS devueltos,
            COALESCE(SUM(CASE WHEN tipo = 'handoff_camara' THEN 1 ELSE 0 END), 0) AS handoffs
        FROM eventos
        WHERE {where_ev}
        """,
        params_ev,
        uno=True,
    )
    top = leer(
        f"""
        SELECT detalle, COUNT(*) FROM eventos
        WHERE tipo IN ('producto_tomado', 'interaccion') AND {where_ev}
        GROUP BY detalle ORDER BY COUNT(*) DESC LIMIT 5
        """,
        params_ev,
    )
    return {
        "trafico": int(fila[0] or 0),
        "permanencia_eventos": int(fila[1] or 0),
        "permanencia_promedio_seg": round(float(fila[2] or 0), 1),
        "interacciones": int(fila[3] or 0),
        "tomados": int(fila[4] or 0),
        "devueltos": int(fila[5] or 0),
        "handoffs": int(fila[6] or 0),
        "top_productos": [{"producto": p[0], "veces": p[1]} for p in top],
    }


def _texto_resumen(s):
    partes = [f"Se registraron {s['trafico']} detecciones de personas."]
    if s["permanencia_eventos"]:
        partes.append(f"La permanencia promedio en las perchas fue de {s['permanencia_promedio_seg']} segundos ({s['permanencia_eventos']} casos).")
    else:
        partes.append("Aún no hay permanencias registradas en las zonas de percha.")
    partes.append(f"Hubo {s['tomados']} tomas de producto y {s['devueltos']} devoluciones.")
    if s.get("handoffs"):
        partes.append(f"El rastreador Re-ID completó {s['handoffs']} transiciones continuas entre cámaras.")
    if s["top_productos"]:
        lista = ", ".join(f"{p['producto']} ({p['veces']})" for p in s["top_productos"])
        partes.append(f"Los artículos con más interacción fueron: {lista}.")
    return " ".join(partes)


@app.get("/api/stats")
async def api_stats(
    request: Request,
    dias: int = Query(30, ge=1, le=365),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
):
    await obtener_usuario_actual(request)
    try:
        s = await asyncio.to_thread(_obtener_stats, dias, fecha_inicio, fecha_fin)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except Exception:
        raise HTTPException(status_code=500, detail="Error consultando estadísticas")

    s["resumen"] = _texto_resumen(s)
    return s


@app.get("/api/live_events")
async def api_live_events(request: Request):
    await obtener_usuario_actual(request)
    return camara.obtener_telemetria()


# Streaming MJPEG

@app.get("/api/video_feed")
async def video_feed(request: Request):
    await obtener_usuario_actual(request)
    if MODO != "local" and camara.latest_raw_frame is None:
        raise HTTPException(status_code=503, detail="Video no disponible en modo nube")

    async def generar():
        ultimo = -1
        try:
            while True:
                if await request.is_disconnected():
                    break
                fid, jpg = camara.get_frame_bytes(ultimo)
                if jpg:
                    ultimo = fid
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
                await asyncio.sleep(0.03)
        except (ConnectionError, asyncio.CancelledError, GeneratorExit):
            return

    return StreamingResponse(generar(), media_type="multipart/x-mixed-replace; boundary=frame")


VIDEO_TOKEN = os.getenv("VIDEO_PUSH_TOKEN", "")
_ultimo_frame = {"jpg": None, "id": 0}
_frame_lock = threading.Lock()


@app.post("/api/frame_push")
async def frame_push(request: Request):
    recibido = request.headers.get("X-Token", "")
    if not VIDEO_TOKEN or not secrets.compare_digest(recibido, VIDEO_TOKEN):
        raise HTTPException(status_code=403, detail="Token inválido")
    jpg = await request.body()
    if not jpg or len(jpg) > 2_000_000:
        raise HTTPException(status_code=400, detail="Frame inválido")
    with _frame_lock:
        _ultimo_frame["jpg"] = jpg
        _ultimo_frame["id"] += 1
    return {"ok": True}


@app.get("/api/video_live")
async def video_live(request: Request):
    await obtener_usuario_actual(request)

    async def generar():
        ultimo = -1
        try:
            while True:
                if await request.is_disconnected():
                    break
                with _frame_lock:
                    jpg, fid = _ultimo_frame["jpg"], _ultimo_frame["id"]
                if jpg and fid != ultimo:
                    ultimo = fid
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
                await asyncio.sleep(0.05)
        except (ConnectionError, asyncio.CancelledError, GeneratorExit):
            return

    return StreamingResponse(generar(), media_type="multipart/x-mixed-replace; boundary=frame")
