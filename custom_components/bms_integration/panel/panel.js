/**
 * BMS Устройства — панель обслуживания установки.
 *
 * Обычный веб-компонент без сборщика и без Lit: панель не должна зависеть от
 * внутреннего устройства фронтенда Home Assistant, иначе его обновление молча
 * гасит панель (так и случилось с попыткой одолжить у него LitElement).
 * Штатные элементы HA (ha-form и прочие) при этом доступны — они
 * зарегистрированы глобально и работают внутри нашего shadow DOM.
 */

const TABS = [
  { id: "devices", label: "Устройства", icon: "🔌" },
  { id: "log", label: "Журнал", icon: "📄" },
  { id: "report", label: "Отчёты", icon: "📊" },
  { id: "rooms", label: "Комнаты", icon: "🏠" },
  { id: "settings", label: "Настройки", icon: "⚙️" },
];

const REFRESH_MS = 5000;

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

function humanAge(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.round(seconds)} с назад`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} мин назад`;
  return `${Math.round(seconds / 3600)} ч назад`;
}

function humanTime(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

class BmsDevicesPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._tab = "devices";
    this._search = "";
    this._overview = null;
    this._log = null;
    this._report = null;
    this._error = null;
    this._busy = new Set();
    this._built = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._refresh();
  }
  get hass() {
    return this._hass;
  }

  connectedCallback() {
    this._build();
    this._timer = setInterval(() => this._refresh(), REFRESH_MS);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  // ---------------------------------------------------------------- данные --
  async _call(type, payload = {}) {
    return this._hass.callWS({ type: `bms_integration/${type}`, ...payload });
  }

  async _refresh() {
    if (!this._hass) return;
    try {
      if (this._tab === "devices" || this._tab === "rooms") {
        this._overview = await this._call("overview");
      } else if (this._tab === "log") {
        this._log = await this._call("logs", { limit: 200 });
      } else if (this._tab === "report") {
        this._report = await this._call("report", { limit: 100 });
      } else if (this._tab === "settings") {
        this._overview = await this._call("overview");
      }
      this._error = null;
    } catch (err) {
      this._error = err?.message || String(err);
    }
    this._renderBody();
  }

  // ---------------------------------------------------------------- каркас --
  _build() {
    if (this._built) return;
    this._built = true;
    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <div class="wrap">
        <header>
          <div class="brand">
            <span class="logo">⚡</span>
            <div>
              <div class="h1">BMS Устройства</div>
              <div class="sub" id="subtitle">Обслуживание установки</div>
            </div>
          </div>
          <div class="actions">
            <input id="search" class="search" type="search" placeholder="Поиск…" />
            <button class="btn" data-act="refresh">Обновить</button>
          </div>
        </header>
        <nav id="tabs"></nav>
        <main id="body"><div class="muted">Загрузка…</div></main>
      </div>
    `;

    const tabs = this.shadowRoot.getElementById("tabs");
    tabs.innerHTML = TABS.map(
      (t) =>
        `<button class="tab" data-tab="${t.id}"><span>${t.icon}</span>${t.label}</button>`
    ).join("");
    this._syncTabs();

    this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
    this.shadowRoot.getElementById("search").addEventListener("input", (ev) => {
      this._search = ev.target.value.toLowerCase();
      this._renderBody();
    });
  }

  _syncTabs() {
    this.shadowRoot.querySelectorAll(".tab").forEach((el) => {
      el.classList.toggle("active", el.dataset.tab === this._tab);
    });
    const search = this.shadowRoot.getElementById("search");
    search.style.display = ["devices", "log", "rooms"].includes(this._tab) ? "" : "none";
  }

  async _onClick(ev) {
    const target = ev.composedPath().find((el) => el.dataset && (el.dataset.tab || el.dataset.act));
    if (!target) return;

    if (target.dataset.tab) {
      this._tab = target.dataset.tab;
      this._syncTabs();
      this.shadowRoot.getElementById("body").innerHTML = `<div class="muted">Загрузка…</div>`;
      await this._refresh();
      return;
    }

    const act = target.dataset.act;
    if (act === "refresh") {
      await this._refresh();
    } else if (act === "reconnect" || act === "refresh-dps") {
      await this._deviceAction(target, act);
    } else if (act === "expand") {
      const row = target.closest(".device");
      row.classList.toggle("open");
    }
  }

  async _deviceAction(button, act) {
    const { deviceId, nodeId } = button.dataset;
    const key = `${deviceId}:${act}`;
    if (this._busy.has(key)) return;
    this._busy.add(key);
    button.disabled = true;
    button.textContent = "…";
    try {
      await this._call("action", {
        device_id: deviceId,
        node_id: nodeId || null,
        action: act === "reconnect" ? "reconnect" : "refresh",
      });
      this._toast("Команда отправлена");
    } catch (err) {
      this._toast(err?.message || "Не получилось", true);
    } finally {
      this._busy.delete(key);
      await this._refresh();
    }
  }

  _toast(text, isError = false) {
    const el = document.createElement("div");
    el.className = `toast${isError ? " error" : ""}`;
    el.textContent = text;
    this.shadowRoot.querySelector(".wrap").appendChild(el);
    setTimeout(() => el.remove(), 2600);
  }

  // ------------------------------------------------------------- рендеринг --
  _renderBody() {
    const body = this.shadowRoot.getElementById("body");
    if (this._error) {
      body.innerHTML = `<div class="card error">Ошибка: ${esc(this._error)}</div>`;
      return;
    }
    const render = {
      devices: () => this._renderDevices(),
      log: () => this._renderLog(),
      report: () => this._renderReport(),
      rooms: () => this._renderRooms(),
      settings: () => this._renderSettings(),
    }[this._tab];
    body.innerHTML = render ? render() : "";
    this._renderSubtitle();
  }

  _renderSubtitle() {
    const el = this.shadowRoot.getElementById("subtitle");
    const s = this._overview?.summary;
    el.textContent = s
      ? `${s.connected} из ${s.total} на связи · ${s.entities} сущностей`
      : "Обслуживание установки";
  }

  _matches(device) {
    if (!this._search) return true;
    return [device.name, device.host, device.device_id, device.area_name]
      .filter(Boolean)
      .some((v) => String(v).toLowerCase().includes(this._search));
  }

  _renderDevices() {
    const data = this._overview;
    if (!data) return `<div class="muted">Загрузка…</div>`;
    const s = data.summary;

    const cards = `
      <div class="stats">
        ${this._stat(s.connected, "на связи", "ok")}
        ${this._stat(s.offline, "не отвечают", s.offline ? "bad" : "")}
        ${this._stat(s.quiet, "молчат > 2 мин", s.quiet ? "warn" : "")}
        ${this._stat(s.gateways, "шлюзов")}
        ${this._stat(s.subdevices, "за шлюзом")}
        ${this._stat(s.entities, "сущностей")}
      </div>`;

    // Шлюз и его дети идут вместе — это главное отношение в такой установке.
    const shown = data.devices.filter((d) => this._matches(d));
    const gateways = shown.filter((d) => !d.is_subdevice);
    const children = shown.filter((d) => d.is_subdevice);
    const byGateway = {};
    children.forEach((c) => {
      (byGateway[c.gateway_id] = byGateway[c.gateway_id] || []).push(c);
    });

    let out = cards;
    for (const gw of gateways) {
      out += this._deviceRow(gw);
      for (const child of byGateway[gw.device_id] || []) out += this._deviceRow(child, true);
      delete byGateway[gw.device_id];
    }
    // Дети, чей шлюз отфильтрован поиском, не должны исчезнуть.
    Object.values(byGateway).forEach((list) => list.forEach((c) => (out += this._deviceRow(c, true))));

    if (!shown.length) out += `<div class="muted">Ничего не найдено</div>`;
    return out;
  }

  _stat(value, label, tone = "") {
    return `<div class="stat ${tone}"><div class="v">${value}</div><div class="l">${label}</div></div>`;
  }

  _deviceRow(d, child = false) {
    const state = d.connected ? "ok" : d.available ? "warn" : "bad";
    const stateText = d.connected
      ? "на связи"
      : d.available
      ? "переподключается"
      : "нет связи";
    const badges = [
      d.is_gateway ? `<span class="badge gw">шлюз · ${d.sub_device_count}</span>` : "",
      d.area_name ? `<span class="badge">${esc(d.area_name)}</span>` : "",
      d.quiet ? `<span class="badge warn">молчит ${humanAge(d.last_update_age)}</span>` : "",
      d.enable_debug ? `<span class="badge">отладка</span>` : "",
    ].join("");

    const entities = d.entities
      .map(
        (e) =>
          `<div class="ent"><span class="ename">${esc(e.name)}</span>
             <code>${esc(e.entity_id)}</code>
             <span class="estate ${e.state === "unavailable" || e.state === "unknown" ? "bad" : ""}">${esc(
            e.state ?? "—"
          )}</span></div>`
      )
      .join("");

    return `
      <div class="device ${child ? "child" : ""}">
        <div class="drow" data-act="expand">
          <span class="dot ${state}" title="${stateText}"></span>
          <div class="dmain">
            <div class="dname">${esc(d.name)} ${badges}</div>
            <div class="dmeta">${esc(d.host)} · ${esc(d.protocol)} · ${d.entity_count} сущн. ·
              последний ответ ${humanAge(d.last_update_age)}</div>
          </div>
          <div class="dactions">
            <button class="btn small" data-act="refresh-dps"
              data-device-id="${esc(d.device_id)}" data-node-id="${esc(d.node_id || "")}">Опросить</button>
            <button class="btn small" data-act="reconnect"
              data-device-id="${esc(d.device_id)}" data-node-id="${esc(d.node_id || "")}">Переподключить</button>
          </div>
        </div>
        <div class="dbody">${entities || `<div class="muted">Нет сущностей</div>`}</div>
      </div>`;
  }

  _renderLog() {
    const data = this._log;
    if (!data) return `<div class="muted">Загрузка…</div>`;
    if (!data.records.length) {
      return `<div class="card">
        <p>Записей пока нет.</p>
        <p class="muted">Журнал собирается в памяти с момента запуска. Если тут пусто —
        скорее всего интеграция пишет только предупреждения; включите подробный
        уровень в «Настройках» этой панели.</p>
      </div>`;
    }
    const rows = data.records
      .filter((r) => !this._search || r.message.toLowerCase().includes(this._search))
      .slice()
      .reverse()
      .map(
        (r) => `<div class="logrow ${r.level.toLowerCase()}">
          <span class="lt">${humanTime(r.time)}</span>
          <span class="ll">${r.level}</span>
          <span class="lm">${esc(r.message)}</span>
        </div>`
      )
      .join("");
    return `<div class="loghead muted">${data.captured} записей в памяти</div><div class="log">${rows}</div>`;
  }

  _renderReport() {
    const data = this._report;
    if (!data) return `<div class="muted">Загрузка…</div>`;
    if (!data.entries.length) {
      return `<div class="card"><p>Разрывов связи не зафиксировано.</p>
        <p class="muted">Здесь появляются отключения и восстановления устройств
        с причиной и длительностью — файл bms_integration_availability.jsonl.</p></div>`;
    }
    const rows = data.entries
      .slice()
      .reverse()
      .map(
        (e) => `<tr>
          <td class="nowrap">${esc(new Date(e.ts).toLocaleString("ru-RU"))}</td>
          <td>${esc(e.name || e.device_id)}</td>
          <td><span class="ev ${esc(e.event)}">${esc(e.event)}</span></td>
          <td>${esc(e.reason || "")}</td>
          <td class="nowrap">${e.disconnect_age_sec != null ? Math.round(e.disconnect_age_sec) + " с" : ""}</td>
        </tr>`
      )
      .join("");
    return `<table class="tbl">
      <thead><tr><th>Время</th><th>Устройство</th><th>Событие</th><th>Причина</th><th>Длительность</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  _renderRooms() {
    return `<div class="card"><p>Раздел в работе — комнаты и привязка устройств.</p></div>`;
  }

  _renderSettings() {
    return `<div class="card"><p>Раздел в работе — настройки интеграции и объекта.</p></div>`;
  }
}

const STYLES = `
  :host { display: block; background: var(--primary-background-color); min-height: 100vh; }
  .wrap { font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
          color: var(--primary-text-color); position: relative; }
  header { display: flex; align-items: center; justify-content: space-between; gap: 16px;
           padding: 14px 20px; background: var(--app-header-background-color, var(--primary-color));
           color: var(--app-header-text-color, #fff); flex-wrap: wrap; }
  .brand { display: flex; align-items: center; gap: 12px; }
  .logo { font-size: 26px; }
  .h1 { font-size: 20px; font-weight: 500; line-height: 1.1; }
  .sub { font-size: 13px; opacity: .85; }
  .actions { display: flex; gap: 8px; align-items: center; }
  .search { padding: 8px 12px; border-radius: 8px; border: none; min-width: 200px;
            background: rgba(255,255,255,.15); color: inherit; }
  .search::placeholder { color: rgba(255,255,255,.7); }
  .btn { padding: 8px 14px; border-radius: 8px; border: none; cursor: pointer;
         background: rgba(255,255,255,.18); color: inherit; font-size: 14px; }
  .btn:hover { background: rgba(255,255,255,.28); }
  .btn.small { padding: 6px 10px; font-size: 13px; background: var(--secondary-background-color);
               color: var(--primary-text-color); }
  .btn:disabled { opacity: .5; cursor: default; }
  nav { display: flex; gap: 4px; padding: 0 16px; background: var(--card-background-color);
        border-bottom: 1px solid var(--divider-color); overflow-x: auto; }
  .tab { background: none; border: none; padding: 14px 16px; cursor: pointer; font-size: 14px;
         color: var(--secondary-text-color); border-bottom: 3px solid transparent; white-space: nowrap; }
  .tab span { margin-right: 6px; }
  .tab.active { color: var(--primary-color); border-bottom-color: var(--primary-color); font-weight: 500; }
  main { padding: 16px; max-width: 1200px; }
  .muted { color: var(--secondary-text-color); }
  .card { background: var(--card-background-color); border-radius: 12px; padding: 16px; }
  .card.error { border-left: 4px solid var(--error-color); }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
           gap: 10px; margin-bottom: 16px; }
  .stat { background: var(--card-background-color); border-radius: 12px; padding: 12px 14px; }
  .stat .v { font-size: 24px; font-weight: 600; }
  .stat .l { font-size: 12px; color: var(--secondary-text-color); }
  .stat.ok .v { color: var(--success-color, #4caf50); }
  .stat.bad .v { color: var(--error-color, #f44336); }
  .stat.warn .v { color: var(--warning-color, #ff9800); }
  .device { background: var(--card-background-color); border-radius: 12px; margin-bottom: 8px; overflow: hidden; }
  .device.child { margin-left: 24px; border-left: 3px solid var(--divider-color); }
  .drow { display: flex; align-items: center; gap: 12px; padding: 12px 14px; cursor: pointer; }
  .drow:hover { background: var(--secondary-background-color); }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex: none; }
  .dot.ok { background: var(--success-color, #4caf50); }
  .dot.warn { background: var(--warning-color, #ff9800); }
  .dot.bad { background: var(--error-color, #f44336); }
  .dmain { flex: 1; min-width: 0; }
  .dname { font-weight: 500; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .dmeta { font-size: 12px; color: var(--secondary-text-color); margin-top: 2px; }
  .dactions { display: flex; gap: 6px; flex: none; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 400;
           background: var(--secondary-background-color); color: var(--secondary-text-color); }
  .badge.gw { background: var(--primary-color); color: #fff; }
  .badge.warn { background: var(--warning-color, #ff9800); color: #000; }
  .dbody { display: none; padding: 0 14px 12px 36px; }
  .device.open .dbody { display: block; }
  .ent { display: flex; gap: 10px; align-items: center; padding: 4px 0; font-size: 13px; flex-wrap: wrap; }
  .ename { min-width: 140px; }
  .ent code { color: var(--secondary-text-color); font-size: 12px; }
  .estate { margin-left: auto; font-weight: 500; }
  .estate.bad { color: var(--error-color); }
  .log { font-family: ui-monospace, monospace; font-size: 12.5px; }
  .loghead { margin-bottom: 8px; }
  .logrow { display: flex; gap: 10px; padding: 5px 8px; border-radius: 6px; align-items: baseline; }
  .logrow:nth-child(odd) { background: var(--card-background-color); }
  .lt { color: var(--secondary-text-color); flex: none; }
  .ll { flex: none; width: 62px; font-weight: 600; }
  .logrow.warning .ll { color: var(--warning-color, #ff9800); }
  .logrow.error .ll { color: var(--error-color, #f44336); }
  .logrow.info .ll { color: var(--info-color, #2196f3); }
  .lm { white-space: pre-wrap; word-break: break-word; }
  .tbl { width: 100%; border-collapse: collapse; background: var(--card-background-color);
         border-radius: 12px; overflow: hidden; font-size: 13px; }
  .tbl th { text-align: left; padding: 10px 12px; background: var(--secondary-background-color);
            font-weight: 500; }
  .tbl td { padding: 9px 12px; border-top: 1px solid var(--divider-color); vertical-align: top; }
  .nowrap { white-space: nowrap; }
  .ev { padding: 2px 8px; border-radius: 999px; background: var(--secondary-background-color); font-size: 12px; }
  .ev.disconnect_detected, .ev.command_failed { background: var(--error-color); color: #fff; }
  .ev.reconnect_succeeded { background: var(--success-color, #4caf50); color: #fff; }
  .toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
           background: var(--primary-color); color: #fff; padding: 10px 18px; border-radius: 8px;
           box-shadow: 0 4px 14px rgba(0,0,0,.3); z-index: 10; }
  .toast.error { background: var(--error-color); }
`;

customElements.define("bms-devices-panel", BmsDevicesPanel);
