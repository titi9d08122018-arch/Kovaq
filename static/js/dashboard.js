(function () {
    "use strict";

    // Estado global del dashboard (enlazado a sucursal, período, horario y fechas con datos)
    let moduloActivo = "tab-vivo";
    let filtroTemporal = {
        sucursal_id: "SUC-NORTE",
        agrupacion: "semana",
        dias: 30,
        horas: null,
        fecha_inicio: "",
        fecha_fin: "",
        hora_inicio: "",
        hora_fin: "",
    };
    let capasMapa = { calor: true, fov: true, vectores: true };
    let modoVistaMatriz = "franjas";
    let datosMatrizGlobal = null;
    let ultimoEstadoMapa = null;
    let categoriaActiva = "Todas";
    let lastSeenEventId = 0;

    let chartSerieTemporal = null;
    let chartHeatmapHora = null;
    let chartFranjasDonut = null;

    // Estado del calendario inteligente (solo meses y días con registros reales)
    const MESES_ES = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ];
    let fechasConDatosMap = new Map();
    let calMinFecha = "";
    let calMaxFecha = "";
    let calMesActual = { year: new Date().getFullYear(), month: new Date().getMonth() };
    let calDraftInicio = "";
    let calDraftFin = "";

    // Caché SWR en cliente (20s TTL) para cambios instantáneos entre sucursales y períodos
    const SWR_TTL_MS = 20000;
    const bundleCache = new Map();
    const heatCache = new Map();

    // Control de concurrencia cancelable por canal
    const controllers = { bundle: null, mapa: null, live: null, heat: null, inv: null };
    const reqSeq = { bundle: 0, mapa: 0, heat: 0, inv: 0 };

    // Animaciones suaves cancelables de KPIs y vectores del mapa 2D
    const kpiAnimFrames = new Map();
    const kpiValoresPrevios = new Map();
    const personasSmooth = new Map();

    // Datos reales de trackeo YOLOv8n cuadro a cuadro para el video demo
    let demoTracks = null;
    let liveVideoPersonas = [];
    let liveTelemetriaBackend = null;
    let rafHudId = null;

    // Offscreen canvas y LUT térmico (azul frío -> cian -> amarillo -> naranja -> rojo caliente)
    const heatOffscreen = document.createElement("canvas");
    heatOffscreen.width = 480;
    heatOffscreen.height = 240;
    const heatLUT = construirLutTermico();

    function prefiereMovimientoReducido() {
        return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    }

    function invalidarCaches() {
        bundleCache.clear();
        heatCache.clear();
    }

    function construirLutTermico() {
        const lut = new Uint8ClampedArray(256 * 4);
        const stops = [
            [0.0, 37, 99, 235, 0],
            [0.18, 37, 99, 235, 115],
            [0.42, 6, 182, 212, 155],
            [0.65, 250, 204, 21, 185],
            [0.84, 249, 115, 22, 215],
            [1.0, 220, 38, 38, 235],
        ];
        for (let i = 0; i < 256; i++) {
            const t = i / 255;
            let s0 = stops[0];
            let s1 = stops[stops.length - 1];
            for (let k = 0; k < stops.length - 1; k++) {
                if (t >= stops[k][0] && t <= stops[k + 1][0]) {
                    s0 = stops[k];
                    s1 = stops[k + 1];
                    break;
                }
            }
            const span = Math.max(0.0001, s1[0] - s0[0]);
            const f = (t - s0[0]) / span;
            lut[i * 4] = Math.round(s0[1] + (s1[1] - s0[1]) * f);
            lut[i * 4 + 1] = Math.round(s0[2] + (s1[2] - s0[2]) * f);
            lut[i * 4 + 2] = Math.round(s0[3] + (s1[3] - s0[3]) * f);
            lut[i * 4 + 3] = Math.round(s0[4] + (s1[4] - s0[4]) * f);
        }
        return lut;
    }

    function fetchCancelable(canal, url) {
        if (controllers[canal]) {
            try {
                controllers[canal].abort();
            } catch (_e) {
                // ignore
            }
        }
        const ctrl = new AbortController();
        controllers[canal] = ctrl;
        reqSeq[canal] = (reqSeq[canal] || 0) + 1;
        const mySeq = reqSeq[canal];
        return fetch(url, { signal: ctrl.signal }).then((res) => ({ res, seq: mySeq }));
    }

    function setCargandoToolbar(activo) {
        const track = document.getElementById("toolbar-progress-track");
        if (track) track.classList.toggle("loading", Boolean(activo));
    }

    function mostrarAviso(bannerId, mensaje, tipo = "info") {
        const el = document.getElementById(bannerId);
        if (!el) return;
        const estilos = {
            ok: "bg-emerald-50 border-emerald-300 text-emerald-900",
            error: "bg-red-50 border-red-300 text-red-900",
            info: "bg-slate-50 border-kovaq-border text-kovaq-navy",
        };
        el.className = `text-xs font-semibold px-3.5 py-2 rounded-lg border ${estilos[tipo] || estilos.info}`;
        el.textContent = mensaje;
    }

    function ocultarAviso(bannerId) {
        const el = document.getElementById(bannerId);
        if (el) el.classList.add("hidden");
    }

    function marcarEstadoCarga(texto) {
        const el = document.getElementById("indicador-carga-datos");
        if (el) el.textContent = texto;
    }

    function animarNumero(elId, valorDestino, formateador) {
        const el = document.getElementById(elId);
        if (!el) return;
        const destino = Number.isFinite(Number(valorDestino)) ? Number(valorDestino) : 0;
        const prevFrame = kpiAnimFrames.get(elId);
        if (prevFrame) {
            window.cancelAnimationFrame(prevFrame);
            kpiAnimFrames.delete(elId);
        }

        const inicio = kpiValoresPrevios.has(elId) ? kpiValoresPrevios.get(elId) : destino;
        kpiValoresPrevios.set(elId, destino);

        if (prefiereMovimientoReducido() || Math.abs(destino - inicio) < 0.01) {
            el.textContent = formateador(destino);
            return;
        }

        const duracion = 220;
        const t0 = performance.now();
        function paso(now) {
            const p = Math.min(1, (now - t0) / duracion);
            const eased = 1 - Math.pow(1 - p, 3);
            const actual = inicio + (destino - inicio) * eased;
            el.textContent = formateador(actual);
            if (p < 1) {
                kpiAnimFrames.set(elId, window.requestAnimationFrame(paso));
            } else {
                kpiAnimFrames.delete(elId);
                el.textContent = formateador(destino);
            }
        }
        kpiAnimFrames.set(elId, window.requestAnimationFrame(paso));
    }

    function construirQueryTemporal(extra = {}) {
        const params = new URLSearchParams();
        if (filtroTemporal.sucursal_id) {
            params.set("sucursal_id", filtroTemporal.sucursal_id);
        }
        if (filtroTemporal.horas !== null && filtroTemporal.horas !== undefined && !filtroTemporal.fecha_inicio) {
            params.set("horas", String(filtroTemporal.horas));
        } else {
            params.set("dias", String(filtroTemporal.dias || 30));
        }
        if (filtroTemporal.fecha_inicio) params.set("fecha_inicio", filtroTemporal.fecha_inicio);
        if (filtroTemporal.fecha_fin) params.set("fecha_fin", filtroTemporal.fecha_fin);
        if (filtroTemporal.hora_inicio !== "") params.set("hora_inicio", String(filtroTemporal.hora_inicio));
        if (filtroTemporal.hora_fin !== "") params.set("hora_fin", String(filtroTemporal.hora_fin));
        Object.entries(extra).forEach(([k, v]) => {
            if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
        });
        return params.toString();
    }

    function sucursalEfectivaEscritura() {
        return filtroTemporal.sucursal_id && filtroTemporal.sucursal_id !== "todas"
            ? filtroTemporal.sucursal_id
            : "SUC-NORTE";
    }

    // =========================================================================
    // GESTIÓN DE POPOVERS FLOTANTES (Calendario y Capas del Mapa)
    // =========================================================================
    function cerrarPopover(popoverId, triggerId) {
        const pop = document.getElementById(popoverId);
        const btn = document.getElementById(triggerId);
        if (pop) pop.classList.add("hidden");
        if (btn) btn.setAttribute("aria-expanded", "false");
    }

    function abrirPopover(popoverId, triggerId) {
        if (popoverId !== "popover-calendario") cerrarPopover("popover-calendario", "btn-abrir-calendario");
        if (popoverId !== "popover-capas") cerrarPopover("popover-capas", "btn-menu-capas");
        const pop = document.getElementById(popoverId);
        const btn = document.getElementById(triggerId);
        if (!pop || !btn) return;
        const estabaOculto = pop.classList.contains("hidden");
        pop.classList.toggle("hidden", !estabaOculto);
        btn.setAttribute("aria-expanded", estabaOculto ? "true" : "false");
        if (estabaOculto && popoverId === "popover-calendario") {
            calDraftInicio = filtroTemporal.fecha_inicio || "";
            calDraftFin = filtroTemporal.fecha_fin || "";
            renderCalendarioDisponible();
        }
    }

    function inicializarCierreGlobalPopovers() {
        // Evitar que clics internos (especialmente al re-renderizar días del calendario) cierren el popover
        ["popover-calendario", "popover-capas"].forEach((id) => {
            const pop = document.getElementById(id);
            if (pop) {
                pop.addEventListener("click", (ev) => ev.stopPropagation());
            }
        });

        document.addEventListener("click", (ev) => {
            const path = typeof ev.composedPath === "function" ? ev.composedPath() : [];
            const calPop = document.getElementById("popover-calendario");
            const calBtn = document.getElementById("btn-abrir-calendario");
            if (calPop && !calPop.classList.contains("hidden")) {
                const dentroCal = path.includes(calPop) || calPop.contains(ev.target);
                const enBtnCal = calBtn && (path.includes(calBtn) || calBtn.contains(ev.target));
                if (!dentroCal && !enBtnCal) {
                    cerrarPopover("popover-calendario", "btn-abrir-calendario");
                }
            }
            const capPop = document.getElementById("popover-capas");
            const capBtn = document.getElementById("btn-menu-capas");
            if (capPop && !capPop.classList.contains("hidden")) {
                const dentroCap = path.includes(capPop) || capPop.contains(ev.target);
                const enBtnCap = capBtn && (path.includes(capBtn) || capBtn.contains(ev.target));
                if (!dentroCap && !enBtnCap) {
                    cerrarPopover("popover-capas", "btn-menu-capas");
                }
            }
        });

        document.addEventListener("keydown", (ev) => {
            if (ev.key !== "Escape") return;
            const calPop = document.getElementById("popover-calendario");
            if (calPop && !calPop.classList.contains("hidden")) {
                cerrarPopover("popover-calendario", "btn-abrir-calendario");
                document.getElementById("btn-abrir-calendario")?.focus();
            }
            const capPop = document.getElementById("popover-capas");
            if (capPop && !capPop.classList.contains("hidden")) {
                cerrarPopover("popover-capas", "btn-menu-capas");
                document.getElementById("btn-menu-capas")?.focus();
            }
        });
    }

    // =========================================================================
    // CALENDARIO INTELIGENTE RESTRINGIDO A FECHAS CON DATOS REGISTRADOS
    // =========================================================================
    function parseIsoYM(iso) {
        if (!iso || iso.length < 7) return null;
        const y = parseInt(iso.slice(0, 4), 10);
        const m = parseInt(iso.slice(5, 7), 10) - 1;
        if (!Number.isFinite(y) || !Number.isFinite(m)) return null;
        return { year: y, month: m, ym: y * 12 + m };
    }

    function actualizarFechasDisponibles(info) {
        if (!info || !Array.isArray(info.fechas)) return;
        fechasConDatosMap = new Map();
        info.fechas.forEach((f) => {
            if (f && f.fecha) fechasConDatosMap.set(f.fecha, Number(f.visitas || 0));
        });
        calMinFecha = info.min_fecha || "";
        calMaxFecha = info.max_fecha || "";

        const minObj = parseIsoYM(calMinFecha);
        const maxObj = parseIsoYM(calMaxFecha);
        if (minObj && maxObj) {
            const currYM = calMesActual.year * 12 + calMesActual.month;
            if (currYM < minObj.ym || currYM > maxObj.ym) {
                calMesActual = { year: maxObj.year, month: maxObj.month };
            }
        }

        const rangoLbl = document.getElementById("cal-rango-disponible");
        if (rangoLbl) {
            if (calMinFecha && calMaxFecha) {
                rangoLbl.textContent = `${info.total_dias || fechasConDatosMap.size} días registrados (${calMinFecha} al ${calMaxFecha})`;
            } else {
                rangoLbl.textContent = "Sin fechas con registros en esta sucursal";
            }
        }

        const pop = document.getElementById("popover-calendario");
        if (pop && !pop.classList.contains("hidden")) {
            renderCalendarioDisponible();
        }
    }

    function renderCalendarioDisponible() {
        const grid = document.getElementById("cal-dias-grid");
        const titulo = document.getElementById("cal-mes-titulo");
        const btnPrev = document.getElementById("btn-cal-prev");
        const btnNext = document.getElementById("btn-cal-next");
        const selTxt = document.getElementById("cal-seleccion-texto");
        if (!grid) return;

        const minObj = parseIsoYM(calMinFecha);
        const maxObj = parseIsoYM(calMaxFecha);
        if (minObj && maxObj) {
            const currYM = calMesActual.year * 12 + calMesActual.month;
            if (currYM < minObj.ym) calMesActual = { year: minObj.year, month: minObj.month };
            if (currYM > maxObj.ym) calMesActual = { year: maxObj.year, month: maxObj.month };
        }

        const { year, month } = calMesActual;
        const currYM = year * 12 + month;

        if (titulo) titulo.textContent = `${MESES_ES[month] || ""} ${year}`;
        if (btnPrev) btnPrev.disabled = !minObj || currYM <= minObj.ym;
        if (btnNext) btnNext.disabled = !maxObj || currYM >= maxObj.ym;

        grid.textContent = "";
        const primerDia = new Date(year, month, 1);
        const offsetLunes = (primerDia.getDay() + 6) % 7;
        const totalDiasMes = new Date(year, month + 1, 0).getDate();

        for (let i = 0; i < offsetLunes; i++) {
            const vacio = document.createElement("span");
            vacio.setAttribute("aria-hidden", "true");
            grid.appendChild(vacio);
        }

        for (let d = 1; d <= totalDiasMes; d++) {
            const iso = `${year}-${String(month + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
            const visitas = fechasConDatosMap.get(iso) || 0;
            const tieneDatos = visitas > 0;

            const btn = document.createElement("button");
            btn.type = "button";
            btn.textContent = String(d);
            btn.className = "cal-day";

            if (tieneDatos) {
                btn.classList.add("has-data");
                btn.title = `${iso}: ${visitas} registros`;
                btn.setAttribute("aria-label", `${iso}, ${visitas} registros`);

                const ini = calDraftInicio;
                const fin = calDraftFin || calDraftInicio;
                if (ini && (iso === ini || iso === fin)) {
                    btn.classList.add("selected-edge");
                    btn.setAttribute("aria-pressed", "true");
                } else if (ini && fin && iso > ini && iso < fin) {
                    btn.classList.add("in-range");
                }

                btn.addEventListener("click", (ev) => {
                    ev.stopPropagation();
                    if (!calDraftInicio || (calDraftInicio && calDraftFin)) {
                        calDraftInicio = iso;
                        calDraftFin = "";
                    } else if (iso < calDraftInicio) {
                        calDraftFin = calDraftInicio;
                        calDraftInicio = iso;
                    } else {
                        calDraftFin = iso;
                    }
                    renderCalendarioDisponible();
                });
            } else {
                btn.disabled = true;
                btn.setAttribute("aria-label", `${iso}, sin datos registrados`);
            }

            grid.appendChild(btn);
        }

        if (selTxt) {
            if (calDraftInicio && calDraftFin && calDraftFin !== calDraftInicio) {
                selTxt.textContent = `${calDraftInicio} a ${calDraftFin}`;
            } else if (calDraftInicio) {
                selTxt.textContent = `Día: ${calDraftInicio}`;
            } else {
                selTxt.textContent = "Elige 1 día o rango";
            }
        }
    }

    // =========================================================================
    // NAVEGACIÓN ARIA TABLIST (clic + teclado)
    // =========================================================================
    function cambiarModulo(moduloId) {
        const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
        const panes = Array.from(document.querySelectorAll('[role="tabpanel"]'));

        panes.forEach((pane) => pane.classList.toggle("active", pane.id === moduloId));
        tabs.forEach((tab) => {
            const esActivo = tab.getAttribute("aria-controls") === moduloId;
            tab.classList.toggle("active", esActivo);
            tab.setAttribute("aria-selected", esActivo ? "true" : "false");
            tab.setAttribute("tabindex", esActivo ? "0" : "-1");
        });

        moduloActivo = moduloId;

        if (moduloId === "tab-vivo") {
            if (ultimoEstadoMapa) dibujarPlanoTienda2D(ultimoEstadoMapa);
            else cargarBundleDashboard({ silencioso: false });
        } else if (moduloId === "tab-percha") {
            cargarHeatmaps();
        } else if (moduloId === "tab-inventario") {
            if (!document.querySelector(".item-inventario")) cargarInventarioReal();
        } else if (moduloId === "tab-marketing") {
            if (!ultimoEstadoMapa) cargarBundleDashboard({ silencioso: false });
        } else if (moduloId === "tab-config") {
            cargarConfiguracionMapa();
        }
    }

    function inicializarTabs() {
        const tablist = document.querySelector('[role="tablist"]');
        if (!tablist) return;
        const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));

        tabs.forEach((tab, idx) => {
            tab.addEventListener("click", () => cambiarModulo(tab.getAttribute("aria-controls")));
            tab.addEventListener("keydown", (ev) => {
                let nextIdx = null;
                if (ev.key === "ArrowRight") nextIdx = (idx + 1) % tabs.length;
                else if (ev.key === "ArrowLeft") nextIdx = (idx - 1 + tabs.length) % tabs.length;
                else if (ev.key === "Home") nextIdx = 0;
                else if (ev.key === "End") nextIdx = tabs.length - 1;
                if (nextIdx !== null) {
                    ev.preventDefault();
                    tabs[nextIdx].focus();
                    cambiarModulo(tabs[nextIdx].getAttribute("aria-controls"));
                }
            });
        });
    }

    // =========================================================================
    // ETIQUETAS Y REFRESCO CON CACHÉ SWR + BARRA DE PROGRESO SUTIL
    // =========================================================================
    function actualizarEtiquetasPeriodo() {
        const mapNombre = { hora: "Hora", dia: "Día", semana: "Semana", mes: "Mes" };
        const agrupTxt = mapNombre[filtroTemporal.agrupacion] || "Semana";
        const lblChart = document.getElementById("lbl-agrupacion-chart");
        if (lblChart) lblChart.textContent = agrupTxt;

        const selSuc = document.getElementById("select-sucursal-global");
        const nombreSuc = selSuc ? selSuc.options[selSuc.selectedIndex]?.text || filtroTemporal.sucursal_id : filtroTemporal.sucursal_id;

        let textoRango = "";
        if (filtroTemporal.fecha_inicio) {
            textoRango = filtroTemporal.fecha_fin && filtroTemporal.fecha_fin !== filtroTemporal.fecha_inicio
                ? `${filtroTemporal.fecha_inicio} a ${filtroTemporal.fecha_fin}`
                : filtroTemporal.fecha_inicio;
        } else if (filtroTemporal.horas !== null) {
            textoRango = filtroTemporal.horas === 1 ? "Última 1 hora" : `Últimas ${filtroTemporal.horas} horas`;
        } else {
            textoRango = `Últimos ${filtroTemporal.dias} días (${agrupTxt})`;
        }

        let textoHorario = "Todo el día";
        if (filtroTemporal.hora_inicio !== "" || filtroTemporal.hora_fin !== "") {
            const hi = filtroTemporal.hora_inicio !== "" ? String(filtroTemporal.hora_inicio).padStart(2, "0") : "00";
            const hf = filtroTemporal.hora_fin !== "" ? String(filtroTemporal.hora_fin).padStart(2, "0") : "23";
            textoHorario = `${hi}:00 a ${hf}:59`;
        }

        const sub = document.getElementById("subtitulo-periodo-activo");
        if (sub) sub.textContent = `${nombreSuc} • ${textoRango} • ${textoHorario}`;

        const badgePercha = document.getElementById("badge-rango-fechas-percha");
        if (badgePercha) badgePercha.textContent = `${textoRango} • ${textoHorario}`;

        const lblCal = document.getElementById("lbl-boton-calendario");
        if (lblCal) {
            if (filtroTemporal.fecha_inicio) {
                lblCal.textContent = filtroTemporal.fecha_fin && filtroTemporal.fecha_fin !== filtroTemporal.fecha_inicio
                    ? `${filtroTemporal.fecha_inicio} a ${filtroTemporal.fecha_fin}`
                    : filtroTemporal.fecha_inicio;
            } else {
                lblCal.textContent = "Elegir fechas";
            }
        }
    }

    function aplicarBundleDashboard(b) {
        if (!b) return;
        if (b.fechas_disponibles) actualizarFechasDisponibles(b.fechas_disponibles);
        if (b.stats) aplicarDatosStats(b.stats);
        if (b.resumen_publicidad) aplicarResumenPublicidad(b.resumen_publicidad);
        if (Array.isArray(b.inventario)) renderTablaInventario(b.inventario);
        if (b.map_state) {
            actualizarSelectorCamaras(b.map_state.camaras || [], b.map_state.camara_activa);
            dibujarPlanoTienda2D(b.map_state);
            actualizarPanelesReID(b.map_state);
        }
    }

    async function refrescarDashboardsPorPeriodo(opciones = {}) {
        const silencioso = Boolean(opciones.silencioso);
        const forzar = Boolean(opciones.forzar);
        if (!silencioso) setCargandoToolbar(true);

        const tareas = [cargarBundleDashboard({ silencioso, forzar })];
        if (moduloActivo === "tab-percha") tareas.push(cargarHeatmaps({ forzar }));
        if (moduloActivo === "tab-config") tareas.push(cargarConfiguracionMapa());

        await Promise.allSettled(tareas);

        if (!silencioso) setCargandoToolbar(false);
        const ahora = new Date();
        const hh = String(ahora.getHours()).padStart(2, "0");
        const mm = String(ahora.getMinutes()).padStart(2, "0");
        const ss = String(ahora.getSeconds()).padStart(2, "0");
        marcarEstadoCarga(`Actualizado ${hh}:${mm}:${ss}`);
    }

    async function cargarBundleDashboard(opciones = {}) {
        const silencioso = Boolean(opciones.silencioso);
        const forzar = Boolean(opciones.forzar);
        const q = construirQueryTemporal();
        const cached = bundleCache.get(q);

        // Respuesta instantánea desde caché SWR mientras se revalida en segundo plano
        if (!forzar && cached && (Date.now() - cached.ts) < SWR_TTL_MS) {
            aplicarBundleDashboard(cached.data);
            if ((Date.now() - cached.ts) < 5000 && !silencioso) {
                return;
            }
        }

        try {
            const { res, seq } = await fetchCancelable("bundle", `/api/dashboard_bundle?${q}`);
            if (seq !== reqSeq.bundle) return;
            if (!res.ok) {
                if (!silencioso) {
                    mostrarAviso("global-status-banner", "No se pudieron actualizar los datos en este momento. Se reintentará automáticamente.", "error");
                }
                return;
            }
            const b = await res.json();
            if (seq !== reqSeq.bundle) return;

            bundleCache.set(q, { ts: Date.now(), data: b });
            ocultarAviso("global-status-banner");
            aplicarBundleDashboard(b);
        } catch (err) {
            if (err && err.name === "AbortError") return;
            if (!silencioso) {
                mostrarAviso("global-status-banner", "Conexión interrumpida temporalmente. Manteniendo últimos datos visibles.", "error");
            }
        }
    }

    function inicializarFiltrosTemporalesYSucursal() {
        const selSuc = document.getElementById("select-sucursal-global");
        if (selSuc) {
            selSuc.addEventListener("change", () => {
                filtroTemporal.sucursal_id = selSuc.value;
                actualizarEtiquetasPeriodo();
                refrescarDashboardsPorPeriodo({ silencioso: false });
            });
        }

        // Selector desplegable de período
        const selPeriodo = document.getElementById("select-periodo-preset");
        if (selPeriodo) {
            selPeriodo.addEventListener("change", () => {
                const mapaPresets = {
                    "1h": { agrup: "hora", horas: 1, dias: 1 },
                    "6h": { agrup: "hora", horas: 6, dias: 1 },
                    "24h": { agrup: "hora", horas: 24, dias: 1 },
                    "7d": { agrup: "dia", horas: null, dias: 7 },
                    "30d": { agrup: "semana", horas: null, dias: 30 },
                    "90d": { agrup: "mes", horas: null, dias: 90 },
                };
                const cfg = mapaPresets[selPeriodo.value] || mapaPresets["30d"];
                filtroTemporal.agrupacion = cfg.agrup;
                filtroTemporal.horas = cfg.horas;
                filtroTemporal.dias = cfg.dias;
                filtroTemporal.fecha_inicio = "";
                filtroTemporal.fecha_fin = "";
                calDraftInicio = "";
                calDraftFin = "";

                const selAgrup = document.getElementById("select-agrupacion-temporal");
                if (selAgrup) selAgrup.value = filtroTemporal.agrupacion;

                actualizarEtiquetasPeriodo();
                refrescarDashboardsPorPeriodo({ silencioso: false });
            });
        }

        // Selector desplegable de franja horaria + panel personalizado bajo demanda
        const selHorario = document.getElementById("select-horario-preset");
        const panelPers = document.getElementById("panel-horario-personalizado");
        const hiSel = document.getElementById("filtro-hora-inicio");
        const hfSel = document.getElementById("filtro-hora-fin");

        if (selHorario) {
            selHorario.addEventListener("change", () => {
                const v = selHorario.value;
                const franjas = {
                    todo: ["", ""],
                    manana: ["6", "11"],
                    mediodia: ["11", "14"],
                    tarde: ["14", "18"],
                    noche: ["18", "23"],
                };
                if (v === "personalizado") {
                    if (panelPers) panelPers.classList.remove("hidden");
                    hiSel?.focus();
                    return;
                }
                if (panelPers) panelPers.classList.add("hidden");
                const [hi, hf] = franjas[v] || ["", ""];
                filtroTemporal.hora_inicio = hi;
                filtroTemporal.hora_fin = hf;
                if (hiSel) hiSel.value = hi;
                if (hfSel) hfSel.value = hf;
                actualizarEtiquetasPeriodo();
                refrescarDashboardsPorPeriodo({ silencioso: false });
            });
        }

        [hiSel, hfSel].forEach((sel) => {
            if (!sel) return;
            sel.addEventListener("change", () => {
                filtroTemporal.hora_inicio = hiSel ? hiSel.value : "";
                filtroTemporal.hora_fin = hfSel ? hfSel.value : "";
                actualizarEtiquetasPeriodo();
                refrescarDashboardsPorPeriodo({ silencioso: false });
            });
        });

        const btnResetHorario = document.getElementById("btn-reset-horario");
        if (btnResetHorario) {
            btnResetHorario.addEventListener("click", () => {
                filtroTemporal.hora_inicio = "";
                filtroTemporal.hora_fin = "";
                if (hiSel) hiSel.value = "";
                if (hfSel) hfSel.value = "";
                if (selHorario) selHorario.value = "todo";
                if (panelPers) panelPers.classList.add("hidden");
                actualizarEtiquetasPeriodo();
                refrescarDashboardsPorPeriodo({ silencioso: false });
            });
        }

        // Controles del popover del calendario inteligente
        const btnAbrirCal = document.getElementById("btn-abrir-calendario");
        if (btnAbrirCal) {
            btnAbrirCal.addEventListener("click", () => abrirPopover("popover-calendario", "btn-abrir-calendario"));
        }
        const btnCerrarCal = document.getElementById("btn-cerrar-calendario");
        if (btnCerrarCal) {
            btnCerrarCal.addEventListener("click", () => cerrarPopover("popover-calendario", "btn-abrir-calendario"));
        }

        const btnPrev = document.getElementById("btn-cal-prev");
        if (btnPrev) {
            btnPrev.addEventListener("click", () => {
                const m = calMesActual.month - 1;
                calMesActual = m < 0 ? { year: calMesActual.year - 1, month: 11 } : { year: calMesActual.year, month: m };
                renderCalendarioDisponible();
            });
        }
        const btnNext = document.getElementById("btn-cal-next");
        if (btnNext) {
            btnNext.addEventListener("click", () => {
                const m = calMesActual.month + 1;
                calMesActual = m > 11 ? { year: calMesActual.year + 1, month: 0 } : { year: calMesActual.year, month: m };
                renderCalendarioDisponible();
            });
        }

        const btnRango = document.getElementById("btn-aplicar-rango");
        if (btnRango) {
            btnRango.addEventListener("click", () => {
                if (!calDraftInicio) {
                    const selTxt = document.getElementById("cal-seleccion-texto");
                    if (selTxt) selTxt.textContent = "Elige al menos 1 día con datos";
                    return;
                }
                const fi = calDraftInicio;
                const ff = calDraftFin || calDraftInicio;
                filtroTemporal.fecha_inicio = fi;
                filtroTemporal.fecha_fin = ff;
                filtroTemporal.horas = null;

                // Ajustar agrupación óptima según longitud del rango elegido
                const msDiff = Math.max(0, new Date(ff) - new Date(fi));
                const diasDiff = Math.round(msDiff / 86400000);
                filtroTemporal.agrupacion = diasDiff === 0 ? "hora" : (diasDiff <= 14 ? "dia" : "semana");
                const selAgrup = document.getElementById("select-agrupacion-temporal");
                if (selAgrup) selAgrup.value = filtroTemporal.agrupacion;

                cerrarPopover("popover-calendario", "btn-abrir-calendario");
                actualizarEtiquetasPeriodo();
                refrescarDashboardsPorPeriodo({ silencioso: false });
            });
        }

        const btnLimpiar = document.getElementById("btn-limpiar-filtros");
        if (btnLimpiar) {
            btnLimpiar.addEventListener("click", () => {
                filtroTemporal.agrupacion = "semana";
                filtroTemporal.dias = 30;
                filtroTemporal.horas = null;
                filtroTemporal.fecha_inicio = "";
                filtroTemporal.fecha_fin = "";
                filtroTemporal.hora_inicio = "";
                filtroTemporal.hora_fin = "";
                calDraftInicio = "";
                calDraftFin = "";

                if (selPeriodo) selPeriodo.value = "30d";
                if (selHorario) selHorario.value = "todo";
                if (hiSel) hiSel.value = "";
                if (hfSel) hfSel.value = "";
                if (panelPers) panelPers.classList.add("hidden");
                const selAgrup = document.getElementById("select-agrupacion-temporal");
                if (selAgrup) selAgrup.value = "semana";

                cerrarPopover("popover-calendario", "btn-abrir-calendario");
                actualizarEtiquetasPeriodo();
                refrescarDashboardsPorPeriodo({ silencioso: false });
            });
        }

        const selAgrup = document.getElementById("select-agrupacion-temporal");
        if (selAgrup) {
            selAgrup.addEventListener("change", () => {
                filtroTemporal.agrupacion = selAgrup.value;
                actualizarEtiquetasPeriodo();
                cargarHeatmaps();
            });
        }
    }

    async function cargarSucursales() {
        try {
            const res = await fetch("/api/branches");
            if (!res.ok) return;
            const data = await res.json();
            const lista = data.sucursales || [];
            poblarSelectorSucursales(lista);
            renderTablaSucursales(lista);
        } catch (_e) {
            // Silencioso
        }
    }

    function poblarSelectorSucursales(lista) {
        const sel = document.getElementById("select-sucursal-global");
        if (!sel || lista.length === 0) return;
        const prev = filtroTemporal.sucursal_id || "SUC-NORTE";
        sel.textContent = "";
        lista.forEach((s) => {
            const opt = document.createElement("option");
            opt.value = s.id;
            const nombreCorto = String(s.nombre || s.id).split("•")[0].trim();
            opt.textContent = `${nombreCorto} (${s.ciudad})`;
            sel.appendChild(opt);
        });
        const optAll = document.createElement("option");
        optAll.value = "todas";
        optAll.textContent = "Todas las sucursales (Consolidado)";
        sel.appendChild(optAll);
        sel.value = Array.from(sel.options).some((o) => o.value === prev) ? prev : lista[0].id;
        filtroTemporal.sucursal_id = sel.value;
        actualizarEtiquetasPeriodo();
    }

    function renderTablaSucursales(lista) {
        const tbody = document.getElementById("tabla-sucursales-body");
        if (!tbody) return;
        tbody.textContent = "";
        lista.forEach((s) => {
            const tr = document.createElement("tr");
            const tdId = document.createElement("th");
            tdId.scope = "row";
            tdId.className = "p-2.5 font-mono-num font-bold text-kovaq-orange";
            tdId.textContent = s.id;
            const tdNom = document.createElement("td");
            tdNom.className = "p-2.5 font-bold";
            tdNom.textContent = s.nombre;
            const tdCiu = document.createElement("td");
            tdCiu.className = "p-2.5 text-kovaq-slate";
            tdCiu.textContent = s.ciudad;
            const tdFmt = document.createElement("td");
            tdFmt.className = "p-2.5";
            tdFmt.textContent = s.formato;
            const tdArea = document.createElement("td");
            tdArea.className = "p-2.5 font-mono-num";
            tdArea.textContent = `${Number(s.area_m2 || 1200).toLocaleString()} m2`;
            tr.append(tdId, tdNom, tdCiu, tdFmt, tdArea);
            tbody.appendChild(tr);
        });
    }

    // =========================================================================
    // MAPA ARQUITECTÓNICO 2D + MAPA DE CALOR + INTERPOLACIÓN SUAVE DE VECTORES
    // =========================================================================
    function renderizarMapaCalorTermico(ctx, W, H, grid) {
        if (!Array.isArray(grid) || grid.length === 0 || !grid[0].length) return;
        const rows = grid.length;
        const cols = grid[0].length;
        let maxVal = 0.0;
        for (let r = 0; r < rows; r++) {
            for (let c = 0; c < cols; c++) {
                if (grid[r][c] > maxVal) maxVal = grid[r][c];
            }
        }
        if (maxVal <= 0.01) return;

        const ow = heatOffscreen.width;
        const oh = heatOffscreen.height;
        const octx = heatOffscreen.getContext("2d", { willReadFrequently: true });
        octx.clearRect(0, 0, ow, oh);

        const cw = ow / cols;
        const ch = oh / rows;
        const radio = Math.max(cw, ch) * 1.85;

        for (let r = 0; r < rows; r++) {
            for (let c = 0; c < cols; c++) {
                const val = grid[r][c];
                if (val <= 0.05) continue;
                const intensity = Math.min(1.0, val / maxVal);
                const cx = (c + 0.5) * cw;
                const cy = (r + 0.5) * ch;
                const grad = octx.createRadialGradient(cx, cy, 1, cx, cy, radio);
                grad.addColorStop(0, `rgba(0, 0, 0, ${(intensity * 0.62).toFixed(3)})`);
                grad.addColorStop(1, "rgba(0, 0, 0, 0)");
                octx.fillStyle = grad;
                octx.beginPath();
                octx.arc(cx, cy, radio, 0, Math.PI * 2);
                octx.fill();
            }
        }

        const imgData = octx.getImageData(0, 0, ow, oh);
        const px = imgData.data;
        for (let i = 0; i < px.length; i += 4) {
            const alpha = px[i + 3];
            if (alpha > 8) {
                const idx = Math.min(255, alpha) * 4;
                px[i] = heatLUT[idx];
                px[i + 1] = heatLUT[idx + 1];
                px[i + 2] = heatLUT[idx + 2];
                px[i + 3] = heatLUT[idx + 3];
            }
        }
        octx.putImageData(imgData, 0, 0);

        ctx.save();
        ctx.globalAlpha = 0.72;
        ctx.drawImage(heatOffscreen, 0, 0, W, H);
        ctx.restore();
    }

    function obtenerPosicionInterpolada(pers) {
        const clave = `${pers.origen || "be"}_${pers.global_id || pers.etiqueta}`;
        const tx = Number(pers.x || 0.5);
        const ty = Number(pers.y || 0.5);
        const prev = personasSmooth.get(clave);
        if (!prev || prefiereMovimientoReducido()) {
            const pos = { x: tx, y: ty, tx, ty };
            personasSmooth.set(clave, pos);
            return pos;
        }
        prev.tx = tx;
        prev.ty = ty;
        prev.x += (tx - prev.x) * 0.24;
        prev.y += (ty - prev.y) * 0.24;
        if (Math.abs(tx - prev.x) < 0.0005) prev.x = tx;
        if (Math.abs(ty - prev.y) < 0.0005) prev.y = ty;
        return prev;
    }

    function dibujarPlanoTienda2D(data) {
        const canvas = document.getElementById("store-map-canvas");
        if (!canvas || !data) return;
        ultimoEstadoMapa = data;

        const ctx = canvas.getContext("2d");
        const W = canvas.width;
        const H = canvas.height;
        const plano = data.plano_arquitectura || {};

        // 1. Piso arquitectónico de tienda
        ctx.fillStyle = "#F8FAFC";
        ctx.fillRect(0, 0, W, H);

        ctx.strokeStyle = "#E2E8F0";
        ctx.lineWidth = 1;
        for (let x = 24; x < W - 20; x += 32) {
            ctx.beginPath(); ctx.moveTo(x, 18); ctx.lineTo(x, H - 18); ctx.stroke();
        }
        for (let y = 18; y < H - 18; y += 32) {
            ctx.beginPath(); ctx.moveTo(24, y); ctx.lineTo(W - 24, y); ctx.stroke();
        }

        // 2. Bodega / Reposición
        const bod = plano.bodega;
        if (bod) {
            const bx = bod.x * W, by = bod.y * H, bw = bod.w * W, bh = bod.h * H;
            ctx.save();
            ctx.fillStyle = "#E2E8F0";
            ctx.fillRect(bx, by, bw, bh);
            ctx.beginPath();
            ctx.rect(bx, by, bw, bh);
            ctx.clip();
            ctx.strokeStyle = "#CBD5E1";
            ctx.lineWidth = 1.5;
            for (let hx = bx - bh; hx < bx + bw + bh; hx += 14) {
                ctx.beginPath(); ctx.moveTo(hx, by + bh); ctx.lineTo(hx + bh, by); ctx.stroke();
            }
            ctx.restore();
            ctx.strokeStyle = "#475569";
            ctx.lineWidth = 2;
            ctx.strokeRect(bx, by, bw, bh);
            ctx.fillStyle = "#334155";
            ctx.font = "bold 11px 'Space Grotesk', sans-serif";
            ctx.fillText(bod.label || "BODEGA / PERSONAL", bx + 12, by + bh * 0.62);
        }

        // 3. Capa de Mapa de Calor Térmico
        if (capasMapa.calor) {
            renderizarMapaCalorTermico(ctx, W, H, data.heatmap_grid || []);
        }

        // 4. Muros perimetrales
        const wallPadX = 24;
        const wallPadY = 18;
        ctx.save();
        ctx.strokeStyle = "#0C2340";
        ctx.lineWidth = 6;
        ctx.strokeRect(wallPadX, wallPadY, W - wallPadX * 2, H - wallPadY * 2);
        ctx.restore();

        // 5. Etiquetas de Pasillos y Cabeceras
        (plano.pasillos || []).forEach((ps) => {
            ctx.save();
            ctx.fillStyle = "rgba(12, 35, 64, 0.52)";
            ctx.font = "bold 10px 'JetBrains Mono', monospace";
            ctx.fillText(ps.label || "", ps.x * W, ps.y * H);
            ctx.restore();
        });

        (plano.cabeceras || []).forEach((cab) => {
            const cx = cab.x * W, cy = cab.y * H, cw = cab.w * W, ch = cab.h * H;
            ctx.save();
            ctx.fillStyle = "#FFF7ED";
            ctx.strokeStyle = "#C23B00";
            ctx.lineWidth = 1.5;
            ctx.fillRect(cx, cy, cw, ch);
            ctx.strokeRect(cx, cy, cw, ch);
            ctx.fillStyle = "#C23B00";
            ctx.font = "bold 9px 'JetBrains Mono', monospace";
            ctx.fillText(cab.label || "CAB", cx + 4, cy + ch / 2 + 3);
            ctx.restore();
        });

        // 6. Puerta de Acceso Principal
        const puerta = plano.puerta_entrada || (data.zonas_arquitectura || {}).puerta_entrada;
        if (puerta) {
            const px = puerta.x * W, py = puerta.y * H, pw = puerta.w * W, ph = puerta.h * H;
            ctx.save();
            ctx.fillStyle = "#ECFDF5";
            ctx.fillRect(wallPadX - 4, py, pw + 10, ph);
            ctx.strokeStyle = "#059669";
            ctx.lineWidth = 2.5;
            ctx.strokeRect(px, py, pw, ph);
            ctx.strokeStyle = "#10B981";
            ctx.lineWidth = 3;
            ctx.beginPath();
            ctx.moveTo(wallPadX, py + ph * 0.12);
            ctx.lineTo(wallPadX, py + ph * 0.44);
            ctx.moveTo(wallPadX, py + ph * 0.56);
            ctx.lineTo(wallPadX, py + ph * 0.88);
            ctx.stroke();
            ctx.fillStyle = "#047857";
            ctx.font = "bold 10px 'Space Grotesk', sans-serif";
            ctx.save();
            ctx.translate(px + pw * 0.55, py + ph * 0.5);
            ctx.rotate(-Math.PI / 2);
            ctx.textAlign = "center";
            ctx.fillText(puerta.nombre || "ACCESO", 0, 3);
            ctx.restore();
            ctx.restore();
        }

        // 7. Zona de Atención y Línea de Cajas
        const aten = plano.zona_atencion;
        if (aten) {
            const ax = aten.x * W, ay = aten.y * H, aw = aten.w * W, ah = aten.h * H;
            ctx.save();
            ctx.fillStyle = "#F1F5F9";
            ctx.strokeStyle = "#64748B";
            ctx.lineWidth = 1.5;
            ctx.fillRect(ax, ay, aw, ah);
            ctx.strokeRect(ax, ay, aw, ah);
            ctx.fillStyle = "#334155";
            ctx.font = "bold 9px 'Space Grotesk', sans-serif";
            ctx.textAlign = "center";
            ctx.fillText(aten.label || "ATENCIÓN", ax + aw / 2, ay + ah / 2 + 3, aw - 8);
            ctx.restore();
        }

        const cajas = plano.linea_cajas || (data.zonas_arquitectura || {}).linea_cajas;
        if (cajas) {
            const cx = cajas.x * W, cy = cajas.y * H, cw = cajas.w * W, ch = cajas.h * H;
            const nCajas = Number(cajas.cajas || 4);
            ctx.save();
            ctx.fillStyle = "rgba(239, 246, 255, 0.92)";
            ctx.strokeStyle = "#1D4ED8";
            ctx.lineWidth = 2;
            ctx.fillRect(cx, cy, cw, ch);
            ctx.strokeRect(cx, cy, cw, ch);

            ctx.fillStyle = "#1E3A8A";
            ctx.font = "bold 10px 'Space Grotesk', sans-serif";
            ctx.textAlign = "center";
            ctx.fillText(cajas.nombre || "CAJAS", cx + cw / 2, cy + 14, cw - 8);

            const slotH = (ch - 24) / Math.max(1, nCajas);
            for (let i = 0; i < nCajas; i++) {
                const sy = cy + 20 + i * slotH;
                ctx.fillStyle = "#DBEAFE";
                ctx.strokeStyle = "#2563EB";
                ctx.lineWidth = 1.2;
                ctx.fillRect(cx + 8, sy + 2, cw - 16, Math.max(14, slotH - 6));
                ctx.strokeRect(cx + 8, sy + 2, cw - 16, Math.max(14, slotH - 6));
                ctx.fillStyle = "#1E3A8A";
                ctx.font = "bold 9px 'JetBrains Mono', monospace";
                ctx.fillText(`POS ${i + 1}`, cx + cw / 2, sy + Math.max(12, (slotH - 4) * 0.65));
            }
            ctx.restore();
        }

        // 8. Columnas estructurales
        (plano.columnas || []).forEach(([colX, colY]) => {
            ctx.save();
            ctx.fillStyle = "#334155";
            ctx.fillRect(colX * W - 6, colY * H - 6, 12, 12);
            ctx.strokeStyle = "#0F172A";
            ctx.lineWidth = 1.5;
            ctx.strokeRect(colX * W - 6, colY * H - 6, 12, 12);
            ctx.restore();
        });

        // 9. Góndolas / Perchas de productos
        (data.perchas || []).forEach((p) => {
            const px = p.map_x * W, py = p.map_y * H, pw = p.map_w * W, ph = p.map_h * H;
            const temp = p.temperatura || "fria";
            const bordeColor = temp === "caliente" ? "#DC2626" : (temp === "templada" ? "#D97706" : "#2563EB");
            const badgeBg = temp === "caliente" ? "#FEE2E2" : (temp === "templada" ? "#FEF3C7" : "#DBEAFE");
            const badgeTxt = temp === "caliente" ? "#991B1B" : (temp === "templada" ? "#92400E" : "#1E40AF");
            const tempLbl = temp === "caliente" ? "CALIENTE" : (temp === "templada" ? "MEDIA" : "FRÍA");

            ctx.save();
            ctx.fillStyle = "rgba(12, 35, 64, 0.08)";
            ctx.fillRect(px + 3, py + 3, pw, ph);

            ctx.fillStyle = "#FFFFFF";
            ctx.strokeStyle = bordeColor;
            ctx.lineWidth = 2.5;
            ctx.beginPath();
            ctx.roundRect(px, py, pw, ph, 6);
            ctx.fill();
            ctx.stroke();

            ctx.strokeStyle = "#CBD5E1";
            ctx.lineWidth = 1;
            ctx.beginPath();
            if (pw >= ph) {
                ctx.moveTo(px + 6, py + ph / 2);
                ctx.lineTo(px + pw - 6, py + ph / 2);
            } else {
                ctx.moveTo(px + pw / 2, py + 6);
                ctx.lineTo(px + pw / 2, py + ph - 6);
            }
            ctx.stroke();

            ctx.fillStyle = "#0C2340";
            ctx.font = "bold 11px 'Space Grotesk', sans-serif";
            ctx.fillText(p.id, px + 8, py + 16);

            ctx.fillStyle = "#475569";
            ctx.font = "600 10px 'Inter', sans-serif";
            ctx.fillText(String(p.categoria || "").slice(0, 14), px + 8, py + 30);

            ctx.fillStyle = "#0C2340";
            ctx.font = "bold 10px 'JetBrains Mono', monospace";
            ctx.fillText(`${p.tomas || 0} tomas`, px + 8, py + Math.min(ph - 8, 46));

            if (pw >= 120) {
                const bw = 54;
                ctx.fillStyle = badgeBg;
                ctx.fillRect(px + pw - bw - 6, py + 6, bw, 16);
                ctx.fillStyle = badgeTxt;
                ctx.font = "bold 9px 'JetBrains Mono', monospace";
                ctx.fillText(tempLbl, px + pw - bw - 2, py + 17);
            }
            ctx.restore();
        });

        // 10. Cámaras CCTV con conos FOV
        if (capasMapa.fov) {
            (data.camaras || []).forEach((cam) => {
                const cx = cam.pos_x * W;
                const cy = cam.pos_y * H;
                const rad = ((cam.angulo || 0) * Math.PI) / 180;
                const fovRad = (((cam.fov || 70) * Math.PI) / 180) / 2;
                const alcancePx = (cam.alcance || 0.4) * W * 0.52;
                const esActiva = cam.id === data.camara_activa;

                ctx.save();
                ctx.beginPath();
                ctx.moveTo(cx, cy);
                ctx.arc(cx, cy, alcancePx, rad - fovRad, rad + fovRad);
                ctx.closePath();
                ctx.fillStyle = esActiva ? "rgba(194, 59, 0, 0.12)" : "rgba(12, 35, 64, 0.06)";
                ctx.fill();
                ctx.strokeStyle = esActiva ? "rgba(194, 59, 0, 0.55)" : "rgba(12, 35, 64, 0.25)";
                ctx.setLineDash([5, 4]);
                ctx.lineWidth = 1.3;
                ctx.stroke();
                ctx.setLineDash([]);

                ctx.translate(cx, cy);
                ctx.rotate(rad);
                ctx.fillStyle = esActiva ? "#C23B00" : "#0C2340";
                ctx.strokeStyle = "#FFFFFF";
                ctx.lineWidth = 1.8;
                ctx.beginPath();
                ctx.roundRect(-9, -6, 15, 12, 3);
                ctx.fill();
                ctx.stroke();
                ctx.beginPath();
                ctx.moveTo(6, -4);
                ctx.lineTo(12, -7);
                ctx.lineTo(12, 7);
                ctx.lineTo(6, 4);
                ctx.closePath();
                ctx.fill();
                ctx.restore();

                ctx.save();
                const nearTop = cy < 115;
                const lblX = nearTop ? Math.min(W - 76, cx + 16) : Math.max(30, cx - 22);
                const lblY = nearTop ? cy - 8 : cy - 24;
                ctx.fillStyle = esActiva ? "#C23B00" : "#0C2340";
                ctx.fillRect(lblX, lblY, 46, 15);
                ctx.fillStyle = "#FFFFFF";
                ctx.font = "bold 9px 'JetBrains Mono', monospace";
                ctx.textAlign = "center";
                ctx.fillText(cam.id, lblX + 23, lblY + 11);
                ctx.restore();
            });
        }

        // 11. Vectores anónimos vivos con interpolación fluida
        if (capasMapa.vectores) {
            const listaPersonas = combinarPersonasMapa(data.personas || []);
            listaPersonas.forEach((pers) => {
                const interp = obtenerPosicionInterpolada(pers);
                const px = interp.x * W;
                const py = interp.y * H;
                const esCli = pers.es_cliente_valido !== false;
                const color = esCli ? (pers.origen === "video_yolo" ? "#C23B00" : "#059669") : "#64748B";

                if (Array.isArray(pers.trayectoria) && pers.trayectoria.length > 1) {
                    ctx.save();
                    ctx.strokeStyle = color;
                    ctx.lineWidth = esCli ? 2 : 1.2;
                    ctx.setLineDash(esCli ? [] : [4, 4]);
                    ctx.beginPath();
                    pers.trayectoria.forEach((pt, i) => {
                        if (i === 0) ctx.moveTo(pt.x * W, pt.y * H);
                        else ctx.lineTo(pt.x * W, pt.y * H);
                    });
                    ctx.stroke();
                    ctx.restore();
                }

                ctx.save();
                ctx.fillStyle = color;
                ctx.strokeStyle = "#FFFFFF";
                ctx.lineWidth = 2;
                ctx.beginPath();
                if (esCli) {
                    ctx.arc(px, py, 7, 0, Math.PI * 2);
                } else {
                    ctx.moveTo(px, py - 7);
                    ctx.lineTo(px + 7, py);
                    ctx.lineTo(px, py + 7);
                    ctx.lineTo(px - 7, py);
                    ctx.closePath();
                }
                ctx.fill();
                ctx.stroke();

                ctx.fillStyle = "#0C2340";
                ctx.font = "bold 10px 'JetBrains Mono', monospace";
                ctx.fillText(pers.etiqueta || "CLI", px + 10, py + 4);
                ctx.restore();
            });
        }

        // 12. Escala arquitectónica
        const dims = plano.dimensiones_m || [42, 24];
        ctx.save();
        ctx.fillStyle = "rgba(255, 255, 255, 0.9)";
        ctx.fillRect(34, H - 40, 152, 18);
        ctx.fillStyle = "#334155";
        ctx.font = "bold 9px 'JetBrains Mono', monospace";
        ctx.fillText(`Escala: ${dims[0]}m x ${dims[1]}m | N ↑`, 40, H - 28);
        ctx.restore();

        const tituloEl = document.getElementById("titulo-plano-sucursal");
        if (tituloEl && data.sucursal_nombre) {
            tituloEl.textContent = `Mapa de Perchas y Calor • ${data.sucursal_nombre}`;
        }
        const subEl = document.getElementById("subtitulo-plano-arquitectura");
        if (subEl) {
            const muestras = data.heatmap_meta?.total_muestras || 0;
            subEl.textContent = `${plano.nombre_plano || "Planta Comercial"} • ${muestras} registros en el período seleccionado.`;
        }

        const totalPerchas = (data.perchas || []).length;
        const totalCams = (data.camaras || []).length;
        canvas.setAttribute(
            "aria-label",
            `Mapa arquitectónico de ${data.sucursal_nombre || "sucursal"}: ${totalCams} cámaras CCTV, ${totalPerchas} perchas y ${data.heatmap_meta?.total_muestras || 0} registros del período.`
        );
    }

    function combinarPersonasMapa(personasBackend) {
        const res = [...personasBackend];
        liveVideoPersonas.forEach((vp) => {
            if (!vp.map) return;
            res.push({
                global_id: vp.id,
                etiqueta: vp.label,
                es_cliente_valido: vp.cliente,
                x: vp.map[0],
                y: vp.map[1],
                camara_actual: "CAM-01",
                postura_cabeza: vp.postura,
                estado_accion: vp.accion,
                trayectoria: [],
                origen: "video_yolo",
            });
        });
        return res;
    }

    function inicializarInteraccionMapa() {
        const canvas = document.getElementById("store-map-canvas");
        const tooltip = document.getElementById("map-tooltip");
        if (canvas && tooltip) {
            canvas.addEventListener("mousemove", (ev) => {
                if (!ultimoEstadoMapa) return;
                const rect = canvas.getBoundingClientRect();
                const nx = (ev.clientX - rect.left) / rect.width;
                const ny = (ev.clientY - rect.top) / rect.height;

                const percha = (ultimoEstadoMapa.perchas || []).find(
                    (p) => nx >= p.map_x && nx <= p.map_x + p.map_w && ny >= p.map_y && ny <= p.map_y + p.map_h
                );
                const cam = (ultimoEstadoMapa.camaras || []).find(
                    (c) => Math.hypot(nx - c.pos_x, ny - c.pos_y) <= 0.035
                );

                if (percha) {
                    tooltip.textContent = `${percha.id} (${percha.categoria}) | Tomas: ${percha.tomas || 0} | Dev: ${percha.devoluciones || 0} | Permanencia: ${percha.comportamiento?.dwell_prom || 0}s`;
                    tooltip.style.left = `${Math.min(rect.width - 230, Math.max(8, ev.clientX - rect.left + 12))}px`;
                    tooltip.style.top = `${Math.max(8, ev.clientY - rect.top - 48)}px`;
                    tooltip.classList.remove("hidden");
                    canvas.style.cursor = "pointer";
                } else if (cam) {
                    tooltip.textContent = `${cam.id}: ${cam.nombre} (Clic para seleccionar cámara)`;
                    tooltip.style.left = `${Math.min(rect.width - 230, Math.max(8, ev.clientX - rect.left + 12))}px`;
                    tooltip.style.top = `${Math.max(8, ev.clientY - rect.top - 40)}px`;
                    tooltip.classList.remove("hidden");
                    canvas.style.cursor = "pointer";
                } else {
                    tooltip.classList.add("hidden");
                    canvas.style.cursor = "default";
                }
            });

            canvas.addEventListener("mouseleave", () => tooltip.classList.add("hidden"));

            canvas.addEventListener("click", (ev) => {
                if (!ultimoEstadoMapa) return;
                const rect = canvas.getBoundingClientRect();
                const nx = (ev.clientX - rect.left) / rect.width;
                const ny = (ev.clientY - rect.top) / rect.height;
                const cam = (ultimoEstadoMapa.camaras || []).find(
                    (c) => Math.hypot(nx - c.pos_x, ny - c.pos_y) <= 0.04
                );
                if (cam) {
                    const selCam = document.getElementById("select-camara-mapa");
                    if (selCam) {
                        selCam.value = cam.id;
                        selCam.dispatchEvent(new Event("change"));
                    }
                }
            });
        }

        // Menú desplegable de capas del mapa
        const btnCapas = document.getElementById("btn-menu-capas");
        if (btnCapas) {
            btnCapas.addEventListener("click", () => abrirPopover("popover-capas", "btn-menu-capas"));
        }

        [
            { id: "btn-capa-calor", key: "calor" },
            { id: "btn-capa-fov", key: "fov" },
            { id: "btn-capa-vectores", key: "vectores" },
        ].forEach(({ id, key }) => {
            const btn = document.getElementById(id);
            if (!btn) return;
            btn.addEventListener("click", () => {
                capasMapa[key] = !capasMapa[key];
                btn.classList.toggle("active", capasMapa[key]);
                btn.setAttribute("aria-pressed", capasMapa[key] ? "true" : "false");
                const chk = btn.querySelector("i");
                if (chk) chk.style.opacity = capasMapa[key] ? "1" : "0";
                if (ultimoEstadoMapa) dibujarPlanoTienda2D(ultimoEstadoMapa);
            });
        });
    }

    function actualizarSelectorCamaras(camaras, camActiva) {
        const sel = document.getElementById("select-camara-mapa");
        const selPlano = document.getElementById("plano-cam");
        if (sel && Array.isArray(camaras) && camaras.length > 0) {
            const prev = sel.value;
            sel.textContent = "";
            camaras.forEach((c) => {
                const o = document.createElement("option");
                o.value = c.id;
                o.textContent = `${c.id} • ${c.nombre.split("•").pop().trim()}`;
                sel.appendChild(o);
            });
            sel.value = camaras.some((c) => c.id === prev) ? prev : (camActiva && camaras.some((c) => c.id === camActiva) ? camActiva : camaras[0].id);
        }
        if (selPlano && Array.isArray(camaras) && camaras.length > 0) {
            selPlano.textContent = "";
            camaras.forEach((c) => {
                const o = document.createElement("option");
                o.value = c.id;
                const partes = String(c.nombre || "").split("•");
                const suf = partes.length > 1 ? partes[partes.length - 1].trim() : c.nombre;
                o.textContent = `${c.id} • ${suf}`;
                selPlano.appendChild(o);
            });
        }
    }

    function actualizarPanelesReID(data) {
        const handoffCount = document.getElementById("map-handoff-counter");
        const muestras = data.heatmap_meta?.total_muestras || 0;
        const handoffs = data.handoffs || [];
        if (handoffCount) {
            handoffCount.textContent = `${muestras} muestras térmicas • ${handoffs.length} handoffs`;
        }

        const simBtnTxt = document.getElementById("txt-toggle-sim");
        if (simBtnTxt) {
            simBtnTxt.textContent = data.simulacion_activa ? "Pausar" : "Activar";
        }

        const personas = combinarPersonasMapa(data.personas || []);
        const badgeReId = document.getElementById("badge-resumen-reid");
        if (badgeReId) {
            badgeReId.textContent = `${personas.length} en escena • ${handoffs.length} traspasos`;
        }

        const listaPers = document.getElementById("lista-personas-reid");
        if (listaPers) {
            listaPers.textContent = "";
            if (personas.length === 0) {
                listaPers.textContent = "Sin vectores activos en el plano en este instante.";
            } else {
                personas.slice(0, 8).forEach((p) => {
                    const row = document.createElement("div");
                    row.className = "flex flex-wrap items-center justify-between gap-2 bg-slate-50 border border-kovaq-border rounded-lg px-3 py-2";
                    const left = document.createElement("div");
                    const strong = document.createElement("strong");
                    strong.className = "font-mono-num text-kovaq-navy mr-2";
                    strong.textContent = `${p.etiqueta} (${p.camara_actual || "CAM-01"})`;
                    const accion = document.createElement("span");
                    accion.className = "text-kovaq-slate";
                    accion.textContent = p.estado_accion || "En tránsito";
                    left.append(strong, accion);

                    const badge = document.createElement("span");
                    badge.className = p.es_cliente_valido === false
                        ? "bg-slate-200 text-kovaq-navy px-2 py-0.5 rounded font-bold"
                        : "bg-emerald-100 text-emerald-900 px-2 py-0.5 rounded font-bold";
                    badge.textContent = p.es_cliente_valido === false ? "Staff excluido" : (p.postura_cabeza || "Cliente");

                    row.append(left, badge);
                    listaPers.appendChild(row);
                });
            }
        }

        const listaHandoffs = document.getElementById("lista-handoffs-reid");
        if (listaHandoffs) {
            listaHandoffs.textContent = "";
            if (handoffs.length === 0) {
                listaHandoffs.textContent = "Aún no se registran traspasos entre cámaras en esta sesión.";
            } else {
                handoffs.slice(0, 6).forEach((h) => {
                    const row = document.createElement("div");
                    row.className = "flex items-center justify-between bg-slate-50 border border-kovaq-border rounded-lg px-3 py-2";
                    const left = document.createElement("span");
                    left.className = "font-mono-num font-bold text-kovaq-navy";
                    left.textContent = `S#${h.global_id}: ${h.origen} -> ${h.destino}`;
                    const right = document.createElement("span");
                    right.className = "font-mono-num text-kovaq-slate";
                    right.textContent = `${h.hora} (${Math.round((h.similitud || 0.9) * 100)}%)`;
                    row.append(left, right);
                    listaHandoffs.appendChild(row);
                });
            }
        }
    }

    async function sincronizarMapa2D() {
        try {
            const { res, seq } = await fetchCancelable("mapa", `/api/map_state?${construirQueryTemporal()}`);
            if (!res.ok || seq !== reqSeq.mapa) return;
            const data = await res.json();
            if (seq !== reqSeq.mapa) return;
            actualizarSelectorCamaras(data.camaras || [], data.camara_activa);
            dibujarPlanoTienda2D(data);
            actualizarPanelesReID(data);
        } catch (_e) {
            // Silencioso en abort
        }
    }

    // =========================================================================
    // TRACKEO EN TIEMPO REAL SOBRE EL VIDEO (POLÍGONOS EN PERSPECTIVA + PERSONAS)
    // =========================================================================
    async function cargarDemoTracks() {
        try {
            const res = await fetch("/static/demo_tracks.json");
            if (res.ok) demoTracks = await res.json();
        } catch (_e) {
            // Silencioso
        }
    }

    function dibujarPoligonoPercha(ctx, W, H, polyNorm, label, colorHex, alignRight) {
        if (!Array.isArray(polyNorm) || polyNorm.length < 3) return;
        ctx.save();
        ctx.beginPath();
        polyNorm.forEach((pt, idx) => {
            const px = pt[0] * W;
            const py = pt[1] * H;
            if (idx === 0) ctx.moveTo(px, py);
            else ctx.lineTo(px, py);
        });
        ctx.closePath();

        ctx.fillStyle = colorHex === "#FE5000" ? "rgba(254, 80, 0, 0.14)" : "rgba(245, 158, 11, 0.14)";
        ctx.fill();
        ctx.strokeStyle = colorHex || "#FE5000";
        ctx.lineWidth = 2.5;
        ctx.stroke();

        if (polyNorm.length >= 5) {
            ctx.strokeStyle = "rgba(255, 255, 255, 0.28)";
            ctx.lineWidth = 1;
            ctx.setLineDash([6, 4]);
            const pTopNear = alignRight ? polyNorm[1] : polyNorm[0];
            const pTopFar = alignRight ? polyNorm[0] : polyNorm[1];
            const pBotFar = alignRight ? polyNorm[4] : polyNorm[2];
            const pBotNear = alignRight ? polyNorm[2] : polyNorm[4];
            [0.33, 0.66].forEach((f) => {
                ctx.beginPath();
                ctx.moveTo((pTopNear[0] + (pBotNear[0] - pTopNear[0]) * f) * W, (pTopNear[1] + (pBotNear[1] - pTopNear[1]) * f) * H);
                ctx.lineTo((pTopFar[0] + (pBotFar[0] - pTopFar[0]) * f) * W, (pTopFar[1] + (pBotFar[1] - pTopFar[1]) * f) * H);
                ctx.stroke();
            });
            ctx.setLineDash([]);
        }

        ctx.font = "bold 12px 'Space Grotesk', sans-serif";
        const textW = ctx.measureText(label).width + 14;
        const lx = alignRight ? (W - textW - 10) : 10;
        const ly = (polyNorm[0][1] * H) + 8;
        ctx.fillStyle = "rgba(12, 35, 64, 0.90)";
        ctx.fillRect(lx, ly, textW, 22);
        ctx.fillStyle = colorHex || "#FE5000";
        ctx.fillRect(lx, ly, 4, 22);
        ctx.fillStyle = "#FFFFFF";
        ctx.fillText(label, lx + 9, ly + 15);
        ctx.restore();
    }

    function dibujarPersonaTracked(ctx, W, H, p, etiquetasOcupadas) {
        const [nx1, ny1, nx2, ny2] = p.box;
        const x1 = nx1 * W;
        const y1 = ny1 * H;
        const x2 = nx2 * W;
        const y2 = ny2 * H;
        const bw = Math.max(8, x2 - x1);
        const bh = Math.max(12, y2 - y1);

        const esCli = p.cliente !== false;
        const enPercha = Boolean(p.percha);
        const color = !esCli ? "#94A3B8" : (enPercha ? "#FE5000" : "#22C55E");

        ctx.save();
        const headH = Math.max(14, bh * 0.22);
        ctx.fillStyle = "rgba(12, 35, 64, 0.58)";
        ctx.fillRect(x1 + 2, y1 + 2, bw - 4, headH);
        ctx.fillStyle = "rgba(255, 255, 255, 0.85)";
        ctx.font = "bold 9px 'JetBrains Mono', monospace";
        ctx.fillText("ANÓNIMO", x1 + 6, y1 + Math.min(headH - 4, 14));

        ctx.strokeStyle = color;
        ctx.lineWidth = 2.2;
        ctx.strokeRect(x1, y1, bw, bh);

        const corner = Math.min(14, bw * 0.25, bh * 0.2);
        ctx.lineWidth = 3.5;
        ctx.beginPath();
        ctx.moveTo(x1, y1 + corner); ctx.lineTo(x1, y1); ctx.lineTo(x1 + corner, y1);
        ctx.moveTo(x2 - corner, y1); ctx.lineTo(x2, y1); ctx.lineTo(x2, y1 + corner);
        ctx.moveTo(x1, y2 - corner); ctx.lineTo(x1, y2); ctx.lineTo(x1 + corner, y2);
        ctx.moveTo(x2 - corner, y2); ctx.lineTo(x2, y2); ctx.lineTo(x2, y2 - corner);
        ctx.stroke();

        if (p.gaze === "left" || p.gaze === "right") {
            const gy = y1 + headH * 0.6;
            const gxStart = p.gaze === "left" ? x1 : x2;
            const gxEnd = p.gaze === "left" ? Math.max(4, x1 - 28) : Math.min(W - 4, x2 + 28);
            ctx.strokeStyle = "#FACC15";
            ctx.lineWidth = 2.5;
            ctx.beginPath();
            ctx.moveTo(gxStart, gy);
            ctx.lineTo(gxEnd, gy);
            ctx.stroke();
        }

        const dwellTxt = p.dwell > 0 ? ` (${p.dwell.toFixed(1)}s)` : "";
        const tag = `${p.label} | ${p.postura}${dwellTxt}`;
        ctx.font = "bold 11px 'JetBrains Mono', monospace";
        const tw = ctx.measureText(tag).width + 12;
        const th = 20;
        const tx = Math.min(W - tw - 4, Math.max(4, x1));
        let ty = Math.max(4, y1 - 24);
        if (Array.isArray(etiquetasOcupadas)) {
            for (let intentos = 0; intentos < 4; intentos++) {
                const choca = etiquetasOcupadas.some(
                    (r) => tx < r.x + r.w + 4 && tx + tw + 4 > r.x && ty < r.y + r.h + 2 && ty + th + 2 > r.y
                );
                if (!choca) break;
                ty = ty >= 28 ? ty - 23 : Math.min(H - th - 4, ty + 23);
            }
            etiquetasOcupadas.push({ x: tx, y: ty, w: tw, h: th });
        }
        ctx.fillStyle = !esCli ? "#475569" : (enPercha ? "#C23B00" : "#047857");
        ctx.fillRect(tx, ty, tw, th);
        ctx.fillStyle = "#FFFFFF";
        ctx.fillText(tag, tx + 6, ty + 14);
        ctx.restore();
    }

    function iniciarBucleHudVideo() {
        if (rafHudId !== null) return;
        let ultimoSyncMapa = 0;

        function tick(ts) {
            rafHudId = window.requestAnimationFrame(tick);
            if (document.hidden || moduloActivo !== "tab-vivo") return;

            const canvas = document.getElementById("yolo-hud-canvas");
            const videoEl = document.getElementById("demo-video-player");
            const hudChip = document.getElementById("video-hud-telemetry");
            if (!canvas) return;

            const ctx = canvas.getContext("2d");
            const W = canvas.width;
            const H = canvas.height;
            ctx.clearRect(0, 0, W, H);
            const etiquetasOcupadas = [];

            if (videoEl && demoTracks && Array.isArray(demoTracks.frames) && demoTracks.frames.length > 0) {
                const shelves = demoTracks.shelves || [];
                shelves.forEach((sh) => {
                    dibujarPoligonoPercha(
                        ctx, W, H, sh.poly,
                        `${sh.id} • ${sh.categoria}`,
                        sh.color,
                        sh.label_align === "right"
                    );
                });

                const fps = demoTracks.fps || 24;
                const total = demoTracks.frames.length;
                const fIdx = Math.floor((videoEl.currentTime || 0) * fps) % total;
                const frameObj = demoTracks.frames[Math.max(0, fIdx)] || { p: [] };
                const personas = frameObj.p || [];
                liveVideoPersonas = personas;

                personas.forEach((p) => dibujarPersonaTracked(ctx, W, H, p, etiquetasOcupadas));

                const nCli = personas.filter((p) => p.cliente).length;
                const nStaff = personas.length - nCli;
                if (hudChip) {
                    hudChip.textContent = `YOLOv8n • ${nCli} clientes • ${nStaff} staff • ${(videoEl.currentTime || 0).toFixed(1)}s`;
                }

                // Interpolación suave de vectores en el mapa 2D (~15 fps cuando hay movimiento)
                const intervaloMapa = prefiereMovimientoReducido() ? 400 : 66;
                if (ultimoEstadoMapa && ts - ultimoSyncMapa >= intervaloMapa) {
                    ultimoSyncMapa = ts;
                    dibujarPlanoTienda2D(ultimoEstadoMapa);
                }
                return;
            }

            if (liveTelemetriaBackend) {
                (liveTelemetriaBackend.perchas_roi || []).forEach((po) => {
                    if (po.poly_norm) {
                        dibujarPoligonoPercha(ctx, W, H, po.poly_norm, `${po.id} • ${po.categoria || ""}`, po.color || "#FE5000", po.id === "PER-A2");
                    }
                });
                (liveTelemetriaBackend.personas_box || []).forEach((pb) => {
                    const [x1, y1, x2, y2] = pb.box || [0, 0, 0, 0];
                    dibujarPersonaTracked(ctx, W, H, {
                        label: pb.etiqueta || `CLI-${pb.id}`,
                        cliente: pb.es_cliente_valido !== false,
                        box: [x1 / 480, y1 / 360, x2 / 480, y2 / 360],
                        percha: pb.percha_id,
                        postura: pb.postura_cabeza || "En escena",
                        dwell: pb.dwell || 0,
                        gaze: null,
                    }, etiquetasOcupadas);
                });
                if (hudChip) {
                    hudChip.textContent = `YOLOv8n Live • ${(liveTelemetriaBackend.personas_box || []).length} personas • ${liveTelemetriaBackend.fps_yolo || 0} FPS`;
                }
            }
        }

        rafHudId = window.requestAnimationFrame(tick);
    }

    // Telemetría viva y auditoría (capada a 50 filas)
    async function sincronizarTelemetriaBackend() {
        if (document.hidden) return;
        try {
            const res = await fetch("/api/live_events");
            if (!res.ok) return;
            const data = await res.json();
            liveTelemetriaBackend = data;

            const estadoEl = document.getElementById("estado-sistema-texto");
            if (estadoEl) {
                if (data.simulacion_activa) estadoEl.textContent = "Trackeo en vivo";
                else if (data.estado === "en_vivo") estadoEl.textContent = "Cámara conectada";
                else estadoEl.textContent = "Sistema activo";
            }

            const sesionEl = document.getElementById("stat-live-sesion");
            const liveCount = Math.max(data.personas_activas || 0, liveVideoPersonas.filter((p) => p.cliente).length);
            if (sesionEl) sesionEl.textContent = `Sesión viva: ${liveCount} activos`;

            const sesTom = document.getElementById("stat-sesion-tomados");
            const sesDev = document.getElementById("stat-sesion-devueltos");
            if (sesTom) sesTom.textContent = String(data.agarrados || 0);
            if (sesDev) sesDev.textContent = String(data.devueltos || 0);

            const logBody = document.getElementById("yolo-event-log-body");
            const eventos = Array.isArray(data.eventos) ? data.eventos : [];
            if (logBody && eventos.length > 0) {
                const nuevos = eventos.filter((ev) => (ev.id || 0) > lastSeenEventId).reverse();
                if (nuevos.length > 0) {
                    const emptyRow = document.getElementById("row-empty-audit");
                    if (emptyRow) emptyRow.remove();

                    nuevos.forEach((ev) => {
                        if ((ev.id || 0) > lastSeenEventId) lastSeenEventId = ev.id;
                        const tr = document.createElement("tr");
                        tr.className = "hover:bg-slate-50";

                        const tdHora = document.createElement("td");
                        tdHora.className = "p-3 font-mono-num text-kovaq-slate";
                        tdHora.textContent = ev.hora || "";

                        const tdTipo = document.createElement("td");
                        tdTipo.className = "p-3 font-bold";
                        const pill = document.createElement("span");
                        if (ev.tipo === "Continuidad Cámara") {
                            pill.className = "bg-emerald-100 text-emerald-900 px-2 py-0.5 rounded";
                        } else if (String(ev.tipo || "").includes("Devuelto")) {
                            pill.className = "bg-blue-100 text-blue-900 px-2 py-0.5 rounded";
                        } else {
                            pill.className = "bg-orange-100 text-kovaq-orange px-2 py-0.5 rounded";
                        }
                        pill.textContent = ev.tipo || "Evento";
                        tdTipo.appendChild(pill);

                        const tdVec = document.createElement("td");
                        tdVec.className = "p-3 font-mono-num font-bold";
                        tdVec.textContent = ev.global_id || `S#${ev.cliente_id || 1}`;

                        const tdProd = document.createElement("td");
                        tdProd.className = "p-3";
                        tdProd.textContent = `${ev.percha_id || "PER"} • ${ev.producto || ""}`;

                        const tdConf = document.createElement("td");
                        tdConf.className = "p-3 font-mono-num text-kovaq-slate";
                        tdConf.textContent = `${Math.round((ev.conf || 0.9) * 100)}%`;

                        tr.append(tdHora, tdTipo, tdVec, tdProd, tdConf);
                        logBody.insertBefore(tr, logBody.firstChild);
                    });

                    while (logBody.children.length > 50) {
                        logBody.lastElementChild.remove();
                    }
                }
            }
        } catch (_e) {
            // Silencioso
        }
    }

    // =========================================================================
    // KPIs Y CONDUCTA FRENTE A LA PERCHA (con transición numérica suave)
    // =========================================================================
    function aplicarDatosStats(d) {
        const traf = Number(d.trafico || 0);
        const tom = Number(d.tomados || 0);
        const dev = Number(d.devueltos || 0);
        const perm = Number(d.permanencia_promedio_seg || 0);
        const conv = traf > 0 ? Math.min(100, (tom / traf) * 100) : 0;

        animarNumero("stat-hist-trafico", traf, (v) => String(Math.round(v)));
        animarNumero("stat-hist-permanencia", perm, (v) => `${v.toFixed(1)} s`);
        animarNumero("stat-hist-tomados", tom, (v) => String(Math.round(v)));
        animarNumero("stat-hist-conversion", conv, (v) => `${v.toFixed(1)}%`);

        const devEl = document.getElementById("stat-hist-devueltos");
        if (devEl) devEl.textContent = `${dev} devueltos a último momento`;

        const bar = document.getElementById("progress-live-conversion");
        const barWrap = document.getElementById("progress-live-conversion-bar");
        if (bar) bar.style.width = `${conv.toFixed(1)}%`;
        if (barWrap) barWrap.setAttribute("aria-valuenow", String(Math.round(conv)));
    }

    function aplicarResumenPublicidad(d) {
        const priv = d.privacidad || {};
        const privEl = document.getElementById("exec-privacidad-resumen");
        if (privEl) {
            privEl.textContent = `${priv.clientes_validados_puerta_caja || 0} clientes validados en percha • ${priv.empleados_excluidos || 0} recorridos de personal excluidos.`;
        }

        const comp = d.comportamiento_visual || {};
        const setPair = (idCnt, idDwell, obj) => {
            animarNumero(idCnt, Number(obj?.casos || 0), (v) => String(Math.round(v)));
            const dEl = document.getElementById(idDwell);
            if (dEl) dEl.textContent = `${Number(obj?.dwell || 0).toFixed(1)} s promedio`;
        };
        setPair("comp-mira-compra", "comp-mira-compra-dwell", comp.mira_y_compra);
        setPair("comp-no-mira-compra", "comp-no-mira-compra-dwell", comp.no_mira_y_compra);
        setPair("comp-solo-mira", "comp-solo-mira-dwell", comp.solo_mira);
        setPair("comp-devuelve", "comp-devuelve-dwell", comp.ultimo_momento_devuelve);

        const post = d.postura_cabeza || {};
        animarNumero("postura-giro", Number(post.giro_percha || 0), (v) => String(Math.round(v)));
        animarNumero("postura-sube", Number(post.sube_cabeza || 0), (v) => String(Math.round(v)));
        animarNumero("postura-agacha", Number(post.se_agacha || 0), (v) => String(Math.round(v)));

        const renderZonas = (idLista, items, vacioTxt) => {
            const ul = document.getElementById(idLista);
            if (!ul) return;
            ul.textContent = "";
            if (!items || items.length === 0) {
                const li = document.createElement("li");
                li.className = "text-kovaq-slate";
                li.textContent = vacioTxt;
                ul.appendChild(li);
                return;
            }
            items.forEach((z) => {
                const li = document.createElement("li");
                li.className = "flex items-center justify-between gap-2";
                const nom = document.createElement("span");
                nom.className = "font-bold";
                nom.textContent = `${z.id} • ${z.nombre}`;
                const cnt = document.createElement("span");
                cnt.className = "font-mono-num text-kovaq-slate shrink-0";
                cnt.textContent = `${z.tomas} tomas`;
                li.append(nom, cnt);
                ul.appendChild(li);
            });
        };
        renderZonas("lista-zonas-calientes", d.zonas_calientes, "Sin perchas calientes en este período.");
        renderZonas("lista-zonas-frias", d.zonas_frias, "Todas las perchas registran buena rotación en este período.");

        const renderProdResumen = (idLista, items, keyVal, sufijo) => {
            const ul = document.getElementById(idLista);
            if (!ul) return;
            ul.textContent = "";
            const filtrados = (items || []).filter((it) => Number(it[keyVal] || 0) > 0);
            if (filtrados.length === 0) {
                const li = document.createElement("li");
                li.className = "text-kovaq-slate";
                li.textContent = "Sin movimientos en el rango filtrado.";
                ul.appendChild(li);
                return;
            }
            filtrados.slice(0, 4).forEach((it) => {
                const li = document.createElement("li");
                li.className = "flex items-center justify-between gap-2";
                const nom = document.createElement("span");
                nom.className = "font-bold";
                nom.textContent = `${it.nombre} (${it.percha_id})`;
                const cnt = document.createElement("span");
                cnt.className = "font-mono-num text-kovaq-slate shrink-0";
                cnt.textContent = `${it[keyVal]} ${sufijo}`;
                li.append(nom, cnt);
                ul.appendChild(li);
            });
        };
        renderProdResumen("lista-productos-llevados", d.productos_mas_llevados, "llevados_netos", "llevados");
        renderProdResumen("lista-productos-indecision", d.productos_cambio_ultimo_momento, "devueltos", "devueltos");

        const aiResumen = document.getElementById("motor-ai-resumen-texto");
        if (aiResumen && d.motor_marketing_ai?.resumen_ejecutivo) {
            aiResumen.textContent = d.motor_marketing_ai.resumen_ejecutivo;
        }

        const tbodyPromos = document.getElementById("tabla-eval-promos-body");
        if (tbodyPromos) {
            tbodyPromos.textContent = "";
            const evals = d.evaluacion_promociones || [];
            if (evals.length === 0) {
                const tr = document.createElement("tr");
                const td = document.createElement("td");
                td.colSpan = 4;
                td.className = "p-4 text-center text-kovaq-slate";
                td.textContent = "Sin promociones activas. Puedes configurar campañas opcionales en Configuración.";
                tr.appendChild(td);
                tbodyPromos.appendChild(tr);
            } else {
                evals.forEach((pr) => {
                    const tr = document.createElement("tr");
                    tr.className = "hover:bg-slate-50";

                    const tdNom = document.createElement("td");
                    tdNom.className = "p-2.5";
                    const t1 = document.createElement("strong");
                    t1.className = "block text-kovaq-navy";
                    t1.textContent = `${pr.percha_id} • ${pr.titulo}`;
                    const t2 = document.createElement("span");
                    t2.className = "text-kovaq-slate";
                    t2.textContent = `${pr.producto} (-${pr.descuento_pct}%)`;
                    tdNom.append(t1, t2);

                    const tdCnt = document.createElement("td");
                    tdCnt.className = "p-2.5 font-mono-num whitespace-nowrap";
                    tdCnt.textContent = `${pr.tomas} / ${pr.devueltos}`;

                    const tdRet = document.createElement("td");
                    tdRet.className = "p-2.5 font-mono-num font-bold text-emerald-800 whitespace-nowrap";
                    tdRet.textContent = `${pr.efectividad_pct}%`;

                    const tdDiag = document.createElement("td");
                    tdDiag.className = "p-2.5";
                    const st = document.createElement("strong");
                    st.className = "block text-kovaq-orange";
                    st.textContent = pr.estado_efecto;
                    tdDiag.append(st);

                    tr.append(tdNom, tdCnt, tdRet, tdDiag);
                    tbodyPromos.appendChild(tr);
                });
            }
        }
    }

    async function cargarResumenEjecutivoPublicidad() {
        await cargarBundleDashboard({ silencioso: false, forzar: true });
    }

    // =========================================================================
    // ANALÍTICA CONSOLIDADA (/api/analytics/bundle + SWR)
    // =========================================================================
    function aplicarAnaliticaBundle(data) {
        if (!data) return;
        if (data.serie_temporal) renderGraficoSerieTemporal(data.serie_temporal);
        if (data.heatmap_hora) renderGraficoHeatmapHora(data.heatmap_hora.labels || [], data.heatmap_hora.data || []);
        if (data.franjas) {
            renderDonutFranjas(data.franjas);
            actualizarTarjetasFranjas(data.franjas);
        }
        if (data.heatmap_matriz) {
            datosMatrizGlobal = data.heatmap_matriz;
            actualizarInsightsMatriz(datosMatrizGlobal);
            renderizarMatrizSegunModo(datosMatrizGlobal);
        }
        if (data.perfiles) renderPerfilesPermanencia(data.perfiles);
        if (data.top_productos) renderTopProductosAnalitica(data.top_productos);
    }

    async function cargarHeatmaps(opciones = {}) {
        if (typeof Chart === "undefined") return;
        const forzar = Boolean(opciones.forzar);
        const qSerie = construirQueryTemporal({ agrupacion: filtroTemporal.agrupacion || "semana" });
        const cached = heatCache.get(qSerie);

        if (!forzar && cached && (Date.now() - cached.ts) < SWR_TTL_MS) {
            aplicarAnaliticaBundle(cached.data);
            if ((Date.now() - cached.ts) < 5000) return;
        }

        try {
            const { res, seq } = await fetchCancelable("heat", `/api/analytics/bundle?${qSerie}`);
            if (!res.ok || seq !== reqSeq.heat) return;
            const data = await res.json();
            if (seq !== reqSeq.heat) return;
            heatCache.set(qSerie, { ts: Date.now(), data });
            aplicarAnaliticaBundle(data);
        } catch (_e) {
            // Silencioso en abort
        }
    }

    function renderPerfilesPermanencia(perfiles) {
        const cont = document.getElementById("lista-perfiles-permanencia");
        if (!cont) return;
        cont.textContent = "";
        if (!perfiles || perfiles.length === 0) {
            const p = document.createElement("p");
            p.className = "text-kovaq-slate m-0";
            p.textContent = "Sin registros de permanencia en este rango.";
            cont.appendChild(p);
            return;
        }
        perfiles.forEach((pf) => {
            const card = document.createElement("div");
            card.className = "bg-slate-50 border border-kovaq-border rounded-xl p-3";
            const tit = document.createElement("strong");
            tit.className = "block text-kovaq-navy font-bold";
            tit.textContent = pf.tipo;
            const val = document.createElement("span");
            val.className = "block text-xl font-extrabold font-mono-num text-kovaq-orange mt-1";
            val.textContent = `${pf.cantidad} visitas`;
            const sub = document.createElement("span");
            sub.className = "block text-kovaq-slate";
            sub.textContent = `${pf.promedio_seg} s promedio`;
            card.append(tit, val, sub);
            cont.appendChild(card);
        });
    }

    function renderTopProductosAnalitica(productos) {
        const cont = document.getElementById("lista-top-productos-analitica");
        if (!cont) return;
        cont.textContent = "";
        if (!productos || productos.length === 0) {
            const p = document.createElement("p");
            p.className = "text-kovaq-slate m-0";
            p.textContent = "Sin interacciones de productos en el rango seleccionado.";
            cont.appendChild(p);
            return;
        }
        const maxVal = Math.max(1, ...productos.map((p) => Number(p.interacciones || 0)));
        productos.forEach((pr) => {
            const row = document.createElement("div");
            row.className = "space-y-1";
            const head = document.createElement("div");
            head.className = "flex items-center justify-between gap-2";
            const nom = document.createElement("span");
            nom.className = "font-bold text-kovaq-navy truncate";
            nom.textContent = String(pr.nombre || "").replace(/\s*\(VEC-[^)]+\)/g, "");
            const cnt = document.createElement("span");
            cnt.className = "font-mono-num font-bold text-kovaq-orange shrink-0";
            cnt.textContent = `${pr.interacciones} tomas`;
            head.append(nom, cnt);

            const track = document.createElement("div");
            track.className = "w-full bg-slate-200 rounded-full h-2";
            const fill = document.createElement("div");
            fill.className = "bg-kovaq-navy h-2 rounded-full transition-[width] duration-300";
            fill.style.width = `${Math.max(8, Math.round((pr.interacciones / maxVal) * 100))}%`;
            track.appendChild(fill);
            row.append(head, track);
            cont.appendChild(row);
        });
    }

    function renderGraficoSerieTemporal(ds) {
        const canvas = document.getElementById("chart-serie-temporal");
        if (!canvas) return;
        const labels = ds.labels || [];
        const trafico = ds.trafico || [];
        const tomados = ds.tomados || [];
        const devueltos = ds.devueltos || [];

        canvas.setAttribute(
            "aria-label",
            `Serie temporal por ${ds.agrupacion || "semana"} con ${labels.length} períodos: visitas ${trafico.reduce((a, b) => a + b, 0)}, tomas ${tomados.reduce((a, b) => a + b, 0)}, devoluciones ${devueltos.reduce((a, b) => a + b, 0)}.`
        );

        const anim = prefiereMovimientoReducido() ? false : { duration: 240 };

        if (chartSerieTemporal) {
            chartSerieTemporal.data.labels = labels;
            chartSerieTemporal.data.datasets[0].data = trafico;
            chartSerieTemporal.data.datasets[1].data = tomados;
            chartSerieTemporal.data.datasets[2].data = devueltos;
            chartSerieTemporal.update(prefiereMovimientoReducido() ? "none" : undefined);
            return;
        }

        chartSerieTemporal = new Chart(canvas, {
            type: "line",
            data: {
                labels,
                datasets: [
                    { label: "Visitantes", data: trafico, borderColor: "#0C2340", backgroundColor: "rgba(12, 35, 64, 0.08)", borderWidth: 2.5, tension: 0.3, fill: true },
                    { label: "Productos Tomados", data: tomados, borderColor: "#C23B00", backgroundColor: "rgba(194, 59, 0, 0.12)", borderWidth: 2.5, tension: 0.3, fill: false },
                    { label: "Devueltos", data: devueltos, borderColor: "#1D4ED8", borderDash: [5, 5], borderWidth: 2, tension: 0.3, fill: false },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: anim,
                plugins: { legend: { position: "top" } },
                scales: { y: { beginAtZero: true }, x: { ticks: { maxTicksLimit: 12 } } },
            },
        });
    }

    function renderGraficoHeatmapHora(labels, valores) {
        const canvas = document.getElementById("chart-heatmap-hora");
        if (!canvas) return;
        const anim = prefiereMovimientoReducido() ? false : { duration: 220 };
        if (chartHeatmapHora) {
            chartHeatmapHora.data.labels = labels;
            chartHeatmapHora.data.datasets[0].data = valores;
            chartHeatmapHora.update(prefiereMovimientoReducido() ? "none" : undefined);
            return;
        }
        chartHeatmapHora = new Chart(canvas, {
            type: "bar",
            data: {
                labels,
                datasets: [{ label: "Visitas por hora", data: valores, backgroundColor: "#0C2340", borderRadius: 4 }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: anim,
                plugins: { legend: { display: false } },
                scales: { y: { beginAtZero: true } },
            },
        });
    }

    function renderDonutFranjas(df) {
        const canvas = document.getElementById("chart-franjas-donut");
        if (!canvas) return;
        const pct = df.porcentajes || {};
        const datos = [pct["Mañana"] || 0, pct["Tarde"] || 0, pct["Noche"] || 0];
        const anim = prefiereMovimientoReducido() ? false : { duration: 220 };
        if (chartFranjasDonut) {
            chartFranjasDonut.data.datasets[0].data = datos;
            chartFranjasDonut.update(prefiereMovimientoReducido() ? "none" : undefined);
            return;
        }
        chartFranjasDonut = new Chart(canvas, {
            type: "doughnut",
            data: {
                labels: ["Mañana", "Tarde", "Noche"],
                datasets: [{ data: datos, backgroundColor: ["#0C2340", "#C23B00", "#1D4ED8"], borderWidth: 2, borderColor: "#FFFFFF" }],
            },
            options: { responsive: true, maintainAspectRatio: false, animation: anim, plugins: { legend: { position: "bottom" } } },
        });
    }

    function actualizarTarjetasFranjas(df) {
        const pct = df.porcentajes || {};
        ["manana", "tarde", "noche"].forEach((k) => {
            const el = document.getElementById(`franja-${k}-trafico`);
            if (el) el.textContent = `${df[k]?.trafico || 0} clientes`;
        });
        const mapPct = { "franja-manana-pct": pct["Mañana"], "franja-tarde-pct": pct["Tarde"], "franja-noche-pct": pct["Noche"] };
        Object.entries(mapPct).forEach(([id, val]) => {
            const el = document.getElementById(id);
            if (el && val !== undefined) el.textContent = `${val}%`;
        });
    }

    function actualizarInsightsMatriz(dm) {
        const setTxt = (id, txt) => {
            const el = document.getElementById(id);
            if (el) el.textContent = txt;
        };
        if (dm.dia_pico) {
            setTxt("insight-dia-pico", dm.dia_pico.dia || "Sin datos");
            setTxt("insight-dia-pico-val", `${dm.dia_pico.total || 0} visitas`);
        }
        if (dm.hora_pico) {
            setTxt("insight-hora-pico", `${dm.hora_pico.hora} (${dm.hora_pico.dia})`);
            const h = parseInt(String(dm.hora_pico.hora || "0").split(":")[0], 10) || 0;
            let franja = "Madrugada (00h a 06h)";
            if (h >= 18) franja = "Noche (18h a 23h)";
            else if (h >= 14) franja = "Tarde (14h a 18h)";
            else if (h >= 11) franja = "Mediodía (11h a 14h)";
            else if (h >= 6) franja = "Mañana (06h a 11h)";
            setTxt("insight-patron", `Pico en ${franja}`);
        }
    }

    function renderizarMatrizSegunModo(dm) {
        const cont = document.getElementById("heatmap-matriz-container");
        if (!cont || !dm) return;
        cont.textContent = "";

        const table = document.createElement("table");
        table.className = "w-full text-left border-collapse text-xs";
        const thead = document.createElement("thead");
        thead.className = "bg-slate-100 text-kovaq-navy font-bold uppercase border-b border-kovaq-border";
        const trHead = document.createElement("tr");

        if (modoVistaMatriz === "franjas") {
            ["Día", "Mañana (06-11h)", "Mediodía (11-14h)", "Tarde (14-18h)", "Noche (18-23h)", "Madrugada (23-05h)", "Total Día"].forEach((h) => {
                const th = document.createElement("th");
                th.scope = "col";
                th.className = "p-2.5 whitespace-nowrap";
                th.textContent = h;
                trHead.appendChild(th);
            });
            thead.appendChild(trHead);
            const tbody = document.createElement("tbody");
            tbody.className = "divide-y divide-kovaq-border";
            (dm.resumen_franjas || []).forEach((row) => {
                const tr = document.createElement("tr");
                const thDia = document.createElement("th");
                thDia.scope = "row";
                thDia.className = "p-2.5 font-bold text-kovaq-navy";
                thDia.textContent = row.dia;
                tr.appendChild(thDia);
                ["manana", "mediodia", "tarde", "noche", "madrugada", "total"].forEach((k) => {
                    const td = document.createElement("td");
                    td.className = k === "total" ? "p-2.5 font-mono-num font-bold text-kovaq-navy" : "p-2.5 font-mono-num";
                    td.textContent = String(row[k] || 0);
                    tr.appendChild(td);
                });
                tbody.appendChild(tr);
            });
            table.append(thead, tbody);
        } else {
            const th0 = document.createElement("th");
            th0.scope = "col";
            th0.className = "p-2";
            th0.textContent = "Día";
            trHead.appendChild(th0);
            (dm.horas || []).forEach((hr) => {
                const th = document.createElement("th");
                th.scope = "col";
                th.className = "p-1.5 text-center font-mono-num";
                th.textContent = hr.slice(0, 2);
                trHead.appendChild(th);
            });
            thead.appendChild(trHead);
            const tbody = document.createElement("tbody");
            tbody.className = "divide-y divide-kovaq-border";
            (dm.dias || []).forEach((dia, di) => {
                const tr = document.createElement("tr");
                const thDia = document.createElement("th");
                thDia.scope = "row";
                thDia.className = "p-2 font-bold text-kovaq-navy";
                thDia.textContent = dia;
                tr.appendChild(thDia);
                const fila = (dm.matriz || [])[di] || [];
                fila.forEach((v) => {
                    const td = document.createElement("td");
                    td.className = v >= 6
                        ? "p-1.5 text-center font-mono-num font-bold bg-[#C23B00] text-white"
                        : (v > 0 ? "p-1.5 text-center font-mono-num bg-orange-50 text-kovaq-navy" : "p-1.5 text-center font-mono-num text-kovaq-slate");
                    td.textContent = String(v);
                    tr.appendChild(td);
                });
                tbody.appendChild(tr);
            });
            table.append(thead, tbody);
        }
        cont.appendChild(table);
    }

    // =========================================================================
    // CATÁLOGO DE PRODUCTOS Y CONDUCTA POR PERCHA
    // =========================================================================
    function renderTablaInventario(items) {
        const tbody = document.getElementById("tabla-inventario-body");
        if (!tbody) return;
        tbody.textContent = "";

        if (!items || items.length === 0) {
            const tr = document.createElement("tr");
            const td = document.createElement("td");
            td.colSpan = 7;
            td.className = "p-6 text-center text-kovaq-slate";
            td.textContent = "No hay productos en el planograma de esta sucursal.";
            tr.appendChild(td);
            tbody.appendChild(tr);
            filtrarInventarioLocal();
            return;
        }

        items.forEach((p) => {
            const tr = document.createElement("tr");
            tr.className = "hover:bg-slate-50 item-inventario";
            tr.dataset.nombre = `${p.nombre} ${p.categoria} ${p.percha_id} ${p.sucursal_id || ""}`.toLowerCase();
            tr.dataset.categoria = p.categoria;

            const tdProd = document.createElement("td");
            tdProd.className = "p-3 font-bold text-kovaq-navy";
            const spNom = document.createElement("span");
            spNom.className = "block";
            spNom.textContent = p.nombre;
            const spSub = document.createElement("span");
            spSub.className = "text-[11px] font-mono-num text-kovaq-slate";
            spSub.textContent = `${p.percha_id} • ${p.nivel} (${p.sucursal_id || "SUC-NORTE"})`;
            tdProd.append(spNom, spSub);

            const tdCat = document.createElement("td");
            tdCat.className = "p-3";
            const spCat = document.createElement("span");
            spCat.className = "bg-slate-100 text-kovaq-navy px-2 py-0.5 rounded font-bold whitespace-nowrap";
            spCat.textContent = p.categoria;
            tdCat.appendChild(spCat);

            const tdStock = document.createElement("td");
            tdStock.className = "p-3 font-mono-num whitespace-nowrap";
            tdStock.textContent = `${p.stock} u.`;

            const tdTomas = document.createElement("td");
            tdTomas.className = "p-3 font-mono-num font-bold whitespace-nowrap";
            tdTomas.textContent = `${p.tomas} tomas (${p.devueltos} dev.)`;

            const tdEvac = document.createElement("td");
            tdEvac.className = "p-3 font-mono-num font-bold text-emerald-800 whitespace-nowrap";
            tdEvac.textContent = `${p.tasa_evacuacion}%`;

            const tdEst = document.createElement("td");
            tdEst.className = "p-3";
            const spEst = document.createElement("span");
            const estTxt = String(p.estado || "");
            if (estTxt.includes("Crítico")) {
                spEst.className = "inline-block bg-red-100 text-red-900 px-2 py-0.5 rounded font-bold whitespace-nowrap";
            } else if (estTxt.includes("Indecisión")) {
                spEst.className = "inline-block bg-amber-100 text-amber-900 px-2 py-0.5 rounded font-bold whitespace-nowrap";
            } else if (estTxt.includes("Fría")) {
                spEst.className = "inline-block bg-blue-100 text-blue-900 px-2 py-0.5 rounded font-bold whitespace-nowrap";
            } else if (estTxt.includes("Caliente")) {
                spEst.className = "inline-block bg-orange-100 text-kovaq-orange px-2 py-0.5 rounded font-bold whitespace-nowrap";
            } else {
                spEst.className = "inline-block bg-emerald-100 text-emerald-900 px-2 py-0.5 rounded font-bold whitespace-nowrap";
            }
            spEst.textContent = estTxt;
            tdEst.appendChild(spEst);

            const tdAcc = document.createElement("td");
            tdAcc.className = "p-3 text-right";
            const btnAds = document.createElement("button");
            btnAds.type = "button";
            btnAds.className = "btn-kovaq-outline min-h-[34px] text-xs whitespace-nowrap";
            btnAds.textContent = "Analizar con IA";
            btnAds.addEventListener("click", () => {
                preguntarIARapido(`Analiza la conducta frente al producto ${p.nombre} en la percha ${p.percha_id} (${p.nivel}) y sugiere mejoras.`);
            });
            tdAcc.appendChild(btnAds);

            tr.append(tdProd, tdCat, tdStock, tdTomas, tdEvac, tdEst, tdAcc);
            tbody.appendChild(tr);
        });

        filtrarInventarioLocal();
    }

    async function cargarInventarioReal() {
        try {
            const { res, seq } = await fetchCancelable("inv", `/api/analytics/inventario?${construirQueryTemporal()}`);
            if (!res.ok || seq !== reqSeq.inv) return;
            const data = await res.json();
            if (seq !== reqSeq.inv) return;
            renderTablaInventario(data.productos || []);
        } catch (_e) {
            // Silencioso
        }
    }

    function filtrarInventarioLocal() {
        const q = (document.getElementById("input-buscar-inventario")?.value || "").trim().toLowerCase();
        const rows = Array.from(document.querySelectorAll(".item-inventario"));
        let visibles = 0;
        rows.forEach((tr) => {
            const matchCat = categoriaActiva === "Todas" || tr.dataset.categoria === categoriaActiva;
            const matchText = !q || (tr.dataset.nombre || "").includes(q);
            const ok = matchCat && matchText;
            tr.style.display = ok ? "" : "none";
            if (ok) visibles += 1;
        });
        const lbl = document.getElementById("inventario-count-label");
        if (lbl) lbl.textContent = `Mostrando ${visibles} de ${rows.length} productos del planograma.`;
    }

    // =========================================================================
    // CONFIGURACIÓN (Planograma, Sucursales, Cámaras y Promociones Opcionales)
    // =========================================================================
    async function cargarConfiguracionMapa() {
        const q = construirQueryTemporal();
        try {
            const [resSuc, resCams, resPerchas] = await Promise.all([
                fetch("/api/branches"),
                fetch(`/api/cameras?sucursal_id=${encodeURIComponent(filtroTemporal.sucursal_id)}`),
                fetch(`/api/shelves?${q}`),
            ]);
            if (resSuc.ok) renderTablaSucursales((await resSuc.json()).sucursales || []);
            if (resCams.ok) {
                const cams = (await resCams.json()).camaras || [];
                renderTablaCamaras(cams);
                actualizarSelectorCamaras(cams);
            }
            if (resPerchas.ok) {
                const perchas = (await resPerchas.json()).perchas || [];
                renderTablaPerchas(perchas);
                const selPromoPercha = document.getElementById("promo-percha");
                if (selPromoPercha && perchas.length > 0) {
                    selPromoPercha.textContent = "";
                    perchas.forEach((p) => {
                        const o = document.createElement("option");
                        o.value = p.id;
                        o.textContent = `${p.id} • ${p.categoria}`;
                        selPromoPercha.appendChild(o);
                    });
                }
            }
        } catch (_e) {
            // Silencioso
        }
    }

    function renderTablaCamaras(camaras) {
        const tbody = document.getElementById("tabla-camaras-body");
        const countEl = document.getElementById("config-camaras-count");
        if (countEl) countEl.textContent = `${camaras.length} dispositivos`;
        if (!tbody) return;
        tbody.textContent = "";

        camaras.forEach((c) => {
            const tr = document.createElement("tr");
            const tdNom = document.createElement("td");
            tdNom.className = "p-2.5 font-bold";
            tdNom.textContent = `${c.id} • ${c.nombre}`;

            const tdRuta = document.createElement("td");
            tdRuta.className = "p-2.5 font-mono-num text-kovaq-slate";
            tdRuta.textContent = c.ruta || c.fuente;

            const tdEst = document.createElement("td");
            tdEst.className = "p-2.5";
            const sp = document.createElement("span");
            sp.className = c.estado === "Activa"
                ? "bg-emerald-100 text-emerald-900 px-2 py-0.5 rounded font-bold"
                : "bg-slate-200 text-kovaq-navy px-2 py-0.5 rounded font-bold";
            sp.textContent = c.estado;
            tdEst.appendChild(sp);

            const tdAcc = document.createElement("td");
            tdAcc.className = "p-2.5 text-right";
            const btnDel = document.createElement("button");
            btnDel.type = "button";
            btnDel.className = "min-w-[36px] min-h-[36px] inline-flex items-center justify-center rounded-lg border border-red-300 text-red-800 hover:bg-red-700 hover:text-white";
            btnDel.setAttribute("aria-label", `Eliminar cámara ${c.id}`);
            const ic = document.createElement("i");
            ic.className = "bi bi-trash";
            ic.setAttribute("aria-hidden", "true");
            btnDel.appendChild(ic);
            btnDel.addEventListener("click", () => eliminarCamaraBackend(c.id));
            tdAcc.appendChild(btnDel);

            tr.append(tdNom, tdRuta, tdEst, tdAcc);
            tbody.appendChild(tr);
        });
    }

    function renderTablaPerchas(perchas) {
        const tbody = document.getElementById("tabla-perchas-config-body");
        const countEl = document.getElementById("config-perchas-count");
        if (countEl) countEl.textContent = `${perchas.length} perchas`;
        if (!tbody) return;
        tbody.textContent = "";

        perchas.forEach((p) => {
            const tr = document.createElement("tr");

            const tdId = document.createElement("th");
            tdId.scope = "row";
            tdId.className = "p-2.5 font-mono-num font-bold text-kovaq-orange";
            tdId.textContent = p.id;

            const tdNom = document.createElement("td");
            tdNom.className = "p-2.5 font-bold";
            tdNom.textContent = `${p.nombre} (${p.camara_id})`;

            const tdNiv = document.createElement("td");
            tdNiv.className = "p-2.5 text-kovaq-slate";
            tdNiv.textContent = (p.niveles || []).map((n) => `${n.nivel}: ${n.producto}`).join(" | ");

            const tdPromo = document.createElement("td");
            tdPromo.className = "p-2.5";
            tdPromo.textContent = (p.promociones && p.promociones.length > 0)
                ? p.promociones.map((pr) => `${pr.titulo} (-${pr.descuento_pct}%)`).join(", ")
                : "Sin extras";

            const tdAct = document.createElement("td");
            tdAct.className = "p-2.5 font-mono-num font-bold";
            tdAct.textContent = `${p.tomas || 0} tomas / ${p.devoluciones || 0} dev.`;

            tr.append(tdId, tdNom, tdNiv, tdPromo, tdAct);
            tbody.appendChild(tr);
        });
    }

    async function eliminarCamaraBackend(camId) {
        const res = await fetch(`/api/cameras/${encodeURIComponent(camId)}?sucursal_id=${encodeURIComponent(filtroTemporal.sucursal_id)}`, { method: "DELETE" });
        if (res.ok) {
            invalidarCaches();
            renderTablaCamaras((await res.json()).camaras || []);
            mostrarAviso("config-feedback-banner", `Cámara ${camId} eliminada del plano.`, "ok");
            sincronizarMapa2D();
        } else {
            mostrarAviso("config-feedback-banner", "No tienes permisos o falló la eliminación de cámara.", "error");
        }
    }

    // =========================================================================
    // CHAT KOVAQ IA (100% textContent, enfocado en productos y perchas)
    // =========================================================================
    function agregarBurbuja(autor, texto, tipo) {
        const cont = document.getElementById("chat-messages");
        if (!cont) return null;
        const div = document.createElement("div");
        const esUsuario = tipo === "user";
        div.className = esUsuario
            ? "p-3 rounded-xl text-xs bg-kovaq-navy text-white ml-auto max-w-[90%]"
            : "p-3 rounded-xl text-xs bg-white border border-kovaq-border text-kovaq-navy mr-auto max-w-[90%]";

        const strong = document.createElement("strong");
        strong.className = esUsuario ? "font-title block mb-1 text-orange-200" : "font-title block mb-1 text-kovaq-orange";
        strong.textContent = autor;

        const span = document.createElement("span");
        span.className = "texto whitespace-pre-wrap leading-relaxed";
        span.textContent = texto;

        div.append(strong, span);
        cont.appendChild(div);
        cont.scrollTop = cont.scrollHeight;
        return div;
    }

    async function enviarPreguntaIA(mensaje) {
        const modo = document.getElementById("chat-modo")?.value || "estrategia";
        const btn = document.getElementById("chat-submit");
        agregarBurbuja("Tú", mensaje, "user");
        if (btn) btn.disabled = true;
        const burbujaIA = agregarBurbuja("Kovaq IA", "Analizando productos y conducta en la sucursal seleccionada...", "bot");

        try {
            const res = await fetch("/api/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    mensaje,
                    modo,
                    sucursal_id: filtroTemporal.sucursal_id,
                    dias: filtroTemporal.dias || 30,
                    horas: filtroTemporal.horas,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || "Error consultando IA");
            if (burbujaIA) burbujaIA.querySelector(".texto").textContent = data.respuesta || "Sin respuesta.";
            const cont = document.getElementById("chat-messages");
            if (cont) cont.scrollTop = cont.scrollHeight;
        } catch (err) {
            if (burbujaIA) burbujaIA.querySelector(".texto").textContent = `Error: ${err.message}`;
            const cont = document.getElementById("chat-messages");
            if (cont) cont.scrollTop = cont.scrollHeight;
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    function preguntarIARapido(texto) {
        cambiarModulo("tab-marketing");
        enviarPreguntaIA(texto);
    }

    // =========================================================================
    // VIDEO, FUENTES Y SUBIDA INSTANTÁNEA
    // =========================================================================
    function desmontarStreamPrevio() {
        const liveImg = document.getElementById("live-stream-img");
        if (liveImg) {
            liveImg.src = "";
            liveImg.remove();
        }
    }

    function mostrarVideoRespaldo() {
        desmontarStreamPrevio();
        const cont = document.getElementById("video-container");
        if (!cont) return;
        cont.textContent = "";
        const video = document.createElement("video");
        video.id = "demo-video-player";
        video.width = 960;
        video.height = 540;
        video.preload = "auto";
        video.loop = true;
        video.muted = true;
        video.playsInline = true;
        video.className = "w-full h-full";
        const src = document.createElement("source");
        src.src = "/static/demo_supermercado.mp4";
        src.type = "video/mp4";
        video.appendChild(src);
        cont.appendChild(video);

        if (!prefiereMovimientoReducido()) video.play().catch(() => {});
    }

    function mostrarStreamProcesado() {
        desmontarStreamPrevio();
        const cont = document.getElementById("video-container");
        if (!cont) return;
        cont.textContent = "";
        const img = document.createElement("img");
        img.id = "live-stream-img";
        img.width = 960;
        img.height = 540;
        img.decoding = "async";
        img.alt = "Transmisión procesada por KOVAQ con polígonos de percha y detección anónima";
        img.className = "w-full h-full";
        img.src = `/api/video_feed?t=${Date.now()}`;
        img.addEventListener("error", () => mostrarVideoRespaldo());
        cont.appendChild(img);
    }

    function inicializarControlesVideoYSubida() {
        const videoEl = document.getElementById("demo-video-player");
        const btnPlay = document.getElementById("btn-toggle-video-play");
        const txtPlay = document.getElementById("txt-toggle-video-play");

        if (videoEl && prefiereMovimientoReducido()) {
            videoEl.pause();
            if (txtPlay) txtPlay.textContent = "Reproducir";
            if (btnPlay) btnPlay.setAttribute("aria-pressed", "true");
        }

        if (btnPlay) {
            btnPlay.addEventListener("click", () => {
                const v = document.getElementById("demo-video-player");
                if (!v) return;
                if (v.paused) {
                    v.play().catch(() => {});
                    if (txtPlay) txtPlay.textContent = "Pausar";
                    btnPlay.setAttribute("aria-pressed", "false");
                } else {
                    v.pause();
                    if (txtPlay) txtPlay.textContent = "Reproducir";
                    btnPlay.setAttribute("aria-pressed", "true");
                }
            });
        }

        const btnSim = document.getElementById("btn-toggle-sim");
        if (btnSim) {
            btnSim.addEventListener("click", async () => {
                const res = await fetch("/api/simulation/toggle", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({}),
                });
                if (res.ok) sincronizarMapa2D();
                else mostrarAviso("global-status-banner", "Requiere rol de gestión para pausar o activar la simulación.", "error");
            });
        }

        const selCamMapa = document.getElementById("select-camara-mapa");
        if (selCamMapa) {
            selCamMapa.addEventListener("change", async () => {
                const res = await fetch("/api/cameras/select", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ camara_id: selCamMapa.value }),
                });
                if (res.ok) {
                    const tit = document.getElementById("titulo-camara-activa");
                    if (tit) tit.textContent = selCamMapa.options[selCamMapa.selectedIndex]?.text || selCamMapa.value;
                    sincronizarMapa2D();
                }
            });
        }

        const selFuente = document.getElementById("select-camara");
        const rtspPanel = document.getElementById("rtsp-input-panel");
        if (selFuente && rtspPanel) {
            selFuente.addEventListener("change", () => {
                rtspPanel.classList.toggle("hidden", selFuente.value !== "rtsp");
            });
        }

        async function aplicarFuente(tipo, ruta = null) {
            const toast = document.getElementById("feed-loading-toast");
            if (toast) {
                toast.textContent = "Conectando fuente...";
                toast.classList.remove("hidden");
            }
            const res = await fetch("/api/fuente", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ tipo, ruta }),
            });
            if (toast) toast.classList.add("hidden");
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                mostrarAviso("global-status-banner", err.detail || "No se pudo cambiar la fuente.", "error");
                return;
            }
            if (tipo === "video") {
                mostrarVideoRespaldo();
            } else {
                const modo = document.body.dataset.modo || "local";
                if (modo === "local") mostrarStreamProcesado();
                else mostrarVideoRespaldo();
            }
        }

        const btnConectar = document.getElementById("btn-conectar-fuente");
        if (btnConectar && selFuente) {
            btnConectar.addEventListener("click", () => {
                if (selFuente.value === "rtsp") {
                    if (rtspPanel) rtspPanel.classList.remove("hidden");
                    document.getElementById("input-rtsp-url")?.focus();
                } else {
                    aplicarFuente(selFuente.value);
                }
            });
        }

        const btnRtsp = document.getElementById("btn-conectar-rtsp");
        if (btnRtsp) {
            btnRtsp.addEventListener("click", () => {
                const url = (document.getElementById("input-rtsp-url")?.value || "").trim();
                if (!url) {
                    mostrarAviso("global-status-banner", "Ingresa una URL rtsp:// válida antes de conectar.", "error");
                    return;
                }
                aplicarFuente("rtsp", url);
            });
        }

        const btnSubir = document.getElementById("btn-subir-archivo");
        if (btnSubir) {
            btnSubir.addEventListener("click", async () => {
                const input = document.getElementById("archivo-input");
                const statusEl = document.getElementById("upload-status");
                if (!input || !input.files || !input.files[0]) {
                    if (statusEl) statusEl.textContent = "Selecciona un archivo de video o imagen antes de analizar.";
                    return;
                }
                btnSubir.disabled = true;
                if (statusEl) statusEl.textContent = "Subiendo y analizando fotogramas clave con YOLOv8n...";
                const fd = new FormData();
                fd.append("archivo", input.files[0]);
                try {
                    const res = await fetch("/api/upload", { method: "POST", body: fd });
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) throw new Error(data.detail || "Error procesando archivo");
                    if (statusEl) statusEl.textContent = data.resumen || "Archivo analizado correctamente.";
                    invalidarCaches();
                    mostrarStreamProcesado();
                    await Promise.all([refrescarDashboardsPorPeriodo({ forzar: true }), sincronizarTelemetriaBackend()]);
                } catch (err) {
                    if (statusEl) statusEl.textContent = `Error: ${err.message}`;
                } finally {
                    btnSubir.disabled = false;
                }
            });
        }
    }

    function inicializarFormulariosYControles() {
        const btnFranjas = document.getElementById("btn-vista-franjas");
        const btn24h = document.getElementById("btn-vista-24h");
        const toggleMatriz = (modo) => {
            modoVistaMatriz = modo;
            if (btnFranjas && btn24h) {
                btnFranjas.setAttribute("aria-pressed", modo === "franjas" ? "true" : "false");
                btn24h.setAttribute("aria-pressed", modo === "24h" ? "true" : "false");
                btnFranjas.className = modo === "franjas"
                    ? "btn-kovaq-outline min-h-[34px] text-xs bg-kovaq-navy text-white border-kovaq-navy"
                    : "btn-kovaq-outline min-h-[34px] text-xs";
                btn24h.className = modo === "24h"
                    ? "btn-kovaq-outline min-h-[34px] text-xs bg-kovaq-navy text-white border-kovaq-navy"
                    : "btn-kovaq-outline min-h-[34px] text-xs";
            }
            if (datosMatrizGlobal) renderizarMatrizSegunModo(datosMatrizGlobal);
        };
        if (btnFranjas) btnFranjas.addEventListener("click", () => toggleMatriz("franjas"));
        if (btn24h) btn24h.addEventListener("click", () => toggleMatriz("24h"));

        // Productos: búsqueda con debounce + menú desplegable de categoría
        const searchInv = document.getElementById("input-buscar-inventario");
        let debounceTimer = null;
        if (searchInv) {
            searchInv.addEventListener("input", () => {
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(filtrarInventarioLocal, 140);
            });
        }
        const selCat = document.getElementById("select-categoria-productos");
        if (selCat) {
            selCat.addEventListener("change", () => {
                categoriaActiva = selCat.value || "Todas";
                filtrarInventarioLocal();
            });
        }

        // Chat IA
        const chatForm = document.getElementById("chat-form");
        if (chatForm) {
            chatForm.addEventListener("submit", (ev) => {
                ev.preventDefault();
                const input = document.getElementById("chat-input");
                const msg = (input?.value || "").trim();
                if (!msg) return;
                input.value = "";
                enviarPreguntaIA(msg);
            });
        }
        document.querySelectorAll(".btn-chat-quick").forEach((b) => {
            b.addEventListener("click", () => {
                const p = b.dataset.prompt;
                if (p) enviarPreguntaIA(p);
            });
        });

        // Nueva sucursal
        const formSuc = document.getElementById("form-nueva-sucursal");
        if (formSuc) {
            formSuc.addEventListener("submit", async (ev) => {
                ev.preventDefault();
                const idVal = (document.getElementById("suc-id")?.value || "").trim().toUpperCase();
                const res = await fetch("/api/branches", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        id: idVal || null,
                        nombre: document.getElementById("suc-nombre").value.trim(),
                        ciudad: document.getElementById("suc-ciudad").value.trim(),
                        formato: document.getElementById("suc-formato").value,
                        area_m2: 1200,
                    }),
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok) {
                    invalidarCaches();
                    poblarSelectorSucursales(data.sucursales || []);
                    renderTablaSucursales(data.sucursales || []);
                    formSuc.reset();
                    mostrarAviso("config-feedback-banner", `Sucursal ${data.sucursal_id} registrada correctamente.`, "ok");
                } else {
                    mostrarAviso("config-feedback-banner", data.detail || "No se pudo registrar la sucursal.", "error");
                }
            });
        }

        // Regenerar datos de simulación
        const btnReseed = document.getElementById("btn-regenerar-simulacion");
        if (btnReseed) {
            btnReseed.addEventListener("click", async () => {
                btnReseed.disabled = true;
                mostrarAviso("config-feedback-banner", "Regenerando datos históricos y horarios de simulación...", "info");
                try {
                    const res = await fetch("/api/simulation/reseed", { method: "POST" });
                    const data = await res.json().catch(() => ({}));
                    if (res.ok) {
                        invalidarCaches();
                        mostrarAviso("config-feedback-banner", data.mensaje || "Datos de simulación actualizados.", "ok");
                        await refrescarDashboardsPorPeriodo({ forzar: true });
                    } else {
                        mostrarAviso("config-feedback-banner", data.detail || "No tienes permisos para regenerar simulación.", "error");
                    }
                } finally {
                    btnReseed.disabled = false;
                }
            });
        }

        // Formulario nueva cámara
        const formCam = document.getElementById("form-nueva-camara");
        if (formCam) {
            formCam.addEventListener("submit", async (ev) => {
                ev.preventDefault();
                const res = await fetch("/api/cameras", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        nombre: document.getElementById("cam-nombre").value.trim(),
                        ruta: document.getElementById("cam-url").value.trim(),
                        estado: document.getElementById("cam-estado").value,
                        sucursal_id: sucursalEfectivaEscritura(),
                    }),
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok) {
                    invalidarCaches();
                    renderTablaCamaras(data.camaras || []);
                    formCam.reset();
                    mostrarAviso("config-feedback-banner", "Cámara registrada correctamente en la sucursal.", "ok");
                    sincronizarMapa2D();
                } else {
                    mostrarAviso("config-feedback-banner", data.detail || "No se pudo registrar la cámara.", "error");
                }
            });
        }

        // Formulario nueva promoción (opcional)
        const formPromo = document.getElementById("form-nueva-promo");
        if (formPromo) {
            formPromo.addEventListener("submit", async (ev) => {
                ev.preventDefault();
                const res = await fetch("/api/promotions", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        percha_id: document.getElementById("promo-percha").value,
                        producto: document.getElementById("promo-producto").value.trim(),
                        titulo: document.getElementById("promo-titulo").value.trim(),
                        tipo_publicidad: document.getElementById("promo-tipo").value.trim(),
                        descuento_pct: parseInt(document.getElementById("promo-desc").value || "0", 10),
                        sucursal_id: sucursalEfectivaEscritura(),
                    }),
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok) {
                    invalidarCaches();
                    formPromo.reset();
                    mostrarAviso("config-feedback-banner", "Promoción opcional guardada.", "ok");
                    await Promise.all([cargarConfiguracionMapa(), cargarResumenEjecutivoPublicidad(), sincronizarMapa2D()]);
                } else {
                    mostrarAviso("config-feedback-banner", data.detail || "No se pudo guardar la promoción.", "error");
                }
            });
        }

        // Formulario planograma por niveles
        const formPlano = document.getElementById("form-planograma-percha");
        if (formPlano) {
            formPlano.addEventListener("submit", async (ev) => {
                ev.preventDefault();
                const res = await fetch("/api/shelves", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        id: document.getElementById("plano-id").value.trim().toUpperCase(),
                        nombre: document.getElementById("plano-nombre").value.trim(),
                        categoria: document.getElementById("plano-cat").value.trim(),
                        camara_id: document.getElementById("plano-cam").value,
                        sucursal_id: sucursalEfectivaEscritura(),
                        niveles: [
                            { nivel: "Superior (Subir Cabeza)", y1: 0.18, y2: 0.42, producto: document.getElementById("plano-prod-sup").value.trim() },
                            { nivel: "Medio (Altura Ojos)", y1: 0.42, y2: 0.68, producto: document.getElementById("plano-prod-med").value.trim() },
                            { nivel: "Inferior (Agacharse)", y1: 0.68, y2: 0.90, producto: document.getElementById("plano-prod-inf").value.trim() },
                        ],
                    }),
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok) {
                    invalidarCaches();
                    renderTablaPerchas(data.perchas || []);
                    formPlano.reset();
                    mostrarAviso("config-feedback-banner", "Planograma actualizado en la sucursal.", "ok");
                    await Promise.all([cargarInventarioReal(), cargarResumenEjecutivoPublicidad(), sincronizarMapa2D()]);
                } else {
                    mostrarAviso("config-feedback-banner", data.detail || "No se pudo guardar el planograma.", "error");
                }
            });
        }
    }

    // Arranque y polling silencioso gobernado por visibilidad
    document.addEventListener("DOMContentLoaded", async () => {
        inicializarTabs();
        inicializarCierreGlobalPopovers();
        inicializarFiltrosTemporalesYSucursal();
        inicializarInteraccionMapa();
        inicializarControlesVideoYSubida();
        inicializarFormulariosYControles();
        actualizarEtiquetasPeriodo();

        await Promise.allSettled([cargarSucursales(), cargarDemoTracks()]);
        iniciarBucleHudVideo();
        await Promise.allSettled([
            refrescarDashboardsPorPeriodo({ silencioso: false }),
            sincronizarTelemetriaBackend(),
        ]);

        // Revalidar silenciosamente al volver a enfocar la pestaña
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) {
                refrescarDashboardsPorPeriodo({ silencioso: true, forzar: true });
                if (moduloActivo === "tab-vivo") sincronizarTelemetriaBackend();
            }
        });

        setInterval(() => {
            if (!document.hidden && moduloActivo === "tab-vivo") {
                sincronizarTelemetriaBackend();
            }
        }, 4000);

        setInterval(() => {
            if (!document.hidden) {
                refrescarDashboardsPorPeriodo({ silencioso: true, forzar: true });
            }
        }, 30000);
    });
})();
