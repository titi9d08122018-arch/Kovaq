import main
from tests.conftest import crear_usuario_prueba


def _set_cookie_del_login(client):
    usuario, clave = crear_usuario_prueba()
    r = client.post("/login", data={"username": usuario, "password": clave}, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["set-cookie"].lower()


def test_cookie_lleva_samesite_lax_y_httponly(client):
    cookie = _set_cookie_del_login(client)
    assert "samesite=lax" in cookie
    assert "httponly" in cookie


def test_cookie_es_secure_en_modo_nube(client):
    assert main.MODO == "nube"
    assert "secure" in _set_cookie_del_login(client)


def test_cookie_no_es_secure_en_modo_local(client, monkeypatch):
    # En local se entra por http://localhost: una cookie secure no se enviaría
    monkeypatch.setattr(main, "MODO", "local")
    assert "secure" not in _set_cookie_del_login(client)
