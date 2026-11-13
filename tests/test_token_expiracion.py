import time

import jwt

import main
from tests.conftest import crear_usuario_prueba


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def _token_de_la_cookie(client):
    return client.cookies.get("access_token").strip('"').split(" ", 1)[1]


def test_el_token_del_login_vence_en_8_horas_por_defecto(client):
    usuario, clave = crear_usuario_prueba()
    antes = time.time()
    _login(client, usuario, clave)
    payload = jwt.decode(_token_de_la_cookie(client), main.SECRET_KEY, algorithms=[main.ALGORITHM])
    assert abs(payload["exp"] - (antes + 8 * 3600)) < 60


def test_la_duracion_es_configurable_con_access_token_expire_minutes(client, monkeypatch):
    monkeypatch.setattr(main, "ACCESS_TOKEN_EXPIRE_MINUTES", 15)
    usuario, clave = crear_usuario_prueba()
    antes = time.time()
    _login(client, usuario, clave)
    payload = jwt.decode(_token_de_la_cookie(client), main.SECRET_KEY, algorithms=[main.ALGORITHM])
    assert abs(payload["exp"] - (antes + 15 * 60)) < 60


def test_la_variable_de_entorno_se_lee_al_arrancar():
    import os, subprocess, sys
    env = {**os.environ, "ACCESS_TOKEN_EXPIRE_MINUTES": "30"}
    r = subprocess.run([sys.executable, "-c", "import main; print(main.ACCESS_TOKEN_EXPIRE_MINUTES)"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "30", r.stderr


def test_un_token_valido_da_acceso_al_dashboard(client):
    usuario, clave = crear_usuario_prueba()
    _login(client, usuario, clave)
    r = client.get("/api/stats")
    assert r.status_code == 200


def test_un_token_vencido_es_rechazado(client):
    crear_usuario_prueba(username="ana")
    vencido = jwt.encode({"sub": "ana", "rol": "admin", "exp": int(time.time()) - 10},
                         main.SECRET_KEY, algorithm=main.ALGORITHM)
    client.cookies.set("access_token", f"Bearer {vencido}")
    assert client.get("/api/stats").status_code == 401
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_un_token_sin_exp_es_rechazado(client):
    crear_usuario_prueba(username="ana")
    sin_exp = jwt.encode({"sub": "ana", "rol": "admin"}, main.SECRET_KEY, algorithm=main.ALGORITHM)
    client.cookies.set("access_token", f"Bearer {sin_exp}")
    assert client.get("/api/stats").status_code == 401
