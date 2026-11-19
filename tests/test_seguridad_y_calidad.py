import database
from tests.conftest import crear_usuario_prueba


def _login(client, username, password):
    r = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert r.status_code == 303


def test_rbac_bloquea_vendedor_en_endpoints_administrativos(client):
    u, p = crear_usuario_prueba(username="vend1", rol="vendedor")
    _login(client, u, p)

    assert client.post("/api/cameras", json={"nombre": "Cam X", "ruta": "0"}).status_code == 403
    assert client.delete("/api/cameras/CAM-01").status_code == 403
    assert client.post("/api/shelves", json={"nombre": "Percha X"}).status_code == 403
    assert client.post("/api/promotions", json={"titulo": "Promo X"}).status_code == 403
    assert client.post("/api/control", json={"accion": "parar"}).status_code == 403
    assert client.post("/api/fuente", json={"tipo": "video"}).status_code == 403
    assert client.post("/api/simulation/toggle", json={"activar": False}).status_code == 403


def test_usuario_desactivado_pierde_acceso_inmediato_con_jwt_existente(client):
    u, p = crear_usuario_prueba(username="carlos", rol="admin")
    _login(client, u, p)
    assert client.get("/api/stats").status_code == 200

    database.escribir("UPDATE usuarios SET estado = 'inactivo' WHERE username = ?", (u,))
    assert client.get("/api/stats").status_code == 401


def test_ssrf_y_path_traversal_bloqueados_en_fuente_y_camaras(client):
    u, p = crear_usuario_prueba(username="adm_sec", rol="admin")
    _login(client, u, p)

    assert client.post("/api/fuente", json={"tipo": "rtsp", "ruta": "http://169.254.169.254/latest/meta-data/"}).status_code == 400
    assert client.post("/api/fuente", json={"tipo": "rtsp", "ruta": "rtsp://127.0.0.1:554/stream"}).status_code == 400
    assert client.post("/api/fuente", json={"tipo": "imagen", "ruta": "/etc/passwd"}).status_code == 400
    assert client.post("/api/fuente", json={"tipo": "video", "ruta": "../database.py"}).status_code == 400


def test_credenciales_rtsp_se_redactan_en_api_cameras(client):
    u, p = crear_usuario_prueba(username="adm_cam", rol="admin")
    _login(client, u, p)

    r_post = client.post(
        "/api/cameras",
        json={"id": "CAM-99", "nombre": "Pasillo Sur", "ruta": "rtsp://admin:superclave99@192.168.1.50:554/live"},
    )
    assert r_post.status_code == 200
    r_get = client.get("/api/cameras")
    assert r_get.status_code == 200
    cam99 = next(c for c in r_get.json()["camaras"] if c["id"] == "CAM-99")
    assert "superclave99" not in cam99["ruta"]
    assert "***@192.168.1.50:554" in cam99["ruta"]


def test_conteo_exacto_1x_por_producto_tomado(client):
    u, p = crear_usuario_prueba(username="adm_cnt", rol="admin")
    _login(client, u, p)

    antes = client.get("/api/stats?dias=1").json()
    database.guardar_producto_tomado("Producto Test Unico", sesion_id=999, percha_nombre="PER-A1")
    despues = client.get("/api/stats?dias=1").json()

    assert despues["tomados"] - antes["tomados"] == 1
    assert despues["interacciones"] - antes["interacciones"] == 1


def test_fechas_invalidas_y_logout_post_y_cabeceras_seguridad(client):
    u, p = crear_usuario_prueba(username="adm_hdr", rol="admin")
    _login(client, u, p)

    r_bad = client.get("/api/stats?fecha_inicio=no-es-fecha")
    assert r_bad.status_code in (400, 422)

    r_ok = client.get("/api/stats")
    assert r_ok.headers.get("x-frame-options") == "DENY"
    assert r_ok.headers.get("x-content-type-options") == "nosniff"
    assert r_ok.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

    assert client.get("/logout", follow_redirects=False).status_code == 405
    r_out = client.post("/logout", follow_redirects=False)
    assert r_out.status_code == 303
    assert r_out.headers["location"] == "/login"


def test_sucursales_filtro_horario_y_mapa_calor_enlazado(client):
    u, p = crear_usuario_prueba(username="adm_suc", rol="admin")
    _login(client, u, p)

    # 1. Sucursales precargadas y alta de nueva sucursal
    r_br = client.get("/api/branches")
    assert r_br.status_code == 200
    sucs = r_br.json()["sucursales"]
    assert len(sucs) >= 3
    ids = {s["id"] for s in sucs}
    assert {"SUC-NORTE", "SUC-CENTRO", "SUC-SUR"}.issubset(ids)

    r_new = client.post("/api/branches", json={
        "id": "SUC-VALLE", "nombre": "Sucursal Valle", "ciudad": "Quito", "formato": "Supermercado", "area_m2": 1100
    })
    assert r_new.status_code == 200
    assert any(s["id"] == "SUC-VALLE" for s in r_new.json()["sucursales"])

    # 2. Control por horas (1h, 6h, 24h) vs días (7d, 30d) actualiza los datos reales
    st_1h = client.get("/api/stats?horas=1&sucursal_id=SUC-NORTE").json()
    st_24h = client.get("/api/stats?horas=24&sucursal_id=SUC-NORTE").json()
    st_30d = client.get("/api/stats?dias=30&sucursal_id=SUC-NORTE").json()
    st_centro = client.get("/api/stats?dias=30&sucursal_id=SUC-CENTRO").json()
    st_todas = client.get("/api/stats?dias=30&sucursal_id=todas").json()

    assert st_1h["trafico"] > 0
    assert st_24h["trafico"] > st_1h["trafico"]
    assert st_30d["trafico"] > st_24h["trafico"]
    assert st_todas["trafico"] >= st_30d["trafico"] + st_centro["trafico"]

    # 3. Serie temporal agrupada por horas y filtro de franja horaria (hora_inicio..hora_fin)
    r_hora = client.get("/api/analytics/serie_temporal?agrupacion=hora&horas=24&sucursal_id=SUC-NORTE")
    assert r_hora.status_code == 200
    dh = r_hora.json()
    assert dh["agrupacion"] == "hora"
    assert len(dh["labels"]) >= 6
    assert sum(dh["trafico"]) > 0

    st_manana = client.get("/api/stats?dias=30&hora_inicio=8&hora_fin=11&sucursal_id=SUC-NORTE").json()
    assert st_manana["trafico"] > 0
    assert st_manana["trafico"] < st_30d["trafico"]

    # 4. Mapa arquitectónico y mapa de calor espacial enlazados al período y sucursal
    map_1h = client.get("/api/map_state?horas=1&sucursal_id=SUC-NORTE").json()
    map_30d = client.get("/api/map_state?dias=30&sucursal_id=SUC-NORTE").json()
    map_centro = client.get("/api/map_state?dias=30&sucursal_id=SUC-CENTRO").json()

    assert "plano_arquitectura" in map_30d
    assert "bodega" in map_30d["plano_arquitectura"]
    assert "linea_cajas" in map_30d["plano_arquitectura"]
    assert len(map_30d["camaras"]) >= 3
    assert map_30d["heatmap_meta"]["total_muestras"] > map_1h["heatmap_meta"]["total_muestras"]
    assert map_centro["sucursal_id"] == "SUC-CENTRO"
    assert any(p["id"].startswith("PER-CEN") for p in map_centro["perchas"])

    # 4b. Top productos agrupa correctamente sin fragmentar por sesión VEC
    r_top = client.get("/api/analytics/productos/top?dias=30&sucursal_id=SUC-NORTE")
    assert r_top.status_code == 200
    top_items = r_top.json()["productos"]
    assert len(top_items) >= 3
    assert top_items[0]["interacciones"] >= 10
    assert "(VEC-" not in top_items[0]["nombre"]
    assert "[PER-" not in top_items[0]["nombre"]

    # 4c. Endpoints consolidados de alta velocidad (/api/dashboard_bundle y /api/analytics/bundle)
    r_bun = client.get("/api/dashboard_bundle?dias=30&sucursal_id=SUC-NORTE")
    assert r_bun.status_code == 200
    bun = r_bun.json()
    assert bun["stats"]["trafico"] >= bun["stats"]["tomados"]
    assert bun["stats"]["devueltos"] == bun["resumen_publicidad"]["comportamiento_visual"]["ultimo_momento_devuelve"]["casos"]
    cal_ids = {z["id"] for z in bun["resumen_publicidad"]["zonas_calientes"]}
    fri_ids = {z["id"] for z in bun["resumen_publicidad"]["zonas_frias"]}
    assert cal_ids.isdisjoint(fri_ids)

    r_abun = client.get("/api/analytics/bundle?agrupacion=semana&dias=30&sucursal_id=SUC-NORTE")
    assert r_abun.status_code == 200
    abun = r_abun.json()
    assert len(abun["serie_temporal"]["labels"]) >= 3
    for fk in ("manana", "tarde", "noche"):
        assert 0.0 <= abun["franjas"][fk]["conversion"] <= 100.0
    for row in abun["heatmap_matriz"]["resumen_franjas"]:
        assert row["manana"] + row["mediodia"] + row["tarde"] + row["noche"] + row["madrugada"] == row["total"]

    # 5. Polígonos en perspectiva y trackeo real del video demo
    import json, os
    tracks_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "demo_tracks.json")
    assert os.path.exists(tracks_path)
    with open(tracks_path, "r", encoding="utf-8") as fh:
        dt = json.load(fh)
    assert dt["total_frames"] == 240
    assert len(dt["shelves"]) == 2
    assert len(dt["shelves"][0]["poly"]) == 5
    assert any(len(fr["p"]) >= 2 for fr in dt["frames"])

