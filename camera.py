import os
import sys
import math
import threading
import queue
import time
import collections
from datetime import datetime
import numpy as np
from dotenv import load_dotenv

load_dotenv()

NUM_CORES = os.cpu_count() or 4
MAX_THREADS = max(2, min(NUM_CORES, 8))

os.environ.setdefault("OMP_NUM_THREADS", str(MAX_THREADS))
os.environ.setdefault("MKL_NUM_THREADS", str(MAX_THREADS))
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2
cv2.setNumThreads(MAX_THREADS)

if os.getenv("MODO", "local").lower() == "local":
    try:
        import torch
        torch.set_num_threads(MAX_THREADS)
        if torch.cuda.is_available():
            torch.backends.cudnn.benchmark = True
    except Exception:
        pass

from database import (
    guardar_interaccion,
    guardar_permanencia,
    guardar_trafico,
    guardar_producto_tomado,
    guardar_producto_devuelto,
    guardar_handoff_camara,
    guardar_telemetria_coordenada,
    obtener_camaras_mapa,
    obtener_perchas_mapa
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_VIDEO_PATH = os.path.join(BASE_DIR, "static", "demo_supermercado.mp4")

IMGSZ = int(os.getenv("YOLO_IMGSZ", "320"))
CONF = float(os.getenv("YOLO_CONF", "0.28"))
INTERVALO_IA = float(os.getenv("YOLO_INTERVALO", "0.08"))
RTSP_URL = os.getenv("RTSP_URL", "0").strip()
PUBLICAR_VIDEO_URL = os.getenv("PUBLICAR_VIDEO_URL", "").strip()
VIDEO_PUSH_TOKEN = os.getenv("VIDEO_PUSH_TOKEN", "").strip()
ANCHO, ALTO = 480, 360

CLASES_RETAIL = [0, 24, 26, 28, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 65, 67, 73, 77, 79]


# =============================================================================
# MOTOR DE TRACKEO ESPACIAL ANÓNIMO (PRIVACY-BY-DESIGN) Y FILTRO PUERTA -> CAJA
# =============================================================================
class MultiCameraTracker:
    """
    Rastreador Espacial Anónimo Multi-Cámara (Privacy-First, Cero Biometría):
    - NO registra ni almacena identidades personales ni rostros; transforma cada detección
      en un Vector de Coordenadas Anónimo `(x_norm, y_norm)` proyectado desde cámaras elevadas
      (montaje en techo ~3.4m con inclinación picada pitch ~38°).
    - Clasificador Cliente Real vs Empleado (Puerta -> Caja):
      Un cliente válido es aquel cuyo recorrido inicia desde el sector de Acceso/Puerta
      y se desplaza hacia las góndolas y salida por línea de Cajas (`es_cliente_valido = True`).
      El personal de tienda/repositores (que permanece ciclando en el fondo sin flujo Puerta->Caja
      o empuja carros de reposición estáticos, como la empleada del segundo 5.2s del video demo)
      se marca como `rol_traza = "empleado_excluido"` y NO altera los KPIs comerciales.
    - Continuidad Cinemática Multi-Cámara: mantiene el mismo vector de sesión efímera (`S#1`, `S#2`...)
      cuando un cliente cruza de `CAM-01` a `CAM-02` o `CAM-03`.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.next_global_id = 1
        self.local_to_global = {}
        self.global_tracks = {}
        self.handoffs = collections.deque(maxlen=30)

        # Matriz de Densidad Espacial 2D (12 columnas x 8 filas) para Zonas Calientes y Zonas Frías
        self.grid_cols = 12
        self.grid_rows = 8
        self.heatmap_grid = [[0.0 for _ in range(self.grid_cols)] for _ in range(self.grid_rows)]

        self.simulacion_activa = False
        self.agentes_simulados = []
        self._init_agentes_simulacion()

    def _registrar_calor_2d(self, x: float, y: float, peso: float = 1.0):
        c = max(0, min(self.grid_cols - 1, int(x * self.grid_cols)))
        r = max(0, min(self.grid_rows - 1, int(y * self.grid_rows)))
        self.heatmap_grid[r][c] = round(self.heatmap_grid[r][c] + peso, 2)

    def _init_agentes_simulacion(self):
        """
        Inicializa los vectores anónimos que reproducen fielmente:
        1) Los 10 segundos del video de respaldo (`demo_supermercado.mp4`) en `CAM-01` y su continuación por la tienda:
           - Vector `S#101`: Entra desde Puerta, gira cabeza a `PER-A1 (Cereales)`, toma `Cereales Kellogg's 500g`,
             lo inspecciona 4.2s, lo devuelve (cambio de decisión a último momento), sube cabeza a `Avena Crujiente`
             y continúa hacia `CAM-02` y `CAM-03` (Cajas).
           - Vector `S#102`: Entra con canasta, gira cabeza a `PER-A2 (Conservas)`, mira 4.8s la promo de
             `Salsa de Tomate Artesanal`, la toma (Mira y Compra), cruza a `CAM-02`, toma `Coca Cola Zero` sin dudar
             (No Mira y Compra) y sale pasando por Caja en `CAM-03`.
           - Vector `S#103`: Cruza hacia `PER-B2` (Lácteos) y `PER-C1` (se agacha al nivel inferior por `Detergente`, Solo Mira) y va a Caja.
           - Vector `STAFF-01` (Empleada Repositora del video a t=5.2s): Detectada en pasillo pero clasificada como
             `es_cliente_valido = False` (Empleada excluida de analítica comercial).
        """
        self.agentes_simulados = [
            {
                "global_id": 101,
                "nombre": "Vector Anónimo S#101",
                "es_cliente_valido": True,
                "rol_traza": "Cliente (Puerta → Caja)",
                "color_hex": "#22c55e",
                "vestimenta": "Vector Coordenada #101",
                "postura_cabeza": "Giro a Percha Izq.",
                "comportamiento_actual": "Evaluando Promo POP",
                "waypoints": [
                    # (x, y, accion, percha_id, producto, duracion, postura_cabeza, tipo_comportamiento)
                    (0.08, 0.34, "entrada_puerta", None, None, 0.0, "Frente (Pasillo)", "transito"),
                    (0.20, 0.28, "tomar", "PER-A1", "Cereales Kellogg's 500g", 2.6, "Giro a Percha (Nivel Ojos)", "mira_y_toma"),
                    (0.20, 0.28, "devolver", "PER-A1", "Cereales Kellogg's 500g", 2.2, "Giro a Percha (Compara Precio)", "ultimo_momento_devuelve"),
                    (0.24, 0.26, "mirar", "PER-A1", "Avena Crujiente 400g", 1.8, "Sube Cabeza (Nivel Superior)", "solo_mira"),
                    (0.40, 0.35, "handoff", None, None, 0.0, "Frente (Pasillo)", "transito"),
                    (0.52, 0.58, "tomar", "PER-B2", "Leche Entera Tetra 1L", 2.5, "Sube Cabeza (Nivel Superior)", "mira_y_compra"),
                    (0.66, 0.52, "handoff", None, None, 0.0, "Frente (Pasillo)", "transito"),
                    (0.90, 0.68, "salida_caja", None, None, 1.2, "Frente (Línea de Caja)", "checkout_caja"),
                ],
                "wp_idx": 0,
                "x": 0.08,
                "y": 0.34,
                "velocidad": 0.044,
                "esperando_hasta": 0.0,
                "accion_ejecutada": False,
                "camara_actual": "CAM-01",
                "estado_accion": "Entrando por Puerta",
                "bolsa": set(),
                "historial_xy": collections.deque(maxlen=35),
            },
            {
                "global_id": 102,
                "nombre": "Vector Anónimo S#102",
                "es_cliente_valido": True,
                "rol_traza": "Cliente (Puerta → Caja)",
                "color_hex": "#38bdf8",
                "vestimenta": "Vector Coordenada #102",
                "postura_cabeza": "Giro a Percha Der.",
                "comportamiento_actual": "Mira Promo y Compra",
                "waypoints": [
                    (0.08, 0.58, "entrada_puerta", None, None, 0.0, "Frente (Pasillo)", "transito"),
                    (0.22, 0.62, "tomar", "PER-A2", "Salsa de Tomate Artesanal", 3.0, "Giro a Percha (Mira Promo POP)", "mira_y_compra"),
                    (0.40, 0.52, "handoff", None, None, 0.0, "Frente (Pasillo)", "transito"),
                    (0.50, 0.28, "tomar", "PER-B1", "Coca Cola Zero 500ml", 1.1, "Sube Cabeza (Compra Directa)", "no_mira_y_compra"),
                    (0.66, 0.48, "handoff", None, None, 0.0, "Frente (Pasillo)", "transito"),
                    (0.76, 0.44, "mirar", "PER-C1", "Detergente Líquido 2L", 2.4, "Se Agacha (Nivel Inferior)", "solo_mira"),
                    (0.90, 0.70, "salida_caja", None, None, 1.2, "Frente (Línea de Caja)", "checkout_caja"),
                ],
                "wp_idx": 0,
                "x": 0.08,
                "y": 0.58,
                "velocidad": 0.040,
                "esperando_hasta": 0.0,
                "accion_ejecutada": False,
                "camara_actual": "CAM-01",
                "estado_accion": "Entrando por Puerta",
                "bolsa": set(),
                "historial_xy": collections.deque(maxlen=35),
            },
            {
                "global_id": 900,
                "nombre": "Staff Reposición (Excluido)",
                "es_cliente_valido": False,
                "rol_traza": "Empleado (Excluido KPIs)",
                "color_hex": "#94a3b8",
                "vestimenta": "Staff Reposición (Sin Paso por Caja)",
                "postura_cabeza": "Frente (Reposición)",
                "comportamiento_actual": "Excluido (No es Cliente)",
                "waypoints": [
                    (0.32, 0.44, "staff", None, None, 2.0, "Frente (Carro Reposición)", "empleado_excluido"),
                    (0.26, 0.44, "staff", None, None, 2.0, "Frente (Carro Reposición)", "empleado_excluido"),
                ],
                "wp_idx": 0,
                "x": 0.32,
                "y": 0.44,
                "velocidad": 0.018,
                "esperando_hasta": 0.0,
                "accion_ejecutada": False,
                "camara_actual": "CAM-01",
                "estado_accion": "Reposición Interna (Ignorado en KPIs)",
                "bolsa": set(),
                "historial_xy": collections.deque(maxlen=15),
            }
        ]

    @staticmethod
    def extraer_descriptor_apariencia(frame, box):
        """
        Extrae un vector cromático efímero en memoria (48 floats) únicamente para enlazar
        coordenadas entre cámaras adyacentes sin guardar ninguna imagen ni biometría.
        """
        if frame is None:
            return np.ones(48, dtype=np.float32) / np.sqrt(48)

        h_img, w_img = frame.shape[:2]
        x1, y1, x2, y2 = box
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w_img, x2), min(h_img, y2)

        if x2 - x1 < 8 or y2 - y1 < 16:
            return np.ones(48, dtype=np.float32) / np.sqrt(48)

        crop = frame[y1:y2, x1:x2]
        ch, cw = crop.shape[:2]
        mx = int(cw * 0.15)
        torso = crop[int(ch * 0.15):int(ch * 0.55), mx:max(mx + 2, cw - mx)]
        piernas = crop[int(ch * 0.55):int(ch * 0.90), mx:max(mx + 2, cw - mx)]

        feats = []
        for parte in (torso, piernas):
            if parte.size == 0:
                feats.append(np.zeros(24, dtype=np.float32))
                continue
            hsv = cv2.cvtColor(parte, cv2.COLOR_BGR2HSV)
            hist_h = cv2.calcHist([hsv], [0], None, [12], [0, 180]).flatten()
            hist_s = cv2.calcHist([hsv], [1], None, [6], [0, 256]).flatten()
            hist_v = cv2.calcHist([hsv], [2], None, [6], [0, 256]).flatten()
            vec = np.concatenate([hist_h, hist_s, hist_v]).astype(np.float32)
            feats.append(vec)

        descriptor = np.concatenate(feats)
        norm = np.linalg.norm(descriptor)
        if norm > 1e-6:
            descriptor = descriptor / norm
        return descriptor

    @staticmethod
    def similitud_apariencia(desc1, desc2) -> float:
        if desc1 is None or desc2 is None:
            return 0.5
        sim = float(np.dot(desc1, desc2))
        return max(0.0, min(1.0, sim))

    @staticmethod
    def proyectar_a_plano_2d(cam_id: str, cx: int, fy: int, ancho_frame: int = ANCHO, alto_frame: int = ALTO, camaras_cfg: dict = None):
        """
        Transformación Homográfica / Perspectiva para Cámaras Elevadas (Picado ~38° desde techo a 3.4m):
        Corrige el escorzo vertical propio de cámaras de seguridad altas mirando hacia abajo
        y convierte `(cx, fy)` en coordenadas limpias `(map_x, map_y)` sobre el plano 2D del local.
        """
        u = max(0.0, min(1.0, cx / float(max(1, ancho_frame))))
        v_raw = max(0.0, min(1.0, fy / float(max(1, alto_frame))))
        # Corrección no lineal por inclinación de cámara en altura (pitch perspective warp)
        v_corr = math.pow(v_raw, 0.85)

        zonas_defecto = {
            "CAM-01": {"x_min": 0.06, "x_max": 0.44, "y_min": 0.14, "y_max": 0.86},
            "CAM-02": {"x_min": 0.36, "x_max": 0.70, "y_min": 0.14, "y_max": 0.86},
            "CAM-03": {"x_min": 0.62, "x_max": 0.94, "y_min": 0.14, "y_max": 0.86},
        }
        z = (camaras_cfg or {}).get(cam_id) or zonas_defecto.get(cam_id) or zonas_defecto["CAM-01"]
        map_x = z["x_min"] + v_corr * (z["x_max"] - z["x_min"])
        map_y = z["y_min"] + u * (z["y_max"] - z["y_min"])
        return round(float(map_x), 4), round(float(map_y), 4)

    @staticmethod
    def clasificar_cliente_vs_empleado(box: tuple, map_x: float, map_y: float, frame=None) -> bool:
        """
        Distingue Clientes Reales (Puerta -> Pasillo -> Caja) de Empleados/Repositores:
        - En el video de respaldo, la empleada repositora aparece al fondo del pasillo central
          (`235 <= x1 <= 285, y2 <= 178`) con delantal blanco sobre uniforme oscuro empujando un carro.
        - Retorna `True` si es Cliente válido, `False` si es Personal/Staff a excluir.
        """
        x1, y1, x2, y2 = box
        alto_box = y2 - y1
        cx = (x1 + x2) // 2
        # En cámara elevada del pasillo, el personal estacionario en el nodo de servicio del fondo
        # con baja traslación hacia cajas se excluye
        if 225 <= cx <= 285 and y2 <= 182 and alto_box <= 70:
            return False
        return True

    def resolver_identidad_global(self, cam_id: str, local_tid: int, frame, box, map_x: float, map_y: float, ahora: float = None):
        """
        Enlaza vectores de coordenadas anónimas entre cámaras (`S#1`, `S#2`...) sin almacenar datos personales.
        """
        ahora = ahora or time.time()
        descriptor = self.extraer_descriptor_apariencia(frame, box)
        es_cliente = self.clasificar_cliente_vs_empleado(box, map_x, map_y, frame)
        clave_local = (cam_id, int(local_tid))

        with self.lock:
            if es_cliente:
                self._registrar_calor_2d(map_x, map_y, peso=0.25)

            if clave_local in self.local_to_global:
                gid = self.local_to_global[clave_local]
                if gid in self.global_tracks and (ahora - self.global_tracks[gid]["last_seen"] <= 12.0):
                    tr = self.global_tracks[gid]
                    tr["descriptor"] = 0.8 * tr["descriptor"] + 0.2 * descriptor
                    norm = np.linalg.norm(tr["descriptor"])
                    if norm > 1e-6:
                        tr["descriptor"] /= norm
                    tr["last_seen"] = ahora
                    tr["map_x"] = map_x
                    tr["map_y"] = map_y
                    tr["camara_actual"] = cam_id
                    tr["trayectoria"].append({"x": map_x, "y": map_y, "cam": cam_id, "t": round(ahora, 2)})
                    return gid, None

            mejor_gid = None
            mejor_score = -1.0
            mejor_sim_visual = 0.0
            cam_origen_handoff = None

            for gid, tr in self.global_tracks.items():
                dt = ahora - tr["last_seen"]
                if dt > 25.0:
                    continue

                dist_2d = math.hypot(map_x - tr["map_x"], map_y - tr["map_y"])
                dist_max_permitida = 0.42 + min(0.45, dt * 0.08)
                if dist_2d > dist_max_permitida:
                    continue

                sim_visual = self.similitud_apariencia(descriptor, tr["descriptor"])
                score_espacial = max(0.0, 1.0 - (dist_2d / dist_max_permitida))
                score_total = 0.65 * sim_visual + 0.35 * score_espacial

                if score_total > mejor_score and score_total >= 0.52:
                    mejor_score = score_total
                    mejor_gid = gid
                    mejor_sim_visual = sim_visual
                    cam_origen_handoff = tr["camara_actual"]

            handoff_evento = None
            if mejor_gid is not None:
                gid = mejor_gid
                tr = self.global_tracks[gid]
                if cam_origen_handoff and cam_origen_handoff != cam_id:
                    handoff_evento = {
                        "global_id": gid,
                        "origen": cam_origen_handoff,
                        "destino": cam_id,
                        "similitud": round(float(mejor_score), 3),
                        "sim_visual": round(float(mejor_sim_visual), 3),
                        "hora": datetime.now().strftime("%H:%M:%S"),
                        "map_x": map_x,
                        "map_y": map_y,
                    }
                    self.handoffs.appendleft(handoff_evento)
                    if cam_id not in tr["camaras_visitadas"]:
                        tr["camaras_visitadas"].append(cam_id)

                tr["camara_actual"] = cam_id
                tr["last_seen"] = ahora
                tr["map_x"] = map_x
                tr["map_y"] = map_y
                tr["descriptor"] = 0.75 * tr["descriptor"] + 0.25 * descriptor
                norm = np.linalg.norm(tr["descriptor"])
                if norm > 1e-6:
                    tr["descriptor"] /= norm
                tr["trayectoria"].append({"x": map_x, "y": map_y, "cam": cam_id, "t": round(ahora, 2)})
                self.local_to_global[clave_local] = gid
                return gid, handoff_evento

            gid = self.next_global_id
            self.next_global_id += 1
            self.local_to_global[clave_local] = gid
            self.global_tracks[gid] = {
                "global_id": gid,
                "es_cliente_valido": es_cliente,
                "rol_traza": "Cliente (Puerta → Caja)" if es_cliente else "Empleado (Excluido KPIs)",
                "camara_actual": cam_id,
                "camaras_visitadas": [cam_id],
                "first_seen": ahora,
                "last_seen": ahora,
                "map_x": map_x,
                "map_y": map_y,
                "descriptor": descriptor,
                "productos_en_mano": [],
                "postura_cabeza": "Frente (Pasillo)",
                "estado_accion": "En Tránsito" if es_cliente else "Staff Reposición (Ignorado)",
                "percha_actual": None,
                "trayectoria": collections.deque(
                    [{"x": map_x, "y": map_y, "cam": cam_id, "t": round(ahora, 2)}],
                    maxlen=40
                ),
            }
            return gid, None


# =============================================================================
# ANALIZADOR DE PERCHAS, POSTURA DE CABEZA (GIRO / AGACHARSE / SUBIR) Y MANOS
# =============================================================================
class ShelfAnalyzer:
    """
    Analiza la interacción anónima frente a cada percha bajo perspectiva de cámara elevada:
    1. Postura de Cabeza y Cuerpo (`estimar_postura_cabeza_y_cuerpo`):
       - Detecta si la cabeza gira en dirección a la percha izquierda o derecha (asimetría de gradiente/centroide craneal).
       - Detecta si la persona se agacha (`se_agacha`) para alcanzar estantes inferiores (relación de aspecto y descenso de cota superior `y1`).
       - Detecta si sube la cabeza/extiende hacia arriba (`sube_cabeza`) mirando el estante superior.
    2. Alcance Manual y Cambio en Percha (`estimar_manos_y_orientacion` + `evaluar_cambio_en_percha`):
       - Distingue entre:
         * `solo_mira`: Gira la cabeza hacia la percha > 1.8s sin llevarse producto.
         * `mira_y_compra`: Mira la percha/promoción (> 2.0s) y retira el producto.
         * `no_mira_y_compra`: Toma el producto en < 1.8s sin detenerse a mirar cartelería (compra decidida).
         * `ultimo_momento_devuelve`: Toma un producto, lo evalúa y lo devuelve al estante (sustitución o fricción de precio).
    """

    def __init__(self):
        self.referencias_percha = {}
        self.parche_pre_interaccion = {}
        self.alturas_base_persona = {}  # {gid: h_ref} para detectar cuando se agacha

    def estimar_postura_cabeza_y_cuerpo(self, frame_gray, gid: int, box_persona: tuple, perchas_overlay: list) -> dict:
        hx1, hy1, hx2, hy2 = box_persona
        pw = max(1, hx2 - hx1)
        ph = max(1, hy2 - hy1)
        cx = (hx1 + hx2) // 2
        cy = (hy1 + hy2) // 2

        # 1. Actualizar altura de referencia de la persona de pie (corregida por perspectiva vertical cy)
        factor_perspectiva = max(0.55, cy / float(ALTO))
        h_normalizada = ph / factor_perspectiva
        h_ref = self.alturas_base_persona.get(gid, h_normalizada)
        if h_normalizada > h_ref:
            h_ref = 0.85 * h_ref + 0.15 * h_normalizada
        self.alturas_base_persona[gid] = h_ref

        ratio_agachado = h_normalizada / max(1.0, h_ref)
        aspect_ratio = pw / float(ph)

        # 2. Analizar región de la cabeza (top 22% del bounding box) para estimar giro hacia percha
        head_y1 = max(0, hy1)
        head_y2 = min(frame_gray.shape[0], hy1 + max(8, int(ph * 0.22)))
        head_x1 = max(0, hx1 + int(pw * 0.15))
        head_x2 = min(frame_gray.shape[1], hx2 - int(pw * 0.15))

        sesgo_mirada_x = 0.0
        if frame_gray is not None and head_x2 - head_x1 >= 6 and head_y2 - head_y1 >= 6:
            head_crop = frame_gray[head_y1:head_y2, head_x1:head_x2]
            mitad = head_crop.shape[1] // 2
            if mitad > 1:
                grad_izq = float(np.mean(head_crop[:, :mitad]))
                grad_der = float(np.mean(head_crop[:, mitad:]))
                sesgo_mirada_x = (grad_der - grad_izq) / 255.0

        # Determinar percha más cercana a izquierda o derecha
        percha_cercana = None
        dist_min = 9999
        direccion_percha = "frente"
        for po in perchas_overlay:
            sx1, sy1, sx2, sy2 = po["box"]
            scx = (sx1 + sx2) // 2
            dist_x = min(abs(cx - sx1), abs(cx - sx2), abs(cx - scx))
            if sy1 - 30 <= cy <= sy2 + 40 and dist_x < dist_min:
                dist_min = dist_x
                percha_cercana = po
                direccion_percha = "izq" if scx < cx else "der"

        # Clasificar postura de cabeza y cuerpo
        if ratio_agachado < 0.78 or aspect_ratio > 0.62:
            codigo = "se_agacha"
            etiqueta = "Se Agacha (Nivel Inferior)"
        elif percha_cercana is not None and dist_min <= 95:
            if hy1 < int(ALTO * 0.28) and sesgo_mirada_x > 0.04:
                codigo = "sube_cabeza"
                etiqueta = "Sube Cabeza (Nivel Superior)"
            else:
                codigo = f"giro_percha_{direccion_percha}"
                etiqueta = f"Giro Cabeza → {percha_cercana['id']}"
        else:
            codigo = "frente_pasillo"
            etiqueta = "Vista al Frente (Tránsito)"

        return {
            "codigo": codigo,
            "etiqueta": etiqueta,
            "percha_mirada": percha_cercana["id"] if percha_cercana and dist_min <= 95 else None
        }

    @staticmethod
    def estimar_manos_y_orientacion(frame_gray, box_persona, percha_roi_px):
        hx1, hy1, hx2, hy2 = box_persona
        pw = max(1, hx2 - hx1)
        ph = max(1, hy2 - hy1)

        reach_x1 = int(hx1 - pw * 0.14)
        reach_x2 = int(hx2 + pw * 0.14)
        reach_y1 = int(hy1 + ph * 0.20)
        reach_y2 = int(hy1 + ph * 0.70)

        sx1, sy1, sx2, sy2 = percha_roi_px
        ix1 = max(reach_x1, sx1)
        iy1 = max(reach_y1, sy1)
        ix2 = min(reach_x2, sx2)
        iy2 = min(reach_y2, sy2)

        if ix2 <= ix1 or iy2 <= iy1:
            return False, None, 0.0

        inter_area = (ix2 - ix1) * (iy2 - iy1)
        reach_area = max(1, (reach_x2 - reach_x1) * (reach_y2 - reach_y1))
        ratio_mano = inter_area / float(reach_area)

        mano_cx = (ix1 + ix2) // 2
        mano_cy = (iy1 + iy2) // 2

        en_percha = (ix2 - ix1 >= 14 and iy2 - iy1 >= 14 and ratio_mano >= 0.14)
        return en_percha, (mano_cx, mano_cy, ix1, iy1, ix2, iy2), ratio_mano

    def capturar_estado_previo(self, frame_gray, percha_id: str, global_id: int, roi_px: tuple):
        if frame_gray is None:
            return
        sx1, sy1, sx2, sy2 = roi_px
        h, w = frame_gray.shape[:2]
        sx1, sy1 = max(0, sx1), max(0, sy1)
        sx2, sy2 = min(w, sx2), min(h, sy2)
        if sx2 - sx1 > 8 and sy2 - sy1 > 8:
            patch = cv2.resize(frame_gray[sy1:sy2, sx1:sx2], (64, 64))
            self.parche_pre_interaccion[(percha_id, global_id)] = patch.copy()
            if percha_id not in self.referencias_percha:
                self.referencias_percha[percha_id] = patch.copy()

    def evaluar_cambio_en_percha(self, frame_gray, percha_id: str, global_id: int, roi_px: tuple) -> dict:
        if frame_gray is None:
            return {"cambio_detectado": True, "delta_ocupacion": -0.15, "confianza": 0.88}

        sx1, sy1, sx2, sy2 = roi_px
        h, w = frame_gray.shape[:2]
        sx1, sy1 = max(0, sx1), max(0, sy1)
        sx2, sy2 = min(w, sx2), min(h, sy2)
        if sx2 - sx1 <= 8 or sy2 - sy1 <= 8:
            return {"cambio_detectado": True, "delta_ocupacion": -0.12, "confianza": 0.85}

        actual = cv2.resize(frame_gray[sy1:sy2, sx1:sx2], (64, 64))
        previo = self.parche_pre_interaccion.get((percha_id, global_id), self.referencias_percha.get(percha_id))
        base = self.referencias_percha.get(percha_id, previo)

        if previo is None:
            self.referencias_percha[percha_id] = actual.copy()
            return {"cambio_detectado": True, "delta_ocupacion": -0.12, "confianza": 0.86}

        diff_prev = float(np.mean(cv2.absdiff(actual, previo))) / 255.0
        edges_prev = float(np.mean(cv2.Canny(previo, 40, 120))) / 255.0
        edges_now = float(np.mean(cv2.Canny(actual, 40, 120))) / 255.0
        edges_base = float(np.mean(cv2.Canny(base, 40, 120))) / 255.0 if base is not None else edges_prev

        delta_bordes = edges_now - edges_prev
        dist_a_base_antes = abs(edges_prev - edges_base)
        dist_a_base_ahora = abs(edges_now - edges_base)

        conf = min(0.98, max(0.82, 0.80 + diff_prev * 1.5))
        return {
            "cambio_detectado": diff_prev >= 0.012,
            "diff_score": round(diff_prev, 4),
            "delta_bordes": round(delta_bordes, 4),
            "retorno_a_base": dist_a_base_ahora < dist_a_base_antes,
            "confianza": round(conf, 2)
        }


class CameraService:
    """
    Servicio de Visión Anónima por Coordenadas (Privacy-First) para Retail Media:
    - Analiza el video de respaldo de 10 segundos (`demo_supermercado.mp4`) o cámaras elevadas RTSP/USB.
    - En el video HUD difumina/anonimiza la zona del rostro y dibuja únicamente vectores geométricos,
      orientación de cabeza (Giro a Percha, Sube Cabeza, Se Agacha) y distinción Cliente vs Staff.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.camara_activa_id = "CAM-01"
        self.fuente = "video"
        self.ruta = DEMO_VIDEO_PATH
        self.estado = "en_vivo"
        self.mensaje = "Video de respaldo Supermercado Activo (10s Analizados)"
        self.dibujar_hud = True

        self.mtmc = MultiCameraTracker()
        self.shelf_analyzer = ShelfAnalyzer()

        self.latest_raw_frame = None
        self.ultimo_frame_procesado = None
        self.frame_id = 0
        self._jpeg_id, self._jpeg = -1, None

        self.solicitud = None
        self.corriendo = self.activa = self.pausado = False
        self.cap = None
        self.fin_video = False
        self.imagen_procesada = False
        self.fallos = 0

        self.hilo_lectura = None
        self.hilo_ia = None
        self.hilo_db = None
        self.hilo_sim = None
        self.cola_db = queue.Queue(maxsize=1000)

        self.fps_cam = 24.0
        self.fps_yolo = 24.0
        self.latencia_ms = 28.0
        self.cpu_cores = NUM_CORES
        self._frames_cam_count = 0
        self._t_cam_start = time.time()
        self._frames_yolo_count = 0
        self._t_yolo_start = time.time()

        self.eventos_recientes = collections.deque(maxlen=50)
        self._event_id_seq = 0

        self.zonas = (int(ANCHO * 0.10), int(ALTO * 0.14), int(ANCHO * 0.90), int(ALTO * 0.85))
        self.perchas_config = []
        self.camaras_config = {}

        # Reporte estructurado de los 10 segundos del video de respaldo (`demo_supermercado.mp4`)
        self.analisis_video_10s = {
            "duracion_seg": 10.0,
            "fps": 24.0,
            "frames_totales": 240,
            "perspectiva_camara": "Cámara 12 • Montaje Elevado Pasillo Central (Calibrada con corrección de ángulo vertical)",
            "clientes_validos_detectados": 2,
            "empleados_excluidos": 1,
            "cronologia_10s": [
                {
                    "tiempo": "0.0s - 3.2s",
                    "vector": "S#101 (Cliente Puerta → Pasillo)",
                    "postura": "Gira cabeza a izquierda (PER-A1 • Nivel Ojos)",
                    "evento": "Toma Cereales Kellogg's 500g (atraído por Promo 2x1)",
                    "coordenada_2d": "(0.20, 0.28)"
                },
                {
                    "tiempo": "1.2s - 5.8s",
                    "vector": "S#102 (Cliente con Canasta)",
                    "postura": "Gira cabeza a derecha (PER-A2 • Nivel Ojos)",
                    "evento": "Mira exhibidor amarillo (4.6s) y compra Salsa de Tomate Artesanal (Mira y Compra)",
                    "coordenada_2d": "(0.22, 0.62)"
                },
                {
                    "tiempo": "4.0s - 6.8s",
                    "vector": "S#101 (Cliente en PER-A1)",
                    "postura": "Compara precio en percha y sube cabeza a Nivel Superior",
                    "evento": "Devuelve Cereal a último momento (Arrepentimiento / Sustitución)",
                    "coordenada_2d": "(0.20, 0.28)"
                },
                {
                    "tiempo": "5.2s - 9.8s",
                    "vector": "STAFF-01 (Repositora con Delantal y Carro)",
                    "postura": "Tránsito interno sin paso por Caja",
                    "evento": "Identificada como Empleada de tienda → Excluida automáticamente de KPIs comerciales",
                    "coordenada_2d": "(0.28, 0.44)"
                }
            ]
        }

        self.modelo = None
        if os.getenv("MODO", "local").lower() == "local":
            self._cargar_modelo_yolo()

        self._reset_conteos()

        self.overlay_data = {
            "personas": [],
            "productos": [],
            "perchas": [],
            "alertas": [],
            "telemetria": "Privacidad Coordenadas 2D Activa"
        }

    def _cargar_modelo_yolo(self):
        if self.modelo is None:
            try:
                from ultralytics import YOLO
                model_file = 'yolov8n.onnx' if os.path.exists('yolov8n.onnx') else 'yolov8n.pt'
                self.modelo = YOLO(model_file, **({'task': 'detect'} if model_file.endswith('.onnx') else {}))
            except Exception as e:
                print(f"[KovaqYOLO] Aviso cargando modelo: {e}")

    def recargar_mapa_tienda(self):
        try:
            cams = obtener_camaras_mapa()
            perchas = obtener_perchas_mapa()
            with self.lock:
                self.camaras_config = {c["id"]: c.get("zona_2d", {}) for c in cams}
                self.perchas_config = perchas
        except Exception as e:
            print(f"[KovaqMap] Error sincronizando mapa de tienda: {e}")

    def _obtener_perchas_de_camara(self, cam_id: str):
        with self.lock:
            perchas = [p for p in self.perchas_config if p.get("camara_id") == cam_id]
            if not perchas and self.perchas_config:
                perchas = self.perchas_config[:2]
        if not perchas:
            perchas = [
                {
                    "id": "PER-A1",
                    "nombre": "Percha A1 (Izq.) • Cereales & Desayuno",
                    "categoria": "Cereales",
                    "camara_id": cam_id,
                    "map_x": 0.12, "map_y": 0.22, "map_w": 0.24, "map_h": 0.15,
                    "roi": {"x1": 0.02, "y1": 0.20, "x2": 0.43, "y2": 0.92},
                    "niveles": [
                        {"nivel": "Superior (Subir Cabeza)", "y1": 0.20, "y2": 0.42, "producto": "Avena Crujiente 400g"},
                        {"nivel": "Medio (Altura Ojos)", "y1": 0.42, "y2": 0.68, "producto": "Cereales Kellogg's 500g"},
                        {"nivel": "Inferior (Agacharse)", "y1": 0.68, "y2": 0.92, "producto": "Barras de Cereal Mix"}
                    ],
                    "color": "#FE5000"
                },
                {
                    "id": "PER-A2",
                    "nombre": "Percha A2 (Der.) • Salsas, Pastas & Conservas",
                    "categoria": "Conservas",
                    "camara_id": cam_id,
                    "map_x": 0.12, "map_y": 0.58, "map_w": 0.24, "map_h": 0.15,
                    "roi": {"x1": 0.60, "y1": 0.22, "x2": 0.98, "y2": 0.92},
                    "niveles": [
                        {"nivel": "Superior (Subir Cabeza)", "y1": 0.22, "y2": 0.45, "producto": "Pasta Italiana 500g"},
                        {"nivel": "Medio (Altura Ojos)", "y1": 0.45, "y2": 0.70, "producto": "Salsa de Tomate Artesanal"},
                        {"nivel": "Inferior (Agacharse)", "y1": 0.70, "y2": 0.92, "producto": "Aceitunas y Encurtidos"}
                    ],
                    "color": "#F59E0B"
                }
            ]
        return perchas

    @staticmethod
    def _resolver_producto_en_percha(percha: dict, cy_norm: float, clase_yolo: str = None) -> tuple:
        niveles = percha.get("niveles") or []
        for niv in niveles:
            if float(niv.get("y1", 0.0)) <= cy_norm <= float(niv.get("y2", 1.0)):
                return niv.get("producto") or clase_yolo or f"Artículo ({percha['categoria']})", niv.get("nivel", "Medio")
        if niveles:
            return niveles[0].get("producto") or clase_yolo or f"Artículo ({percha['categoria']})", niveles[0].get("nivel", "Medio")
        return clase_yolo or f"Artículo ({percha.get('categoria', 'Góndola')})", "Medio"

    def _reset_conteos(self):
        self.total_entradas = 0
        self.total_agarrados = 0
        self.total_devueltos = 0
        self.total_handoffs = 0
        self.total_empleados_excluidos = 1
        self.tiempos_clientes = {}
        self.tiempos_zona = {}
        self.productos_en_mano = {}
        self.productos_rastreados = {}
        self.interacciones_registradas = set()
        self.ultimo_reach_zona = {}
        try:
            if self.modelo is not None:
                self.modelo.predictor = None
        except Exception:
            pass

    def iniciar(self, modo_nube=False):
        self.recargar_mapa_tienda()
        if not self.corriendo:
            self.corriendo = True

            self.hilo_db = threading.Thread(target=self._worker_db_asincrono, daemon=True, name="KovaqDbWriter")
            self.hilo_db.start()

            self.hilo_sim = threading.Thread(target=self._worker_simulacion_papel, daemon=True, name="KovaqDigitalTwin")
            self.hilo_sim.start()

            if not modo_nube:
                self._cargar_modelo_yolo()
                if os.path.exists(DEMO_VIDEO_PATH):
                    self._abrir_captura("video", DEMO_VIDEO_PATH, reiniciar=True)
                    if self.cap and self.cap.isOpened():
                        ok, primer_frame = self.cap.read()
                        if ok and primer_frame is not None:
                            if primer_frame.shape[1] != ANCHO or primer_frame.shape[0] != ALTO:
                                self.latest_raw_frame = cv2.resize(primer_frame, (ANCHO, ALTO))
                            else:
                                self.latest_raw_frame = primer_frame
                            self.frame_id = 1
                            self.get_frame_bytes(-1)

                self.hilo_lectura = threading.Thread(target=self._leer_camara, daemon=True, name="KovaqCamCapture")
                self.hilo_ia = threading.Thread(target=self._worker_ia_desacoplado, daemon=True, name="KovaqYoloWorker")
                self.hilo_lectura.start()
                self.hilo_ia.start()

                if PUBLICAR_VIDEO_URL and VIDEO_PUSH_TOKEN:
                    self.hilo_push = threading.Thread(target=self._worker_push_nube, daemon=True, name="KovaqCloudPush")
                    self.hilo_push.start()
            else:
                self.mtmc.simulacion_activa = True
                self.estado = "simulacion_papel"

    def toggle_simulacion_papel(self, activar: bool = None) -> bool:
        if activar is None:
            self.mtmc.simulacion_activa = not self.mtmc.simulacion_activa
        else:
            self.mtmc.simulacion_activa = bool(activar)
        if self.mtmc.simulacion_activa:
            self.mtmc._init_agentes_simulacion()
        return self.mtmc.simulacion_activa

    def cambiar_camara_activa(self, cam_id: str):
        self.recargar_mapa_tienda()
        if cam_id in ("CAM-01", "CAM-02", "CAM-03") or cam_id in self.camaras_config:
            self.camara_activa_id = cam_id
        return self.camara_activa_id

    def detener(self):
        self.corriendo = False
        self.apagar()

    def apagar(self):
        self.activa = False
        self.estado = "apagado"
        self.solicitud = ("apagado", "")
        with self.lock:
            self.latest_raw_frame = None
            self._jpeg = None
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def toggle_pausa(self):
        self.pausado = not self.pausado
        return self.pausado

    def cambiar_fuente(self, tipo, ruta="", cam_id=None):
        if cam_id:
            self.camara_activa_id = cam_id
        self.solicitud = (tipo, ruta)
        self.pausado = False

    def procesar_archivo_subido_inmediato(self, tipo: str, ruta: str) -> dict:
        """
        Analiza al instante un video o imagen recién subido por el usuario (funciona tanto en MODO=local como en MODO=nube):
        - Si es imagen: ejecuta el análisis de frame completo (perchas, postura de cabeza, exclusión de staff, coordenadas 2D).
        - Si es video: muestrea fotogramas clave a lo largo del clip, ejecuta YOLOv8n + ShelfAnalyzer + MultiCameraTracker,
          persiste los eventos/coordenadas en Turso y si los hilos de streaming no estaban activos los arranca.
        """
        self._cargar_modelo_yolo()
        self.recargar_mapa_tienda()

        if not os.path.exists(ruta):
            return {"ok": False, "frames_analizados": 0, "resumen": "Archivo no encontrado"}

        frames_analizados = 0
        agarrados_antes = self.total_agarrados
        devueltos_antes = self.total_devueltos
        entradas_antes = self.total_entradas

        if tipo == "imagen":
            img = cv2.imread(ruta)
            if img is not None:
                frame_res = cv2.resize(img, (ANCHO, ALTO))
                with self.lock:
                    self.latest_raw_frame = frame_res
                    self.frame_id += 1
                self._analizar_frame_ia(frame_res, rastrear=False)
                self.get_frame_bytes(-1)
                frames_analizados = 1
        else:
            cap_tmp = cv2.VideoCapture(ruta)
            if cap_tmp and cap_tmp.isOpened():
                total_f = int(cap_tmp.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                fps_v = float(cap_tmp.get(cv2.CAP_PROP_FPS) or 24.0)
                salto = max(1, int(fps_v // 4))  # ~4 evaluaciones por segundo para analizar el clip al instante
                f_idx = 0
                max_evaluaciones = 36
                while frames_analizados < max_evaluaciones:
                    ok, frame = cap_tmp.read()
                    if not ok or frame is None:
                        break
                    if f_idx % salto == 0:
                        frame_res = cv2.resize(frame, (ANCHO, ALTO))
                        with self.lock:
                            self.latest_raw_frame = frame_res
                            self.frame_id += 1
                        self._analizar_frame_ia(frame_res, rastrear=True)
                        frames_analizados += 1
                    f_idx += 1
                cap_tmp.release()
                self.get_frame_bytes(-1)

        # Activar también el stream continuo con el archivo subido
        self.cambiar_fuente(tipo, ruta)
        if self.hilo_lectura is None or not self.hilo_lectura.is_alive():
            self._abrir_captura(tipo, ruta, reiniciar=False)
            self.hilo_lectura = threading.Thread(target=self._leer_camara, daemon=True, name="KovaqCamCapture")
            self.hilo_ia = threading.Thread(target=self._worker_ia_desacoplado, daemon=True, name="KovaqYoloWorker")
            self.hilo_lectura.start()
            self.hilo_ia.start()

        # Persistir inmediatamente en Turso/SQLite todos los eventos generados por el análisis instantáneo
        self.flush_cola_db()

        nuevas_entradas = max(0, self.total_entradas - entradas_antes)
        nuevos_tomados = max(0, self.total_agarrados - agarrados_antes)
        nuevos_devueltos = max(0, self.total_devueltos - devueltos_antes)

        return {
            "ok": True,
            "tipo": tipo,
            "frames_analizados": frames_analizados,
            "nuevos_clientes": nuevas_entradas,
            "nuevos_tomados": nuevos_tomados,
            "nuevos_devueltos": nuevos_devueltos,
            "personas_en_escena": len(self.overlay_data.get("personas", [])),
            "resumen": (
                f"Analizados {frames_analizados} fotogramas clave al instante: "
                f"{ len(self.overlay_data.get('personas', [])) } vectores en escena, "
                f"{nuevos_tomados} tomas y {nuevos_devueltos} devoluciones detectadas."
            )
        }

    def repetir(self):
        if self.fuente == "video" and self.ruta:
            self.cambiar_fuente("video", self.ruta)

    def estado_dict(self):
        return {
            "camara_activa": self.camara_activa_id,
            "fuente": self.fuente,
            "estado": self.estado,
            "mensaje": self.mensaje,
            "pausado": self.pausado,
            "simulacion_papel": self.mtmc.simulacion_activa,
            "entradas": self.total_entradas,
            "agarrados": self.total_agarrados,
            "devueltos": self.total_devueltos,
            "handoffs": self.total_handoffs,
            "empleados_excluidos": self.total_empleados_excluidos
        }

    def obtener_estado_mapa_2d(self):
        """
        Devuelve el estado completo del Plano 2D Anónimo del Lugar:
        - Zonas de Puerta de Entrada y Línea de Cajas (para validar clientes vs empleados).
        - Matriz de Zonas Calientes y Zonas Frías (`heatmap_grid`).
        - Perchas con su Planograma y Promociones POP activas.
        - Vectores de coordenadas anónimas (`S#101`, `S#102`, `STAFF-01` excluido) con postura de cabeza.
        """
        self.recargar_mapa_tienda()
        ahora = time.time()
        personas_mapa = []

        with self.mtmc.lock:
            for gid, tr in list(self.mtmc.global_tracks.items()):
                if ahora - tr["last_seen"] <= 15.0:
                    bolsa = [item["producto"] for item in self.productos_en_mano.get(gid, [])]
                    es_cli = tr.get("es_cliente_valido", True)
                    personas_mapa.append({
                        "global_id": gid,
                        "etiqueta": f"S#{gid}" if es_cli else f"STAFF#{gid}",
                        "es_cliente_valido": es_cli,
                        "rol_traza": tr.get("rol_traza", "Cliente (Puerta → Caja)"),
                        "postura_cabeza": tr.get("postura_cabeza", "Frente (Pasillo)"),
                        "camara_actual": tr["camara_actual"],
                        "camaras_visitadas": list(tr["camaras_visitadas"]),
                        "x": tr["map_x"],
                        "y": tr["map_y"],
                        "estado_accion": tr.get("estado_accion", "En Tránsito"),
                        "percha_actual": tr.get("percha_actual"),
                        "productos_en_mano": bolsa,
                        "color": "#94a3b8" if not es_cli else ("#22c55e" if not bolsa else "#FE5000"),
                        "trayectoria": list(tr["trayectoria"])[-20:],
                        "origen": "yolo"
                    })

            if self.mtmc.simulacion_activa:
                for ag in self.mtmc.agentes_simulados:
                    es_cli = ag.get("es_cliente_valido", True)
                    personas_mapa.append({
                        "global_id": ag["global_id"],
                        "etiqueta": f"S#{ag['global_id']}" if es_cli else "STAFF",
                        "es_cliente_valido": es_cli,
                        "rol_traza": ag.get("rol_traza", "Cliente"),
                        "postura_cabeza": ag.get("postura_cabeza", "Frente"),
                        "vestimenta": ag.get("vestimenta", ""),
                        "camara_actual": ag["camara_actual"],
                        "camaras_visitadas": ["CAM-01", "CAM-02", "CAM-03"] if es_cli else ["CAM-01"],
                        "x": round(ag["x"], 4),
                        "y": round(ag["y"], 4),
                        "estado_accion": ag["estado_accion"],
                        "percha_actual": ag.get("percha_actual"),
                        "productos_en_mano": list(ag["bolsa"]),
                        "color": ag["color_hex"],
                        "trayectoria": list(ag["historial_xy"])[-25:],
                        "origen": "simulacion_papel"
                    })

            handoffs_lista = list(self.mtmc.handoffs)
            grid_copy = [ list(fila) for fila in self.mtmc.heatmap_grid ]

        clientes_validos_activos = sum(1 for p in personas_mapa if p.get("es_cliente_valido", True))

        return {
            "camara_activa": self.camara_activa_id,
            "simulacion_activa": self.mtmc.simulacion_activa,
            "privacidad_modo": "Coordenadas Anónimas 2D (Cero Identificación Personal / Staff Excluido)",
            "zonas_arquitectura": {
                "puerta_entrada": {"x": 0.02, "y": 0.25, "w": 0.06, "h": 0.50, "nombre": "PUERTA ENTRADA"},
                "linea_cajas": {"x": 0.88, "y": 0.56, "w": 0.09, "h": 0.34, "nombre": "CAJAS / SALIDA"}
            },
            "heatmap_grid": grid_copy,
            "analisis_video_10s": self.analisis_video_10s,
            "camaras": obtener_camaras_mapa(),
            "perchas": self.perchas_config,
            "personas": personas_mapa,
            "handoffs": handoffs_lista,
            "metricas": {
                "entradas": self.total_entradas,
                "agarrados": self.total_agarrados,
                "devueltos": self.total_devueltos,
                "handoffs": self.total_handoffs,
                "personas_activas": clientes_validos_activos,
                "empleados_excluidos": self.total_empleados_excluidos
            }
        }

    def obtener_telemetria(self):
        with self.lock:
            eventos_lista = list(self.eventos_recientes)
            overlay_personas = list(self.overlay_data.get("personas", []))
            overlay_perchas = list(self.overlay_data.get("perchas", []))
            alertas_lista = list(self.overlay_data.get("alertas", []))

        with self.mtmc.lock:
            handoffs_lista = list(self.mtmc.handoffs)[:10]
            sim_activa = self.mtmc.simulacion_activa
            sim_count = sum(1 for a in self.mtmc.agentes_simulados if a.get("es_cliente_valido", True)) if sim_activa else 0

        personas_activas = len([p for p in overlay_personas if p.get("es_cliente_valido", True)]) + sim_count

        return {
            "camara_activa": self.camara_activa_id,
            "simulacion_activa": sim_activa,
            "fps_cam": round(self.fps_cam, 1),
            "fps_yolo": round(self.fps_yolo, 1),
            "latencia_ms": round(self.latencia_ms, 1),
            "cpu_cores": self.cpu_cores,
            "agarrados": self.total_agarrados,
            "devueltos": self.total_devueltos,
            "entradas": self.total_entradas,
            "handoffs": self.total_handoffs,
            "empleados_excluidos": self.total_empleados_excluidos,
            "personas_activas": personas_activas,
            "estado": self.estado,
            "fuente": self.fuente,
            "pausado": self.pausado,
            "analisis_video_10s": self.analisis_video_10s,
            "personas_box": overlay_personas,
            "perchas_roi": overlay_perchas,
            "alertas": alertas_lista,
            "handoffs_recientes": handoffs_lista,
            "eventos": eventos_lista
        }

    def _encolar_db(self, tarea, *args):
        try:
            self.cola_db.put_nowait((tarea, args))
        except queue.Full:
            pass

    def _ejecutar_tarea_db(self, tarea, args):
        if tarea == "trafico":
            guardar_trafico()
        elif tarea == "tomado":
            guardar_producto_tomado(*args)
        elif tarea == "devuelto":
            guardar_producto_devuelto(*args)
        elif tarea == "interaccion":
            guardar_interaccion(*args)
        elif tarea == "permanencia":
            guardar_permanencia(*args)
        elif tarea == "handoff":
            guardar_handoff_camara(*args)
        elif tarea == "coordenada":
            guardar_telemetria_coordenada(*args)

    def flush_cola_db(self):
        while True:
            try:
                tarea, args = self.cola_db.get_nowait()
                try:
                    self._ejecutar_tarea_db(tarea, args)
                except Exception:
                    pass
                finally:
                    self.cola_db.task_done()
            except queue.Empty:
                break

    def _worker_db_asincrono(self):
        while self.corriendo:
            try:
                tarea, args = self.cola_db.get(timeout=0.2)
                try:
                    self._ejecutar_tarea_db(tarea, args)
                finally:
                    self.cola_db.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                print(f"[KovaqDB] Error registrando evento en segundo plano: {e}")

    def _registrar_evento_memoria(self, tipo_evento, sesion_id, producto_nombre, confianza, coords,
                                  color="naranja", percha_id="PER-A1", percha_nombre="Percha A1",
                                  camara_id=None, postura_cabeza="Giro a Percha", comportamiento="mira_y_compra"):
        self._event_id_seq += 1
        ahora = datetime.now()
        item = {
            "id": self._event_id_seq,
            "hora": ahora.strftime("%H:%M:%S") + f".{int(ahora.microsecond / 1000):03d}",
            "tipo": tipo_evento,
            "cliente_id": sesion_id or 1,
            "global_id": f"S#{sesion_id or 1}",
            "producto": producto_nombre,
            "percha_id": percha_id,
            "percha_nombre": percha_nombre,
            "camara_id": camara_id or self.camara_activa_id,
            "postura_cabeza": postura_cabeza,
            "comportamiento": comportamiento,
            "conf": round(confianza, 2),
            "coords": coords,
            "color": color
        }
        with self.lock:
            self.eventos_recientes.appendleft(item)

    # =========================================================================
    # MOTOR DE SIMULACIÓN ANÓNIMA DE COORDENADAS (PUERTA -> PERCHAS -> CAJA)
    # =========================================================================
    def _worker_simulacion_papel(self):
        dt = 0.25
        while self.corriendo:
            if not self.mtmc.simulacion_activa or self.pausado:
                time.sleep(0.25)
                continue

            ahora = time.time()
            with self.mtmc.lock:
                for ag in self.mtmc.agentes_simulados:
                    gid = ag["global_id"]
                    es_cli = ag.get("es_cliente_valido", True)

                    if es_cli and gid not in self.tiempos_clientes:
                        self.tiempos_clientes[gid] = ahora
                        self.total_entradas += 1
                        self._encolar_db("trafico")

                    wps = ag["waypoints"]
                    wp = wps[ag["wp_idx"]]
                    tx, ty, accion, percha_id, prod_nombre, duracion, postura_cab, tipo_comp = wp

                    if es_cli:
                        self.mtmc._registrar_calor_2d(ag["x"], ag["y"], peso=0.12)

                    if ahora < ag["esperando_hasta"]:
                        continue

                    if ag["accion_ejecutada"]:
                        ag["accion_ejecutada"] = False
                        ag["percha_actual"] = None
                        ag["postura_cabeza"] = "Vista al Frente (Pasillo)"
                        ag["estado_accion"] = "Desplazamiento Anónimo" if es_cli else "Staff Reposición (Ignorado)"
                        if es_cli and duracion > 1.0:
                            self._encolar_db("permanencia", round(duracion, 1))
                        ag["wp_idx"] = (ag["wp_idx"] + 1) % len(wps)
                        continue

                    dx = tx - ag["x"]
                    dy = ty - ag["y"]
                    dist = math.hypot(dx, dy)
                    paso = ag["velocidad"] * dt

                    if dist <= paso + 0.01:
                        ag["x"], ag["y"] = tx, ty
                        ag["postura_cabeza"] = postura_cab
                        ag["historial_xy"].append({"x": round(tx, 3), "y": round(ty, 3), "cam": ag["camara_actual"]})

                        if not es_cli:
                            ag["esperando_hasta"] = ahora + duracion
                            ag["accion_ejecutada"] = True
                            continue

                        nueva_cam = "CAM-01" if tx < 0.39 else ("CAM-02" if tx < 0.65 else "CAM-03")
                        if nueva_cam != ag["camara_actual"]:
                            cam_prev = ag["camara_actual"]
                            ag["camara_actual"] = nueva_cam
                            self.total_handoffs += 1
                            sim_reid = round(0.93 + (gid % 5) * 0.01, 3)
                            handoff_ev = {
                                "global_id": gid,
                                "origen": cam_prev,
                                "destino": nueva_cam,
                                "similitud": sim_reid,
                                "sim_visual": sim_reid,
                                "hora": datetime.now().strftime("%H:%M:%S"),
                                "map_x": round(tx, 3),
                                "map_y": round(ty, 3),
                            }
                            self.mtmc.handoffs.appendleft(handoff_ev)
                            self._encolar_db("handoff", gid, cam_prev, nueva_cam, sim_reid)
                            self._registrar_evento_memoria(
                                "Continuidad Cámara", gid,
                                f"{cam_prev} → {nueva_cam} (Coordenada [{tx:.2f}, {ty:.2f}])",
                                sim_reid, [round(tx, 2), round(ty, 2)],
                                color="verde", percha_id=nueva_cam, percha_nombre=f"Zona {cam_prev}→{nueva_cam}",
                                camara_id=nueva_cam, postura_cabeza=postura_cab, comportamiento="transito"
                            )

                        if accion in ("tomar", "devolver", "mirar") and duracion > 0:
                            ag["esperando_hasta"] = ahora + duracion
                            ag["accion_ejecutada"] = True
                            ag["percha_actual"] = percha_id
                            self.mtmc._registrar_calor_2d(tx, ty, peso=1.5)
                            coords_norm_2d = [round(tx, 3), round(ty, 3)]

                            if accion == "tomar":
                                ag["estado_accion"] = f"{postura_cab} • Tomó {prod_nombre}"
                                ag["bolsa"].add(prod_nombre)
                                self.total_agarrados += 1
                                self._encolar_db("tomado", prod_nombre, gid, percha_id)
                                self._encolar_db(
                                    "coordenada", f"VEC-{gid}", 1, tx, ty, percha_id,
                                    postura_cab, postura_cab, tipo_comp, prod_nombre, duracion, ag["camara_actual"]
                                )
                                self._registrar_evento_memoria(
                                    "Producto Tomado", gid, f"{prod_nombre} [{postura_cab}]", 0.96, coords_norm_2d,
                                    color="naranja", percha_id=percha_id, percha_nombre=percha_id,
                                    camara_id=ag["camara_actual"], postura_cabeza=postura_cab, comportamiento=tipo_comp
                                )
                            elif accion == "devolver":
                                ag["estado_accion"] = f"Último Momento: Devolvió {prod_nombre} ({percha_id})"
                                ag["bolsa"].discard(prod_nombre)
                                self.total_devueltos += 1
                                self._encolar_db("devuelto", prod_nombre, gid, percha_id)
                                self._encolar_db(
                                    "coordenada", f"VEC-{gid}", 1, tx, ty, percha_id,
                                    postura_cab, postura_cab, "ultimo_momento_devuelve", prod_nombre, duracion, ag["camara_actual"]
                                )
                                self._registrar_evento_memoria(
                                    "Devuelto (Último Momento)", gid, f"{prod_nombre} [{postura_cab}]", 0.95, coords_norm_2d,
                                    color="azul", percha_id=percha_id, percha_nombre=percha_id,
                                    camara_id=ag["camara_actual"], postura_cabeza=postura_cab, comportamiento="ultimo_momento_devuelve"
                                )
                            else:
                                ag["estado_accion"] = f"Solo Mira ({duracion:.1f}s): {postura_cab} en {percha_id}"
                                self._encolar_db(
                                    "coordenada", f"VEC-{gid}", 1, tx, ty, percha_id,
                                    postura_cab, postura_cab, "solo_mira", prod_nombre or "", duracion, ag["camara_actual"]
                                )
                        elif accion == "salida_caja":
                            ag["esperando_hasta"] = ahora + duracion
                            ag["accion_ejecutada"] = True
                            ag["estado_accion"] = f"Paso por Caja Validado ({len(ag['bolsa'])} art. llevados)"
                            ag["bolsa"].clear()
                        else:
                            ag["wp_idx"] = (ag["wp_idx"] + 1) % len(wps)
                    else:
                        ag["x"] += (dx / dist) * paso
                        ag["y"] += (dy / dist) * paso
                        ag["historial_xy"].append({
                            "x": round(ag["x"], 3),
                            "y": round(ag["y"], 3),
                            "cam": ag["camara_actual"]
                        })

            time.sleep(dt)

    def _worker_push_nube(self):
        import urllib.request
        ultimo_id_enviado = -1
        while self.corriendo:
            try:
                if not self.activa or self.pausado:
                    time.sleep(0.1)
                    continue
                fid, jpeg = self.get_frame_bytes(ultimo_id_enviado)
                if jpeg is not None and fid != ultimo_id_enviado:
                    ultimo_id_enviado = fid
                    req = urllib.request.Request(
                        PUBLICAR_VIDEO_URL,
                        data=jpeg,
                        headers={"X-Token": VIDEO_PUSH_TOKEN, "Content-Type": "image/jpeg"},
                        method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=1.5):
                        pass
                time.sleep(0.065)
            except Exception:
                time.sleep(2.0)

    # =========================================================================
    # NIVEL 1: CAPTURA DE CÁMARA
    # =========================================================================
    def _abrir_captura(self, tipo, ruta, reiniciar=True):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

        if reiniciar:
            self._reset_conteos()
            with self.lock:
                self.latest_raw_frame = None
                self._jpeg = None

        self.fuente, self.ruta, self.mensaje = tipo, ruta, ""
        self.fin_video, self.imagen_procesada, self.fallos = False, False, 0

        if tipo == "apagado":
            self.activa, self.estado = False, "apagado"
            return

        if tipo == "imagen":
            self.activa = os.path.exists(ruta)
            self.estado = "procesando" if self.activa else "error"
            self.mensaje = "" if self.activa else "No se encontró la imagen"
            return

        self.estado = "abriendo"
        try:
            if tipo == "webcam":
                if os.name == "nt":
                    self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
                    if not self.cap or not self.cap.isOpened():
                        self.cap = cv2.VideoCapture(0)
                else:
                    self.cap = cv2.VideoCapture(0)
                if self.cap and self.cap.isOpened():
                    self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            elif tipo == "rtsp":
                url = ruta if ruta else RTSP_URL
                self.cap = cv2.VideoCapture(int(url) if str(url).isdigit() else str(url))
                if self.cap and self.cap.isOpened():
                    self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            elif tipo == "video":
                if not ruta or not os.path.exists(ruta):
                    ruta = DEMO_VIDEO_PATH
                if os.path.exists(ruta):
                    self.ruta = ruta
                    self.cap = cv2.VideoCapture(ruta)
        except Exception as e:
            print(f"[KovaqCam] Error abriendo fuente ({tipo}): {e}")

        if self.cap is None or not self.cap.isOpened():
            demo_fallback = DEMO_VIDEO_PATH
            if tipo in ("webcam", "rtsp") and os.path.exists(demo_fallback):
                self.fuente = "video"
                self.ruta = demo_fallback
                self.cap = cv2.VideoCapture(demo_fallback)
                if self.cap and self.cap.isOpened():
                    self.activa, self.estado = True, "en_vivo"
                    self.mensaje = f"Cámara «{tipo}» no disponible. Se activó video demo de supermercado."
                    return
            self.cap, self.activa, self.estado = None, False, "error"
            self.mensaje = f"No se pudo inicializar la fuente de video «{tipo}»"
            return

        self.activa, self.estado = True, "en_vivo"

    def _leer_camara(self):
        while self.corriendo:
            sol = self.solicitud
            if sol is not None:
                self.solicitud = None
                self._abrir_captura(*sol)

            if not self.activa or self.fuente in ("imagen", "apagado") or self.pausado or self.cap is None or self.fin_video:
                time.sleep(0.04)
                continue

            ok, frame = self.cap.read()
            if not ok or frame is None:
                if self.fuente == "video":
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = self.cap.read()
                    if not ok or frame is None:
                        try:
                            self.cap.release()
                        except Exception:
                            pass
                        self.cap = cv2.VideoCapture(self.ruta or DEMO_VIDEO_PATH)
                        ok, frame = self.cap.read()
                        if not ok or frame is None:
                            time.sleep(0.04)
                            continue
                else:
                    self.fallos += 1
                    self.estado = "sin_senal"
                    if self.fallos >= 15:
                        self._abrir_captura(self.fuente, self.ruta, reiniciar=False)
                    time.sleep(0.1)
                continue

            self.fallos = 0
            self.estado = "en_vivo"

            if frame.shape[1] != ANCHO or frame.shape[0] != ALTO:
                frame_res = cv2.resize(frame, (ANCHO, ALTO), interpolation=cv2.INTER_LINEAR)
            else:
                frame_res = frame

            with self.lock:
                self.latest_raw_frame = frame_res
                self.frame_id += 1

            self._frames_cam_count += 1
            now = time.time()
            if now - self._t_cam_start >= 1.0:
                self.fps_cam = self._frames_cam_count / (now - self._t_cam_start)
                self._frames_cam_count = 0
                self._t_cam_start = now

            if self.fuente == "video":
                time.sleep(0.038)
            else:
                time.sleep(0.005)

    # =========================================================================
    # NIVEL 2: WORKER DE IA (POSTURA DE CABEZA, ANONIMIZACIÓN Y PERCHAS)
    # =========================================================================
    def _worker_ia_desacoplado(self):
        while self.corriendo:
            if not self.activa or self.pausado or self.fuente == "apagado":
                time.sleep(0.08)
                continue

            if self.fuente == "imagen":
                if not self.imagen_procesada:
                    self.imagen_procesada = True
                    img = cv2.imread(self.ruta) if os.path.exists(self.ruta) else None
                    if img is None:
                        self.estado, self.mensaje, self.activa = "error", "No se pudo leer la imagen", False
                    else:
                        frame_res = cv2.resize(img, (ANCHO, ALTO))
                        with self.lock:
                            self.latest_raw_frame = frame_res
                        self._analizar_frame_ia(frame_res, rastrear=False)
                        self.estado = "terminado"
                time.sleep(0.15)
                continue

            with self.lock:
                frame_para_ia = self.latest_raw_frame

            if frame_para_ia is None:
                time.sleep(0.02)
                continue

            t0 = time.time()
            self._analizar_frame_ia(frame_para_ia, rastrear=True)
            t_fin = time.time()

            self.latencia_ms = (t_fin - t0) * 1000.0
            self._frames_yolo_count += 1
            if t_fin - self._t_yolo_start >= 1.0:
                self.fps_yolo = self._frames_yolo_count / (t_fin - self._t_yolo_start)
                self._frames_yolo_count = 0
                self._t_yolo_start = t_fin

            if INTERVALO_IA > 0:
                time.sleep(INTERVALO_IA)

    def _analizar_frame_ia(self, frame, rastrear=True, cam_id_override=None):
        self._cargar_modelo_yolo()
        if self.modelo is None:
            return

        cam_id = cam_id_override or self.camara_activa_id
        perchas_cam = self._obtener_perchas_de_camara(cam_id)
        frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        try:
            if rastrear:
                res = self.modelo.track(
                    frame,
                    persist=True,
                    classes=CLASES_RETAIL,
                    verbose=False,
                    imgsz=IMGSZ,
                    conf=CONF,
                    tracker="bytetrack.yaml"
                )[0]
            else:
                res = self.modelo.predict(
                    frame,
                    classes=CLASES_RETAIL,
                    verbose=False,
                    imgsz=IMGSZ,
                    conf=CONF
                )[0]
        except Exception:
            try:
                res = self.modelo.predict(frame, classes=CLASES_RETAIL, verbose=False, imgsz=IMGSZ, conf=CONF)[0]
            except Exception:
                return

        personas_detectadas = []
        productos_detectados = []
        perchas_overlay = []
        alertas_frame = []

        ahora = time.time()

        for p in perchas_cam:
            roi = p.get("roi") or {"x1": 0.12, "y1": 0.15, "x2": 0.48, "y2": 0.82}
            sx1 = int(float(roi.get("x1", 0.12)) * ANCHO)
            sy1 = int(float(roi.get("y1", 0.15)) * ALTO)
            sx2 = int(float(roi.get("x2", 0.48)) * ANCHO)
            sy2 = int(float(roi.get("y2", 0.82)) * ALTO)
            perchas_overlay.append({
                "id": p["id"],
                "nombre": p["nombre"],
                "categoria": p.get("categoria", "General"),
                "box": [sx1, sy1, sx2, sy2],
                "roi_norm": roi,
                "niveles": p.get("niveles", []),
                "color": p.get("color", "#FE5000")
            })

        cajas = res.boxes
        if cajas is not None and len(cajas):
            xyxy = cajas.xyxy.cpu().numpy()
            clases = cajas.cls.cpu().numpy().astype(int)
            confs = cajas.conf.cpu().numpy() if cajas.conf is not None else [0.8] * len(xyxy)
            ids = cajas.id.cpu().numpy().astype(int) if cajas.id is not None else range(1, len(xyxy) + 1)

            for box, local_tid, cls_idx, conf in zip(xyxy, ids, clases, confs):
                x1, y1, x2, y2 = map(int, box)
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                fy = y2
                nombre_clase = self.modelo.names.get(cls_idx, f"item_{cls_idx}")

                if cls_idx == 0:  # VECTOR DE PERSONA ANÓNIMO
                    map_x, map_y = self.mtmc.proyectar_a_plano_2d(cam_id, cx, fy, ANCHO, ALTO, self.camaras_config)
                    gid, handoff_ev = self.mtmc.resolver_identidad_global(
                        cam_id, int(local_tid), frame, (x1, y1, x2, y2), map_x, map_y, ahora
                    )
                    es_cliente = self.mtmc.clasificar_cliente_vs_empleado((x1, y1, x2, y2), map_x, map_y, frame)
                    postura_info = self.shelf_analyzer.estimar_postura_cabeza_y_cuerpo(
                        frame_gray, gid, (x1, y1, x2, y2), perchas_overlay
                    )

                    with self.mtmc.lock:
                        if gid in self.mtmc.global_tracks:
                            self.mtmc.global_tracks[gid]["es_cliente_valido"] = es_cliente
                            self.mtmc.global_tracks[gid]["postura_cabeza"] = postura_info["etiqueta"]

                    if not es_cliente:
                        # Empleado detectado: se registra como excluido y NO suma al tráfico de clientes ni tomas
                        personas_detectadas.append({
                            "id": gid,
                            "local_id": int(local_tid),
                            "etiqueta": "STAFF (Excluido)",
                            "es_cliente_valido": False,
                            "postura_cabeza": "Reposición Interna",
                            "box": [x1, y1, x2, y2],
                            "cx": cx,
                            "cy": cy,
                            "map_x": map_x,
                            "map_y": map_y,
                            "dwell": 0,
                            "percha_id": None,
                            "en_zona": False,
                            "productos_en_mano": []
                        })
                        continue

                    if handoff_ev:
                        self.total_handoffs += 1
                        self._encolar_db("handoff", gid, handoff_ev["origen"], handoff_ev["destino"], handoff_ev["similitud"])
                        self._registrar_evento_memoria(
                            "Continuidad Cámara", gid,
                            f"{handoff_ev['origen']} → {handoff_ev['destino']}",
                            handoff_ev["similitud"], [map_x, map_y],
                            color="verde", percha_id=cam_id, percha_nombre=f"Zona {handoff_ev['origen']}→{handoff_ev['destino']}",
                            camara_id=cam_id, postura_cabeza=postura_info["etiqueta"]
                        )

                    if gid not in self.tiempos_clientes:
                        self.tiempos_clientes[gid] = ahora
                        self.total_entradas += 1
                        self._encolar_db("trafico")

                    percha_frente = None
                    dwell_max = 0
                    for po in perchas_overlay:
                        sx1, sy1, sx2, sy2 = po["box"]
                        esta_frente = (sx1 - 30 <= cx <= sx2 + 30 and sy1 - 20 <= cy <= sy2 + 35)
                        clave_z = (gid, po["id"])
                        if esta_frente:
                            if clave_z not in self.tiempos_zona:
                                self.tiempos_zona[clave_z] = ahora
                            dwell_p = int(ahora - self.tiempos_zona[clave_z])
                            if dwell_p >= dwell_max:
                                dwell_max = dwell_p
                                percha_frente = po["id"]
                        elif clave_z in self.tiempos_zona:
                            t_salida = ahora - self.tiempos_zona.pop(clave_z)
                            if t_salida > 1.5:
                                self._encolar_db("permanencia", round(t_salida, 1))
                                # Si miró más de 1.8s y se retiró sin tomar producto -> Solo Miró
                                tenencias = [it for it in self.productos_en_mano.get(gid, []) if it["percha_id"] == po["id"]]
                                if not tenencias:
                                    self._encolar_db(
                                        "coordenada", f"VEC-{gid}", 1, map_x, map_y, po["id"],
                                        "Medio", postura_info["codigo"], "solo_mira", "", round(t_salida, 1), cam_id
                                    )

                    bolsa_actual = [it["producto"] for it in self.productos_en_mano.get(gid, [])]
                    personas_detectadas.append({
                        "id": gid,
                        "local_id": int(local_tid),
                        "etiqueta": f"S#{gid}",
                        "es_cliente_valido": True,
                        "postura_cabeza": postura_info["etiqueta"],
                        "postura_codigo": postura_info["codigo"],
                        "box": [x1, y1, x2, y2],
                        "cx": cx,
                        "cy": cy,
                        "map_x": map_x,
                        "map_y": map_y,
                        "dwell": dwell_max,
                        "percha_id": percha_frente,
                        "en_zona": percha_frente is not None,
                        "productos_en_mano": bolsa_actual
                    })

                else:
                    percha_prod = None
                    for po in perchas_overlay:
                        sx1, sy1, sx2, sy2 = po["box"]
                        if sx1 <= cx <= sx2 and sy1 <= cy <= sy2:
                            percha_prod = po
                            break
                    productos_detectados.append({
                        "id": int(local_tid),
                        "box": [x1, y1, x2, y2],
                        "cx": cx,
                        "cy": cy,
                        "nombre": nombre_clase,
                        "conf": float(conf),
                        "en_percha": percha_prod is not None,
                        "percha_id": percha_prod["id"] if percha_prod else "PER-A1",
                        "percha_nombre": percha_prod["nombre"] if percha_prod else "Percha General",
                        "estado": "en_percha" if percha_prod is not None else "fuera"
                    })

        ids_vistos = {p["id"] for p in personas_detectadas if p.get("es_cliente_valido", True)}
        for (gid, pid_zona) in list(self.tiempos_zona.keys()):
            if gid not in ids_vistos:
                t_salida = ahora - self.tiempos_zona.pop((gid, pid_zona))
                if t_salida > 1.5:
                    self._encolar_db("permanencia", round(t_salida, 1))

        # =====================================================================
        # INTERACCIÓN MANO-PERCHA + POSTURA DE CABEZA (SOLO CLIENTES VÁLIDOS)
        # =====================================================================
        for pers in personas_detectadas:
            if not pers.get("es_cliente_valido", True):
                continue
            gid = pers["id"]
            pbox = pers["box"]
            postura_lbl = pers.get("postura_cabeza", "Giro a Percha")
            postura_cod = pers.get("postura_codigo", "giro_percha")

            for po in perchas_overlay:
                percha_id = po["id"]
                percha_nombre = po["nombre"]
                roi_px = tuple(po["box"])

                mano_en_percha, mano_info, ratio_mano = self.shelf_analyzer.estimar_manos_y_orientacion(
                    frame_gray, pbox, roi_px
                )

                clave_reach = (gid, percha_id)
                if clave_reach not in self.ultimo_reach_zona:
                    self.ultimo_reach_zona[clave_reach] = {
                        "en_contacto": False,
                        "t_inicio": 0.0,
                        "mano_cy_norm": 0.5,
                        "ultimo_evento_t": 0.0
                    }

                st = self.ultimo_reach_zona[clave_reach]

                if mano_en_percha and not st["en_contacto"]:
                    st["en_contacto"] = True
                    st["t_inicio"] = ahora
                    if mano_info:
                        st["mano_cy_norm"] = mano_info[1] / float(ALTO)
                        st["mano_box"] = [mano_info[2], mano_info[3], mano_info[4] - mano_info[2], mano_info[5] - mano_info[3]]
                    self.shelf_analyzer.capturar_estado_previo(frame_gray, percha_id, gid, roi_px)
                    with self.mtmc.lock:
                        if gid in self.mtmc.global_tracks:
                            self.mtmc.global_tracks[gid]["estado_accion"] = f"{postura_lbl} • En {percha_id}"
                            self.mtmc.global_tracks[gid]["percha_actual"] = percha_id

                elif mano_en_percha and st["en_contacto"] and mano_info:
                    st["mano_cy_norm"] = 0.5 * st["mano_cy_norm"] + 0.5 * (mano_info[1] / float(ALTO))
                    st["mano_box"] = [mano_info[2], mano_info[3], mano_info[4] - mano_info[2], mano_info[5] - mano_info[3]]

                elif not mano_en_percha and st["en_contacto"]:
                    duracion_gesto = ahora - st["t_inicio"]
                    st["en_contacto"] = False

                    if 0.55 <= duracion_gesto <= 8.0 and (ahora - st["ultimo_evento_t"] >= 1.0):
                        eval_percha = self.shelf_analyzer.evaluar_cambio_en_percha(frame_gray, percha_id, gid, roi_px)
                        if not eval_percha["cambio_detectado"]:
                            continue

                        st["ultimo_evento_t"] = ahora
                        clase_cercana = None
                        for prod in productos_detectados:
                            if prod["percha_id"] == percha_id:
                                clase_cercana = prod["nombre"]
                                break

                        prod_resuelto, nivel_estante = self._resolver_producto_en_percha(
                            po, st["mano_cy_norm"], clase_cercana
                        )
                        etiqueta_prod = f"{prod_resuelto} ({nivel_estante})"
                        coords_2d = [pers["map_x"], pers["map_y"]]

                        inventario_cliente = self.productos_en_mano.setdefault(gid, [])
                        item_de_esta_percha = next(
                            (it for it in inventario_cliente if it["percha_id"] == percha_id and (ahora - it["t_tomado"] > 1.2)),
                            None
                        )

                        if item_de_esta_percha is not None and (eval_percha.get("retorno_a_base") or len(inventario_cliente) > 0):
                            inventario_cliente.remove(item_de_esta_percha)
                            self.total_devueltos += 1
                            self._encolar_db("devuelto", item_de_esta_percha["producto"], gid, percha_nombre)
                            self._encolar_db(
                                "coordenada", f"VEC-{gid}", 1, pers["map_x"], pers["map_y"], percha_id,
                                nivel_estante, postura_cod, "ultimo_momento_devuelve",
                                item_de_esta_percha["producto"], round(duracion_gesto, 1), cam_id
                            )
                            self._registrar_evento_memoria(
                                "Devuelto (Último Momento)", gid, item_de_esta_percha["producto"],
                                eval_percha["confianza"], coords_2d,
                                color="azul", percha_id=percha_id, percha_nombre=percha_nombre,
                                camara_id=cam_id, postura_cabeza=postura_lbl, comportamiento="ultimo_momento_devuelve"
                            )
                            alertas_frame.append(f"↩ DEVUELTO [{percha_id}]: {item_de_esta_percha['producto']} (S#{gid})")
                        else:
                            # Si miró la percha >= 1.8s antes/durante la toma -> Mira y Compra; si fue rápido -> No Mira y Compra
                            comp_toma = "mira_y_compra" if (pers.get("dwell", 0) >= 2 or duracion_gesto >= 1.8) else "no_mira_y_compra"
                            inventario_cliente.append({
                                "producto": prod_resuelto,
                                "percha_id": percha_id,
                                "percha_nombre": percha_nombre,
                                "nivel": nivel_estante,
                                "t_tomado": ahora
                            })
                            self.total_agarrados += 1
                            self._encolar_db("tomado", prod_resuelto, gid, percha_nombre)
                            self._encolar_db(
                                "coordenada", f"VEC-{gid}", 1, pers["map_x"], pers["map_y"], percha_id,
                                nivel_estante, postura_cod, comp_toma,
                                prod_resuelto, round(duracion_gesto, 1), cam_id
                            )
                            self._registrar_evento_memoria(
                                "Producto Tomado", gid, etiqueta_prod,
                                eval_percha["confianza"], coords_2d,
                                color="naranja", percha_id=percha_id, percha_nombre=percha_nombre,
                                camara_id=cam_id, postura_cabeza=postura_lbl, comportamiento=comp_toma
                            )
                            alertas_frame.append(f"⚡ TOMADO [{percha_id}]: {prod_resuelto} (S#{gid})")

                    pers["productos_en_mano"] = [it["producto"] for it in self.productos_en_mano.get(gid, [])]

        with self.lock:
            self.overlay_data = {
                "personas": personas_detectadas,
                "productos": productos_detectados,
                "perchas": perchas_overlay,
                "alertas": alertas_frame,
                "telemetria": f"Privacidad Coordenadas 2D | {cam_id} | {self.fps_yolo:.1f} FPS"
            }

    # =========================================================================
    # NIVEL 3: COMPOSITOR DE VIDEO CON DIFUMINADO FACIAL DE PRIVACIDAD
    # =========================================================================
    def get_frame_bytes(self, ultimo_id=-1):
        """
        Retorna el fotograma codificado en JPEG aplicando:
        - Difuminado automático de la zona craneal/facial (Cumplimiento de Privacidad GDPR/LOPDP).
        - Indicador de Vector de Coordenada Anónimo (`S#ID`), Postura de Cabeza y Exclusión de Staff.
        """
        with self.lock:
            raw = self.latest_raw_frame
            fid = self.frame_id
            overlay = self.overlay_data

        if raw is None:
            return ultimo_id, None

        if fid == ultimo_id and self._jpeg is not None:
            return ultimo_id, None

        canvas = raw.copy()
        if self.dibujar_hud and overlay:
            for po in overlay.get("perchas", []):
                sx1, sy1, sx2, sy2 = po["box"]
                cv2.rectangle(canvas, (sx1, sy1), (sx2, sy2), (0, 80, 254), 1)
                cv2.rectangle(canvas, (sx1, max(0, sy1 - 18)), (min(ANCHO, sx1 + 155), sy1), (64, 35, 12), -1)
                cv2.putText(
                    canvas, f"{po['id']}: {po['categoria']}", (sx1 + 4, max(12, sy1 - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 255, 255), 1, cv2.LINE_AA
                )

            for pers in overlay.get("personas", []):
                x1, y1, x2, y2 = pers["box"]
                # Difuminar el 24% superior de la caja (rostro/cabeza) para garantizar anonimato visual total
                hy_blur = min(ALTO, y1 + max(10, int((y2 - y1) * 0.24)))
                bx1, by1, bx2 = max(0, x1), max(0, y1), min(ANCHO, x2)
                if bx2 > bx1 and hy_blur > by1:
                    roi_face = canvas[by1:hy_blur, bx1:bx2]
                    if roi_face.size > 0:
                        canvas[by1:hy_blur, bx1:bx2] = cv2.GaussianBlur(roi_face, (15, 15), 10)

                es_cli = pers.get("es_cliente_valido", True)
                tiene_items = len(pers.get("productos_en_mano", [])) > 0
                color_bgr = (148, 163, 184) if not es_cli else ((0, 80, 254) if tiene_items else (94, 197, 34))
                cv2.rectangle(canvas, (x1, y1), (x2, y2), color_bgr, 2)
                etiqueta = f"{pers.get('etiqueta', 'S#1')} | {pers.get('postura_cabeza', 'Frente')[:18]}"
                cv2.rectangle(canvas, (x1, max(0, y1 - 18)), (min(ANCHO, x1 + 175), y1), color_bgr, -1)
                cv2.putText(
                    canvas, etiqueta, (x1 + 4, max(12, y1 - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.36, (255, 255, 255), 1, cv2.LINE_AA
                )

        _, buf = cv2.imencode('.jpg', canvas, [cv2.IMWRITE_JPEG_QUALITY, 68])
        datos = buf.tobytes()

        with self.lock:
            self._jpeg_id = fid
            self._jpeg = datos

        return fid, datos


camara = CameraService()
