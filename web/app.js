(() => {
  "use strict";

  const runs = JSON.parse(document.getElementById("run-data").textContent || "[]");
  const runSelect = document.getElementById("run-select");
  const protocolSelect = document.getElementById("protocol-select");
  const dialog = document.getElementById("cell-dialog");
  const params = new URLSearchParams(window.location.search);

  const statusView = {
    pass: { symbol: "\u2713", label: "Pass" },
    fail: { symbol: "\u00d7", label: "Fail" },
    error: { symbol: "!", label: "Error" },
    unsupported: { symbol: "\u2013", label: "Unsupported" },
  };

  const protocolNames = {
    http2: "HTTP/2",
    http3: "HTTP/3",
    "udp-over-stream": "UDP over stream",
    "udp-over-datagram": "UDP over datagram",
  };
  const protocolBadges = {
    http2: "H2",
    http3: "H3",
    "udp-over-stream": "UDP/S",
    "udp-over-datagram": "UDP/D",
  };
  const protocolValues = Object.keys(protocolNames);

  if (!runs.length) {
    document.getElementById("empty-state").hidden = false;
    runSelect.disabled = true;
    protocolSelect.disabled = true;
    return;
  }

  for (const run of runs) {
    const option = document.createElement("option");
    option.value = run.run_id;
    option.textContent = formatRunLabel(run.started_at);
    runSelect.append(option);
  }

  const requestedRun = params.get("run");
  runSelect.value = runs.some((run) => run.run_id === requestedRun)
    ? requestedRun
    : runs[0].run_id;
  const requestedProtocol = params.get("protocol");
  protocolSelect.value = ["all", ...protocolValues].includes(requestedProtocol)
    ? requestedProtocol
    : "all";

  runSelect.addEventListener("change", render);
  protocolSelect.addEventListener("change", render);
  document.getElementById("dialog-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });

  document.getElementById("report-content").hidden = false;
  render();

  function render() {
    const run = runs.find((item) => item.run_id === runSelect.value) || runs[0];
    const protocol = protocolSelect.value;
    updateLocation(run.run_id, protocol);
    document.getElementById("run-context").textContent = run.run_id;
    document.getElementById("run-target").textContent = run.target;
    document.getElementById("run-started").textContent = formatDate(run.started_at);
    document.getElementById("run-duration").textContent = formatDuration(
      Date.parse(run.finished_at) - Date.parse(run.started_at),
    );
    document.getElementById("runner-version").textContent = `v${run.runner_version}`;

    const implementations = new Map(run.implementations.map((item) => [item.key, item]));
    const clients = unique(run.results.map((item) => item.client));
    const servers = unique(run.results.map((item) => item.server));
    const cells = new Map(run.results.map((item) => [`${item.client}:${item.server}`, item]));
    renderMatrix(clients, servers, cells, implementations, protocol);
    renderUdpTables(clients, servers, cells, implementations);
    renderSummary(run.results, protocol);
    renderImplementations(run.implementations);
  }

  function renderMatrix(clients, servers, cells, implementations, protocol) {
    const head = document.getElementById("matrix-head");
    const body = document.getElementById("matrix-body");
    head.replaceChildren();
    body.replaceChildren();

    const corner = document.createElement("th");
    corner.className = "corner-label";
    corner.scope = "col";
    corner.textContent = "Client implementation";
    head.append(corner);
    for (const server of servers) {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = implementations.get(server)?.name || server;
      head.append(th);
    }

    for (const client of clients) {
      const row = document.createElement("tr");
      const label = document.createElement("th");
      label.className = "row-label";
      label.scope = "row";
      label.textContent = implementations.get(client)?.name || client;
      const axis = document.createElement("span");
      axis.textContent = "CLIENT";
      label.append(axis);
      row.append(label);
      for (const server of servers) {
        const cell = cells.get(`${client}:${server}`);
        const td = document.createElement("td");
        if (cell) td.append(createCellButton(cell, implementations, protocol));
        row.append(td);
      }
      body.append(row);
    }
  }

  function createCellButton(cell, implementations, protocol) {
    const status = statusFor(cell, protocol);
    const view = statusView[status] || statusView.error;
    const visibleProbes = protocol === "all"
      ? cell.probes
      : cell.probes.filter((probe) => probe.protocol === protocol);
    const probeSummary = visibleProbes
      .map((probe) => `${probeLabel(probe.protocol)} ${statusView[probe.status]?.label || probe.status}`)
      .join(", ");
    const button = document.createElement("button");
    button.type = "button";
    button.className = `cell-button ${status}`;
    button.title = `${view.label}: open details`;
    button.setAttribute(
      "aria-label",
      `${implementations.get(cell.client)?.name || cell.client} client to ${
        implementations.get(cell.server)?.name || cell.server
      } server: ${view.label}. ${probeSummary}`,
    );
    const symbol = document.createElement("span");
    symbol.className = "symbol";
    symbol.setAttribute("aria-hidden", "true");
    symbol.textContent = view.symbol;
    const label = document.createElement("span");
    label.className = "status-label";
    label.textContent = view.label;
    const badges = document.createElement("span");
    badges.className = "probe-badges";
    badges.setAttribute("aria-hidden", "true");
    for (const probe of visibleProbes) {
      const badge = document.createElement("span");
      badge.className = `probe-badge ${probe.status}`;
      badge.textContent = probeLabel(probe.protocol);
      badges.append(badge);
    }
    button.append(symbol, label, badges);
    const memory = cellMemInfo(visibleProbes);
    if (memory) {
      const mem = document.createElement("span");
      mem.className = "cell-mem";
      mem.textContent = `C ${formatMemKb(memory.client)} / S ${formatMemKb(memory.server)} MB`;
      mem.title = "Peak container memory during the load phase (client / server)";
      button.append(mem);
    }
    button.addEventListener("click", () => showDetails(cell, implementations, protocol));
    return button;
  }

  function renderUdpTables(clients, servers, cells, implementations) {
    for (const mode of ["stream", "datagram"]) {
      const protocol = mode === "stream" ? "udp-over-stream" : "udp-over-datagram";
      const head = document.getElementById(`udp-${mode}-head`);
      const body = document.getElementById(`udp-${mode}-body`);
      head.replaceChildren();
      body.replaceChildren();

      const corner = document.createElement("th");
      corner.className = "corner-label";
      corner.scope = "col";
      corner.textContent = "Client implementation";
      head.append(corner);
      for (const server of servers) {
        const th = document.createElement("th");
        th.scope = "col";
        th.textContent = implementations.get(server)?.name || server;
        head.append(th);
      }

      for (const client of clients) {
        const row = document.createElement("tr");
        const label = document.createElement("th");
        label.className = "row-label";
        label.scope = "row";
        label.textContent = implementations.get(client)?.name || client;
        const axis = document.createElement("span");
        axis.textContent = "CLIENT";
        label.append(axis);
        row.append(label);
        for (const server of servers) {
          const cell = cells.get(`${client}:${server}`);
          const probe = cell?.probes.find((item) => item.protocol === protocol);
          const td = document.createElement("td");
          if (!probe) {
            td.className = "udp-na";
            td.title = "This run did not include this UDP probe";
            td.append("—");
          } else {
            td.append(createUdpCellButton(cell, probe, implementations, protocol));
          }
          row.append(td);
        }
        body.append(row);
      }
    }
  }

  function createUdpCellButton(cell, probe, implementations, protocol) {
    const view = statusView[probe.status] || statusView.error;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `cell-button rate-cell ${probe.status}`;
    button.title = `${view.label}: open details`;

    if (probe.status === "pass") {
      const metrics = probe.metrics || {};
      if (metrics.sent_bytes != null && metrics.window_ms) {
        const up = rateMBs(metrics.sent_bytes, metrics.window_ms);
        const down = probe.duration_ms
          ? rateMBs(metrics.recv_bytes, probe.duration_ms)
          : "0.00";
        const upLine = document.createElement("span");
        upLine.className = "rate-line up";
        upLine.textContent = `\u2191 ${up}`;
        const downLine = document.createElement("span");
        downLine.className = "rate-line down";
        downLine.textContent = `\u2193 ${down}`;
        const unit = document.createElement("span");
        unit.className = "rate-unit";
        unit.textContent = "MB/s";
        button.append(upLine, downLine, unit);
        const p95 = metrics.load_latency_p95_ms ?? metrics.latency_p95_ms;
        if (p95 != null) {
          const latency = document.createElement("span");
          latency.className = "rate-line latency";
          latency.textContent = `p95 ${p95} ms`;
          button.append(latency);
        }
        const memory = cellMemInfo([probe]);
        if (memory) {
          const mem = document.createElement("span");
          mem.className = "rate-line memory";
          mem.textContent = `C ${formatMemKb(memory.client)} / S ${formatMemKb(memory.server)} MB`;
          mem.title = "Peak container memory during the load phase (client / server)";
          button.append(mem);
        }
        button.setAttribute(
          "aria-label",
          `${implementations.get(cell.client)?.name || cell.client} client to ${
            implementations.get(cell.server)?.name || cell.server
          } server, ${protocolNames[protocol]}: pass, upload ${up} MB/s, download ${down} MB/s`,
        );
      } else {
        const symbol = document.createElement("span");
        symbol.className = "symbol";
        symbol.setAttribute("aria-hidden", "true");
        symbol.textContent = view.symbol;
        button.append(symbol);
      }
    } else {
      const symbol = document.createElement("span");
      symbol.className = "symbol";
      symbol.setAttribute("aria-hidden", "true");
      symbol.textContent = view.symbol;
      const label = document.createElement("span");
      label.className = "status-label";
      label.textContent = view.label;
      button.append(symbol, label);
    }

    button.addEventListener("click", () => showDetails(cell, implementations, protocol));
    return button;
  }

  function renderSummary(results, protocol) {
    const counts = { pass: 0, fail: 0, error: 0, unsupported: 0 };
    for (const cell of results) counts[statusFor(cell, protocol)] += 1;
    for (const [status, count] of Object.entries(counts)) {
      document.getElementById(`summary-${status}`).textContent = String(count);
    }
  }

  function renderImplementations(implementations) {
    const list = document.getElementById("implementation-list");
    list.replaceChildren();
    for (const implementation of implementations) {
      const row = document.createElement("div");
      row.className = "implementation-row";
      const identity = document.createElement("div");
      const link = document.createElement("a");
      link.href = implementation.source;
      link.rel = "noreferrer";
      link.textContent = implementation.name;
      const strong = document.createElement("strong");
      strong.append(link);
      identity.append(strong);
      const detail = document.createElement("div");
      const image = document.createElement("p");
      image.textContent = implementation.note || implementation.image;
      detail.append(image);
      const capabilities = document.createElement("span");
      capabilities.className = "capabilities";
      capabilities.textContent = `client:${implementation.client ? "yes" : "no"}  server:${
        implementation.server ? "yes" : "no"
      }`;
      row.append(identity, detail, capabilities);
      list.append(row);
    }
  }

  function showDetails(cell, implementations, protocol) {
    const client = implementations.get(cell.client)?.name || cell.client;
    const server = implementations.get(cell.server)?.name || cell.server;
    document.getElementById("dialog-title").textContent = `${client} \u2192 ${server}`;
    const body = document.getElementById("dialog-body");
    body.replaceChildren();
    const probes = protocol === "all"
      ? cell.probes
      : cell.probes.filter((probe) => probe.protocol === protocol);
    for (const probe of probes) body.append(createProbeDetail(probe));
    dialog.showModal();
  }

  function createProbeDetail(probe) {
    const section = document.createElement("section");
    section.className = "probe-detail";
    const title = document.createElement("div");
    title.className = "probe-title";
    const name = document.createElement("strong");
    name.textContent = protocolNames[probe.protocol] || probe.protocol;
    const status = document.createElement("span");
    status.className = `status-text ${probe.status}`;
    status.textContent = statusView[probe.status]?.label || probe.status;
    title.append(name, status);
    section.append(title);

    const metrics = probe.metrics || {};
    const values = [];
    if (probe.http_status != null) values.push(["HTTP status", String(probe.http_status)]);
    if (probe.duration_ms != null) values.push(["Total", `${probe.duration_ms} ms`]);
    if (probe.protocol === "udp-over-stream" || probe.protocol === "udp-over-datagram") {
      if (metrics.sent_bytes != null && metrics.window_ms != null) {
        values.push(["Upload", `${rateMBs(metrics.sent_bytes, metrics.window_ms)} MB/s`]);
      }
      if (metrics.recv_bytes != null && probe.duration_ms != null) {
        values.push(["Download", `${rateMBs(metrics.recv_bytes, probe.duration_ms)} MB/s`]);
      }
      if (metrics.recv_packets != null && metrics.sent_packets != null) {
        values.push([
          "Echoed datagrams",
          `${metrics.recv_packets} / ${metrics.sent_packets}`,
        ]);
      }
    }
    if (metrics.load_connections != null) {
      values.push([
        "Load sessions",
        `${metrics.load_ok ?? 0} / ${metrics.load_connections}`,
      ]);
    }
    const memInfo = cellMemInfo([probe]);
    if (memInfo) {
      values.push(["Peak memory client", `${formatMemKb(memInfo.client)} MB`]);
      values.push(["Peak memory server", `${formatMemKb(memInfo.server)} MB`]);
    }
    for (const [key, value] of Object.entries(metrics)) {
      if (key.startsWith("load_mem_")) continue; // shown as Peak memory above
      values.push([metricLabel(key), formatMetric(key, value)]);
    }
    if (values.length) {
      const list = document.createElement("dl");
      list.className = "metrics";
      for (const [key, value] of values) {
        const wrapper = document.createElement("div");
        const dt = document.createElement("dt");
        const dd = document.createElement("dd");
        dt.textContent = key;
        dd.textContent = value;
        wrapper.append(dt, dd);
        list.append(wrapper);
      }
      section.append(list);
    }
    if (probe.message) {
      const message = document.createElement("p");
      message.className = "probe-message";
      message.textContent = probe.message;
      section.append(message);
    }
    return section;
  }

  function rateMBs(bytes, milliseconds) {
    const seconds = milliseconds / 1000;
    if (!Number.isFinite(bytes) || !seconds) return "0.00";
    return (bytes / 1000000 / seconds).toFixed(2);
  }

  function formatMemKb(kib) {
    return (kib / 1024).toFixed(1);
  }

  function cellMemInfo(probes) {
    for (const probe of probes) {
      const metrics = probe.metrics || {};
      if (metrics.load_mem_client_max_kb != null) {
        return {
          client: metrics.load_mem_client_max_kb,
          server: metrics.load_mem_server_max_kb || 0,
        };
      }
    }
    return null;
  }

  function metricLabel(key) {
    return key.replace(/_(ms|kb|bytes|packets|samples|connections)$/, "").replace(/_/g, " ");
  }

  function formatMetric(key, value) {
    if (key === "size" || key.endsWith("_bytes")) return `${value} B`;
    if (key.endsWith("_kb")) return `${value} KB`;
    if (key.endsWith("_ms")) return `${value} ms`;
    return String(value);
  }

  function statusFor(cell, protocol) {
    if (protocol === "all") return cell.status;
    return cell.probes.find((probe) => probe.protocol === protocol)?.status || "error";
  }

  function probeLabel(protocol) {
    return protocolBadges[protocol] || protocol;
  }

  function updateLocation(runId, protocol) {
    const query = new URLSearchParams();
    query.set("run", runId);
    if (protocol !== "all") query.set("protocol", protocol);
    history.replaceState(null, "", `${window.location.pathname}?${query}`);
  }

  function formatRunLabel(value) {
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "UTC",
      timeZoneName: "short",
    }).format(new Date(value));
  }

  function formatDate(value) {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "medium",
      timeZone: "UTC",
    }).format(new Date(value));
  }

  function formatDuration(milliseconds) {
    if (!Number.isFinite(milliseconds) || milliseconds < 0) return "unknown";
    if (milliseconds < 1000) return `${milliseconds} ms`;
    const seconds = Math.round(milliseconds / 100) / 10;
    return `${seconds} s`;
  }

  function unique(values) {
    return [...new Set(values)];
  }
})();
