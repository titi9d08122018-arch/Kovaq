import getpass
from passlib.context import CryptContext
from database import escribir, leer

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
MIN_PASSWORD_LEN = 12


def agregar_usuario_seguro():
    print("Crear nuevo usuario - KOVAQ")

    roles = leer("SELECT id, nombre FROM roles")
    if not roles:
        print("Error: la tabla 'roles' está vacía. Inicia primero el servidor con 'uvicorn main:app'.")
        return

    print("\nRoles disponibles:")
    for r in roles:
        print(f"  [ID: {r[0]}] -> {r[1]}")

    try:
        rol_id_elegido = int(input("\nIngresa el ID del rol a asignar: "))
    except ValueError:
        print("Error: debes ingresar un número válido.")
        return

    email = input("Correo o usuario: ").strip()
    password = getpass.getpass(f"Contraseña (mínimo {MIN_PASSWORD_LEN} caracteres): ").strip()
    nombre = input("Nombre completo: ").strip()

    if not email or len(password) < MIN_PASSWORD_LEN:
        print(f"Error: el usuario no puede estar vacío y la contraseña debe tener al menos {MIN_PASSWORD_LEN} caracteres.")
        return

    hashed_pwd = pwd_context.hash(password)

    try:
        escribir(
            "INSERT INTO usuarios (username, password_hash, nombre_completo, rol_id, estado) VALUES (?, ?, ?, ?, 'activo')",
            (email, hashed_pwd, nombre or "Usuario", rol_id_elegido),
        )
        print(f"Usuario '{email}' creado correctamente.")
    except Exception as e:
        print(f"Error al guardar en la base de datos: {e}")


if __name__ == "__main__":
    agregar_usuario_seguro()
