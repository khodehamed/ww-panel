(() => {
  const $ = (id) => document.getElementById(id);
  const charts = new Map();
  let tunnels = [];
  let statusTimer = null;
  let metricsTimer = null;

  const fmtBps = (n) => {
    const v = Number(n) || 0;
    if (v >= 1e9) return (v / 1e9).toFixed(2) + " Gbps";
    if (v >= 1e6) return (v / 1e6).toFixed(2) + " Mbps";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + " Kbps";
    return Math.round(v) + " bps";
  };

  const toast = (msg, danger) => {
    const el = $("toast");
    el.textContent = msg;
    el.hidden = false;
    el.style.borderColor = danger ? "rgba(251,113,133,.45)" : "";
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.hidden = true; }, 3800);
  };

  const api = async (path, opts = {}) => {
    const res = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    let data = {};
    try { data = await res.json(); } catch { data = {}; }
    if (res.status === 401) throw Object.assign(new Error("auth"), { auth: true });
    if (!res.ok || data.ok === false) throw new Error(data.error || data.detail || res.statusText);
    return data;
  };

  const setClock = () => {
    const now = new Date();
    $("clock").textContent = now.toLocaleString("fa-IR", { hour12: false });
  };

  const showApp = (on) => {
    $("login-view").hidden = on;
    $("app-view").hidden = !on;
  };

  const closeModal = () => { $("modal").hidden = true; $("modal-body").innerHTML = ""; };
  $("modal-close").onclick = closeModal;
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

  const pillClass = (status) => {
    if (status === "UP") return "up";
    if (status === "DEGRADED") return "degraded";
    return "down";
  };

  const ensureChart = (id, canvas) => {
    if (!window.Chart) return null;
    if (charts.has(id)) {
      const ch = charts.get(id);
      if (ch.canvas === canvas) return ch;
      ch.destroy();
      charts.delete(id);
    }
    const ch = new Chart(canvas, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          { label: "RX", data: [], borderColor: "#67e8f9", backgroundColor: "rgba(103,232,249,.12)", fill: true, tension: 0.35, pointRadius: 0, borderWidth: 1.6 },
          { label: "TX", data: [], borderColor: "#2dd4bf", backgroundColor: "rgba(45,212,191,.08)", fill: true, tension: 0.35, pointRadius: 0, borderWidth: 1.6 },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { display: false },
          y: { display: false, beginAtZero: true },
        },
      },
    });
    charts.set(id, ch);
    return ch;
  };

  const renderStats = (list) => {
    const up = list.filter((t) => t.status === "UP").length;
    const deg = list.filter((t) => t.status === "DEGRADED").length;
    const down = list.filter((t) => t.status === "DOWN").length;
    const locked = list.filter((t) => t.locked || t.kind === "backhaul").length;
    $("stats").innerHTML = `
      <div class="stat"><span>آنلاین UP</span><b>${up}</b></div>
      <div class="stat"><span>نیمه‌سالم DEGRADED</span><b>${deg}</b></div>
      <div class="stat"><span>قطع DOWN</span><b>${down}</b></div>
      <div class="stat"><span>بک‌هال قفل‌شده</span><b>${locked}</b></div>`;
  };

  const cardHtml = (t) => {
    const ping = t.ping || {};
    const pingTxt = ping.ok ? `${ping.latency_ms ?? "—"} ms` : (ping.error || "بدون پاسخ");
    const locked = t.locked || t.kind === "backhaul";
    const role = t.local_role === "iran" ? "این سمت: ایران" : (t.local_role === "kharej" ? "این سمت: خارج" : "");
    return `
      <article class="card ${pillClass(t.status)}" data-id="${t.id}">
        <div class="card-head">
          <div>
            <h3>${t.name}</h3>
            <div class="meta">${t.unit || "no-unit"} · ${t.family || ""} ${role ? "· " + role : ""}</div>
          </div>
          <div class="pills">
            <span class="pill ${pillClass(t.status)}">${t.status}</span>
            ${locked ? '<span class="pill lock">LOCKED</span>' : ""}
            ${t.encrypt === "1" ? '<span class="pill enc">ENCRYPT</span>' : '<span class="pill lock">NO ENC</span>'}
            ${t.proto ? `<span class="pill proto">PROTO ${t.proto}</span>` : ""}
          </div>
        </div>
        <div class="pair">
          <div>IR ${t.iran_ip || "—"}</div>
          <div class="arrow">↔</div>
          <div>KH ${t.kharej_ip || "—"}</div>
        </div>
        <div class="kv">
          <div>تونل <b>${t.tun || "—"}</b></div>
          <div>systemd <b>${(t.systemd && t.systemd.active) || "—"}</b></div>
          <div>IP تونل <b>${t.tun_local || "—"}</b></div>
          <div>پیر <b>${t.tun_peer || "—"}</b></div>
          <div>پینگ <b>${pingTxt}</b></div>
          <div>پهنای‌باند <b>${fmtBps(t.rx_bps)} ↓ / ${fmtBps(t.tx_bps)} ↑</b></div>
        </div>
        <div class="chart-box"><canvas id="chart-${t.id}"></canvas></div>
        <div class="card-actions">
          <button class="btn ghost" data-act="detail">جزئیات / ویرایش</button>
          <button class="btn warn" data-act="restart" ${locked || !t.unit ? "disabled" : ""}>ریستارت</button>
        </div>
      </article>`;
  };

  const renderCards = () => {
    if (!tunnels.length) {
      $("cards").innerHTML = `<div class="empty">تانلی پیدا نشد. نصب ww51 / ww51v2 را روی همین سرور انجام دهید یا از «تانل جدید» پروفایل ایزوله بسازید.</div>`;
      return;
    }
    $("cards").innerHTML = tunnels.map(cardHtml).join("");
    $("cards").querySelectorAll(".card").forEach((el) => {
      el.querySelector('[data-act="detail"]').onclick = () => openDetail(el.dataset.id);
      const rst = el.querySelector('[data-act="restart"]');
      if (rst) rst.onclick = () => restartTunnel(el.dataset.id);
    });
  };

  const updateCharts = async () => {
    for (const t of tunnels) {
      if (!t.tun) continue;
      try {
        const m = await api(`/api/tunnels/${encodeURIComponent(t.id)}/metrics`);
        const canvas = document.getElementById(`chart-${t.id}`);
        if (!canvas) continue;
        const ch = ensureChart(t.id, canvas);
        if (!ch) continue;
        const pts = (m.points || []).slice(-90);
        ch.data.labels = pts.map((_, i) => i);
        ch.data.datasets[0].data = pts.map((p) => p.rx_bps);
        ch.data.datasets[1].data = pts.map((p) => p.tx_bps);
        ch.update("none");
      } catch (err) {
        if (err.auth) return logoutView();
      }
    }
  };

  const loadTunnels = async () => {
    const data = await api("/api/tunnels");
    tunnels = data.tunnels || [];
    renderStats(tunnels);
    renderCards();
  };

  const field = (label, name, value, extra = "") => `
    <label>${label}
      <input name="${name}" value="${String(value ?? "").replace(/"/g, "&quot;")}" ${extra} />
    </label>`;

  const openDetail = async (id) => {
    const data = await api(`/api/tunnels/${encodeURIComponent(id)}`);
    const t = data.tunnel;
    $("modal-title").textContent = t.locked ? `بک‌هال · ${t.name}` : `ویرایش · ${t.name}`;
    const ro = t.locked || !t.writable;
    $("modal-body").innerHTML = `
      ${t.locked ? `<div class="lock-box">بک‌هال فقط نمایش است. این پنل هرگز آن را ویرایش یا ریستارت نمی‌کند.</div>` : ""}
      ${t.unmanaged ? `<div class="warn-box">کانفیگ WaterWall بدون tunnel.env — فقط‌خواندنی.</div>` : ""}
      ${!ro ? `<div class="warn-box">ذخیره، tunnel.env را بکاپ می‌گیرد و فقط همین یونیت را apply/restart می‌کند. v1/v2/بک‌هال دیگر دست نخورده می‌مانند.</div>` : ""}
      <form id="edit-form">
        <div class="grid2">
          ${field("IP ایران / Iran IP", "iran_ip", t.iran_ip, ro ? "disabled" : "")}
          ${field("IP خارج / Kharej IP", "kharej_ip", t.kharej_ip, ro ? "disabled" : "")}
          ${field("پروتکل PROTO (0-255)", "proto", t.proto, ro ? "disabled" : "")}
          ${field("پورت‌ها PORTS", "ports", t.ports, ro ? "disabled" : "")}
          <label>رمزنگاری ENCRYPT
            <select name="encrypt" ${ro ? "disabled" : ""}>
              <option value="0" ${t.encrypt !== "1" ? "selected" : ""}>خاموش 0</option>
              <option value="1" ${t.encrypt === "1" ? "selected" : ""}>روشن 1</option>
            </select>
          </label>
          ${field("AES_KEY (۳۲ کاراکتر)", "aes_key", "", ro ? "disabled placeholder=\"••••\"" : `placeholder="${t.aes_key_masked || "خالی = بدون تغییر"}"`)}
          ${field("PORT_OFFSET", "port_offset", t.port_offset, ro ? "disabled" : "")}
          ${field("ENC_ALGO", "enc_algo", t.enc_algo, ro ? "disabled" : "")}
        </div>
        <div class="kv" style="margin-top:12px">
          <div>سمت <b>${t.side || "—"}</b></div>
          <div>تونل <b>${t.tun || "—"} ${t.tun_local || ""}</b></div>
          <div>systemd <b>${(t.systemd && t.systemd.active) || "—"}</b></div>
          <div>پوشه <b>${t.install_dir || "—"}</b></div>
        </div>
        ${ro ? "" : `<div class="form-actions">
          <button type="button" class="btn ghost" id="btn-start">Start</button>
          <button type="button" class="btn danger" id="btn-stop">Stop</button>
          <button type="submit" class="btn primary">ذخیره و اعمال</button>
        </div>`}
      </form>`;
    $("modal").hidden = false;
    const form = $("edit-form");
    if (!ro) {
      form.onsubmit = async (e) => {
        e.preventDefault();
        if (!confirm("اعمال شود؟ فقط همین تانل ریستارت می‌شود.")) return;
        const fd = new FormData(form);
        const body = Object.fromEntries(fd.entries());
        if (!body.aes_key) delete body.aes_key;
        try {
          await api(`/api/tunnels/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) });
          toast("اعمال شد");
          closeModal();
          await loadTunnels();
        } catch (err) { toast(err.message, true); }
      };
      $("btn-start").onclick = () => control(id, "start");
      $("btn-stop").onclick = () => control(id, "stop");
    }
  };

  const control = async (id, action) => {
    if (!confirm(`${action} فقط برای همین یونیت؟`)) return;
    try {
      await api(`/api/tunnels/${encodeURIComponent(id)}/${action}`, { method: "POST" });
      toast(action + " انجام شد");
      await loadTunnels();
    } catch (err) { toast(err.message, true); }
  };

  const restartTunnel = (id) => control(id, "restart");

  const openWizard = async () => {
    let slot = null;
    try { slot = (await api("/api/slots/next")).slot; } catch (err) { toast(err.message, true); }
    $("modal-title").textContent = "جادوگر تانل جدید";
    $("modal-body").innerHTML = `
      <div class="warn-box">
        پروفایل ایزوله ساخته می‌شود (${slot ? slot.id : "tN"}) با تونل ${slot ? slot.tun : "wtunN"} و ساب‌نت ${slot ? slot.tun_cidr : "10.10.N.0/24"}.
        یونیت‌های waterwall-proto51 و v2 و بک‌هال تغییر نمی‌کنند.
      </div>
      <form id="new-form">
        <div class="grid2">
          <label>سمت
            <select name="side">
              <option value="ir">ایران (Iran)</option>
              <option value="kharej">خارج (Kharej)</option>
            </select>
          </label>
          ${field("IP ایران", "iran_ip", "", "required")}
          ${field("IP خارج", "kharej_ip", "", "required")}
          ${field("PROTO", "proto", "51")}
          ${field("PORTS", "ports", "443 2053 2083 2087 2096 8443")}
          <label>ENCRYPT
            <select name="encrypt">
              <option value="0" selected>خاموش</option>
              <option value="1">روشن</option>
            </select>
          </label>
          ${field("AES_KEY (۳۲ کاراکتر اگر ENCRYPT=1)", "aes_key", "")}
          ${field("PORT_OFFSET", "port_offset", "10000")}
          ${field("ENC_ALGO", "enc_algo", "chacha20-poly1305")}
        </div>
        <div class="form-actions">
          <button type="submit" class="btn primary">ایجاد تانل ایزوله</button>
        </div>
      </form>`;
    $("modal").hidden = false;
    $("new-form").onsubmit = async (e) => {
      e.preventDefault();
      if (!confirm("تانل جدید ایزوله ایجاد شود؟ v1/v2 دست نمی‌خورند.")) return;
      const body = Object.fromEntries(new FormData(e.target).entries());
      try {
        const res = await api("/api/tunnels", { method: "POST", body: JSON.stringify(body) });
        toast("تانل ساخته شد: " + (res.slot && res.slot.id));
        closeModal();
        await loadTunnels();
      } catch (err) { toast(err.message, true); }
    };
  };

  const logoutView = () => {
    showApp(false);
    clearInterval(statusTimer);
    clearInterval(metricsTimer);
    statusTimer = metricsTimer = null;
  };

  const startLoops = async () => {
    showApp(true);
    setClock();
    await loadTunnels();
    await updateCharts();
    clearInterval(statusTimer);
    clearInterval(metricsTimer);
    statusTimer = setInterval(async () => {
      try { await loadTunnels(); } catch (err) { if (err.auth) logoutView(); }
    }, 5000);
    metricsTimer = setInterval(updateCharts, 2000);
  };

  $("login-form").onsubmit = async (e) => {
    e.preventDefault();
    $("login-error").hidden = true;
    try {
      await api("/api/login", {
        method: "POST",
        body: JSON.stringify({ username: $("login-user").value, password: $("login-pass").value }),
      });
      await startLoops();
    } catch (err) {
      $("login-error").textContent = "ورود نامعتبر";
      $("login-error").hidden = false;
    }
  };

  $("btn-logout").onclick = async () => {
    try { await api("/api/logout", { method: "POST" }); } catch { /* ignore */ }
    logoutView();
  };
  $("btn-refresh").onclick = () => loadTunnels().catch((err) => toast(err.message, true));
  $("btn-new").onclick = openWizard;
  setInterval(setClock, 1000);

  api("/api/me").then(startLoops).catch(() => showApp(false));
})();
