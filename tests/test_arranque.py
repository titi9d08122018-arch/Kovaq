def test_la_app_arranca_en_modo_nube_y_muestra_login(client):
    r = client.get("/login")
    assert r.status_code == 200


def test_el_arranque_crea_las_tablas_en_la_bd_temporal(client, db_temporal):
    import database
    roles = {r[0] for r in database.leer("SELECT nombre FROM roles")}
    assert {"admin", "dueno", "vendedor"} <= roles
    assert db_temporal.exists()
