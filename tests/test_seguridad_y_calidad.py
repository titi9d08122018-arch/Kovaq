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
