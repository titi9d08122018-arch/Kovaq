import numpy as np
from tests.conftest import crear_usuario_prueba
from camera import MultiCameraTracker, ShelfAnalyzer


def _login(client):
    usuario, clave = crear_usuario_prueba()
    r = client.post("/login", data={"username": usuario, "password": clave}, follow_redirects=False)
    assert r.status_code == 303


def test_reid_mantiene_vector_anonimo_al_pasar_de_camara_1_a_camara_2_y_3():
    """
    Verifica sobre el papel que cuando una misma persona (mismo vector cromático efímero)
    sale de CAM-01, cruza hacia CAM-02 y luego hacia CAM-03, el MultiCameraTracker
    mantiene exactamente el mismo Vector Anónimo (S#1) y registra los Handoffs sin guardar datos personales.
    """
    mtmc = MultiCameraTracker()

    frame_cam1 = np.zeros((360, 480, 3), dtype=np.uint8)
    box1 = (320, 80, 410, 310)
    frame_cam1[100:200, 330:400] = (220, 90, 20)
    frame_cam1[200:300, 330:400] = (50, 50, 50)

    t0 = 1000.0
    mx1, my1 = mtmc.proyectar_a_plano_2d("CAM-01", 365, 310)
    gid1, handoff1 = mtmc.resolver_identidad_global("CAM-01", local_tid=7, frame=frame_cam1, box=box1, map_x=mx1, map_y=my1, ahora=t0)
    assert gid1 == 1
    assert handoff1 is None

    frame_cam2 = np.zeros((360, 480, 3), dtype=np.uint8)
    box2 = (40, 82, 130, 312)
    frame_cam2[102:202, 50:120] = (215, 92, 22)
    frame_cam2[202:302, 50:120] = (52, 52, 52)

    mx2, my2 = mtmc.proyectar_a_plano_2d("CAM-02", 85, 312)
    gid2, handoff2 = mtmc.resolver_identidad_global("CAM-02", local_tid=23, frame=frame_cam2, box=box2, map_x=mx2, map_y=my2, ahora=t0 + 2.0)

    assert gid2 == gid1
    assert handoff2 is not None
    assert handoff2["origen"] == "CAM-01"
    assert handoff2["destino"] == "CAM-02"
    assert handoff2["similitud"] >= 0.70

    mx3, my3 = mtmc.proyectar_a_plano_2d("CAM-03", 60, 310)
    gid3, handoff3 = mtmc.resolver_identidad_global("CAM-03", local_tid=99, frame=frame_cam2, box=box2, map_x=mx3, map_y=my3, ahora=t0 + 5.0)
    assert gid3 == gid1
    assert handoff3 is not None
    assert handoff3["origen"] == "CAM-02"
    assert handoff3["destino"] == "CAM-03"
    assert mtmc.global_tracks[gid1]["camaras_visitadas"] == ["CAM-01", "CAM-02", "CAM-03"]


def test_filtro_cliente_puerta_caja_excluye_empleados_y_detecta_postura_cabeza():
    """
    Verifica que:
    1) La empleada repositora del fondo del pasillo (como en t=5.2s del video demo) sea clasificada
       como `es_cliente_valido=False` (empleada excluida), mientras que los compradores son `True`.
    2) `ShelfAnalyzer.estimar_postura_cabeza_y_cuerpo` detecte giro de cabeza hacia percha y cuando se agacha.
    """
    mtmc = MultiCameraTracker()
    # Comprador en primer/medio plano del pasillo
    assert mtmc.clasificar_cliente_vs_empleado((163, 186, 271, 357), 0.24, 0.62) is True
    # Empleada repositora al fondo con carro (como Track #3 en demo_supermercado.mp4: [245, 113, 263, 170])
    assert mtmc.clasificar_cliente_vs_empleado((245, 113, 263, 170), 0.28, 0.44) is False

    sa = ShelfAnalyzer()
    frame_gray = np.full((360, 480), 120, dtype=np.uint8)
    perchas_overlay = [{"id": "PER-A1", "box": [10, 72, 206, 331]}]

    # Postura erguida frente a PER-A1 -> detecta giro de cabeza hacia PER-A1
    p_erguido = sa.estimar_postura_cabeza_y_cuerpo(frame_gray, gid=50, box_persona=(170, 90, 225, 290), perchas_overlay=perchas_overlay)
    assert "PER-A1" in p_erguido["etiqueta"] or "giro_percha" in p_erguido["codigo"]

    # Postura agachada (altura cae notablemente respecto a su altura base erguida) -> detecta se_agacha
    p_agachado = sa.estimar_postura_cabeza_y_cuerpo(frame_gray, gid=50, box_persona=(170, 175, 235, 290), perchas_overlay=perchas_overlay)
    assert p_agachado["codigo"] == "se_agacha"


def test_shelf_analyzer_detecta_extension_de_mano_y_cambio_en_percha():
    sa = ShelfAnalyzer()
    roi_percha = (50, 50, 220, 280)
    box_persona_cerca = (160, 60, 270, 330)

    frame_antes = np.full((360, 480), 120, dtype=np.uint8)
    frame_antes[80:160, 80:180] = 240

    en_percha, mano_info, ratio = sa.estimar_manos_y_orientacion(frame_antes, box_persona_cerca, roi_percha)
    assert en_percha is True
    assert mano_info is not None
    assert ratio >= 0.14

    sa.capturar_estado_previo(frame_antes, "PER-A1", global_id=1, roi_px=roi_percha)

    frame_despues = np.full((360, 480), 120, dtype=np.uint8)
    res_pick = sa.evaluar_cambio_en_percha(frame_despues, "PER-A1", global_id=1, roi_px=roi_percha)
    assert res_pick["cambio_detectado"] is True
    assert res_pick["confianza"] >= 0.82


def test_endpoints_planograma_promociones_resumen_ejecutivo_y_marketing_ai(client):
    _login(client)

    # 1. Estado del mapa 2D con zonas Puerta/Caja, matriz de calor y análisis de los 10s del video
    r_map = client.get("/api/map_state")
    assert r_map.status_code == 200
    datos_mapa = r_map.json()
    assert "zonas_arquitectura" in datos_mapa
    assert "heatmap_grid" in datos_mapa
    assert datos_mapa["analisis_video_10s"]["duracion_seg"] == 10.0

    # 2. Subir un nuevo planograma de percha con niveles Superior/Medio/Inferior
    r_shelf = client.post("/api/shelves", json={
        "id": "PER-D1",
        "nombre": "Percha D1 • Galletas & Café",
        "categoria": "Snacks",
        "camara_id": "CAM-02",
        "map_x": 0.44, "map_y": 0.40, "map_w": 0.22, "map_h": 0.14,
        "roi": {"x1": 0.15, "y1": 0.18, "x2": 0.85, "y2": 0.88},
        "niveles": [
            {"nivel": "Superior (Subir Cabeza)", "y1": 0.18, "y2": 0.42, "producto": "Café Molido Premium"},
            {"nivel": "Medio (Altura Ojos)", "y1": 0.42, "y2": 0.68, "producto": "Galletas ChocoChips"},
            {"nivel": "Inferior (Agacharse)", "y1": 0.68, "y2": 0.88, "producto": "Azúcar Morena 1kg"}
        ]
    })
    assert r_shelf.status_code == 200
    assert any(p["id"] == "PER-D1" for p in r_shelf.json()["perchas"])

    # 3. Registrar una promoción / publicidad POP en percha
    r_promo = client.post("/api/promotions", json={
        "percha_id": "PER-D1",
        "producto": "Galletas ChocoChips",
        "titulo": "Promo 3x2 Galletas en Nivel Ojos",
        "tipo_publicidad": "Stopper Saliente + Cenefa LED",
        "descuento_pct": 33
    })
    assert r_promo.status_code == 200
    assert any(pr["percha_id"] == "PER-D1" for pr in r_promo.json()["promociones"])

    # 4. Dashboard Ejecutivo de Publicidad y Motor Analítico Kovaq Marketing AI (0 MB extra, sin depender de API externa)
    r_exec = client.get("/api/analytics/resumen_publicidad")
    assert r_exec.status_code == 200
    ex = r_exec.json()
    assert "zonas_calientes" in ex
    assert "zonas_frias" in ex
    assert "comportamiento_visual" in ex
    assert "postura_cabeza" in ex
    assert "motor_marketing_ai" in ex
    assert ex["privacidad"]["empleados_excluidos"] >= 1

    # 5. Consultar al Motor Analítico Kovaq Marketing AI vía /api/chat (incluso sin GROQ_API_KEY)
    r_chat = client.post("/api/chat", json={"mensaje": "¿Funcionan las promociones y qué pasa a último momento?", "modo": "publicidad"})
    assert r_chat.status_code == 200
    assert len(r_chat.json()["respuesta"]) > 30


def test_analisis_temporal_por_dias_semanas_meses_y_rango_fechas(client):
    """
    Verifica que:
    1) Los datos históricos simulados de 90 días estén sembrados en la base de datos.
    2) El endpoint `/api/analytics/serie_temporal` funcione agrupando por `dia`, `semana` y `mes`.
    3) El filtrado por rango de fechas personalizado (`fecha_inicio` y `fecha_fin`) recalcule los dashboards
       (`/api/stats`, `/api/analytics/resumen_publicidad`, `/api/analytics/heatmap_hora`, `/api/analytics/inventario`).
    """
    _login(client)

    # 1. Serie temporal por días (últimos 30 días)
    r_dia = client.get("/api/analytics/serie_temporal?agrupacion=dia&dias=30")
    assert r_dia.status_code == 200
    datos_dia = r_dia.json()
    assert datos_dia["agrupacion"] == "dia"
    assert len(datos_dia["labels"]) >= 15
    assert sum(datos_dia["interacciones"]) > 0
    assert sum(datos_dia["llevados"]) > 0

    # 2. Serie temporal por semanas (últimos 90 días)
    r_sem = client.get("/api/analytics/serie_temporal?agrupacion=semana&dias=90")
    assert r_sem.status_code == 200
    datos_sem = r_sem.json()
    assert datos_sem["agrupacion"] == "semana"
    assert len(datos_sem["labels"]) >= 8
    assert all("-S" in lbl for lbl in datos_sem["labels"])

    # 3. Serie temporal por meses (últimos 90 días)
    r_mes = client.get("/api/analytics/serie_temporal?agrupacion=mes&dias=90")
    assert r_mes.status_code == 200
    datos_mes = r_mes.json()
    assert datos_mes["agrupacion"] == "mes"
    assert len(datos_mes["labels"]) >= 2

    # 4. Selección de rango de fechas personalizado por el cliente
    from datetime import datetime, timedelta
    hoy = datetime.now()
    f_ini = (hoy - timedelta(days=14)).strftime("%Y-%m-%d")
    f_fin = (hoy - timedelta(days=2)).strftime("%Y-%m-%d")

    r_stats_rango = client.get(f"/api/stats?fecha_inicio={f_ini}&fecha_fin={f_fin}")
    assert r_stats_rango.status_code == 200
    st_rango = r_stats_rango.json()
    assert st_rango["interacciones"] > 0
    assert st_rango["tomados"] > 0

    r_pub_rango = client.get(f"/api/analytics/resumen_publicidad?fecha_inicio={f_ini}&fecha_fin={f_fin}")
    assert r_pub_rango.status_code == 200
    pub_rango = r_pub_rango.json()
    assert pub_rango["comportamiento_visual"]["mira_y_compra"]["casos"] >= 1
    assert len(pub_rango["zonas_calientes"]) >= 1

    r_inv_rango = client.get(f"/api/analytics/inventario?fecha_inicio={f_ini}&fecha_fin={f_fin}")
    assert r_inv_rango.status_code == 200
    assert len(r_inv_rango.json()["productos"]) >= 5


def test_subida_y_analisis_inmediato_de_video_al_momento(client):
    """
    Verifica que `/api/upload` procese y analice inmediatamente el video o imagen subida
    al momento (incluso en modo nube), devolviendo el resumen de fotogramas analizados y vectores detectados.
    """
    _login(client)
    import os
    ruta_demo = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "demo_supermercado.mp4")
    assert os.path.exists(ruta_demo)

    with open(ruta_demo, "rb") as f:
        r_up = client.post(
            "/api/upload",
            files={"archivo": ("demo_supermercado.mp4", f, "video/mp4")},
        )
    assert r_up.status_code == 200
    payload = r_up.json()
    assert payload["ok"] is True
    assert payload["tipo"] == "video"
    assert payload["frames_analizados"] >= 1
    assert payload["personas_en_escena"] >= 1
    assert "Analizados" in payload["resumen"]
    client.post("/api/control", json={"accion": "parar"})
    import shutil
    shutil.rmtree("uploads", ignore_errors=True)

