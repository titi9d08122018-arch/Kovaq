"""Router de analítica avanzada, series temporales (horas/días/semanas/meses), mapa de calor espacial y sucursales."""
import asyncio

from fastapi import APIRouter, HTTPException, Query, Request

from ai_agent import KovaqMarketingAnalyticsEngine
from auth import obtener_usuario_actual
from database import (
    buscar_productos,
    obtener_bundle_analitica,
    obtener_categorias_con_productos,
    obtener_datos_publicidad,
    obtener_heatmap_dia_hora,
    obtener_heatmap_hora,
    obtener_interacciones_por_hora,
    obtener_inventario_real,
    obtener_mapa_calor_espacial,
    obtener_perfil_permanencia,
    obtener_productos_por_franja,
    obtener_resumen_ejecutivo_publicidad,
    obtener_serie_temporal_periodo,
    obtener_stats_por_franja,
    obtener_tendencia_diaria,
    obtener_top_productos,
)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])
_FECHA_PAT = r"^\d{4}-\d{2}-\d{2}$"


async def _ejecutar_consulta(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))


# Series temporales y heatmaps (enlazados a horas, días, semanas, meses y sucursal)

@router.get("/serie_temporal")
async def serie_temporal(
    request: Request,
    agrupacion: str = Query("dia", pattern="^(hora|dia|semana|mes)$"),
    dias: int = Query(30, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    return await _ejecutar_consulta(
        obtener_serie_temporal_periodo,
        agrupacion, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )


@router.get("/heatmap/espacial")
async def heatmap_espacial(
    request: Request,
    dias: int = Query(30, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    return await _ejecutar_consulta(
        obtener_mapa_calor_espacial,
        dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )


@router.get("/heatmap/hora")
async def heatmap_hora(
    request: Request,
    dias: int = Query(7, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    data = await _ejecutar_consulta(
        obtener_heatmap_hora, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )
    horas_map = {str(i).zfill(2): 0 for i in range(24)}
    for row in data:
        if row[0] is not None:
            horas_map[str(row[0]).zfill(2)] = row[1]
    return {"labels": list(horas_map.keys()), "data": list(horas_map.values())}


@router.get("/heatmap/matriz")
async def heatmap_matriz(
    request: Request,
    dias: int = Query(30, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    data = await _ejecutar_consulta(
        obtener_heatmap_dia_hora, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )
    matriz = [[0] * 24 for _ in range(7)]
    nombres_dias = ["Dom", "Lun", "Mar", "Mié", "Jue", "Vie", "Sáb"]

    total_semanal = 0
    max_hora_val = -1
    max_hora_dia = ""
    max_hora_txt = ""

    for row in data:
        if row[0] is None or row[1] is None:
            continue
        dia, hora, total = int(row[0]), int(row[1]), int(row[2])
        if 0 <= dia < 7 and 0 <= hora < 24:
            matriz[dia][hora] = total
            total_semanal += total
            if total > max_hora_val:
                max_hora_val = total
                max_hora_dia = nombres_dias[dia]
                max_hora_txt = f"{hora:02d}:00"

    resumen_franjas = []
    totales_dia = []
    for d in range(7):
        m_val = sum(matriz[d][h] for h in range(6, 11))
        md_val = sum(matriz[d][h] for h in range(11, 14))
        t_val = sum(matriz[d][h] for h in range(14, 18))
        n_val = sum(matriz[d][h] for h in range(18, 23))
        mad_val = sum(matriz[d][h] for h in [23, 0, 1, 2, 3, 4, 5])
        tot_dia = sum(matriz[d])
        totales_dia.append(tot_dia)

        resumen_franjas.append({
            "dia": nombres_dias[d],
            "manana": m_val,
            "mediodia": md_val,
            "tarde": t_val,
            "noche": n_val,
            "madrugada": mad_val,
            "total": tot_dia,
        })

    max_dia_idx = max(range(7), key=lambda i: totales_dia[i]) if totales_dia else 0
    dia_pico = {
        "dia": nombres_dias[max_dia_idx],
        "total": totales_dia[max_dia_idx] if totales_dia else 0,
    }
    hora_pico = {
        "hora": max_hora_txt or "00:00",
        "dia": max_hora_dia or "Sin datos",
        "total": max_hora_val if max_hora_val >= 0 else 0,
    }

    return {
        "dias": nombres_dias,
        "horas": [f"{h:02d}:00" for h in range(24)],
        "matriz": matriz,
        "total_semanal": total_semanal,
        "dia_pico": dia_pico,
        "hora_pico": hora_pico,
        "resumen_franjas": resumen_franjas,
    }


@router.get("/heatmap/interacciones")
async def heatmap_interacciones(
    request: Request,
    dias: int = Query(7, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    data = await _ejecutar_consulta(
        obtener_interacciones_por_hora, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )
    resultado = {}
    for hora, producto, total in data:
        resultado.setdefault(hora, []).append({"producto": producto, "total": total})
    return resultado


# Categorías, inventario y búsqueda

@router.get("/inventario")
async def inventario(
    request: Request,
    dias: int = Query(30, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    items = await _ejecutar_consulta(
        obtener_inventario_real, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )
    return {"productos": items}


@router.get("/productos/top")
async def top_productos(
    request: Request,
    limite: int = Query(10, ge=1, le=50),
    dias: int = Query(30, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    data = await _ejecutar_consulta(
        obtener_top_productos, limite, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )
    return {"productos": [{"nombre": p[0], "interacciones": p[1]} for p in data]}


@router.get("/productos/buscar")
async def buscar(request: Request, q: str = Query(..., min_length=1, max_length=100)):
    await obtener_usuario_actual(request)
    data = await asyncio.to_thread(buscar_productos, q)
    return {"resultados": [{"nombre": p[0], "interacciones": p[1]} for p in data]}


@router.get("/categorias")
async def categorias(request: Request, sucursal_id: str | None = Query(None, max_length=32)):
    await obtener_usuario_actual(request)
    data = await asyncio.to_thread(obtener_categorias_con_productos, 90, sucursal_id)
    cats = {}
    for cat, prod, inter in data:
        cats.setdefault(cat, []).append({"producto": prod, "interacciones": inter})
    return {"categorias": cats}


# Horarios y perfiles de permanencia

@router.get("/horarios/franjas")
async def stats_franjas(
    request: Request,
    dias: int = Query(7, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    data = await _ejecutar_consulta(
        obtener_stats_por_franja, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )
    franjas = {
        "manana": {"trafico": 0, "interacciones": 0, "permanencia": 0.0, "conversion": 0.0},
        "tarde": {"trafico": 0, "interacciones": 0, "permanencia": 0.0, "conversion": 0.0},
        "noche": {"trafico": 0, "interacciones": 0, "permanencia": 0.0, "conversion": 0.0},
    }
    for franja, tipo, total, prom in data:
        if franja not in franjas:
            continue
        if tipo == "trafico":
            franjas[franja]["trafico"] = int(total)
        elif tipo == "interaccion":
            franjas[franja]["interacciones"] = int(total)
        elif tipo == "permanencia":
            franjas[franja]["permanencia"] = round(float(prom), 1)

    total_trafico = sum(v["trafico"] for v in franjas.values())
    for v in franjas.values():
        v["conversion"] = min(100.0, round((v["interacciones"] / v["trafico"]) * 100.0, 1)) if v["trafico"] > 0 else 0.0

    if total_trafico > 0:
        porcentajes = {
            "Mañana": round((franjas["manana"]["trafico"] / total_trafico) * 100, 1),
            "Tarde": round((franjas["tarde"]["trafico"] / total_trafico) * 100, 1),
            "Noche": round((franjas["noche"]["trafico"] / total_trafico) * 100, 1),
        }
    else:
        porcentajes = {"Mañana": 0.0, "Tarde": 0.0, "Noche": 0.0}

    franjas["porcentajes"] = porcentajes
    return franjas


@router.get("/horarios/productos")
async def productos_por_franja(
    request: Request,
    dias: int = Query(7, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    data = await _ejecutar_consulta(
        obtener_productos_por_franja, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )
    resultado = {"manana": [], "tarde": [], "noche": []}
    for franja, producto, interacciones in data:
        if franja in resultado:
            resultado[franja].append({"producto": producto, "interacciones": interacciones})
    return resultado


@router.get("/perfiles")
async def perfiles(
    request: Request,
    dias: int = Query(7, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    data = await _ejecutar_consulta(
        obtener_perfil_permanencia, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )
    return {"perfiles": [{"tipo": p[0], "cantidad": p[1], "promedio_seg": p[2]} for p in data]}


@router.get("/tendencia")
async def tendencia(
    request: Request,
    dias: int = Query(14, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    data = await _ejecutar_consulta(
        obtener_tendencia_diaria, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )
    return {
        "fechas": [r[0] for r in data],
        "trafico": [r[1] for r in data],
        "interacciones": [r[2] for r in data],
        "permanencia": [r[3] or 0 for r in data],
    }


# Publicidad dinámica y resumen ejecutivo

@router.get("/publicidad")
async def publicidad(
    request: Request,
    franja: str | None = Query(None, pattern="^(manana|tarde|noche)$"),
    dias: int = Query(7, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    data = await _ejecutar_consulta(
        obtener_datos_publicidad, franja, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )

    sugerencias = []
    if data["trafico"] > 0:
        total_inter = sum(p["interacciones"] for p in data["top_productos"])
        tasa = (total_inter / data["trafico"]) * 100 if total_inter else 0

        nombre_franja = {"manana": "mañana", "tarde": "tarde", "noche": "noche"}.get(
            data["franja"], "todo el día"
        )

        if tasa < 10:
            sugerencias.append({
                "tipo": "urgente",
                "icono": "bi-exclamation-triangle-fill",
                "titulo": f"Baja conversión en {nombre_franja}",
                "descripcion": (
                    f"Conversión de {tasa:.1f}%. "
                    "Conviene probar Reels cortos (<15 s) geolocalizados a 2 km del local."
                ),
                "presupuesto": "$40-80 USD / 2 semanas",
            })

        if data["permanencia_promedio"] > 20 and tasa < 15:
            sugerencias.append({
                "tipo": "oportunidad",
                "icono": "bi-lightbulb-fill",
                "titulo": "Alta permanencia, baja conversión",
                "descripcion": (
                    f"Clientes permanecen {data['permanencia_promedio']}s sin concretar toma. "
                    "Evaluar cupón digital o ajuste de señalización en punto de venta."
                ),
                "presupuesto": "$30-60 USD / semana",
            })

        for prod in data["top_productos"][:3]:
            sugerencias.append({
                "tipo": "impulso",
                "icono": "bi-graph-up-arrow",
                "titulo": f"Potenciar: {prod['producto']}",
                "descripcion": (
                    f"{prod['interacciones']} interacciones registradas en {nombre_franja}. "
                    "Alto interés: candidato para campaña de Meta Ads con foto de percha."
                ),
                "presupuesto": "$25-50 USD / semana",
            })

        if data["permanencia_promedio"] < 5 and data["trafico"] > 20:
            sugerencias.append({
                "tipo": "alerta",
                "icono": "bi-clock-history",
                "titulo": "Tránsito rápido sin detención",
                "descripcion": (
                    "Los clientes pasan sin detenerse. "
                    "Revisar señalización, iluminación o altura de productos estrella."
                ),
                "presupuesto": "Inversión en PDV",
            })

    data["sugerencias"] = sugerencias
    return data


@router.get("/bundle")
async def bundle_analitica(
    request: Request,
    agrupacion: str = Query("semana", pattern="^(hora|dia|semana|mes)$"),
    dias: int = Query(30, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    return await _ejecutar_consulta(
        obtener_bundle_analitica,
        agrupacion, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )


@router.get("/resumen_publicidad")
async def resumen_publicidad_ejecutivo(
    request: Request,
    dias: int = Query(30, ge=1, le=365),
    horas: int | None = Query(None, ge=1, le=168),
    fecha_inicio: str | None = Query(None, pattern=_FECHA_PAT),
    fecha_fin: str | None = Query(None, pattern=_FECHA_PAT),
    hora_inicio: int | None = Query(None, ge=0, le=23),
    hora_fin: int | None = Query(None, ge=0, le=23),
    sucursal_id: str | None = Query(None, max_length=32),
):
    await obtener_usuario_actual(request)
    exec_data = await _ejecutar_consulta(
        obtener_resumen_ejecutivo_publicidad, dias, fecha_inicio, fecha_fin,
        horas=horas, hora_inicio=hora_inicio, hora_fin=hora_fin, sucursal_id=sucursal_id,
    )
    exec_data["motor_marketing_ai"] = KovaqMarketingAnalyticsEngine.generar_diagnostico_integral(exec_data)
    return exec_data
