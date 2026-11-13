import logging

import database


def _usuarios():
    return database.leer("SELECT username FROM usuarios")


def test_sin_variables_no_crea_ningun_admin_y_avisa_en_el_log(db_temporal, monkeypatch, caplog):
    monkeypatch.delenv("KOVAQ_ADMIN_USER", raising=False)
    monkeypatch.delenv("KOVAQ_ADMIN_PASSWORD", raising=False)
    with caplog.at_level(logging.WARNING):
        database.init_db_sync()
    assert _usuarios() == []
    assert "KOVAQ_ADMIN_USER" in caplog.text


def test_con_variables_validas_crea_el_admin_con_esa_clave(db_temporal, monkeypatch):
    monkeypatch.setenv("KOVAQ_ADMIN_USER", "jefa")
    monkeypatch.setenv("KOVAQ_ADMIN_PASSWORD", "una-clave-larga-1")
    database.init_db_sync()
    fila = database.leer(
        "SELECT u.username, u.password_hash, r.nombre FROM usuarios u JOIN roles r ON u.rol_id = r.id",
        uno=True,
    )
    assert fila[0] == "jefa"
    assert fila[2] == "admin"
    assert database.pwd_context.verify("una-clave-larga-1", fila[1])


def test_clave_de_menos_de_12_caracteres_no_crea_admin(db_temporal, monkeypatch, caplog):
    monkeypatch.setenv("KOVAQ_ADMIN_USER", "jefa")
    monkeypatch.setenv("KOVAQ_ADMIN_PASSWORD", "corta12345")  # 10 caracteres
    with caplog.at_level(logging.WARNING):
        database.init_db_sync()
    assert _usuarios() == []
    assert "12" in caplog.text


def test_solo_usuario_sin_clave_no_crea_admin(db_temporal, monkeypatch):
    monkeypatch.setenv("KOVAQ_ADMIN_USER", "jefa")
    monkeypatch.delenv("KOVAQ_ADMIN_PASSWORD", raising=False)
    database.init_db_sync()
    assert _usuarios() == []


def test_la_clave_antigua_ya_no_aparece_en_el_codigo():
    with open(database.__file__, encoding="utf-8") as f:
        assert "KovaqSeguro2026" not in f.read()
