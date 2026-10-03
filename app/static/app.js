(() => {
  const $ = (id) => document.getElementById(id);
  let tunnels = [];
  let knownKey = "";
  let statusTimer = null;
  let metricsTimer = null;
  let clockTimer = null;

  const fmtBps = (n) => {
    const v = Number(n) || 0;
    if (v >= 1e9) return (v / 1e9).toFixed(2) + " Gbps";
    if (v >= 1e6) return (v / 1e6).toFixed(2) + " Mbps";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + " Kbps";
    return Math.round(v) + " bps";
  };

  const pingTxt = (ping) => {
    const p = ping || {};
    if (p.ok) return `${p.latency_ms ?? "—"} ms`;
    if (p.error === "pending") return "در حال سنجش";
    return p.error || "بدون پاسخ";
  };

  const toast = (msg, danger) => {
    const el = $("toast");
    el.textContent = msg;
    el.classList.toggle("danger", !!danger);
    el.classList.add("is-open");
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(() => {
      el.classList.remove("is-open");
      el.hidden = true;
    }, 3800);
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
    const el = $("clock");
    if (!el) return;
    el.textContent = new Date().toLocaleString("fa-IR", { hour12: false });
  };

  const closeModal = () => {
    const el = $("modal");
    el.classList.remove("is-open");
    el.hidden = true;
    el.setAttribute("aria-hidden", "true");
    $("modal-body").innerHTML = "";
    $("modal-title").textContent = "";
  };

  const openModal = () => {
    const el = $("modal");
    el.classList.add("is-open");
    el.hidden = false;
    el.setAttribute("aria-hidden", "false");
  };

  const showApp = (on) => {
    $("login-view").classList.toggle("is-open", !on);
    $("login-view").hidden = on;
    $("app-view").classList.toggle("is-open", on);
    $("app-view").hidden = !on;
    if (!on) {
      closeModal();
      $("toast").classList.remove("is-open");
      $("toast").hidden = true;
    }
  };

  $("modal-close").onclick = closeModal;
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

  const pillClass = (status) => {
    if (status === "UP") return "up";
    if (status === "DEGRADED") return "degraded";
    return "down";
  };

  const drawSpark = (canvas, rx, tx) => {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 300;
    const h = canvas.clientHeight || 80;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const rxd = rx && rx.length ? rx : [0, 0];
    const txd = tx && tx.length ? tx : [0, 0];
    const n = Math.max(rxd.length, txd.length, 2);
    let max = 1;
    for (const v of rxd) max = Math.max(max, Number(v) || 0);
    for (const v of txd) max = Math.max(max, Number(v) || 0);
    const paint = (data, stroke, fill) => {
      ctx.beginPath();
      data.forEach((v, i) => {
        const x = (i / (n - 1)) * w;
        const y = h - 3 - ((Number(v) || 0) / max) * (h - 8);
        if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      });
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 1.6;
      ctx.stroke();
      ctx.lineTo(w, h);
      ctx.lineTo(0, h);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
    };
    paint(txd, "#2dd4bf", "rgba(45,212,191,.10)");
    paint(rxd, "#67e8f9", "rgba(103,232,249,.12)");
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

  const setText = (root, sel, text) => {
    const el = root.querySelector(sel);
    if (el && el.textContent !== String(text)) el.textContent = text;
  };

  const cardHtml = (t) => {
    const locked = t.locked || t.kind === "backhaul";
    const role = t.local_role === "iran" ? "این سمت: ایران" : (t.local_role === "kharej" ? "این سمت: خارج" : "");
    return `
      <article class="card ${pillClass(t.status)}" data-id="${t.id}">
        <div class="card-head">
          <div>
            <h3 data-f="name">${t.name}</h3>
            <div class="meta" data-f="meta">${t.unit || "no-unit"} · ${t.family || ""} ${role ? "· " + role : ""}</div>
          </div>
          <div class="pills">
            <span class="pill ${pillClass(t.status)}" data-f="status">${t.status}</span>
            ${locked ? '<span class="pill lock">LOCKED</span>' : ""}
            <span class="pill ${t.encrypt === "1" ? "enc" : "lock"}" data-f="enc">${t.encrypt === "1" ? "ENCRYPT" : "NO ENC"}</span>
            <span class="pill proto" data-f="proto">${t.proto ? "PROTO " + t.proto : ""}</span>
          </div>
        </div>
        <div class="pair">
          <div data-f="iran">IR ${t.iran_ip || "—"}</div>
          <div class="arrow">↔</div>
          <div data-f="kharej">KH ${t.kharej_ip || "—"}</div>
        </div>
        <div class="kv">
          <div>تونل <b data-f="tun">${t.tun || "—"}</b></div>
          <div>systemd <b data-f="sysd">${(t.systemd && t.systemd.active) || "—"}</b></div>
          <div>IP تونل <b data-f="tlocal">${t.tun_local || "—"}</b></div>
          <div>پیر <b data-f="tpeer">${t.tun_peer || "—"}</b></div>
          <div>پینگ <b data-f="ping">${pingTxt(t.ping)}</b></div>
          <div>پهنای‌باند <b data-f="bps">${fmtBps(t.rx_bps)} ↓ / ${fmtBps(t.tx_bps)} ↑</b></div>
        </div>
        <div class="chart-box"><canvas data-chart="${t.id}"></canvas></div>
        <div class="card-actions">
          <button class="btn ghost" data-act="detail">جزئیات / ویرایش</button>
          <button class="btn warn" data-act="restart" ${locked || !t.unit ? "disabled" : ""}>ریستارت</button>
        </div>
      </article>`;
  };

  const bindCard = (el) => {
    el.querySelector('[data-act="detail"]').onclick = () => openDetail(el.dataset.id);
    const rst = el.querySelector('[data-act="restart"]');
    if (rst) rst.onclick = () => restartTunnel(el.dataset.id);
  };

  const updateCard = (t) => {
    const el = document.querySelector(`.card[data-id="${CSS.escape(t.id)}"]`);
    if (!el) return;
    el.classList.remove("up", "degraded", "down");
    el.classList.add("card", pillClass(t.status));
    setText(el, '[data-f="status"]', t.status);
    const st = el.querySelector('[data-f="status"]');
    if (st) st.className = `pill ${pillClass(t.status)}`;
    const enc = el.querySelector('[data-f="enc"]');
    if (enc) {
      enc.textContent = t.encrypt === "1" ? "ENCRYPT" : "NO ENC";
      enc.className = `pill ${t.encrypt === "1" ? "enc" : "lock"}`;
    }
    setText(el, '[data-f="proto"]', t.proto ? "PROTO " + t.proto : "");
    setText(el, '[data-f="iran"]', "IR " + (t.iran_ip || "—"));
    setText(el, '[data-f="kharej"]', "KH " + (t.kharej_ip || "—"));
    setText(el, '[data-f="tun"]', t.tun || "—");
    setText(el, '[data-f="sysd"]', (t.systemd && t.systemd.active) || "—");
    setText(el, '[data-f="tlocal"]', t.tun_local || "—");
    setText(el, '[data-f="tpeer"]', t.tun_peer || "—");
    setText(el, '[data-f="ping"]', pingTxt(t.ping));
    setText(el, '[data-f="bps"]', `${fmtBps(t.rx_bps)} ↓ / ${fmtBps(t.tx_bps)} ↑`);
  };

  const idsKey = (list) => list.map((t) => t.id).join("|");

  const rebuildCards = (list) => {
    if (!list.length) {
      $("cards").innerHTML = `<div class="empty">تانلی پیدا نشد. نصب ww51 / ww51v2 را روی همین سرور انجام دهید یا از «تانل جدید» پروفایل ایزوله بسازید.</div>`;
      return;
    }
    $("cards").innerHTML = list.map(cardHtml).join("");
    $("cards").querySelectorAll(".card").forEach(bindCard);
  };

  const applyTunnels = (list) => {
    tunnels = list;
    renderStats(list);
    const key = idsKey(list);
    if (key !== knownKey) {
      knownKey = key;
      rebuildCards(list);
      return;
    }
    list.forEach(updateCard);
  };

  const applyMetrics = (payload) => {
    const rows = (payload && payload.tunnels) || [];
    for (const m of rows) {
      const t = tunnels.find((x) => x.id === m.id);
      if (t) {
        t.rx_bps = m.rx_bps;
        t.tx_bps = m.tx_bps;
        if (m.ping) t.ping = m.ping;
      }
      const el = document.querySelector(`.card[data-id="${CSS.escape(m.id)}"]`);
      if (el) {
        setText(el, '[data-f="bps"]', `${fmtBps(m.rx_bps)} ↓ / ${fmtBps(m.tx_bps)} ↑`);
        if (m.ping) setText(el, '[data-f="ping"]', pingTxt(m.ping));
        const canvas = el.querySelector("canvas");
        const pts = m.points || [];
        drawSpark(canvas, pts.map((p) => p.rx_bps), pts.map((p) => p.tx_bps));
      }
    }
  };

  const loadTunnels = async () => {
    const data = await api("/api/tunnels");
    applyTunnels(data.tunnels || []);
  };

  const loadMetrics = async () => {
    const data = await api("/api/metrics");
    applyMetrics(data);
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
    openModal();
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
          knownKey = "";
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
      knownKey = "";
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
    openModal();
    $("new-form").onsubmit = async (e) => {
      e.preventDefault();
      if (!confirm("تانل جدید ایزوله ایجاد شود؟ v1/v2 دست نمی‌خورند.")) return;
      const body = Object.fromEntries(new FormData(e.target).entries());
      try {
        const res = await api("/api/tunnels", { method: "POST", body: JSON.stringify(body) });
        toast("تانل ساخته شد: " + (res.slot && res.slot.id));
        closeModal();
        knownKey = "";
        await loadTunnels();
      } catch (err) { toast(err.message, true); }
    };
  };

  const logoutView = () => {
    showApp(false);
    clearInterval(statusTimer);
    clearInterval(metricsTimer);
    clearInterval(clockTimer);
    statusTimer = metricsTimer = clockTimer = null;
    knownKey = "";
    tunnels = [];
  };

  const startLoops = async () => {
    showApp(true);
    setClock();
    await loadTunnels();
    try { await loadMetrics(); } catch { /* first paint is enough */ }
    clearInterval(statusTimer);
    clearInterval(metricsTimer);
    clearInterval(clockTimer);
    clockTimer = setInterval(setClock, 1000);
    statusTimer = setInterval(async () => {
      try { await loadTunnels(); } catch (err) { if (err.auth) logoutView(); }
    }, 9000);
    metricsTimer = setInterval(async () => {
      try { await loadMetrics(); } catch (err) { if (err.auth) logoutView(); }
    }, 2500);
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
  $("btn-refresh").onclick = () => {
    knownKey = "";
    loadTunnels().catch((err) => toast(err.message, true));
  };
  $("btn-new").onclick = openWizard;

  closeModal();
  $("toast").classList.remove("is-open");
  $("toast").hidden = true;
  showApp(false);
  api("/api/me").then(startLoops).catch(() => showApp(false));
})();
