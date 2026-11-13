import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETOS = ["JWT_SECRET", "KOVAQ_ADMIN_USER", "KOVAQ_ADMIN_PASSWORD", "TURSO_URL",
            "TURSO_TOKEN", "GROQ_API_KEY", "VIDEO_PUSH_TOKEN"]


def _leer(nombre):
    with open(os.path.join(ROOT, nombre), encoding="utf-8") as f:
        return f.read()


def _env_vars_render():
    """Devuelve {KEY: texto del bloque} de cada envVar en render.yaml."""
    bloques = re.split(r"\n\s*- key:\s*", "\n" + _leer("render.yaml"))[1:]
    return {b.split("\n", 1)[0].strip(): b for b in bloques}


def _env_example():
    vars_ = {}
    for linea in _leer(".env.example").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            k, v = linea.split("=", 1)
            vars_[k.strip()] = v.strip()
    return vars_


def test_render_genera_jwt_secret_automaticamente():
    assert re.search(r"generateValue:\s*true", _env_vars_render()["JWT_SECRET"])


def test_render_declara_los_secretos_con_sync_false():
    env = _env_vars_render()
    for clave in SECRETOS[1:]:
        assert clave in env, f"falta {clave} en render.yaml"
        assert re.search(r"sync:\s*false", env[clave]), f"{clave} debe tener sync: false"


def test_render_no_contiene_valores_de_secretos():
    env = _env_vars_render()
    for clave in SECRETOS:
        assert "value:" not in env[clave], f"{clave} no debe tener un valor escrito en render.yaml"


def test_env_example_incluye_todas_las_variables_que_usa_el_codigo():
    usadas = set()
    for archivo in glob.glob(os.path.join(ROOT, "*.py")):
        if os.path.basename(archivo) == "auth.py":  # código muerto, no lo importa nadie
            continue
        with open(archivo, encoding="utf-8") as f:
            usadas |= set(re.findall(r"os\.getenv\(\s*[\"']([A-Z0-9_]+)[\"']", f.read()))
    usadas |= {"KOVAQ_ADMIN_USER", "KOVAQ_ADMIN_PASSWORD"}
    faltan = usadas - set(_env_example())
    assert not faltan, f"faltan en .env.example: {sorted(faltan)}"


def test_env_example_no_trae_valores_reales_en_los_secretos():
    env = _env_example()
    for clave in SECRETOS:
        assert env.get(clave) == "", f"{clave} debe ir vacío en .env.example"
