(function () {
    "use strict";

    // Estado global del dashboard
    let moduloActivo = "tab-vivo";
    let filtroTemporal = { agrupacion: "semana", dias: 30, fecha_inicio: "", fecha_fin: "" };
    let modoVistaMatriz = "franjas";
    let datosMatrizGlobal = null;
    let categoriaActiva = "Todas";
    let lastSeenEventId = 0;

    let chartSerieTemporal = null;
    let chartHeatmapHora = null;
    let chartFranjasDonut = null;

    const enVuelo = { mapa: false, live: false, stats: false, pub: false, heat: false };

    function escHtml(valor) {
        return String(valor ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
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

    function construirQueryTemporal(extra = {}) {
        const params = new URLSearchParams();
        params.set("dias", String(filtroTemporal.dias || 30));
        if (filtroTemporal.fecha_inicio) params.set("fecha_inicio", filtroTemporal.fecha_inicio);
        if (filtroTemporal.fecha_fin) params.set("fecha_fin", filtroTemporal.fecha_fin);
        Object.entries(extra).forEach(([k, v]) => {
            if (v !== undefined && v !== null) params.set(k, String(v));
        });
        return params.toString();
    }

    // Navegación ARIA Tablist (clic + ArrowLeft/ArrowRight/Home/End)
    function cambiarModulo(moduloId) {
        const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
        const panes = Array.from(document.querySelectorAll('[role="tabpanel"]'));

        panes.forEach((pane) => {
            pane.classList.toggle("active", pane.id === moduloId);
        });
        tabs.forEach((tab) => {
            const esActivo = tab.getAttribute("aria-controls") === moduloId;
            tab.classList.toggle("active", esActivo);
            tab.setAttribute("aria-selected", esActivo ? "true" : "false");
            tab.setAttribute("tabindex", esActivo ? "0" : "-1");
        });

        moduloActivo = moduloId;

        if (moduloId === "tab-percha") {
            cargarHeatmaps();
        } else if (moduloId === "tab-inventario") {
            cargarInventarioReal();
        } else if (moduloId === "tab-marketing") {
            cargarResumenEjecutivoPublicidad();
        } else if (moduloId === "tab-config") {
            cargarConfiguracionMapa();
        }
    }

    function inicializarTabs() {
        const tablist = document.querySelector('[role="tablist"]');
        if (!tablist) return;
        const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));

        tabs.forEach((tab, idx) => {
            tab.addEventListener("click", () => {
                cambiarModulo(tab.getAttribute("aria-controls"));
            });
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

    // Filtros temporales globales
    function actualizarEtiquetasPeriodo() {
        const mapNombre = { dia: "Día", semana: "Semana", mes: "Mes" };
        const agrupTxt = mapNombre[filtroTemporal.agrupacion] || "Semana";
        const lblChart = document.getElementById("lbl-agrupacion-chart");
        if (lblChart) lblChart.textContent = agrupTxt;

        const textoRango = filtroTemporal.fecha_inicio
            ? `Rango: ${filtroTemporal.fecha_inicio} a ${filtroTemporal.fecha_fin}`
            : `Últimos ${filtroTemporal.dias} días (${agrupTxt})`;

        const sub = document.getElementById("subtitulo-periodo-activo");
        if (sub) sub.textContent = `Datos históricos desde Turso • ${textoRango}`;

        ["badge-rango-fechas", "badge-rango-fechas-percha"].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.textContent = textoRango;
        });
    }

    async function refrescarDashboardsPorPeriodo() {
        const tareas = [actualizarDatosDashboard(), cargarResumenEjecutivoPublicidad()];
        if (moduloActivo === "tab-percha") tareas.push(cargarHeatmaps());
        if (moduloActivo === "tab-inventario") tareas.push(cargarInventarioReal());
        await Promise.all(tareas);
    }

    function inicializarFiltrosTemporales() {
        const presetBtns = Array.from(document.querySelectorAll(".btn-periodo-preset"));
        presetBtns.forEach((btn) => {
            btn.addEventListener("click", () => {
                filtroTemporal.agrupacion = btn.dataset.agrup || "semana";
                filtroTemporal.dias = parseInt(btn.dataset.dias || "30", 10);
                filtroTemporal.fecha_inicio = "";
                filtroTemporal.fecha_fin = "";

                const fi = document.getElementById("filtro-fecha-inicio");
                const ff = document.getElementById("filtro-fecha-fin");
                if (fi) fi.value = "";
                if (ff) ff.value = "";

                const selAgrup = document.getElementById("select-agrupacion-temporal");
                if (selAgrup) selAgrup.value = filtroTemporal.agrupacion;

                presetBtns.forEach((b) => {
                    const activo = b === btn;
                    b.setAttribute("aria-pressed", activo ? "true" : "false");
                    b.className = activo
                        ? "btn-periodo-preset min-h-[36px] px-3 py-1 rounded-lg text-xs font-bold border border-kovaq-navy bg-kovaq-navy text-white"
                        : "btn-periodo-preset min-h-[36px] px-3 py-1 rounded-lg text-xs font-bold border border-kovaq-control bg-white text-kovaq-navy";
                });

                actualizarEtiquetasPeriodo();
                refrescarDashboardsPorPeriodo();
            });
        });

        const btnRango = document.getElementById("btn-aplicar-rango");
        if (btnRango) {
            btnRango.addEventListener("click", () => {
                const fi = document.getElementById("filtro-fecha-inicio")?.value || "";
                const ff = document.getElementById("filtro-fecha-fin")?.value || "";
                if (!fi) {
                    mostrarAviso("global-status-banner", "Selecciona al menos la fecha de inicio para aplicar un rango personalizado.", "error");
                    return;
                }
                filtroTemporal.fecha_inicio = fi;
                filtroTemporal.fecha_fin = ff || fi;
                mostrarAviso("global-status-banner", `Filtro aplicado del ${filtroTemporal.fecha_inicio} al ${filtroTemporal.fecha_fin}.`, "ok");
                actualizarEtiquetasPeriodo();
                refrescarDashboardsPorPeriodo();
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

    // Plano Topológico 2D
    function dibujarPlanoTienda2D(data) {
        const canvas = document.getElementById("store-map-canvas");
        if (!canvas) return;
        const ctx = canvas.getContext("2d");
        const W = canvas.width;
        const H = canvas.height;

        ctx.fillStyle = "#08172B";
        ctx.fillRect(0, 0, W, H);

        ctx.strokeStyle = "rgba(255, 255, 255, 0.06)";
        ctx.lineWidth = 1;
        for (let x = 0; x < W; x += 46) {
            ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
        }
        for (let y = 0; y < H; y += 45) {
            ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
        }

        // Matriz de calor espacial
        const grid = data.heatmap_grid || [];
        if (grid.length > 0 && grid[0].length > 0) {
            const rows = grid.length;
            const cols = grid[0].length;
            let maxVal = 1.0;
            for (let r = 0; r < rows; r++) {
                for (let c = 0; c < cols; c++) {
                    if (grid[r][c] > maxVal) maxVal = grid[r][c];
                }
            }
            const cellW = W / cols;
            const cellH = H / rows;
            for (let r = 0; r < rows; r++) {
                for (let c = 0; c < cols; c++) {
                    const val = grid[r][c];
                    if (val <= 0.1) continue;
                    const norm = Math.min(1.0, val / maxVal);
                    const gx = (c + 0.5) * cellW;
                    const gy = (r + 0.5) * cellH;
                    const radio = Math.max(cellW, cellH) * 0.9;
                    const grad = ctx.createRadialGradient(gx, gy, 2, gx, gy, radio);
                    if (norm >= 0.45) {
                        grad.addColorStop(0, `rgba(254, 80, 0, ${(0.18 + norm * 0.32).toFixed(2)})`);
                        grad.addColorStop(1, "rgba(254, 80, 0, 0)");
                    } else {
                        grad.addColorStop(0, `rgba(59, 130, 246, ${(0.12 + norm * 0.2).toFixed(2)})`);
                        grad.addColorStop(1, "rgba(59, 130, 246, 0)");
                    }
                    ctx.fillStyle = grad;
                    ctx.fillRect(c * cellW - cellW * 0.5, r * cellH - cellH * 0.5, cellW * 2, cellH * 2);
                }
            }
        }

        // Puerta de entrada y línea de cajas
        const arq = data.zonas_arquitectura || {};
        [
            { z: arq.puerta_entrada, stroke: "#22C55E", fill: "rgba(34, 197, 94, 0.16)", label: "PUERTA" },
            { z: arq.linea_cajas, stroke: "#3B82F6", fill: "rgba(59, 130, 246, 0.16)", label: "CAJAS" },
        ].forEach(({ z, stroke, fill, label }) => {
            if (!z) return;
            const zx = z.x * W, zy = z.y * H, zw = z.w * W, zh = z.h * H;
            ctx.fillStyle = fill;
            ctx.strokeStyle = stroke;
            ctx.lineWidth = 2;
            ctx.fillRect(zx, zy, zw, zh);
            ctx.strokeRect(zx, zy, zw, zh);
            ctx.fillStyle = "#FFFFFF";
            ctx.font = "bold 10px 'JetBrains Mono', monospace";
            ctx.fillText(label, zx + 4, zy + 16);
        });

        // Cámaras y conos FOV
        (data.camaras || []).forEach((cam) => {
            const cx = cam.pos_x * W;
            const cy = cam.pos_y * H;
            const rad = ((cam.angulo || 0) * Math.PI) / 180;
            const fovRad = (((cam.fov || 68) * Math.PI) / 180) / 2;
            const alcancePx = (cam.alcance || 0.42) * W * 0.55;
            const esActiva = cam.id === data.camara_activa;

            ctx.save();
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.arc(cx, cy, alcancePx, rad - fovRad, rad + fovRad);
            ctx.closePath();
            ctx.fillStyle = esActiva ? "rgba(254, 80, 0, 0.12)" : "rgba(255, 255, 255, 0.04)";
            ctx.fill();
            ctx.strokeStyle = esActiva ? "rgba(254, 80, 0, 0.5)" : "rgba(255, 255, 255, 0.16)";
            ctx.lineWidth = 1;
            ctx.stroke();

            ctx.beginPath();
            ctx.arc(cx, cy, 7, 0, Math.PI * 2);
            ctx.fillStyle = esActiva ? "#FE5000" : "#334155";
            ctx.fill();
            ctx.strokeStyle = "#FFFFFF";
            ctx.lineWidth = 2;
            ctx.stroke();

            ctx.fillStyle = "#FFFFFF";
            ctx.font = "bold 10px 'Space Grotesk', sans-serif";
            ctx.fillText(cam.id, cx - 16, cy - 11);
            ctx.restore();
        });

        // Perchas mapeadas
        (data.perchas || []).forEach((p) => {
            const px = p.map_x * W, py = p.map_y * H, pw = p.map_w * W, ph = p.map_h * H;
            ctx.save();
            ctx.fillStyle = "rgba(15, 23, 42, 0.92)";
            ctx.strokeStyle = p.color || "#FE5000";
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.roundRect(px, py, pw, ph, 8);
            ctx.fill();
            ctx.stroke();

            ctx.fillStyle = "#FFFFFF";
            ctx.font = "bold 11px 'Space Grotesk', sans-serif";
            ctx.fillText(String(p.nombre || p.id).slice(0, 26), px + 8, py + 19);

            ctx.fillStyle = "#CBD5E1";
            ctx.font = "10px 'JetBrains Mono', monospace";
            ctx.fillText(`Tomas: ${p.tomas || 0} | Dev: ${p.devoluciones || 0}`, px + 8, py + 36);
            ctx.restore();
        });

        // Vectores anónimos de personas
        (data.personas || []).forEach((pers) => {
            const px = pers.x * W;
            const py = pers.y * H;
            const esCli = pers.es_cliente_valido !== false;
            const color = esCli ? (pers.color || "#22C55E") : "#94A3B8";

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
                ctx.arc(px, py, 8, 0, Math.PI * 2);
            } else {
                ctx.moveTo(px, py - 8);
                ctx.lineTo(px + 8, py);
                ctx.lineTo(px, py + 8);
                ctx.lineTo(px - 8, py);
                ctx.closePath();
            }
            ctx.fill();
            ctx.stroke();

            ctx.fillStyle = "#FFFFFF";
            ctx.font = "bold 10px 'JetBrains Mono', monospace";
            ctx.fillText(pers.etiqueta || "S#1", px + 11, py + 4);
            ctx.restore();
        });

        const totalClientes = (data.personas || []).filter((p) => p.es_cliente_valido !== false).length;
        const totalPerchas = (data.perchas || []).length;
        canvas.setAttribute(
            "aria-label",
            `Plano 2D activo: ${totalPerchas} perchas mapeadas, ${totalClientes} clientes anónimos en tienda y ${(data.handoffs || []).length} transiciones entre cámaras.`
        );
    }

    function actualizarPanelesReID(data) {
        const handoffCount = document.getElementById("map-handoff-counter");
        const handoffs = data.handoffs || [];
        if (handoffCount) handoffCount.textContent = `${handoffs.length} handoffs activos`;

        const simBtnTxt = document.getElementById("txt-toggle-sim");
        if (simBtnTxt) {
            simBtnTxt.textContent = data.simulacion_activa ? "Pausar simulación" : "Activar simulación";
        }

        const listaPers = document.getElementById("lista-personas-reid");
        if (listaPers) {
            const personas = data.personas || [];
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
                    strong.textContent = `${p.etiqueta} (${p.camara_actual})`;
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
        if (document.hidden || moduloActivo !== "tab-vivo" || enVuelo.mapa) return;
        enVuelo.mapa = true;
        try {
            const res = await fetch("/api/map_state");
            if (!res.ok) return;
            const data = await res.json();
            dibujarPlanoTienda2D(data);
            actualizarPanelesReID(data);
        } catch (_e) {
            // Silencioso en desconexión temporal
        } finally {
            enVuelo.mapa = false;
        }
    }

    // Telemetría viva y auditoría (capada a 50 filas, separada de KPIs históricos)
    async function sincronizarTelemetriaBackend() {
        if (document.hidden || enVuelo.live) return;
        enVuelo.live = true;
        try {
            const res = await fetch("/api/live_events");
            if (!res.ok) return;
            const data = await res.json();

            const estadoEl = document.getElementById("estado-sistema-texto");
            if (estadoEl) {
                if (data.simulacion_activa) estadoEl.textContent = "Gemelo 2D activo";
                else if (data.estado === "en_vivo") estadoEl.textContent = "Cámara en vivo";
                else estadoEl.textContent = "En espera";
            }

            const sesionEl = document.getElementById("stat-live-sesion");
            if (sesionEl) sesionEl.textContent = `Sesión viva: ${data.personas_activas || 0} activos`;

            const sesTom = document.getElementById("stat-sesion-tomados");
            const sesDev = document.getElementById("stat-sesion-devueltos");
            if (sesTom) sesTom.textContent = String(data.agarrados || 0);
            if (sesDev) sesDev.textContent = String(data.devueltos || 0);

            dibujarHudSobreVideo(data);

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
        } finally {
            enVuelo.live = false;
        }
    }

    function dibujarHudSobreVideo(data) {
        const canvas = document.getElementById("yolo-hud-canvas");
        if (!canvas || moduloActivo !== "tab-vivo") return;
        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, canvas.width, canvas.height);

        (data.perchas_roi || []).forEach((po) => {
            const [x1, y1, x2, y2] = po.box || [0, 0, 0, 0];
            ctx.strokeStyle = "#FE5000";
            ctx.lineWidth = 1.5;
            ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
            ctx.fillStyle = "rgba(12, 35, 64, 0.85)";
            ctx.fillRect(x1, Math.max(0, y1 - 18), 140, 18);
            ctx.fillStyle = "#FFFFFF";
            ctx.font = "bold 10px 'Space Grotesk', sans-serif";
            ctx.fillText(`${po.id}: ${po.categoria || ""}`, x1 + 4, Math.max(12, y1 - 5));
        });
    }

    // KPIs Históricos (Fuente Única: /api/stats)
    async function actualizarDatosDashboard() {
        if (enVuelo.stats) return;
        enVuelo.stats = true;
        try {
            const res = await fetch(`/api/stats?${construirQueryTemporal()}`);
            if (!res.ok) return;
            const d = await res.json();

            const traf = Number(d.trafico || 0);
            const tom = Number(d.tomados || 0);
            const dev = Number(d.devueltos || 0);
            const perm = Number(d.permanencia_promedio_seg || 0);
            const conv = traf > 0 ? Math.min(100, (tom / traf) * 100) : 0;

            const setText = (id, val) => {
                const el = document.getElementById(id);
                if (el) el.textContent = val;
            };
            setText("stat-hist-trafico", String(traf));
            setText("stat-hist-permanencia", `${perm.toFixed(1)} s`);
            setText("stat-hist-tomados", String(tom));
            setText("stat-hist-devueltos", `${dev} devueltos a último momento`);
            setText("stat-hist-conversion", `${conv.toFixed(1)}%`);

            const bar = document.getElementById("progress-live-conversion");
            const barWrap = document.getElementById("progress-live-conversion-bar");
            if (bar) bar.style.width = `${conv.toFixed(1)}%`;
            if (barWrap) barWrap.setAttribute("aria-valuenow", String(Math.round(conv)));
        } catch (_e) {
            // Silencioso
        } finally {
            enVuelo.stats = false;
        }
    }

    // Resumen Ejecutivo de Publicidad y Promociones
    async function cargarResumenEjecutivoPublicidad() {
        if (enVuelo.pub) return;
        enVuelo.pub = true;
        try {
            const res = await fetch(`/api/analytics/resumen_publicidad?${construirQueryTemporal()}`);
            if (!res.ok) return;
            const d = await res.json();

            const priv = d.privacidad || {};
            const privEl = document.getElementById("exec-privacidad-resumen");
            if (privEl) {
                privEl.textContent = `${priv.clientes_validados_puerta_caja || 0} clientes validados Puerta a Caja • ${priv.empleados_excluidos || 0} eventos de personal excluidos.`;
            }

            const comp = d.comportamiento_visual || {};
            const setPair = (idCnt, idDwell, obj) => {
                const cEl = document.getElementById(idCnt);
                const dEl = document.getElementById(idDwell);
                if (cEl) cEl.textContent = String(obj?.casos || 0);
                if (dEl) dEl.textContent = `${Number(obj?.dwell || 0).toFixed(1)} s promedio`;
            };
            setPair("comp-mira-compra", "comp-mira-compra-dwell", comp.mira_y_compra);
            setPair("comp-no-mira-compra", "comp-no-mira-compra-dwell", comp.no_mira_y_compra);
            setPair("comp-solo-mira", "comp-solo-mira-dwell", comp.solo_mira);
            setPair("comp-devuelve", "comp-devuelve-dwell", comp.ultimo_momento_devuelve);

            const post = d.postura_cabeza || {};
            const setNum = (id, v) => {
                const el = document.getElementById(id);
                if (el) el.textContent = String(v || 0);
            };
            setNum("postura-giro", post.giro_percha);
            setNum("postura-sube", post.sube_cabeza);
            setNum("postura-agacha", post.se_agacha);

            const renderZonas = (idLista, items) => {
                const ul = document.getElementById(idLista);
                if (!ul) return;
                ul.textContent = "";
                if (!items || items.length === 0) {
                    const li = document.createElement("li");
                    li.textContent = "Sin registros en este rango de fechas.";
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
            renderZonas("lista-zonas-calientes", d.zonas_calientes);
            renderZonas("lista-zonas-frias", d.zonas_frias);

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
                    td.className = "p-6 text-center text-kovaq-slate";
                    td.textContent = "No hay promociones registradas. Crea una en la pestaña Configuración.";
                    tr.appendChild(td);
                    tbodyPromos.appendChild(tr);
                } else {
                    evals.forEach((pr) => {
                        const tr = document.createElement("tr");
                        tr.className = "hover:bg-slate-50";

                        const tdNom = document.createElement("td");
                        tdNom.className = "p-3";
                        const t1 = document.createElement("strong");
                        t1.className = "block text-kovaq-navy";
                        t1.textContent = `${pr.percha_id} • ${pr.titulo}`;
                        const t2 = document.createElement("span");
                        t2.className = "text-kovaq-slate";
                        t2.textContent = `${pr.producto} (${pr.tipo_publicidad}, -${pr.descuento_pct}%)`;
                        tdNom.append(t1, t2);

                        const tdCnt = document.createElement("td");
                        tdCnt.className = "p-3 font-mono-num";
                        tdCnt.textContent = `${pr.tomas} / ${pr.devueltos}`;

                        const tdRet = document.createElement("td");
                        tdRet.className = "p-3 font-mono-num font-bold text-emerald-800";
                        tdRet.textContent = `${pr.efectividad_pct}%`;

                        const tdDiag = document.createElement("td");
                        tdDiag.className = "p-3";
                        const st = document.createElement("strong");
                        st.className = "block text-kovaq-orange";
                        st.textContent = pr.estado_efecto;
                        const dg = document.createElement("span");
                        dg.className = "text-kovaq-slate";
                        dg.textContent = pr.diagnostico;
                        tdDiag.append(st, dg);

                        tr.append(tdNom, tdCnt, tdRet, tdDiag);
                        tbodyPromos.appendChild(tr);
                    });
                }
            }
        } catch (_e) {
            // Silencioso
        } finally {
            enVuelo.pub = false;
        }
    }

    // Gráficos Chart.js actualizados en sitio (sin destroy/new cada tick)
    async function cargarHeatmaps() {
        if (enVuelo.heat || typeof Chart === "undefined") return;
        enVuelo.heat = true;
        const qBase = construirQueryTemporal();
        const qSerie = construirQueryTemporal({ agrupacion: filtroTemporal.agrupacion || "semana" });
        try {
            const [resHora, resMatriz, resFranjas, resSerie] = await Promise.all([
                fetch(`/api/analytics/heatmap/hora?${qBase}`),
                fetch(`/api/analytics/heatmap/matriz?${qBase}`),
                fetch(`/api/analytics/horarios/franjas?${qBase}`),
                fetch(`/api/analytics/serie_temporal?${qSerie}`),
            ]);
            if (resSerie.ok) renderGraficoSerieTemporal(await resSerie.json());
            if (resHora.ok) {
                const dh = await resHora.json();
                renderGraficoHeatmapHora(dh.labels || [], dh.data || []);
            }
            if (resFranjas.ok) {
                const df = await resFranjas.json();
                renderDonutFranjas(df);
                actualizarTarjetasFranjas(df);
            }
            if (resMatriz.ok) {
                datosMatrizGlobal = await resMatriz.json();
                actualizarInsightsMatriz(datosMatrizGlobal);
                renderizarMatrizSegunModo(datosMatrizGlobal);
            }
        } catch (_e) {
            // Silencioso
        } finally {
            enVuelo.heat = false;
        }
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
            `Serie temporal por ${ds.agrupacion || "semana"} con ${labels.length} períodos: tráfico total ${trafico.reduce((a, b) => a + b, 0)}, tomas ${tomados.reduce((a, b) => a + b, 0)}, devoluciones ${devueltos.reduce((a, b) => a + b, 0)}.`
        );

        if (chartSerieTemporal) {
            chartSerieTemporal.data.labels = labels;
            chartSerieTemporal.data.datasets[0].data = trafico;
            chartSerieTemporal.data.datasets[1].data = tomados;
            chartSerieTemporal.data.datasets[2].data = devueltos;
            chartSerieTemporal.update("none");
            return;
        }

        chartSerieTemporal = new Chart(canvas, {
            type: "line",
            data: {
                labels,
                datasets: [
                    { label: "Tráfico Clientes", data: trafico, borderColor: "#0C2340", backgroundColor: "rgba(12, 35, 64, 0.08)", borderWidth: 2.5, tension: 0.3, fill: true },
                    { label: "Productos Tomados", data: tomados, borderColor: "#C23B00", backgroundColor: "rgba(194, 59, 0, 0.12)", borderWidth: 2.5, tension: 0.3, fill: false },
                    { label: "Devueltos", data: devueltos, borderColor: "#1D4ED8", borderDash: [5, 5], borderWidth: 2, tension: 0.3, fill: false },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                plugins: { legend: { position: "top" } },
                scales: { y: { beginAtZero: true }, x: { ticks: { maxTicksLimit: 12 } } },
            },
        });
    }

    function renderGraficoHeatmapHora(labels, valores) {
        const canvas = document.getElementById("chart-heatmap-hora");
        if (!canvas) return;
        if (chartHeatmapHora) {
            chartHeatmapHora.data.labels = labels;
            chartHeatmapHora.data.datasets[0].data = valores;
            chartHeatmapHora.update("none");
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
                animation: false,
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
        if (chartFranjasDonut) {
            chartFranjasDonut.data.datasets[0].data = datos;
            chartFranjasDonut.update("none");
            return;
        }
        chartFranjasDonut = new Chart(canvas, {
            type: "doughnut",
            data: {
                labels: ["Mañana", "Tarde", "Noche"],
                datasets: [{ data: datos, backgroundColor: ["#0C2340", "#C23B00", "#1D4ED8"], borderWidth: 2, borderColor: "#FFFFFF" }],
            },
            options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { position: "bottom" } } },
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
            let franja = "Madrugada (00h - 06h)";
            if (h >= 18) franja = "Noche (18h - 23h)";
            else if (h >= 14) franja = "Tarde (14h - 18h)";
            else if (h >= 11) franja = "Mediodía (11h - 14h)";
            else if (h >= 6) franja = "Mañana (06h - 11h)";
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
            ["Día", "Mañana (06-11h)", "Mediodía (11-14h)", "Tarde (14-18h)", "Noche (18-23h)", "Total Día"].forEach((h) => {
                const th = document.createElement("th");
                th.scope = "col";
                th.className = "p-2.5";
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
                ["manana", "mediodia", "tarde", "noche", "total"].forEach((k) => {
                    const td = document.createElement("td");
                    td.className = "p-2.5 font-mono-num";
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

    // Inventario y Planograma (DOM seguro sin XSS ni SyntaxError en comillas)
    async function cargarInventarioReal() {
        try {
            const res = await fetch(`/api/analytics/inventario?${construirQueryTemporal()}`);
            if (!res.ok) return;
            const data = await res.json();
            const items = data.productos || [];
            const tbody = document.getElementById("tabla-inventario-body");
            if (!tbody) return;
            tbody.textContent = "";

            if (items.length === 0) {
                const tr = document.createElement("tr");
                const td = document.createElement("td");
                td.colSpan = 7;
                td.className = "p-6 text-center text-kovaq-slate";
                td.textContent = "No hay productos en el planograma. Registra una percha en Configuración.";
                tr.appendChild(td);
                tbody.appendChild(tr);
                filtrarInventarioLocal();
                return;
            }

            items.forEach((p) => {
                const tr = document.createElement("tr");
                tr.className = "hover:bg-slate-50 item-inventario";
                tr.dataset.nombre = `${p.nombre} ${p.categoria} ${p.percha_id}`.toLowerCase();
                tr.dataset.categoria = p.categoria;

                const tdProd = document.createElement("td");
                tdProd.className = "p-3 font-bold text-kovaq-navy";
                const spNom = document.createElement("span");
                spNom.className = "block";
                spNom.textContent = p.nombre;
                const spSub = document.createElement("span");
                spSub.className = "text-[11px] font-mono-num text-kovaq-slate";
                spSub.textContent = `${p.percha_id} • ${p.nivel}`;
                tdProd.append(spNom, spSub);

                const tdCat = document.createElement("td");
                tdCat.className = "p-3";
                const spCat = document.createElement("span");
                spCat.className = "bg-slate-100 text-kovaq-navy px-2 py-0.5 rounded font-bold";
                spCat.textContent = p.categoria;
                tdCat.appendChild(spCat);

                const tdStock = document.createElement("td");
                tdStock.className = "p-3 font-mono-num";
                tdStock.textContent = `${p.stock} u.`;

                const tdTomas = document.createElement("td");
                tdTomas.className = "p-3 font-mono-num font-bold";
                tdTomas.textContent = `${p.tomas} tomas (${p.devueltos} dev.)`;

                const tdEvac = document.createElement("td");
                tdEvac.className = "p-3 font-mono-num font-bold text-emerald-800";
                tdEvac.textContent = `${p.tasa_evacuacion}%`;

                const tdEst = document.createElement("td");
                tdEst.className = "p-3";
                const spEst = document.createElement("span");
                spEst.className = String(p.estado || "").includes("Crítico")
                    ? "bg-red-100 text-red-900 px-2 py-0.5 rounded font-bold"
                    : "bg-emerald-100 text-emerald-900 px-2 py-0.5 rounded font-bold";
                spEst.textContent = p.estado;
                tdEst.appendChild(spEst);

                const tdAcc = document.createElement("td");
                tdAcc.className = "p-3 text-right";
                const btnAds = document.createElement("button");
                btnAds.type = "button";
                btnAds.className = "btn-kovaq-outline min-h-[34px] text-xs";
                btnAds.textContent = "Analizar con IA";
                btnAds.addEventListener("click", () => {
                    preguntarIARapido(`Optimizar publicidad para ${p.nombre} en ${p.percha_id}`);
                });
                tdAcc.appendChild(btnAds);

                tr.append(tdProd, tdCat, tdStock, tdTomas, tdEvac, tdEst, tdAcc);
                tbody.appendChild(tr);
            });

            filtrarInventarioLocal();
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

    // Configuración (Cámaras, Planograma y Promociones) con DOM seguro
    async function cargarConfiguracionMapa() {
        try {
            const [resCams, resPerchas] = await Promise.all([fetch("/api/cameras"), fetch("/api/shelves")]);
            if (resCams.ok) renderTablaCamaras((await resCams.json()).camaras || []);
            if (resPerchas.ok) renderTablaPerchas((await resPerchas.json()).perchas || []);
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
                : "Sin pauta POP";

            const tdAct = document.createElement("td");
            tdAct.className = "p-2.5 font-mono-num font-bold";
            tdAct.textContent = `${p.tomas || 0} tomas / ${p.devoluciones || 0} dev.`;

            tr.append(tdId, tdNom, tdNiv, tdPromo, tdAct);
            tbody.appendChild(tr);
        });
    }

    async function eliminarCamaraBackend(camId) {
        const res = await fetch(`/api/cameras/${encodeURIComponent(camId)}`, { method: "DELETE" });
        if (res.ok) {
            renderTablaCamaras((await res.json()).camaras || []);
            mostrarAviso("config-feedback-banner", `Cámara ${camId} eliminada del plano.`, "ok");
            sincronizarMapa2D();
        } else {
            mostrarAviso("config-feedback-banner", "No tienes permisos o falló la eliminación de cámara.", "error");
        }
    }

    // Chat Kovaq Marketing AI (100% textContent)
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
        const modo = document.getElementById("chat-modo")?.value || "publicidad";
        const btn = document.getElementById("chat-submit");
        agregarBurbuja("Tú", mensaje, "user");
        if (btn) btn.disabled = true;
        const burbujaIA = agregarBurbuja("Kovaq Marketing AI", "Consultando métricas del período...", "bot");

        try {
            const res = await fetch("/api/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ mensaje, modo }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || "Error consultando IA");
            if (burbujaIA) burbujaIA.querySelector(".texto").textContent = data.respuesta || "Sin respuesta.";
        } catch (err) {
            if (burbujaIA) burbujaIA.querySelector(".texto").textContent = `Error: ${err.message}`;
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    function preguntarIARapido(texto) {
        cambiarModulo("tab-marketing");
        enviarPreguntaIA(texto);
    }

    // Video, fuentes y subida instantánea
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
        video.width = 480;
        video.height = 360;
        video.preload = "metadata";
        video.loop = true;
        video.muted = true;
        video.playsInline = true;
        video.className = "w-full h-full object-contain";
        const src = document.createElement("source");
        src.src = "/static/demo_supermercado.mp4";
        src.type = "video/mp4";
        video.appendChild(src);
        cont.appendChild(video);

        const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (!reduceMotion) {
            video.play().catch(() => {});
        }
    }

    function mostrarStreamProcesado() {
        desmontarStreamPrevio();
        const cont = document.getElementById("video-container");
        if (!cont) return;
        cont.textContent = "";
        const img = document.createElement("img");
        img.id = "live-stream-img";
        img.width = 480;
        img.height = 360;
        img.decoding = "async";
        img.alt = "Transmisión procesada por KOVAQ con difuminado facial y zonas de percha";
        img.className = "w-full h-full object-contain";
        img.src = `/api/video_feed?t=${Date.now()}`;
        img.addEventListener("error", () => mostrarVideoRespaldo());
        cont.appendChild(img);
    }

    function inicializarControlesVideoYSubida() {
        const videoEl = document.getElementById("demo-video-player");
        const btnPlay = document.getElementById("btn-toggle-video-play");
        const txtPlay = document.getElementById("txt-toggle-video-play");

        if (videoEl && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
            videoEl.pause();
            if (txtPlay) txtPlay.textContent = "Reproducir video";
            if (btnPlay) btnPlay.setAttribute("aria-pressed", "true");
        }

        if (btnPlay) {
            btnPlay.addEventListener("click", () => {
                const v = document.getElementById("demo-video-player");
                if (!v) return;
                if (v.paused) {
                    v.play().catch(() => {});
                    if (txtPlay) txtPlay.textContent = "Pausar video";
                    btnPlay.setAttribute("aria-pressed", "false");
                } else {
                    v.pause();
                    if (txtPlay) txtPlay.textContent = "Reproducir video";
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
                    if (tit) tit.textContent = selCamMapa.options[selCamMapa.selectedIndex].text;
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
            const modo = document.body.dataset.modo || "local";
            if (modo === "local") mostrarStreamProcesado();
            else mostrarVideoRespaldo();
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
                    mostrarStreamProcesado();
                    await Promise.all([refrescarDashboardsPorPeriodo(), sincronizarMapa2D(), sincronizarTelemetriaBackend()]);
                } catch (err) {
                    if (statusEl) statusEl.textContent = `Error: ${err.message}`;
                } finally {
                    btnSubir.disabled = false;
                }
            });
        }
    }

    function inicializarFormulariosYControles() {
        // Matriz franjas vs 24h
        const btnFranjas = document.getElementById("btn-vista-franjas");
        const btn24h = document.getElementById("btn-vista-24h");
        const toggleMatriz = (modo) => {
            modoVistaMatriz = modo;
            if (btnFranjas && btn24h) {
                btnFranjas.setAttribute("aria-pressed", modo === "franjas" ? "true" : "false");
                btn24h.setAttribute("aria-pressed", modo === "24h" ? "true" : "false");
                btnFranjas.className = modo === "franjas"
                    ? "btn-kovaq-outline min-h-[36px] text-xs bg-kovaq-navy text-white border-kovaq-navy"
                    : "btn-kovaq-outline min-h-[36px] text-xs";
                btn24h.className = modo === "24h"
                    ? "btn-kovaq-outline min-h-[36px] text-xs bg-kovaq-navy text-white border-kovaq-navy"
                    : "btn-kovaq-outline min-h-[36px] text-xs";
            }
            if (datosMatrizGlobal) renderizarMatrizSegunModo(datosMatrizGlobal);
        };
        if (btnFranjas) btnFranjas.addEventListener("click", () => toggleMatriz("franjas"));
        if (btn24h) btn24h.addEventListener("click", () => toggleMatriz("24h"));

        // Inventario: búsqueda con debounce (150ms) + botones categoría
        const searchInv = document.getElementById("input-buscar-inventario");
        let debounceTimer = null;
        if (searchInv) {
            searchInv.addEventListener("input", () => {
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(filtrarInventarioLocal, 150);
            });
        }
        const catBtns = Array.from(document.querySelectorAll(".category-tag-btn"));
        catBtns.forEach((btn) => {
            btn.addEventListener("click", () => {
                categoriaActiva = btn.dataset.cat || "Todas";
                catBtns.forEach((b) => {
                    const act = b === btn;
                    b.classList.toggle("active", act);
                    b.setAttribute("aria-pressed", act ? "true" : "false");
                });
                filtrarInventarioLocal();
            });
        });

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
                    }),
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok) {
                    renderTablaCamaras(data.camaras || []);
                    formCam.reset();
                    mostrarAviso("config-feedback-banner", "Cámara registrada correctamente en el plano 2D.", "ok");
                    sincronizarMapa2D();
                } else {
                    mostrarAviso("config-feedback-banner", data.detail || "No se pudo registrar la cámara.", "error");
                }
            });
        }

        // Formulario nueva promoción POP
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
                    }),
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok) {
                    formPromo.reset();
                    mostrarAviso("config-feedback-banner", "Promoción POP guardada y vinculada al análisis.", "ok");
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
                        niveles: [
                            { nivel: "Superior (Subir Cabeza)", y1: 0.18, y2: 0.42, producto: document.getElementById("plano-prod-sup").value.trim() },
                            { nivel: "Medio (Altura Ojos)", y1: 0.42, y2: 0.68, producto: document.getElementById("plano-prod-med").value.trim() },
                            { nivel: "Inferior (Agacharse)", y1: 0.68, y2: 0.90, producto: document.getElementById("plano-prod-inf").value.trim() },
                        ],
                    }),
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok) {
                    renderTablaPerchas(data.perchas || []);
                    formPlano.reset();
                    mostrarAviso("config-feedback-banner", "Planograma actualizado en Turso.", "ok");
                    await Promise.all([cargarInventarioReal(), cargarResumenEjecutivoPublicidad(), sincronizarMapa2D()]);
                } else {
                    mostrarAviso("config-feedback-banner", data.detail || "No se pudo guardar el planograma.", "error");
                }
            });
        }
    }

    // Arranque diferido y polling controlado por visibilidad de pestaña
    document.addEventListener("DOMContentLoaded", () => {
        inicializarTabs();
        inicializarFiltrosTemporales();
        inicializarControlesVideoYSubida();
        inicializarFormulariosYControles();
        actualizarEtiquetasPeriodo();

        // Solo cargar lo visible en Tab 1 al iniciar (Tabs 2-5 cargan bajo demanda)
        sincronizarMapa2D();
        sincronizarTelemetriaBackend();
        actualizarDatosDashboard();
        cargarResumenEjecutivoPublicidad();

        setInterval(() => {
            if (!document.hidden && moduloActivo === "tab-vivo") {
                sincronizarMapa2D();
                sincronizarTelemetriaBackend();
            }
        }, 3500);

        setInterval(() => {
            if (!document.hidden) {
                actualizarDatosDashboard();
                cargarResumenEjecutivoPublicidad();
            }
        }, 18000);

        setInterval(() => {
            if (!document.hidden && moduloActivo === "tab-percha") {
                cargarHeatmaps();
            }
        }, 20000);
    });
})();
