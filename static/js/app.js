(() => {
  document.querySelectorAll("[data-emoji]").forEach((node) => {
    if (!window.twemoji) return;
    window.twemoji.parse(node, {
      base: "/static/vendor/twemoji/assets/",
      folder: "svg",
      ext: ".svg",
    });
  });

  const formatBytes = (value, suffix = "") => {
    let number = Number(value) || 0;
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    while (number >= 1024 && index < units.length - 1) { number /= 1024; index += 1; }
    return `${number < 10 && index ? number.toFixed(2) : number.toFixed(index ? 1 : 0)} ${units[index]}${suffix}`;
  };

  const GAUGE_ARC_LENGTH = 75;
  const gaugeDashArray = (progress) => `${(Math.min(1, Math.max(0, Number(progress) || 0)) * GAUGE_ARC_LENGTH).toFixed(2)} 100`;
  const gaugeState = new WeakMap();
  const gaugeTone = (value, low, medium) => (value <= low ? "gauge-green" : (value <= medium ? "gauge-orange" : "gauge-red"));
  const paintGauge = (gauge, progress, tone) => {
    gauge.classList.remove("gauge-green", "gauge-orange", "gauge-red");
    gauge.classList.add(tone);
    const arc = gauge.querySelector(".gauge-value");
    if (arc) arc.setAttribute("stroke-dasharray", gaugeDashArray(progress));
  };
  const formatMbps = (mbps) => {
    if (mbps >= 100) return Math.round(mbps).toString();
    if (mbps >= 10) return mbps.toFixed(1).replace(/\.0$/, "");
    return mbps.toFixed(2).replace(/\.00$/, "").replace(/0$/, "");
  };
  const firstFiniteNumber = (...values) => {
    for (const value of values) {
      if (value === null || value === undefined || value === "") continue;
      const number = Number(value);
      if (Number.isFinite(number)) return Math.max(0, number);
    }
    return 0;
  };
  const readLiveWireRates = (source = {}) => ({
    rx: firstFiniteNumber(source.rx_bps, source.inbound_bps),
    tx: firstFiniteNumber(source.tx_bps, source.outbound_bps),
  });
  const readWireTraffic = (source = {}) => ({
    rx: firstFiniteNumber(source.rx, source.rx_bytes, source.inbound),
    tx: firstFiniteNumber(source.tx, source.tx_bytes, source.outbound),
  });
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
      paintGauge(gauge, progress, gaugeTone(value, 100, 300));
      if (label) label.textContent = formatMbps(value);
      gauge.setAttribute("aria-label", `${kind === "rx" ? "实时 RX 入站带宽" : "实时 TX 出站带宽"} ${formatMbps(value)} Mbps`);
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
  document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  }));
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
  const sidebarBackdrop = document.querySelector(".sidebar-backdrop");
  const menuToggle = document.querySelector(".menu-toggle");
  const desktopSidebar = window.matchMedia("(min-width: 761px)");
  const closeMobileSidebar = () => {
    sidebar?.classList.remove("open");
    document.body.classList.remove("mobile-nav-open");
    menuToggle?.setAttribute("aria-expanded", "false");
  };
  const applySidebar = (collapsed) => {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    closeMobileSidebar();
    menuToggle?.setAttribute("aria-expanded", String(!collapsed));
    menuToggle?.setAttribute("aria-label", collapsed ? "展开导航" : "收起导航");
    menuToggle?.setAttribute("title", collapsed ? "展开导航" : "收起导航");
  };
  if (sidebar && menuToggle) {
    const saved = window.localStorage.getItem("mochi-sidebar-collapsed") === "1";
    if (desktopSidebar.matches) applySidebar(saved);
    else closeMobileSidebar();
    menuToggle.addEventListener("click", () => {
      if (desktopSidebar.matches) {
        const next = !document.body.classList.contains("sidebar-collapsed");
        window.localStorage.setItem("mochi-sidebar-collapsed", next ? "1" : "0");
        applySidebar(next);
      } else {
        const open = !sidebar.classList.contains("open");
        sidebar.classList.toggle("open", open);
        document.body.classList.toggle("mobile-nav-open", open);
        menuToggle.setAttribute("aria-expanded", String(open));
      }
    });
    sidebarBackdrop?.addEventListener("click", closeMobileSidebar);
    sidebar.querySelectorAll("a").forEach((link) => link.addEventListener("click", closeMobileSidebar));
    desktopSidebar.addEventListener("change", () => {
      if (desktopSidebar.matches) applySidebar(window.localStorage.getItem("mochi-sidebar-collapsed") === "1");
      else closeMobileSidebar();
    });
  }

  const overview = document.querySelector(".live-overview");
  let livePoints = [];
  let connectionPoints = [];
  let overviewBusy = false;
  const drawConnectionsSparkline = () => {
    document.querySelectorAll("[data-connections-sparkline]").forEach((svg) => {
      const line = svg.querySelector(".sparkline-line");
      const area = svg.querySelector(".sparkline-area");
      if (!line || !area || !connectionPoints.length) {
        line?.removeAttribute("d");
        area?.removeAttribute("d");
        return;
      }
      const width = 76;
      const height = 38;
      const bottom = height - 3;
      const maximum = Math.max(1, ...connectionPoints);
      const coordinates = connectionPoints.map((value, index) => ({
        x: index * width / Math.max(1, connectionPoints.length - 1),
        y: bottom - value / maximum * (height - 7),
      }));
      const path = coordinates.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(" ");
      line.setAttribute("d", path);
      area.setAttribute("d", `${path} L${width} ${bottom} L0 ${bottom} Z`);
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
      paintGauge(gauge, 0, "gauge-green");
      if (label) label.textContent = "--";
      gauge.setAttribute("aria-label", `本月已使用 ${formatBytes(usage)}，不限额`);
      return;
    }
    const percentage = usage / quota * 100;
    const displayed = Math.min(100, Math.max(0, percentage));
    detail.textContent = `${formatBytes(usage)} / ${formatBytes(quota)}`;
    paintGauge(gauge, displayed / 100, gaugeTone(percentage, 50, 80));
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
      const totalRates = readLiveWireRates(data.totals);
      document.querySelectorAll('[data-metric="rx"]').forEach((node) => { node.textContent = formatBytes(totalRates.rx, "/s"); });
      document.querySelectorAll('[data-metric="tx"]').forEach((node) => { node.textContent = formatBytes(totalRates.tx, "/s"); });
      updateBandwidthGauge("rx", totalRates.rx);
      updateBandwidthGauge("tx", totalRates.tx);
      document.querySelectorAll('[data-metric="monthly"]').forEach((node) => { node.textContent = formatBytes(data.totals.monthly_bytes); });
      document.querySelectorAll('[data-metric="connections"]').forEach((node) => { node.textContent = data.totals.connections; });
      document.querySelectorAll('[data-metric="tcp-connections"]').forEach((node) => { node.textContent = data.totals.tcp_connections; });
      document.querySelectorAll('[data-metric="udp-connections"]').forEach((node) => { node.textContent = data.totals.udp_connections; });
      document.querySelectorAll('[data-metric="rule-count"]').forEach((node) => { node.textContent = data.totals.rule_count; });
      document.querySelectorAll('[data-metric="previous-hour-rx"]').forEach((node) => {
        node.textContent = formatBytes(firstFiniteNumber(data.totals.previous_hour_rx_bytes, data.totals.previous_hour_inbound_bytes));
      });
      document.querySelectorAll('[data-metric="previous-hour-tx"]').forEach((node) => {
        node.textContent = formatBytes(firstFiniteNumber(data.totals.previous_hour_tx_bytes, data.totals.previous_hour_outbound_bytes));
      });
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
        } else {
          status.className = `connectivity ${rule.reachable ? "online" : "offline"}`;
          status.querySelector("b").textContent = rule.reachable ? `${rule.latency_ms} ms` : "失联";
        }
        const ruleRates = readLiveWireRates(rule);
        row.querySelector(".rx-rate").textContent = `RX ↓ ${formatBytes(ruleRates.rx, "/s")}`;
        row.querySelector(".tx-rate").textContent = `TX ↑ ${formatBytes(ruleRates.tx, "/s")}`;
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
      livePoints.push({time: Date.now(), rx: totalRates.rx, tx: totalRates.tx});
      livePoints = livePoints.slice(-120);
      if (document.querySelector('[data-chart-kind="bandwidth"]') && document.querySelector('[data-range] .active')?.dataset.rangeValue === "live") drawChart(livePoints.map((point) => ({period: new Date(point.time).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}), rx: point.rx, tx: point.tx})), true);
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
    const bars = document.querySelector('[data-chart-kind="traffic"]') !== null;
    canvas.style.width = "100%";
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(1, rect.width);
    const height = Math.max(220, rect.height);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const max = Math.max(1, ...points.flatMap((point) => [Number(point.rx), Number(point.tx)]));
    ctx.font = "11px system-ui";
    ctx.textBaseline = "middle";
    ctx.lineWidth = 1;
    const yTickCount = height < 260 ? 4 : 5;
    const rawYLabels = Array.from({length: yTickCount}, (_, step) => formatBytes(max * (1 - step / (yTickCount - 1)), rates ? "/s" : ""));
    const yLabels = rawYLabels.filter((label, index) => index === 0 || label !== rawYLabels[index - 1]);
    const yLabelWidth = Math.max(...yLabels.map((label) => ctx.measureText(label).width));
    const pad = {left: Math.ceil(yLabelWidth) + 12, right: 10, top: 12, bottom: 36};
    const plotWidth = Math.max(1, width - pad.left - pad.right);
    const plotHeight = Math.max(1, height - pad.top - pad.bottom);
    rawYLabels.forEach((label, step) => {
      if (step && label === rawYLabels[step - 1]) return;
      const y = pad.top + plotHeight * step / (yTickCount - 1);
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y);
      ctx.strokeStyle = "#e8ebf0"; ctx.stroke();
      ctx.fillStyle = "#8a95a5";
      ctx.textAlign = "right";
      ctx.fillText(label, pad.left - 8, y);
    });
    const drawXLabels = (centers) => {
      const bounds = points.map((point, index) => {
        const labelWidth = ctx.measureText(point.period).width;
        const x = Math.max(pad.left + labelWidth / 2, Math.min(width - pad.right - labelWidth / 2, centers[index]));
        return {index, x, left: x - labelWidth / 2, right: x + labelWidth / 2};
      });
      const selected = [];
      bounds.forEach((bound) => {
        const previous = selected[selected.length - 1];
        if (!previous || bound.left >= previous.right + 12) selected.push(bound);
      });
      const last = bounds[bounds.length - 1];
      if (last && selected[selected.length - 1]?.index !== last.index) {
        while (selected.length && last.left < selected[selected.length - 1].right + 12) selected.pop();
        selected.push(last);
      }
      ctx.fillStyle = "#8a95a5";
      ctx.textAlign = "center";
      ctx.textBaseline = "alphabetic";
      selected.forEach(({index, x}) => ctx.fillText(points[index].period, x, height - 9));
    };
    if (bars) {
      const groupWidth = plotWidth / points.length;
      const barWidth = Math.min(16, Math.max(4, groupWidth * 0.3));
      const centers = points.map((_, index) => pad.left + groupWidth * (index + 0.5));
      points.forEach((point, index) => {
        [["rx", "#1677ff", -barWidth], ["tx", "#16a765", 0]].forEach(([key, color, offset]) => {
          const barHeight = plotHeight * Number(point[key]) / max;
          ctx.fillStyle = color;
          ctx.fillRect(centers[index] + offset, height - pad.bottom - barHeight, barWidth, barHeight);
        });
      });
      drawXLabels(centers);
    } else {
      const centers = points.map((_, index) => pad.left + plotWidth * index / Math.max(1, points.length - 1));
      const plot = (key, color) => {
        ctx.beginPath();
        points.forEach((point, index) => {
          const y = height - pad.bottom - plotHeight * Number(point[key]) / max;
          index ? ctx.lineTo(centers[index], y) : ctx.moveTo(centers[index], y);
        });
        ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
      };
      plot("rx", "#1677ff"); plot("tx", "#16a765");
      drawXLabels(centers);
    }
  }

  const periodLabel = (period, days) => {
    const normalized = period.includes("T") ? period : period.replace(" ", "T");
    const date = new Date(`${normalized}${normalized.length === 16 ? ":00" : "T00:00:00"}Z`);
    if (Number.isNaN(date.getTime())) return period;
    if (Number(days) === 30) return date.toLocaleDateString([], {month: "2-digit", day: "2-digit"});
    return date.toLocaleString([], {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false});
  };
  const historyPanel = document.querySelector("[data-history-url]");
  const historyBody = document.querySelector("[data-history-body]");
  const pageSizeSelect = historyPanel?.querySelector(".page-size-select");
  const pagePicker = historyPanel?.querySelector(".page-picker");
  const totalPagesNode = historyPanel?.querySelector(".total-pages");
  const prevPageButton = historyPanel?.querySelector(".prev-page");
  const nextPageButton = historyPanel?.querySelector(".next-page");
  let historyRows = [];
  let historyChartPoints = [];
  let historyPage = 1;
  let historyPageSize = 30;
  let historyInterval = 1;

  const renderHistoryPage = () => {
    if (!historyBody) return;
    const totalPages = Math.max(1, Math.ceil(historyRows.length / historyPageSize));
    historyPage = Math.min(historyPage, totalPages);
    historyBody.replaceChildren();
    if (!historyRows.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 4;
      cell.textContent = "暂无记录";
      row.append(cell);
      historyBody.append(row);
    } else {
      const start = (historyPage - 1) * historyPageSize;
      historyRows.slice(start, start + historyPageSize).forEach((point) => {
        const row = document.createElement("tr");
        const rate = historyPanel.dataset.chartKind === "bandwidth";
        const traffic = readWireTraffic(point);
        const values = rate
          ? [point.period, formatBytes(traffic.rx / historyInterval, "/s"), formatBytes(traffic.tx / historyInterval, "/s")]
          : [point.period, formatBytes(traffic.rx), formatBytes(traffic.tx)];
        values.forEach((value) => { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); });
        const totalCell = document.createElement("td");
        const total = document.createElement("strong");
        const bytes = traffic.rx + traffic.tx;
        total.textContent = rate ? formatBytes(bytes / historyInterval, "/s") : formatBytes(bytes);
        totalCell.append(total);
        row.append(totalCell);
        historyBody.append(row);
      });
    }
    totalPagesNode.textContent = totalPages;
    pagePicker.replaceChildren(...Array.from({length: totalPages}, (_, index) => {
      const option = document.createElement("option");
      option.value = String(index + 1);
      option.textContent = String(index + 1);
      option.selected = index + 1 === historyPage;
      return option;
    }));
    prevPageButton.disabled = historyPage === 1;
    nextPageButton.disabled = historyPage === totalPages;
  };

  async function loadHistory(days = 1) {
    if (!historyPanel) return;
    const kind = historyPanel.dataset.chartKind;
    const response = await fetch(`${historyPanel.dataset.historyUrl}?kind=${kind}&days=${days}`);
    if (!response.ok) return;
    const data = await response.json();
    const rawPoints = data.points.map((point) => {
      const traffic = readWireTraffic(point);
      return {...point, period: periodLabel(point.period, days), rx: traffic.rx, tx: traffic.tx};
    });
    historyInterval = Number(data.interval_seconds) || 1;
    historyRows = rawPoints.slice().reverse();
    historyPage = 1;
    renderHistoryPage();
    historyChartPoints = kind === "bandwidth"
      ? rawPoints.map((point) => ({...point, rx: point.rx / historyInterval, tx: point.tx / historyInterval}))
      : rawPoints;
    drawChart(historyChartPoints, kind === "bandwidth");
  }

  pageSizeSelect?.addEventListener("change", () => {
    historyPageSize = Number(pageSizeSelect.value) || 30;
    historyPage = 1;
    renderHistoryPage();
  });
  pagePicker?.addEventListener("change", () => {
    historyPage = Number(pagePicker.value) || 1;
    renderHistoryPage();
  });
  prevPageButton?.addEventListener("click", () => { if (historyPage > 1) { historyPage -= 1; renderHistoryPage(); } });
  nextPageButton?.addEventListener("click", () => {
    if (historyPage < Math.ceil(historyRows.length / historyPageSize)) { historyPage += 1; renderHistoryPage(); }
  });

  document.querySelectorAll("[data-range] button").forEach((button) => button.addEventListener("click", () => {
    button.parentElement.querySelectorAll("button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    const value = button.dataset.rangeValue;
    document.querySelector("[data-chart-caption]")?.replaceChildren(value === "live" ? "当前实时" : `最近 ${button.textContent}`);
    if (value === "live") drawChart(livePoints.map((point) => ({period: new Date(point.time).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}), rx: point.rx, tx: point.tx})), true);
    else loadHistory(value);
  }));
  let resizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      drawConnectionsSparkline();
      const active = document.querySelector("[data-range] .active")?.dataset.rangeValue;
      if (active === "live" && livePoints.length) {
        drawChart(livePoints.map((point) => ({period: new Date(point.time).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}), rx: point.rx, tx: point.tx})), true);
      } else if (active && historyChartPoints.length) {
        drawChart(historyChartPoints, document.querySelector("[data-chart-kind]")?.dataset.chartKind === "bandwidth");
      }
    }, 120);
  }, {passive: true});
  loadOverview();
  const initialRange = document.querySelector("[data-range] .active")?.dataset.rangeValue;
  if (initialRange && initialRange !== "live") loadHistory(initialRange);
  if (overview) window.setInterval(loadOverview, 1000);
})();
