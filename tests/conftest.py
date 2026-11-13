"""
Configuración común de los tests.

Las variables de entorno se fijan ANTES de importar la app:
- MODO=nube evita importar camera.py (no carga YOLO ni abre la webcam).
- TURSO_URL vacío obliga a usar SQLite local; load_dotenv() no sobrescribe
  variables ya definidas, así que un .env local con Turso no se usa en tests.
- JWT_SECRET de prueba (>= 32 caracteres), solo válido para tests.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # main.py monta "static" y "templates" con rutas relativas

os.environ["MODO"] = "nube"
os.environ["TURSO_URL"] = ""
os.environ["TURSO_TOKEN"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["JWT_SECRET"] = "secreto-de-tests-0123456789abcdefghijklmnop"
os.environ.pop("KOVAQ_ADMIN_USER", None)
os.environ.pop("KOVAQ_ADMIN_PASSWORD", None)

import pytest
from fastapi.testclient import TestClient

import database


@pytest.fixture
def db_temporal(tmp_path, monkeypatch):
    """Cada test usa su propia BD SQLite vacía en un directorio temporal."""
    monkeypatch.setattr(database, "TURSO_URL", "")
    monkeypatch.setattr(database, "DB_LOCAL", str(tmp_path / "test_kovaq.db"))
    return tmp_path / "test_kovaq.db"


@pytest.fixture
def client(db_temporal):
    """Cliente HTTPS (las cookies secure solo viajan por https)."""
    from main import app
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def crear_usuario_prueba(username="ana", password="clave-segura-123", estado="activo", rol="admin"):
    rol_id = database.leer("SELECT id FROM roles WHERE nombre = ?", (rol,), uno=True)[0]
    database.escribir(
        "INSERT INTO usuarios (username, password_hash, nombre_completo, rol_id, estado) VALUES (?, ?, ?, ?, ?)",
        (username, database.pwd_context.hash(password), "Usuario Prueba", rol_id, estado),
    )
    return username, password
