(() => {
  const formatBytes = (value, suffix = "") => {
    let number = Number(value) || 0;
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    while (number >= 1024 && index < units.length - 1) { number /= 1024; index += 1; }
    return `${number < 10 && index ? number.toFixed(2) : number.toFixed(index ? 1 : 0)} ${units[index]}${suffix}`;
  };

  const gaugeState = new WeakMap();
  const gaugeColor = (mbps) => (mbps <= 100 ? "var(--green)" : (mbps <= 300 ? "var(--orange)" : "var(--red)"));
  const formatMbps = (mbps) => {
    if (mbps >= 100) return Math.round(mbps).toString();
    if (mbps >= 10) return mbps.toFixed(1).replace(/\.0$/, "");
    return mbps.toFixed(2).replace(/\.00$/, "").replace(/0$/, "");
  };
  const updateBandwidthGauge = (kind, bps) => {
    const gauge = document.querySelector(`[data-bandwidth-gauge="${kind}"]`);
    if (!gauge) return;
    const target = Math.max(0, Number(bps) || 0) * 8 / 1000000;
    const previous = gaugeState.get(gauge) ?? 0;
    const started = performance.now();
    const duration = 520;
    const label = gauge.querySelector("b");
    const paint = (time) => {
      const ratio = Math.min(1, (time - started) / duration);
      const eased = 1 - Math.pow(1 - ratio, 3);
      const value = previous + (target - previous) * eased;
      const progress = Math.min(1, value / 500);
      gauge.style.setProperty("--gauge-progress", progress.toFixed(4));
      gauge.style.setProperty("--gauge-color", gaugeColor(value));
      if (label) label.textContent = formatMbps(value);
      gauge.setAttribute("aria-label", `${kind === "inbound" ? "实时入站带宽" : "实时出站带宽"} ${formatMbps(value)} Mbps`);
      if (ratio < 1) requestAnimationFrame(paint);
      else gaugeState.set(gauge, target);
    };
    requestAnimationFrame(paint);
  };

  const updateTimeGreeting = () => {
    const hour = new Date().getHours();
    const greeting = hour < 5 ? "凌晨了，快睡吧" : hour < 11 ? "早上好" : hour < 13 ? "中午好" : hour < 18 ? "下午好" : "晚上好";
    document.querySelectorAll("[data-time-greeting]").forEach((node) => {
      node.textContent = `${greeting}，${node.dataset.username || ""}`;
    });
  };
  updateTimeGreeting();
  window.setInterval(updateTimeGreeting, 60_000);

  document.querySelectorAll("[data-bytes]").forEach((node) => { node.textContent = formatBytes(node.dataset.bytes); });
  document.querySelectorAll("[data-dialog-open]").forEach((button) => button.addEventListener("click", () => {
    document.getElementById(button.dataset.dialogOpen)?.showModal();
  }));
  document.querySelectorAll("[data-dialog-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog")?.close()));
  document.querySelectorAll("[data-copy-address], [data-copy-text]").forEach((button) => button.addEventListener("click", async () => {
    const text = button.dataset.copyText || button.dataset.copyAddress || "";
    if (!text) return;
    const defaultLabel = button.getAttribute("aria-label") || "复制";
    const defaultTitle = button.getAttribute("title") || defaultLabel;
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const input = document.createElement("textarea");
      input.value = text;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.append(input);
      input.select();
      document.execCommand("copy");
      input.remove();
    }
    button.setAttribute("title", "已复制");
    button.setAttribute("aria-label", "已复制");
    window.setTimeout(() => { button.setAttribute("title", defaultTitle); button.setAttribute("aria-label", defaultLabel); }, 1400);
  }));

  const sidebar = document.querySelector(".sidebar");
  const menuToggle = document.querySelector(".menu-toggle");
  const desktopSidebar = window.matchMedia("(min-width: 761px)");
  const applySidebar = (collapsed) => {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    sidebar?.classList.toggle("open", !collapsed || !desktopSidebar.matches);
    menuToggle?.setAttribute("aria-expanded", String(!collapsed));
    menuToggle?.setAttribute("aria-label", collapsed ? "展开导航" : "收起导航");
    menuToggle?.setAttribute("title", collapsed ? "展开导航" : "收起导航");
  };
  if (sidebar && menuToggle) {
    const saved = window.localStorage.getItem("mochi-sidebar-collapsed") === "1";
    if (desktopSidebar.matches) applySidebar(saved);
    menuToggle.addEventListener("click", () => {
      if (desktopSidebar.matches) {
        const next = !document.body.classList.contains("sidebar-collapsed");
        window.localStorage.setItem("mochi-sidebar-collapsed", next ? "1" : "0");
        applySidebar(next);
      } else {
        sidebar.classList.toggle("open");
        menuToggle.setAttribute("aria-expanded", String(sidebar.classList.contains("open")));
      }
    });
    desktopSidebar.addEventListener("change", () => applySidebar(window.localStorage.getItem("mochi-sidebar-collapsed") === "1"));
  }

  const overview = document.querySelector(".live-overview");
  let livePoints = [];
  let connectionPoints = [];
  let overviewBusy = false;
  const drawConnectionsSparkline = () => {
    document.querySelectorAll("[data-connections-sparkline]").forEach((canvas) => {
      const ratio = window.devicePixelRatio || 1;
      const width = Number(canvas.getAttribute("width")) || 76;
      const height = Number(canvas.getAttribute("height")) || 38;
      canvas.width = width * ratio;
      canvas.height = height * ratio;
      const ctx = canvas.getContext("2d");
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      if (!connectionPoints.length) return;
      const maximum = Math.max(1, ...connectionPoints);
      const bottom = height - 3;
      const pointAt = (value, index) => ({x: index * width / Math.max(1, connectionPoints.length - 1), y: bottom - value / maximum * (height - 7)});
      const area = ctx.createLinearGradient(0, 0, 0, height);
      area.addColorStop(0, "rgba(139, 92, 246, .22)");
      area.addColorStop(1, "rgba(139, 92, 246, 0)");
      ctx.beginPath();
      connectionPoints.forEach((value, index) => { const point = pointAt(value, index); index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y); });
      ctx.lineTo(width, bottom); ctx.lineTo(0, bottom); ctx.closePath();
      ctx.fillStyle = area; ctx.fill();
      ctx.beginPath();
      connectionPoints.forEach((value, index) => { const point = pointAt(value, index); index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y); });
      ctx.strokeStyle = "#8b5cf6"; ctx.lineWidth = 1.8; ctx.lineJoin = "round"; ctx.lineCap = "round"; ctx.stroke();
    });
  };
  const updateMonthlyQuota = (totals) => {
    const quota = Number(totals.monthly_quota_bytes) || 0;
    const usage = Math.max(0, Number(totals.monthly_bytes) || 0);
    const detail = document.querySelector('[data-metric="monthly-detail"]');
    const gauge = document.querySelector("[data-monthly-progress]");
    if (!detail || !gauge) return;
    const label = gauge.querySelector("b");
    if (!quota) {
      detail.textContent = `${formatBytes(usage)} / 不限额`;
      gauge.style.setProperty("--gauge-progress", "0");
      gauge.style.setProperty("--gauge-color", "var(--green)");
      if (label) label.textContent = "--";
      gauge.setAttribute("aria-label", `本月已使用 ${formatBytes(usage)}，不限额`);
      return;
    }
    const percentage = usage / quota * 100;
    const displayed = Math.min(100, Math.max(0, percentage));
    const color = percentage <= 50 ? "var(--green)" : (percentage <= 80 ? "var(--orange)" : "var(--red)");
    detail.textContent = `${formatBytes(usage)} / ${formatBytes(quota)}`;
    gauge.style.setProperty("--gauge-progress", (displayed / 100).toFixed(4));
    gauge.style.setProperty("--gauge-color", color);
    if (label) label.textContent = Math.round(displayed).toString();
    gauge.setAttribute("aria-label", `本月已使用 ${formatBytes(usage)}，总额 ${formatBytes(quota)}，已用 ${percentage.toFixed(1)}%`);
  };
  async function loadOverview() {
    if (!overview || overviewBusy) return;
    overviewBusy = true;
    try {
      const response = await fetch(overview.dataset.overviewUrl, {headers: {"Accept": "application/json"}});
      if (!response.ok) throw new Error("overview request failed");
      const data = await response.json();
      document.querySelectorAll('[data-metric="inbound"]').forEach((node) => { node.textContent = formatBytes(data.totals.inbound_bps, "/s"); });
      document.querySelectorAll('[data-metric="outbound"]').forEach((node) => { node.textContent = formatBytes(data.totals.outbound_bps, "/s"); });
      updateBandwidthGauge("inbound", data.totals.inbound_bps);
      updateBandwidthGauge("outbound", data.totals.outbound_bps);
      document.querySelectorAll('[data-metric="monthly"]').forEach((node) => { node.textContent = formatBytes(data.totals.monthly_bytes); });
      document.querySelectorAll('[data-metric="connections"]').forEach((node) => { node.textContent = data.totals.connections; });
      connectionPoints.push(Math.max(0, Number(data.totals.connections) || 0));
      connectionPoints = connectionPoints.slice(-60);
      drawConnectionsSparkline();
      updateMonthlyQuota(data.totals);
      data.rules.forEach((rule) => {
        const row = document.querySelector(`[data-rule-id="${rule.id}"]`);
        if (!row) return;
        const status = row.querySelector(".connectivity");
        if (rule.paused_reason) {
          status.className = "connectivity paused";
          status.querySelector("b").textContent = rule.paused_label;
          status.querySelector("small").textContent = "规则已保留";
        } else {
          status.className = `connectivity ${rule.reachable ? "online" : "offline"}`;
          status.querySelector("b").textContent = rule.reachable ? "可用" : "不可达";
          status.querySelector("small").textContent = rule.reachable ? `${rule.latency_ms} ms` : "连接失败";
        }
        row.querySelector(".in-rate").textContent = `↓ ${formatBytes(rule.inbound_bps, "/s")}`;
        row.querySelector(".out-rate").textContent = `↑ ${formatBytes(rule.outbound_bps, "/s")}`;
        row.querySelector(".connection-count").textContent = rule.connections;
      });
      const list = document.querySelector("[data-connection-list]");
      if (list) {
        list.replaceChildren();
        if (!data.rules.length) {
          const empty = document.createElement("div");
          empty.className = "empty-state";
          const message = document.createElement("p");
          message.textContent = "暂无转发规则";
          empty.append(message);
          list.append(empty);
        }
        data.rules.forEach((rule) => {
          const item = document.createElement("div");
          item.className = "connection-item";
          const details = document.createElement("div");
          const destination = document.createElement("strong");
          destination.textContent = `:${rule.listen_port} → ${rule.destination}`;
          const state = document.createElement("small");
          state.className = "block";
          state.textContent = `${rule.owner} · ${rule.paused_reason ? rule.paused_label : (rule.reachable ? `${rule.latency_ms} ms` : "目标不可达")}`;
          const count = document.createElement("strong");
          count.textContent = String(rule.connections);
          details.append(destination, state);
          item.append(details, count);
          list.append(item);
        });
      }
      livePoints.push({time: Date.now(), inbound: data.totals.inbound_bps, outbound: data.totals.outbound_bps});
      livePoints = livePoints.slice(-120);
      if (document.querySelector('[data-chart-kind="bandwidth"]')) drawChart(livePoints.map((point) => ({period: new Date(point.time).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}), inbound: point.inbound, outbound: point.outbound})), true);
    } catch (error) {
      document.querySelectorAll(".connectivity.pending b").forEach((node) => { node.textContent = "刷新失败"; });
    } finally {
      overviewBusy = false;
    }
  }

  const canvas = document.getElementById("history-chart");
  function drawChart(points, rates = false) {
    if (!canvas) return;
    const empty = canvas.parentElement.querySelector(".chart-empty");
    empty.style.display = points.length ? "none" : "grid";
    if (!points.length) return;
    const ratio = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * ratio; canvas.height = rect.height * ratio;
    const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
    const width = rect.width, height = rect.height, pad = {left: 55, right: 18, top: 18, bottom: 36};
    const max = Math.max(1, ...points.flatMap((point) => [Number(point.inbound), Number(point.outbound)]));
    ctx.font = "11px system-ui"; ctx.strokeStyle = "#e8ebf0"; ctx.fillStyle = "#8a95a5"; ctx.lineWidth = 1;
    for (let step = 0; step <= 4; step += 1) { const y = pad.top + (height - pad.top - pad.bottom) * step / 4; ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke(); ctx.fillText(formatBytes(max * (1 - step / 4), rates ? "/s" : ""), 0, y + 4); }
    const plot = (key, color) => { ctx.beginPath(); points.forEach((point, index) => { const x = pad.left + (width - pad.left - pad.right) * index / Math.max(1, points.length - 1); const y = height - pad.bottom - (height - pad.top - pad.bottom) * Number(point[key]) / max; index ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke(); };
    plot("inbound", "#1677ff"); plot("outbound", "#22b865");
    const labelIndexes = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])];
    labelIndexes.forEach((index) => { const x = pad.left + (width - pad.left - pad.right) * index / Math.max(1, points.length - 1); ctx.fillText(points[index].period, Math.min(x, width - 82), height - 10); });
  }

  async function loadHistory(days = 7) {
    const panel = document.querySelector("[data-history-url]");
    if (!panel) return;
    const response = await fetch(`${panel.dataset.historyUrl}?days=${days}`);
    const data = await response.json();
    const points = panel.dataset.chartKind === "bandwidth" ? data.points.map((point) => ({...point, inbound: Number(point.inbound) / 86400, outbound: Number(point.outbound) / 86400})) : data.points;
    drawChart(points, panel.dataset.chartKind === "bandwidth");
    const body = document.querySelector("[data-history-body]");
    body.replaceChildren();
    if (!data.points.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 4;
      cell.textContent = "暂无记录";
      row.append(cell);
      body.append(row);
    }
    data.points.slice().reverse().forEach((point) => {
      const row = document.createElement("tr");
      [point.period, formatBytes(point.inbound), formatBytes(point.outbound)].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      });
      const totalCell = document.createElement("td");
      const total = document.createElement("strong");
      total.textContent = formatBytes(Number(point.inbound) + Number(point.outbound));
      totalCell.append(total);
      row.append(totalCell);
      body.append(row);
    });
  }
  document.querySelectorAll("[data-range] button").forEach((button) => button.addEventListener("click", () => { button.parentElement.querySelectorAll("button").forEach((item) => item.classList.remove("active")); button.classList.add("active"); document.querySelector("[data-chart-caption]")?.replaceChildren(`最近 ${button.textContent}`); loadHistory(button.dataset.days); }));
  window.addEventListener("resize", () => { drawConnectionsSparkline(); if (document.querySelector('[data-chart-kind="bandwidth"]') && livePoints.length) drawChart(livePoints.map((point) => ({period: new Date(point.time).toLocaleTimeString(), inbound: point.inbound, outbound: point.outbound})), true); else loadHistory(document.querySelector("[data-range] .active")?.dataset.days || 7); });
  loadOverview(); loadHistory(7); if (overview) window.setInterval(loadOverview, 1000);
})();
