import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _importar_main_con(jwt_secret):
    """Importa main.py en un proceso nuevo, como lo haría uvicorn al arrancar."""
    env = {**os.environ, "MODO": "nube", "TURSO_URL": ""}
    if jwt_secret is None:
        env.pop("JWT_SECRET", None)
    else:
        env["JWT_SECRET"] = jwt_secret
    return subprocess.run(
        # Se anula load_dotenv para que un .env local no altere el resultado del test
        [sys.executable, "-c", "import dotenv; dotenv.load_dotenv = lambda *a, **k: False; import main"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("valor", [None, "", "cambiar_esto_en_produccion", "a" * 31])
def test_la_app_no_arranca_con_un_jwt_secret_invalido(valor):
    r = _importar_main_con(valor)
    assert r.returncode != 0
    assert "JWT_SECRET" in r.stderr
    assert "secrets.token_urlsafe" in r.stderr  # el mensaje explica cómo generar uno


def test_la_app_arranca_con_un_jwt_secret_de_32_caracteres():
    r = _importar_main_con("b" * 32)
    assert r.returncode == 0, r.stderr


def test_validar_jwt_secret_devuelve_el_valor_valido():
    from main import validar_jwt_secret
    assert validar_jwt_secret("x" * 40) == "x" * 40
