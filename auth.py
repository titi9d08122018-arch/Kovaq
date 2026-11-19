import asyncio
import os
import threading
import time
import jwt
from dotenv import load_dotenv
from fastapi import HTTPException, Request, status

from database import TURSO_URL, leer

load_dotenv()

_SECRETOS_INSEGUROS = {"", "cambiar_esto_en_produccion", "super-secreto-kovaq"}
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))
ROLES_GESTION = ("admin", "admin_tecnico", "dueno")

_AUTH_CACHE_TTL = 10.0
_auth_cache_lock = threading.Lock()
_auth_cache: dict = {}


def invalidar_cache_auth(username: str | None = None):
    with _auth_cache_lock:
        if username:
            _auth_cache.pop(username, None)
        else:
            _auth_cache.clear()


def validar_jwt_secret(valor: str | None) -> str:
    secreto = (valor or "").strip()
    if secreto in _SECRETOS_INSEGUROS or len(secreto) < 32:
        raise RuntimeError(
            "JWT_SECRET es obligatorio y debe tener al menos 32 caracteres "
            "(no uses el valor de ejemplo). Genera uno con: "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return secreto


SECRET_KEY = validar_jwt_secret(os.getenv("JWT_SECRET"))


def _buscar_usuario_activo(username: str):
    if TURSO_URL and username:
        ahora = time.time()
        with _auth_cache_lock:
            cached = _auth_cache.get(username)
            if cached and (ahora - cached["t"]) < _AUTH_CACHE_TTL:
                return cached["u"]

    row = leer(
        "SELECT u.id, u.username, u.nombre_completo, r.nombre "
        "FROM usuarios u JOIN roles r ON u.rol_id = r.id "
        "WHERE u.username = ? AND u.estado = 'activo'",
        (username,),
        uno=True,
    )
    if TURSO_URL and username:
        with _auth_cache_lock:
            if row:
                _auth_cache[username] = {"t": time.time(), "u": row}
            else:
                _auth_cache.pop(username, None)
    return row


async def obtener_usuario_actual(request: Request) -> dict:
    token_cookie = request.cookies.get("access_token")
    if not token_cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No autorizado")
    try:
        token = token_cookie.split(" ", 1)[1]
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"require": ["exp"]})
        usuario = await asyncio.to_thread(_buscar_usuario_activo, payload.get("sub"))
        if not usuario:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario inactivo o inexistente")
        return {"id": usuario[0], "username": usuario[1], "nombre": usuario[2], "rol": usuario[3]}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")


def requerir_roles(*roles_permitidos: str):
    roles_set = set(roles_permitidos)

    async def _verificar(request: Request) -> dict:
        user = await obtener_usuario_actual(request)
        if user["rol"] not in roles_set:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permisos insuficientes")
        return user

    return _verificar
