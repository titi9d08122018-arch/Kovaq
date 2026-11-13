import pytest

from tests.conftest import crear_usuario_prueba


def _login(client, usuario, clave):
    return client.post("/login", data={"username": usuario, "password": clave}, follow_redirects=False)


def test_usuario_activo_puede_iniciar_sesion(client):
    usuario, clave = crear_usuario_prueba(estado="activo")
    r = _login(client, usuario, clave)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    assert "access_token" in r.headers.get("set-cookie", "")


@pytest.mark.parametrize("estado", ["inactivo", "bloqueado", "", None])
def test_usuario_no_activo_es_rechazado_aunque_la_clave_sea_correcta(client, estado):
    usuario, clave = crear_usuario_prueba(estado=estado)
    r = _login(client, usuario, clave)
    assert r.status_code == 200  # vuelve a mostrar el login
    assert "Credenciales inválidas" in r.text
    assert "access_token" not in r.headers.get("set-cookie", "")


def test_clave_incorrecta_sigue_siendo_rechazada(client):
    usuario, _ = crear_usuario_prueba()
    r = _login(client, usuario, "otra-clave-equivocada")
    assert "Credenciales inválidas" in r.text
    assert "access_token" not in r.headers.get("set-cookie", "")
