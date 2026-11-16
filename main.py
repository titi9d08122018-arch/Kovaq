import os
import uuid
import asyncio
import threading
import jwt
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Form, HTTPException, status, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from database import (
    init_db, init_analytics, leer, escribir, pwd_context,
    obtener_camaras_mapa, registrar_o_actualizar_camara_mapa, eliminar_camara_mapa,
    obtener_perchas_mapa, registrar_o_actualizar_percha_mapa,
    obtener_promociones_percha, registrar_promocion_percha, eliminar_promocion_percha,
    _construir_filtro_fecha
)
from ai_agent import preguntar_a_los_datos
from analytics import router as analytics_router
from camera import camara

JWT_SECRET_MIN = 32
JWT_SECRET_INSEGUROS = {"cambiar_esto_en_produccion", "super-secreto-kovaq"}


def validar_jwt_secret(valor):
    """Devuelve el secreto si es seguro; si no, impide que la app arranque."""
    valor = (valor or "").strip()
    if not valor or valor in JWT_SECRET_INSEGUROS or len(valor) < JWT_SECRET_MIN:
        raise RuntimeError(
            f"JWT_SECRET falta, es un valor por defecto o tiene menos de {JWT_SECRET_MIN} caracteres. "
            "La app no arrancará sin un secreto seguro. Genera uno con:\n"
            "    python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
            "y guárdalo como JWT_SECRET en tu archivo .env (o en las variables de entorno de Render)."
        )
    return valor


SECRET_KEY = validar_jwt_secret(os.getenv("JWT_SECRET"))
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))  # 8 horas
MODO = os.getenv("MODO", "local").lower()  # "local" o "nube"

ROLES_GESTION = ["admin", "admin_tecnico", "dueno"]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_VIDEO = os.path.join(BASE_DIR, "static", "demo_supermercado.mp4")


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
        # En nube inicia el motor ligero de Mapeo 2D y Simulación sobre el papel (sin abrir hardware de cámara)
        camara.iniciar(modo_nube=True)
    yield


app = FastAPI(title="Kovaq Retail Analytics", lifespan=lifespan)
app.include_router(analytics_router)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ==========================================
# MENÚ LATERAL UNIFICADO
# ==========================================
MENU = [
    {"url": "/dashboard", "titulo": "1. Dashboard General", "icono": "bi bi-speedometer2", "admin_only": False},
    {"url": "/usuarios", "titulo": "Gestión Usuarios", "icono": "bi bi-people", "admin_only": True},
]


# ==========================================
# AUTENTICACIÓN (100% ASÍNCRONA SIN BLOQUEAR EVENT LOOP)
# ==========================================
def _buscar_usuario_por_username(username: str):
    return leer(
        "SELECT u.id, u.username, u.nombre_completo, r.nombre FROM usuarios u JOIN roles r ON u.rol_id = r.id WHERE u.username = ?",
        (username,), uno=True
    )


def _buscar_usuario_login(user_clean: str):
    return leer(
        "SELECT u.id, u.username, u.password_hash, u.nombre_completo, r.nombre, u.estado FROM usuarios u JOIN roles r ON u.rol_id = r.id WHERE LOWER(u.username) = LOWER(?)",
        (user_clean,), uno=True
    )


async def obtener_usuario_actual(request: Request):
    token_cookie = request.cookies.get("access_token")
    if not token_cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No autorizado")
    try:
        token = token_cookie.split(" ")[1]
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"require": ["exp"]})
        usuario = await asyncio.to_thread(_buscar_usuario_por_username, payload.get("sub"))
        if not usuario:
            raise HTTPException(status_code=401)
        return {"id": usuario[0], "username": usuario[1], "nombre": usuario[2], "rol": usuario[3]}
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")


@app.get("/")
async def root():
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def login_action(request: Request, username: str = Form(...), password: str = Form(...)):
    user_clean = (username or "").strip()
    usuario = await asyncio.to_thread(_buscar_usuario_login, user_clean)
    if not usuario or not pwd_context.verify(password, usuario[2]) or usuario[5] != "activo":
        return templates.TemplateResponse(request, "login.html", {
            "error": "Credenciales inválidas. Verifique su usuario y contraseña."
        })

    vence = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = jwt.encode({"sub": usuario[1], "rol": usuario[4], "exp": vence}, SECRET_KEY, algorithm=ALGORITHM)
    response = RedirectResponse(url="/dashboard", status_code=303)
    es_https = (request.url.scheme == "https") or (request.headers.get("x-forwarded-proto") == "https")
    response.set_cookie(
        key="access_token", value=f"Bearer {token}",
        httponly=True, samesite="lax",
        secure=(MODO == "nube" and es_https),
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("access_token")
    return response


# ==========================================
# RUTAS DE INTERFAZ WEB (VISTAS)
# ==========================================
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    try:
        user = await obtener_usuario_actual(request)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "dashboard.html", {
        "usuario_actual": user, "menu": MENU, "modo": MODO
    })


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
        "SELECT u.id, u.username, u.nombre_completo, r.nombre, u.estado FROM usuarios u JOIN roles r ON u.rol_id = r.id"
    )
    roles = await asyncio.to_thread(leer, "SELECT id, nombre FROM roles")
    return templates.TemplateResponse(request, "usuarios.html", {
        "usuarios": usuarios, "roles": roles,
        "usuario_actual": user, "menu": MENU, "modo": MODO
    })


# ==========================================
# RUTAS DE GESTIÓN DE USUARIOS
# ==========================================
@app.post("/usuarios/crear")
async def crear_usuario(request: Request, nombre: str = Form(...), username: str = Form(...),
                        password: str = Form(...), rol_id: int = Form(...)):
    user = await obtener_usuario_actual(request)
    if user["rol"] not in ROLES_GESTION:
        raise HTTPException(status_code=403, detail="Permisos insuficientes")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 8 caracteres")
    try:
        await asyncio.to_thread(
            escribir,
            "INSERT INTO usuarios (username, password_hash, nombre_completo, rol_id, estado) VALUES (?, ?, ?, ?, 'activo')",
            (username.strip(), pwd_context.hash(password), nombre.strip(), rol_id)
        )
    except Exception:
        raise HTTPException(status_code=409, detail="El usuario ya existe")
    return RedirectResponse(url="/usuarios", status_code=303)


@app.post("/usuarios/eliminar/{usuario_id}")
async def eliminar_usuario(request: Request, usuario_id: int):
    user = await obtener_usuario_actual(request)
    if user["rol"] not in ROLES_GESTION:
        raise HTTPException(status_code=403, detail="Permisos insuficientes")
    if user["id"] == usuario_id:
        raise HTTPException(status_code=400, detail="No puedes eliminar tu propio usuario")
    await asyncio.to_thread(escribir, "DELETE FROM usuarios WHERE id = ?", (usuario_id,))
    return RedirectResponse(url="/usuarios", status_code=303)


# ==========================================
# ENDPOINTS DE MAPEO 2D Y TRACKING MULTI-CÁMARA
# ==========================================
@app.get("/api/map_state")
async def api_map_state(request: Request):
    """Devuelve el estado completo del Plano 2D de la tienda, cámaras, perchas, personas y handoffs Re-ID."""
    await obtener_usuario_actual(request)
    return await asyncio.to_thread(camara.obtener_estado_mapa_2d)


@app.post("/api/simulation/toggle")
async def api_simulation_toggle(request: Request):
    """Activa o desactiva la simulación 'Sobre el Papel' (Digital Twin Multi-Cámara)."""
    await obtener_usuario_actual(request)
    try:
        datos = await request.json()
    except Exception:
        datos = {}
    activar = datos.get("activar")
    estado = camara.toggle_simulacion_papel(activar)
    return {"ok": True, "simulacion_activa": estado}


@app.post("/api/cameras/select")
async def api_select_camera(request: Request):
    """Cambia la cámara activa cuyo ROI de perchas se inspecciona."""
    await obtener_usuario_actual(request)
    datos = await request.json()
    cam_id = (datos.get("camara_id") or "CAM-01").strip()
    actual = camara.cambiar_camara_activa(cam_id)
    return {"ok": True, "camara_activa": actual}


@app.get("/api/cameras")
async def api_listar_camaras(request: Request):
    await obtener_usuario_actual(request)
    cams = await asyncio.to_thread(obtener_camaras_mapa)
    return {"camaras": cams}


@app.post("/api/cameras")
async def api_guardar_camara(request: Request):
    await obtener_usuario_actual(request)
    datos = await request.json()
    cam_id = (datos.get("id") or f"CAM-0{uuid.uuid4().hex[:2].upper()}").strip()
    nombre = (datos.get("nombre") or "Nueva Cámara").strip()
    ruta = (datos.get("ruta") or "0").strip()
    estado = (datos.get("estado") or "Activa").strip()
    pos_x = float(datos.get("pos_x", 0.5))
    pos_y = float(datos.get("pos_y", 0.5))
    angulo = float(datos.get("angulo", 45.0))
    await asyncio.to_thread(
        registrar_o_actualizar_camara_mapa,
        cam_id, nombre, ruta, estado, pos_x, pos_y, angulo
    )
    camara.recargar_mapa_tienda()
    cams = await asyncio.to_thread(obtener_camaras_mapa)
    return {"ok": True, "camaras": cams}


@app.delete("/api/cameras/{cam_id}")
async def api_borrar_camara(request: Request, cam_id: str):
    await obtener_usuario_actual(request)
    await asyncio.to_thread(eliminar_camara_mapa, cam_id)
    camara.recargar_mapa_tienda()
    cams = await asyncio.to_thread(obtener_camaras_mapa)
    return {"ok": True, "camaras": cams}


@app.get("/api/shelves")
async def api_listar_perchas(request: Request):
    await obtener_usuario_actual(request)
    perchas = await asyncio.to_thread(obtener_perchas_mapa)
    return {"perchas": perchas}


@app.post("/api/shelves")
async def api_guardar_percha(request: Request):
    await obtener_usuario_actual(request)
    datos = await request.json()
    percha_id = (datos.get("id") or f"PER-{uuid.uuid4().hex[:3].upper()}").strip()
    nombre = (datos.get("nombre") or "Nueva Percha").strip()
    categoria = (datos.get("categoria") or "General").strip()
    camara_id = (datos.get("camara_id") or "CAM-01").strip()
    map_x = float(datos.get("map_x", 0.3))
    map_y = float(datos.get("map_y", 0.3))
    map_w = float(datos.get("map_w", 0.2))
    map_h = float(datos.get("map_h", 0.14))
    roi = datos.get("roi") or {"x1": 0.15, "y1": 0.15, "x2": 0.85, "y2": 0.85}
    niveles = datos.get("niveles") or [{"nivel": "Medio", "y1": 0.15, "y2": 0.85, "producto": nombre}]
    color = datos.get("color") or "#FE5000"
    await asyncio.to_thread(
        registrar_o_actualizar_percha_mapa,
        percha_id, nombre, categoria, camara_id, map_x, map_y, map_w, map_h, roi, niveles, color
    )
    camara.recargar_mapa_tienda()
    perchas = await asyncio.to_thread(obtener_perchas_mapa)
    return {"ok": True, "perchas": perchas}


@app.get("/api/promotions")
async def api_listar_promociones(request: Request):
    await obtener_usuario_actual(request)
    promos = await asyncio.to_thread(obtener_promociones_percha)
    return {"promociones": promos}


@app.post("/api/promotions")
async def api_guardar_promocion(request: Request):
    await obtener_usuario_actual(request)
    datos = await request.json()
    promo_id = (datos.get("id") or f"PROMO-{uuid.uuid4().hex[:3].upper()}").strip()
    percha_id = (datos.get("percha_id") or "PER-A1").strip()
    producto = (datos.get("producto") or "Producto en Góndola").strip()
    titulo = (datos.get("titulo") or "Promoción Punto de Venta").strip()
    tipo_publicidad = (datos.get("tipo_publicidad") or "Cenefa / Cartel POP").strip()
    mensaje_pop = (datos.get("mensaje_pop") or "").strip()
    descuento_pct = int(datos.get("descuento_pct") or 0)
    await asyncio.to_thread(
        registrar_promocion_percha,
        promo_id, percha_id, producto, titulo, tipo_publicidad, mensaje_pop, descuento_pct
    )
    camara.recargar_mapa_tienda()
    promos = await asyncio.to_thread(obtener_promociones_percha)
    return {"ok": True, "promociones": promos}


@app.delete("/api/promotions/{promo_id}")
async def api_borrar_promocion(request: Request, promo_id: str):
    await obtener_usuario_actual(request)
    await asyncio.to_thread(eliminar_promocion_percha, promo_id)
    camara.recargar_mapa_tienda()
    promos = await asyncio.to_thread(obtener_promociones_percha)
    return {"ok": True, "promociones": promos}



# ==========================================
# ENDPOINTS DE LA API REST (IA, STATS, VIDEO)
# ==========================================
@app.get("/api/sources")
async def listar_fuentes_camara(request: Request):
    await obtener_usuario_actual(request)
    return {
        "fuentes": [
            {"id": "webcam", "nombre": "Cámara Web Predeterminada (USB 0)"},
            {"id": "rtsp", "nombre": "Cámara IP Percha Principal (RTSP)"},
            {"id": "demo", "nombre": "Video Demostración Supermercado"}
        ]
    }


@app.post("/api/chat")
async def api_chat(request: Request):
    await obtener_usuario_actual(request)
    try:
        datos = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Solicitud inválida")

    mensaje = (datos.get("mensaje") or "").strip()
    modo = datos.get("modo", "datos")

    if not mensaje:
        raise HTTPException(status_code=400, detail="El mensaje está vacío")

    estado_vivo = camara.estado_dict() if camara else {}
    respuesta_ia = await preguntar_a_los_datos(mensaje, modo=modo, vivo=estado_vivo)
    return {"respuesta": respuesta_ia}


@app.post("/api/upload")
async def api_upload(request: Request, archivo: UploadFile = File(...)):
    await obtener_usuario_actual(request)

    ext = os.path.splitext(archivo.filename or "")[1].lower()
    if ext in {".mp4", ".avi", ".mov", ".mkv"}:
        tipo = "video"
    elif ext in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
        tipo = "imagen"
    else:
        raise HTTPException(status_code=400, detail="Formato no soportado")

    contenido = await archivo.read()
    if len(contenido) > 200 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El archivo supera 200 MB")

    os.makedirs("uploads", exist_ok=True)
    ruta = os.path.join("uploads", f"{uuid.uuid4().hex}{ext}")
    with open(ruta, "wb") as f:
        f.write(contenido)

    resultado_analisis = await asyncio.to_thread(camara.procesar_archivo_subido_inmediato, tipo, ruta)
    return resultado_analisis


@app.post("/api/control")
async def api_control(request: Request):
    await obtener_usuario_actual(request)
    datos = await request.json()
    accion = datos.get("accion")
    if accion == "pausa":
        return {"ok": True, "pausado": camara.toggle_pausa()}
    if accion == "parar":
        camara.apagar()
        return {"ok": True, "pausado": False}
    raise HTTPException(status_code=400, detail="Acción no válida")


@app.post("/api/fuente")
async def cambiar_fuente_api(request: Request):
    await obtener_usuario_actual(request)
    datos = await request.json()
    tipo = datos.get("tipo", "video")
    cam_id = datos.get("camara_id")
    if tipo not in ("webcam", "rtsp", "video", "imagen", "apagado"):
        raise HTTPException(status_code=400, detail="Fuente no válida")

    ruta = datos.get("ruta")
    if not ruta:
        rutas = {
            "video": DEMO_VIDEO if os.path.exists(DEMO_VIDEO) else "static/demo_supermercado.mp4",
            "imagen": "static/demo.jpg" if os.path.exists("static/demo.jpg") else "uploads/demo.jpg"
        }
        ruta = rutas.get(tipo, "")
    if MODO == "local":
        camara.cambiar_fuente(tipo, ruta, cam_id=cam_id)
    elif cam_id:
        camara.cambiar_camara_activa(cam_id)
    return {"ok": True, "tipo": tipo, "ruta": ruta, "camara_activa": camara.camara_activa_id}


# ==========================================
# ESTADÍSTICAS CONSOLIDADAS CON FILTRO TEMPORAL (DÍA / SEMANA / MES / RANGO)
# ==========================================
def _obtener_stats(dias: int = 30, fecha_inicio: str = None, fecha_fin: str = None):
    where_ev, params_ev = _construir_filtro_fecha(dias, fecha_inicio, fecha_fin, "creado")
    fila = leer(f"""
        SELECT
            COALESCE(SUM(CASE WHEN tipo = 'trafico' THEN 1 ELSE 0 END), 0) AS trafico,
            COALESCE(SUM(CASE WHEN tipo = 'permanencia' THEN 1 ELSE 0 END), 0) AS perm_eventos,
            COALESCE(AVG(CASE WHEN tipo = 'permanencia' THEN valor END), 0) AS perm_prom,
            COALESCE(SUM(CASE WHEN tipo IN ('interaccion', 'producto_tomado') THEN 1 ELSE 0 END), 0) AS inter,
            COALESCE(SUM(CASE WHEN tipo = 'producto_tomado' THEN 1 ELSE 0 END), 0) AS tomados,
            COALESCE(SUM(CASE WHEN tipo = 'producto_devuelto' THEN 1 ELSE 0 END), 0) AS devueltos,
            COALESCE(SUM(CASE WHEN tipo = 'handoff_camara' THEN 1 ELSE 0 END), 0) AS handoffs
        FROM eventos
        WHERE {where_ev}
    """, params_ev, uno=True)
    top = leer(f"""
        SELECT detalle, COUNT(*) FROM eventos
        WHERE tipo IN ('interaccion', 'producto_tomado') AND {where_ev}
        GROUP BY detalle ORDER BY COUNT(*) DESC LIMIT 5
    """, params_ev)
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
async def api_stats(request: Request, dias: int = 30, fecha_inicio: str = None, fecha_fin: str = None):
    await obtener_usuario_actual(request)
    try:
        s = await asyncio.to_thread(_obtener_stats, dias, fecha_inicio, fecha_fin)
    except Exception:
        raise HTTPException(status_code=500, detail="Error consultando estadísticas")

    s["resumen"] = await asyncio.to_thread(_texto_resumen, s)
    return s


@app.get("/api/live_events")
async def api_live_events(request: Request):
    await obtener_usuario_actual(request)
    return camara.obtener_telemetria()


# ==========================================
# VIDEO STREAMING (DISPONIBLE EN LOCAL Y AL SUBIR VIDEO EN NUBE)
# ==========================================
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


# ==========================================
# VIDEO EN VIVO PARA LA WEB (MODO NUBE)
# ==========================================
VIDEO_TOKEN = os.getenv("VIDEO_PUSH_TOKEN", "")
_ultimo_frame = {"jpg": None, "id": 0}
_frame_lock = threading.Lock()


@app.post("/api/frame_push")
async def frame_push(request: Request):
    if not VIDEO_TOKEN or request.headers.get("X-Token") != VIDEO_TOKEN:
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
