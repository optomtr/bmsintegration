/**
 * BMS Control Center — панель обслуживания установки в сайдбаре Home Assistant.
 *
 * Обычный веб-компонент без сборщика и без Lit: панель не должна зависеть от
 * внутреннего устройства фронтенда Home Assistant, иначе его обновление молча
 * гасит панель. Данные приходят только из bms_integration/* WebSocket-команд —
 * фронтенд не читает ни runtime-объекты, ни файлы напрямую.
 */

// ------------------------------------------------------------ параметры --
// Опрос панели. Отчёт и журнал читаются с диска на каждый запрос, поэтому
// берём короткий хвост: панель показывает недавние события, а не архив.
const PANEL_TAG = "bms-devices-panel";
const POLL_MS = 6000;
const REPORT_LIMIT = 200;
const LOG_LIMIT = 200;
const OVERVIEW_REPORT_LIMIT = 40;

// ---------------------------------------------------------------- палитра --
const C = {
  bg: "#F0F3F6", card: "#FFFFFF", line: "#E4E9EE", div: "#EDF0F3",
  ink: "#1B2733", ink2: "#3E4B58", ink3: "#55616D", mut: "#75808B",
  mut2: "#64707C", mut3: "#7A8590", faint: "#A6AFB8",
  accent: "#006FFF", accentHi: "#2F87FF",
  ok: "#149A54", warn: "#B97D00", bad: "#DE2F44",
  stale: "#D96511", grace: "#6E4FD9", cfg: "#C93A8C", blue: "#1E7FE0",
};
const MONO = "ui-monospace,'SF Mono',Menlo,monospace";

// Состояния из брифа: явный текст + цвет + иконка, никогда только цвет.
const ST = {
  online:       { label: "Онлайн",          color: C.ok,    icon: "i-check" },
  connecting:   { label: "Подключение",     color: C.blue,  icon: "i-dots" },
  reconnecting: { label: "Переподключение", color: C.warn,  icon: "i-refresh" },
  grace:        { label: "Grace-период",    color: C.grace, icon: "i-clock" },
  unavailable:  { label: "Недоступно",      color: C.bad,   icon: "i-octagon" },
  stale:        { label: "Шлюз залип",      color: C.stale, icon: "i-alert" },
  config:       { label: "Ошибка конфиг.",  color: C.cfg,   icon: "i-gear" },
};
const KIND = {
  entry:   { icon: "i-shield", label: "Запись конфигурации",   color: C.mut2, size: "13.5px", weight: "600", font: "inherit" },
  gateway: { icon: "i-zigbee", label: "Шлюз Zigbee",           color: "#3E76A8", size: "13px", weight: "600", font: "inherit" },
  direct:  { icon: "i-wifi",   label: "Прямое Wi-Fi устройство", color: "#3E76A8", size: "13px", weight: "600", font: "inherit" },
  device:  { icon: "i-chip",   label: "Подустройство",         color: C.mut2, size: "12.5px", weight: "500", font: "inherit" },
  entity:  { icon: "i-link",   label: "Сущность Home Assistant", color: "#98A2AC", size: "12px", weight: "400", font: MONO },
};

const NAV = [
  { id: "overview", label: "Обзор", icon: "i-grid" },
  { id: "map", label: "Карта связей", icon: "i-network" },
  { id: "device", label: "Устройство", icon: "i-chip" },
  { id: "add", label: "Добавить устройство", icon: "i-plus" },
  { id: "rooms", label: "Комнаты", icon: "i-home" },
  { id: "events", label: "События", icon: "i-list" },
  { id: "settings", label: "Настройки", icon: "i-sliders" },
];

const DEVICE_TABS = [
  { id: "status", label: "Статус" },
  { id: "entities", label: "Сущности" },
  { id: "dps", label: "DPS" },
  { id: "diag", label: "Диагностика" },
  { id: "config", label: "Конфигурация" },
];

const EVENT_META = {
  disconnect_detected: { label: "Разрыв связи", color: C.bad, icon: "i-octagon" },
  reconnect_succeeded: { label: "Связь восстановлена", color: C.ok, icon: "i-check" },
  command_failed: { label: "Команда не прошла", color: C.bad, icon: "i-alert" },
  startup_recovery: { label: "Восстановление при старте", color: C.warn, icon: "i-refresh" },
  gateway_probe_failed: { label: "Шлюз не ответил", color: C.stale, icon: "i-alert" },
  dps_refresh_failed: { label: "Опрос DPS не удался", color: C.warn, icon: "i-refresh" },
};

// ------------------------------------------------------------- утилиты ----
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const soft = (hex, a) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
};

const plural = (n, one, few, many) => {
  const m10 = n % 10, m100 = n % 100;
  const w = m10 === 1 && m100 !== 11 ? one
    : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many;
  return `${n} ${w}`;
};

function age(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.round(seconds)} с`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} мин`;
  return `${Math.round(seconds / 3600)} ч`;
}
const hhmmss = (d) => d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const icon = (id, size = 14, color = "currentColor") =>
  `<svg width="${size}" height="${size}" style="color:${color};flex:0 0 auto"><use href="#${id}"></use></svg>`;

// Для записи конфигурации «Шлюз залип» бессмысленно: у неё нет соединения,
// у неё есть только итог по всем узлам под ней.
const stateLabel = (state, kind) =>
  kind === "entry"
    ? state === "online" ? "Все узлы в норме" : "Есть проблемы"
    : (ST[state] || ST.config).label;

const pill = (state, kind) => {
  const s = ST[state] || ST.config;
  return `<span style="display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 8px;border-radius:5px;
    background:${soft(s.color, 0.12)};color:${s.color};font-size:11.5px;font-weight:600;white-space:nowrap;width:fit-content">
    ${icon(s.icon, 12)}${stateLabel(state, kind)}</span>`;
};

const cardOpen = (title, right = "") => `
  <div style="background:${C.card};border:1px solid ${C.line};border-radius:10px;box-shadow:0 1px 2px rgba(24,39,58,.05)">
    <div style="padding:13px 16px;border-bottom:1px solid ${C.line};display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
      <span style="font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:${C.mut2}">${title}</span>
      ${right}
    </div>`;
const cardClose = `</div>`;

const btn = (label, opts = {}) => {
  const { act = "", data = "", primary = false, danger = false, small = false, ico = "" } = opts;
  const bg = primary ? C.accent : danger ? soft(C.bad, 0.09) : "#F4F6F8";
  const fg = primary ? "#FFFFFF" : danger ? C.bad : C.ink2;
  const bd = primary ? C.accent : danger ? soft(C.bad, 0.4) : "#D0D8DF";
  const h = small ? 26 : 32;
  return `<button data-act="${act}" ${data} style="display:inline-flex;align-items:center;gap:7px;height:${h}px;
    padding:0 ${small ? 10 : 12}px;border-radius:7px;border:1px solid ${bd};background:${bg};color:${fg};
    font-size:${small ? 11.5 : 12.5}px;font-weight:${primary ? 600 : 500};cursor:pointer;white-space:nowrap">
    ${ico ? icon(ico, small ? 12 : 14) : ""}${label}</button>`;
};

const kv = (rows) => `
  <div style="display:flex;flex-direction:column;border:1px solid ${C.line};border-radius:8px;overflow:hidden">
    ${rows.map(([k, v]) => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 11px;border-bottom:1px solid ${C.div};background:#F7F9FB">
        <span style="font-size:11px;color:${C.mut3};flex:0 0 130px">${esc(k)}</span>
        <span style="font-size:11.5px;color:${C.ink2};font-family:${MONO};flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(v)}</span>
      </div>`).join("")}
  </div>`;

const empty = (text, ico = "i-check", color = C.ok) => `
  <div style="padding:30px 16px;display:flex;align-items:center;justify-content:center;gap:10px">
    ${icon(ico, 15, color)}<span style="font-size:12.5px;color:${C.mut2}">${esc(text)}</span></div>`;

// ============================================================== компонент ==
class BmsControlCenter extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._built = false;
    this.s = {
      screen: "overview",
      deviceTab: "status",
      deviceId: null,
      selected: null,
      expert: false,
      incidentsOnly: false,
      mapMode: "tree",
      addDevice: null,       // у какого найденного устройства раскрыта форма
      replaceTarget: null,   // какое устройство заменяем
      replaceWith: null,     // на какое
      search: "",
      eventSearch: "",
      entryFilter: "all",
      collapsed: new Set(),
      updatedAt: null,
      busy: false,
    };
    this.d = { overview: null, topology: null, device: null, logs: null, report: null,
               discovered: null, areas: null, settings: null, replace: null };
    this._err = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    // Home Assistant присваивает свойства панели в цикле. Исключение отсюда
    // обрывает этот цикл, и остальные свойства элемент уже не получает.
    if (first) Promise.resolve().then(() => this.refresh()).catch((err) =>
      console.error("BMS: первое обновление не удалось", err));
  }
  get hass() { return this._hass; }

  connectedCallback() {
    this.build();
    // Home Assistant присваивает el.hass, как только создаст элемент. Если
    // модуль панели к этому моменту ещё не загрузился, элемент не апгрейднут,
    // и присвоение оседает СОБСТВЕННЫМ полем экземпляра - поверх сеттера
    // прототипа. Сеттер не сработает уже никогда: панель навсегда остаётся на
    // «Загрузка…», потому что refresh() выходит на проверке this._hass.
    // Гонка тем вероятнее, чем крупнее файл панели, поэтому проявилась она
    // не сразу. Забираем такие поля обратно на сеттер.
    this.reclaimProperty("hass");
    this._timer = setInterval(() => this.tick(), POLL_MS);
    // A wall panel left on this screen for weeks must not keep polling while
    // nobody is looking at it, and must be current the moment it is.
    this._onVisibility = () => {
      if (document.visibilityState === "visible") this.refresh(true);
    };
    document.addEventListener("visibilitychange", this._onVisibility);
  }
  disconnectedCallback() {
    clearInterval(this._timer);
    document.removeEventListener("visibilitychange", this._onVisibility);
  }

  reclaimProperty(name) {
    if (!Object.prototype.hasOwnProperty.call(this, name)) return;
    const value = this[name];
    delete this[name];
    this[name] = value;   // теперь попадёт в сеттер прототипа
  }

  tick() {
    if (document.visibilityState === "hidden") return;
    if (this._inflight) return;
    if (this._retryAt && Date.now() < this._retryAt) return;
    this.refresh(true);
  }

  // ------------------------------------------------------------- данные --
  ws(type, payload = {}) {
    return this._hass.callWS({ type: `bms_integration/${type}`, ...payload });
  }

  async refresh(silent = false) {
    if (!this._hass) return;
    // One request set at a time: a slow backend used to stack a new set on
    // top of the previous one every 6 seconds.
    if (this._inflight) return;
    this._inflight = true;
    try {
      await this._refresh(silent);
    } finally {
      // Снимать флаг только внутри try было ошибкой: исключение до входа в
      // него оставляло панель «занятой» навсегда, и каждый следующий тик
      // выходил впустую - вечная «Загрузка…».
      this._inflight = false;
    }
  }

  async _refresh(silent) {
    if (!silent) this.s.busy = true, this.paintHeader();
    // Snapshot what was asked for. The operator can navigate or open another
    // device while these are in flight, and a late answer must not be written
    // over the screen that replaced it - the device card in particular, whose
    // buttons would then act on a device the user is no longer looking at.
    const sc = this.s.screen;
    const devId = this.s.deviceId;
    const onScreen = () => this.s.screen === sc;
    try {
      const jobs = [this.ws("overview").then((r) => (this.d.overview = r))];
      if (sc === "map")
        jobs.push(this.ws("topology").then((r) => {
          if (!onScreen()) return;
          this.d.topology = r;
          // Отбрасываем свёрнутые узлы, которых больше нет: за недели работы
          // вкладки набор копил идентификаторы удалённых устройств.
          if (this.s.collapsed.size) {
            const alive = new Set();
            const walk = (n) => { alive.add(n.id); (n.children || []).forEach(walk); };
            (r.tree || []).forEach(walk);
            this.s.collapsed = new Set([...this.s.collapsed].filter((id) => alive.has(id)));
          }
        }));
      if (sc === "device" && devId)
        jobs.push(this.ws("device", { device_id: devId })
          .then((r) => { if (this.s.deviceId === devId) this.d.device = r; })
          .catch(() => { if (this.s.deviceId === devId) this.d.device = null; }));
      if (sc === "add") {
        jobs.push(this.ws("discovered").then((r) => { if (onScreen()) this.d.discovered = r; }));
        jobs.push(this.ws("replace_preview").then((r) => { if (onScreen()) this.d.replace = r; }));
      }
      if (sc === "rooms")
        jobs.push(this.ws("areas").then((r) => { if (onScreen()) this.d.areas = r; }));
      // Настройки тянем всегда: по ним рисуется значок изолированного режима
      // в шапке, а он должен быть виден на любом экране.
      jobs.push(this.ws("settings").then((r) => (this.d.settings = r)));
      if (sc === "device" && devId)
        jobs.push(this.ws("replace_preview", { device_id: devId })
          .then((r) => { if (this.s.deviceId === devId) this.d.replace = r; }));
      if (sc === "events") {
        jobs.push(this.ws("report", { limit: REPORT_LIMIT }).then((r) => { if (onScreen()) this.d.report = r; }));
        jobs.push(this.ws("logs", { limit: LOG_LIMIT }).then((r) => { if (onScreen()) this.d.logs = r; }));
      }
      // Лента последних событий на «Обзоре» берётся из того же отчёта. Без
      // этого запроса она читала несуществующее поле overview.recent и
      // оставалась пустой всю сессию, пока не открыть «События».
      if (sc === "overview")
        jobs.push(this.ws("report", { limit: OVERVIEW_REPORT_LIMIT }).then((r) => { if (onScreen()) this.d.report = r; }));
      await Promise.all(jobs);
      this.s.updatedAt = new Date();
      this._err = null;
      this._fails = 0;
      this._retryAt = 0;
    } catch (e) {
      this._err = e?.message || String(e);
      // Back off rather than re-asking a failing backend every 6 seconds.
      this._fails = (this._fails || 0) + 1;
      this._retryAt = Date.now() + Math.min(60000, POLL_MS * 2 ** Math.min(this._fails, 4));
    }
    this.s.busy = false;
    this.paint(silent);
  }

  go(screen, extra = {}) {
    Object.assign(this.s, { screen }, extra);
    this.paint();
    this.refresh(true);
  }

  toast(text, bad = false) {
    const el = document.createElement("div");
    el.textContent = text;
    el.style.cssText = `position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:20;
      background:${bad ? C.bad : C.ink};color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;
      box-shadow:0 6px 20px rgba(24,39,58,.28)`;
    this.shadowRoot.appendChild(el);
    setTimeout(() => el.remove(), 3000);
  }

  // ------------------------------------------------------------- каркас --
  build() {
    if (this._built) return;
    this._built = true;
    this.shadowRoot.innerHTML = `
      ${SPRITE}
      <style>${CSS}</style>
      <div class="app">
        <header>
          <div class="brand">
            <img src="/bms_integration_panel/assets/bms-logo.png" alt="BMS" class="logo">
            <span class="vr"></span>
            <div>
              <div class="t1">Control Center</div>
              <div class="t2">BMS CONNECTION SYSTEM</div>
            </div>
          </div>
          <div class="hactions" id="hactions"></div>
        </header>
        <nav id="nav"></nav>
        <main id="main"></main>
      </div>`;
    this.shadowRoot.addEventListener("click", (e) => this.onClick(e));
    this.shadowRoot.addEventListener("input", (e) => this.onInput(e));
    this.shadowRoot.addEventListener("change", (e) => this.onInput(e));
    this.paint();
  }

  lockdownBadge() {
    /* Кнопка, а не значок: включить и выключить изолированный режим можно с
       любого экрана одним нажатием. Красная, когда режим действует, - инженер
       на объекте сразу видит, почему не обновляются ключи. Пока настройки не
       загрузились, кнопка не рисуется: её состояние было бы враньём. */
    const st = this.d.settings;
    if (!st) return "";
    const locked = (st.entries || []).some((e) => e.lockdown);
    return `<button data-act="lockdown-toggle" data-on="${locked ? "1" : ""}"
      title="${locked
        ? "Обмен с облаком Tuya запрещён. Нажмите, чтобы снять."
        : "Одним нажатием полностью запретить интеграции обмен с облаком Tuya."}"
      style="display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 10px;
      border-radius:6px;cursor:pointer;font-size:11.5px;font-weight:600;white-space:nowrap;
      border:1px solid ${locked ? C.bad : "#D0D8DF"};
      background:${locked ? soft(C.bad, 0.1) : "#F4F6F8"};
      color:${locked ? C.bad : C.ink2}">
      ${icon(locked ? "i-alert" : "i-check", 12)}
      ${locked ? "Изолированный режим" : "Lockdown"}</button>`;
  }

  paintHeader() {
    if (!this._built) return;
    const s = this.s;
    const upd = s.updatedAt ? hhmmss(s.updatedAt) : "—";
    const ex = s.expert;
    this.shadowRoot.getElementById("hactions").innerHTML = `
      ${this.lockdownBadge()}
      <div class="chip-sel">
        ${icon("i-home", 14, C.mut)}
        <select data-act="entry">
          <option value="all">Все записи</option>
          ${(this.d.overview?.entries || []).map((e) =>
            `<option value="${esc(e.entry_id)}" ${s.entryFilter === e.entry_id ? "selected" : ""}>${esc(e.title)}</option>`).join("")}
        </select>
      </div>
      <div class="upd"><span class="dot-live"></span><span>Обновлено ${upd}</span></div>
      ${btn("Обновить", { act: "refresh", ico: "i-refresh" })}
      <button data-act="expert" title="Экспертный режим: raw-DPS и широкие операции"
        style="display:inline-flex;align-items:center;gap:8px;height:32px;padding:0 11px;border-radius:7px;
        border:1px solid ${ex ? C.stale : "#D0D8DF"};background:${ex ? soft(C.stale, 0.1) : "#F4F6F8"};
        color:${ex ? C.stale : C.ink3};font-size:12px;font-weight:600;letter-spacing:.02em;cursor:pointer">
        <span style="width:26px;height:14px;border-radius:8px;background:${ex ? C.stale : "#C6CFD8"};position:relative;flex:0 0 auto">
          <span style="position:absolute;top:2px;left:${ex ? "14px" : "2px"};width:10px;height:10px;border-radius:50%;background:#fff;transition:left .18s"></span>
        </span>EXPERT</button>`;
  }

  paintNav() {
    if (!this._built) return;
    const sum = this.d.overview?.summary;
    const problems = sum ? (sum.total || 0) - (sum.online || 0) : 0;
    this.shadowRoot.getElementById("nav").innerHTML = NAV.map((n) => {
      const on = this.s.screen === n.id;
      const badge = n.id === "events" && problems > 0
        ? `<span class="badge-n">${problems}</span>` : "";
      return `<button data-act="nav" data-nav="${n.id}" class="tab ${on ? "on" : ""}">
        ${icon(n.icon, 15)}${n.label}${badge}</button>`;
    }).join("");
  }

  paint(silent = false) {
    if (!this._built) return;
    // Шапка и навигация рисуются до основного блока, и раньше их исключение
    // выбрасывало из paint() целиком: главная область оставалась на прежнем
    // кадре - для оператора это неотличимо от вечной «Загрузка…». Пусть
    // неполные данные ломают одну полоску интерфейса, а не всю панель.
    try {
      this.paintHeader();
      this.paintNav();
    } catch (err) {
      console.error("BMS: не удалось отрисовать шапку", err);
    }
    const main = this.shadowRoot.getElementById("main");
    // A background refresh must never throw away what the operator is doing:
    // repainting main replaces every node, which drops focus, the caret and a
    // half-typed value. Header and navigation above are cheap and stay live.
    if (silent && this.holdsLiveInput()) return;
    const scroll = main.scrollTop;
    const outerScroll = main.parentElement ? main.parentElement.scrollTop : 0;
    if (this._err) {
      main.innerHTML = `<div class="pad"><div class="card err">${icon("i-alert", 15, C.bad)} ${esc(this._err)}</div></div>`;
      return;
    }
    const view = {
      overview: () => this.vOverview(), map: () => this.vMap(), device: () => this.vDevice(),
      add: () => this.vAdd(), events: () => this.vEvents(), settings: () => this.vSettings(),
      rooms: () => this.vRooms(),
    }[this.s.screen];
    try {
      main.innerHTML = view ? view() : "";
    } catch (err) {
      // paint() runs outside refresh()'s try/catch. A view that trips over an
      // unexpected shape from the backend used to leave the panel frozen on
      // its last frame with nothing on screen to say why.
      main.innerHTML = `<div class="pad"><div class="card err">${icon("i-alert", 15, C.bad)}
        Ошибка отображения: ${esc(err?.message || String(err))}</div></div>`;
      return;
    }
    if (silent) {
      main.scrollTop = scroll;
      if (main.parentElement) main.parentElement.scrollTop = outerScroll;
    }
  }

  holdsLiveInput() {
    const active = this.shadowRoot.activeElement;
    return !!active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName);
  }

  // ----------------------------------------------------- фильтр по записи --
  // Данные для ДЕЙСТВИЯ берём из полного списка, а не из rows(): rows()
  // отфильтрован выбранной записью в шапке, и команда саб-устройству уходила
  // с node_id = null — то есть в шлюз, а не в нужный узел.
  deviceRow(deviceId) {
    const open = this.d.device?.device;
    if (open && open.device_id === deviceId) return open;
    return (this.d.overview?.devices || []).find((r) => r.device_id === deviceId) || null;
  }

  rows() {
    const all = this.d.overview?.devices || [];
    return this.s.entryFilter === "all" ? all : all.filter((r) => r.entry_id === this.s.entryFilter);
  }

  sum() {
    const rows = this.rows();
    if (this.s.entryFilter === "all" && this.d.overview) return this.d.overview.summary;
    const by = {};
    rows.forEach((r) => (by[r.state] = (by[r.state] || 0) + 1));
    const badGw = rows.filter((r) => r.is_gateway && r.state !== "online");
    return {
      total: rows.length, online: by.online || 0, connecting: by.connecting || 0,
      reconnecting: by.reconnecting || 0, grace: by.grace || 0,
      unavailable: by.unavailable || 0, config: by.config || 0,
      bad_gateways: badGw.length, bad_gateway_children: badGw.reduce((a, g) => a + g.sub_device_count, 0),
      gateways: rows.filter((r) => r.is_gateway).length,
      subdevices: rows.filter((r) => r.is_subdevice).length,
      quiet: rows.filter((r) => r.quiet).length,
      entities: rows.reduce((a, r) => a + r.entity_count, 0),
    };
  }

  // ================================================== 01 Обзор ============
  vOverview() {
    const s = this.sum(), rows = this.rows();
    if (!this.d.overview) return `<div class="pad muted">Загрузка…</div>`;

    const tiles = [
      { label: "Всего устройств", value: s.total, icon: "i-chip", color: "#98A2AC", vColor: C.ink },
      { label: "Онлайн", value: s.online, icon: "i-check", color: C.ok, vColor: C.ok },
      // Без этой плитки устройства в состоянии «Подключение» не попадали
      // никуда: на объекте было «Всего 72» при нулях во всех остальных
      // счётчиках, и обзор выглядел сломанным.
      { label: "Подключение", value: s.connecting, icon: "i-link", color: C.blue, note: "идёт связь", vColor: s.connecting ? C.blue : C.ink },
      { label: "Переподключение", value: s.reconnecting, icon: "i-refresh", color: C.warn, note: "ретраи", vColor: s.reconnecting ? C.warn : C.ink },
      { label: "Grace-период", value: s.grace, icon: "i-clock", color: C.grace, note: "ожидание", vColor: s.grace ? C.grace : C.ink },
      { label: "Недоступно", value: s.unavailable, icon: "i-octagon", color: C.bad, note: "подтв. сбой", vColor: s.unavailable ? C.bad : C.ink },
      { label: "Шлюзы с проблемой", value: s.bad_gateways, icon: "i-alert", color: C.stale, note: s.bad_gateway_children ? `${s.bad_gateway_children} детей` : "", vColor: s.bad_gateways ? C.stale : C.ink },
    ].map((t) => `
      <div class="tile" style="--tc:${t.color}">
        <div class="tl">${icon(t.icon, 14, t.color)}<span>${t.label}</span></div>
        <div class="tv"><span style="color:${t.vColor}">${t.value}</span><span class="tn">${t.note || ""}</span></div>
      </div>`).join("");

    const order = ["online", "connecting", "reconnecting", "grace", "unavailable", "config"];
    const health = order.map((k) => ({ k, n: s[k] || 0, c: ST[k].color, l: ST[k].label }));
    const bar = health.filter((h) => h.n).map((h) =>
      `<span style="width:${(h.n / Math.max(1, s.total)) * 100}%;background:${h.c}"></span>`).join("");
    const legend = health.map((h) => `
      <button data-act="filter-state" data-state="${h.k}" class="hrow">
        <span class="hd" style="background:${h.c}"></span>
        <span class="hl">${h.l}</span><span class="hn">${h.n}</span></button>`).join("");

    const gateways = rows.filter((r) => r.is_gateway);
    const gwList = gateways.length ? gateways.map((g) => {
      const st = ST[g.state] || ST.config;
      const kids = rows.filter((r) => r.gateway_id === g.device_id);
      const affected = kids.filter((k) => k.state !== "online").length;
      return `<button class="gwrow" data-act="open-device" data-id="${esc(g.device_id)}">
        <span class="gwico" style="background:${soft(st.color, 0.12)};color:${st.color}">${icon("i-zigbee", 17)}</span>
        <span class="gwmain">
          <span class="gwtop"><span class="gwname">${esc(g.name)}</span>${pill(g.state)}</span>
          <span class="gwmeta">${esc(g.host)} · ${plural(kids.length, "саб-устройство", "саб-устройства", "саб-устройств")}</span>
        </span>
        <span class="gwaff"><span style="color:${affected ? C.bad : C.ok}">${affected}</span><span>затронуто</span></span>
      </button>`;
    }).join("") : empty("Шлюзов нет — все устройства подключаются напрямую", "i-wifi", C.mut);

    const incidents = (this.d.report?.entries || []).slice(-8).reverse();
    const feed = incidents.length ? incidents.map((e) => {
      const m = EVENT_META[e.event] || { label: e.event, color: C.mut, icon: "i-dots" };
      return `<div class="inc">
        <span class="inct">${esc(new Date(e.ts).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }))}</span>
        <span class="incico" style="background:${soft(m.color, 0.12)};color:${m.color}">${icon(m.icon, 12)}</span>
        <span class="incb"><span class="inct1">${esc(m.label)}</span>
        <span class="inct2">${esc(e.name || e.device_id)}${e.reason ? " · " + esc(e.reason) : ""}</span></span>
      </div>`;
    }).join("") : empty("Инцидентов не зафиксировано");

    const attention = rows.filter((r) => r.state !== "online");
    const attRows = attention.length ? attention.map((r) => `
      <div class="arow">
        <span class="acell">${icon(r.is_subdevice ? "i-zigbee" : "i-wifi", 14, C.mut)}
          <span class="aname">${esc(r.name)}</span></span>
        <span class="amono">${esc(r.area_name || (r.gateway_id ? "через шлюз" : "напрямую"))}</span>
        <span>${pill(r.state)}</span>
        <span class="areason">${esc(r.last_error || "—")}</span>
        <span class="amono">${age(r.last_update_age)}</span>
        ${btn("Открыть", { act: "open-device", data: `data-id="${esc(r.device_id)}"`, small: true })}
      </div>`).join("") : empty(`Все ${s.total} устройств на связи`);

    return `<div class="pad col16" style="max-width:1560px">
      <div class="tiles">${tiles}</div>
      <div class="row16">
        <div class="col16" style="flex:1 1 480px;min-width:320px">
          ${cardOpen("Здоровье соединений", `<span class="mono small">${esc(this.scopeName())}</span>`)}
            <div style="padding:16px">
              <div class="hbar">${bar}</div>
              <div class="hgrid">${legend}</div>
            </div>
          ${cardClose}
          ${cardOpen(`Шлюзы · ${esc(this.scopeName())}`, btn("На карте", { act: "nav", data: 'data-nav="map"', small: true, ico: "i-chev-r" }))}
            <div>${gwList}</div>
          ${cardClose}
        </div>
        <div style="flex:1 1 380px;min-width:300px;display:flex;flex-direction:column;background:${C.card};border:1px solid ${C.line};border-radius:10px;box-shadow:0 1px 2px rgba(24,39,58,.05)">
          <div style="padding:13px 16px;border-bottom:1px solid ${C.line};display:flex;align-items:center;justify-content:space-between;gap:12px">
            <span class="cap">Лента инцидентов</span>
            ${btn("Все события", { act: "nav", data: 'data-nav="events"', small: true, ico: "i-chev-r" })}
          </div>
          <div style="flex:1;padding:4px 0">${feed}</div>
          <div style="padding:11px 16px;border-top:1px solid ${C.line};display:flex;gap:8px;flex-wrap:wrap">
            ${btn("Обновить данные", { act: "refresh", primary: true, ico: "i-refresh" })}
            ${btn("Перезагрузить интеграцию", { act: "reload-all", danger: true, ico: "i-alert" })}
          </div>
        </div>
      </div>
      ${cardOpen("Требуют внимания",
        `${attention.length ? `<span class="badge-w">${attention.length}</span>` : ""}
         <span class="small mut">${esc(attention.length ? "нажмите «Открыть» для карточки устройства" : "все узлы в норме")}</span>`)}
        <div style="overflow-x:auto"><div style="min-width:880px">
          <div class="ahead"><span>Устройство</span><span>Зона / связь</span><span>Состояние</span><span>Причина</span><span>Ответ</span><span></span></div>
          ${attRows}
        </div></div>
      ${cardClose}
    </div>`;
  }

  scopeName() {
    if (this.s.entryFilter === "all") return "все записи";
    const e = (this.d.overview?.entries || []).find((x) => x.entry_id === this.s.entryFilter);
    return e ? e.title : "запись";
  }

  // ================================================== 02 Карта связей =====
  vMap() {
    if (!this.d.topology) return `<div class="pad muted">Загрузка карты…</div>`;
    const s = this.sum();
    const chips = ["online", "connecting", "reconnecting", "grace", "unavailable", "config"].map((k) => {
      const on = this.s.mapState === k;
      return `<button data-act="chip" data-state="${k}" class="chip" style="border-color:${on ? ST[k].color : "#DDE3E9"};
        background:${on ? soft(ST[k].color, 0.1) : C.card};color:${on ? ST[k].color : C.ink3};font-weight:${on ? 600 : 500}">
        <span style="width:7px;height:7px;border-radius:50%;background:${ST[k].color}"></span>${ST[k].label}
        <span style="font-family:${MONO};opacity:.7">${s[k] || 0}</span></button>`;
    }).join("");

    const flat = [];
    // Пока действует фильтр, обходим всё дерево: иначе поиск не находит
    // устройство, спрятанное под свёрнутым узлом («Свернуть всё» сворачивает
    // и корень), и панель отвечает «Ничего не найдено» про живое устройство.
    const filtering = !!(this.s.search || this.s.mapState || this.s.incidentsOnly);
    const walk2 = (node, depth) => {
      const kids = node.children || [];
      const collapsed = !filtering && this.s.collapsed.has(node.id);
      flat.push({ node, depth });
      if (!collapsed) kids.forEach((k) => walk2(k, depth + 1));
    };
    this.d.topology.tree.forEach((t) => walk2(t, 0));

    const visible = flat.filter(({ node }) => {
      const q = this.s.search;
      const match = !q || (node.name + " " + (node.meta || "") + " " + (node.node || "")).toLowerCase().includes(q);
      const stateOk = !this.s.mapState || node.state === this.s.mapState;
      const incOk = !this.s.incidentsOnly || node.state !== "online";
      return match && stateOk && incOk;
    });

    const tree = visible.map(({ node, depth }) => {
      const k = KIND[node.kind] || KIND.device;
      const kids = node.children || [];
      const collapsed = !filtering && this.s.collapsed.has(node.id);
      const sel = this.s.selected === node.id;
      return `<div class="trow ${sel ? "sel" : ""}" data-act="select" data-node="${esc(node.id)}" data-device="${esc(node.device_id || "")}">
        <span class="tmark" style="background:${sel ? C.accent : "transparent"}"></span>
        <span class="tname">
          <span style="width:${depth * 18}px;flex:0 0 auto"></span>
          ${kids.length
            ? `<button class="tchev" data-act="toggle" data-node="${esc(node.id)}">${icon(collapsed ? "i-chev-r" : "i-chev-d", 13)}</button>`
            : `<span class="tdot"></span>`}
          ${icon(k.icon, 15, k.color)}
          <span class="tlabel">
            <span style="font-size:${k.size};font-weight:${k.weight};color:${C.ink};font-family:${k.font};overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(node.name)}</span>
            <span class="tmeta">${esc(node.meta || "")}</span>
          </span>
        </span>
        <span style="width:172px;flex:0 0 auto">${pill(node.state, node.kind)}</span>
        <span class="tval">${esc(node.value || "")}</span>
        <span class="tnode">${esc(node.node || "")}</span>
      </div>`;
    }).join("");

    return `<div class="pad col14" style="max-width:1720px">
      <div class="mtools">
        <div class="search"><span>${icon("i-search", 14, C.mut)}</span>
          <input data-act="search" value="${esc(this.s.search)}" placeholder="Поиск устройства, сущности или node ID">
        </div>
        <div class="chips">${icon("i-filter", 14, C.mut)}${chips}</div>
        <span style="flex:1"></span>
        <button data-act="incidents" class="chip" style="border-color:${this.s.incidentsOnly ? C.stale : "#DDE3E9"};
          background:${this.s.incidentsOnly ? soft(C.stale, 0.1) : C.card};color:${this.s.incidentsOnly ? C.stale : C.ink3}">
          ${icon("i-alert", 13)}Только инциденты</button>
        ${btn("Развернуть всё", { act: "expand-all", small: true })}
        ${btn("Свернуть всё", { act: "collapse-all", small: true })}
      </div>
      <div class="row14">
        <div style="flex:1 1 620px;min-width:320px;background:${C.card};border:1px solid ${C.line};border-radius:10px;box-shadow:0 1px 2px rgba(24,39,58,.05);overflow:hidden">
          <div style="overflow-x:auto"><div style="min-width:760px">
            <div class="thead"><span style="flex:1">Узел цепочки</span><span style="width:172px">Состояние</span>
              <span style="width:126px">Значение</span><span style="width:104px">Node ID</span></div>
            ${tree || empty("Ничего не найдено", "i-search", C.mut)}
          </div></div>
          <div class="tfoot"><span class="small mut">Показано ${visible.length} узлов${this.s.search ? " по фильтру" : ""}</span></div>
        </div>
        ${this.selectedAside()}
      </div>
    </div>`;
  }

  selectedAside() {
    const flat = [];
    const walk = (n) => { flat.push(n); (n.children || []).forEach(walk); };
    (this.d.topology?.tree || []).forEach(walk);
    const node = flat.find((n) => n.id === this.s.selected) || flat[0];
    if (!node) return "";
    const k = KIND[node.kind] || KIND.device;
    const st = ST[node.state] || ST.config;
    const row = (this.rows() || []).find((r) => r.device_id === node.device_id);

    const fields = [["Тип", k.label], ["Идентификатор", node.id]];
    if (node.node) fields.push(["Node ID", node.node]);
    if (row) {
      fields.push(["Адрес", row.host], ["Протокол", row.protocol]);
      fields.push(["Последний ответ", age(row.last_update_age)]);
      if (row.retries) fields.push(["Попыток подряд", row.retries]);
      if (row.last_error) fields.push(["Последняя ошибка", row.last_error]);
      fields.push(["Сущностей", row.entity_count]);
    }

    const warn = row && row.state !== "online" && row.is_gateway
      ? `Шлюз недоступен — это объясняет состояние ${plural(row.sub_device_count, "дочернего устройства", "дочерних устройств", "дочерних устройств")}.`
      : row && row.quiet ? `Устройство не отвечало ${age(row.last_update_age)} — возможно, шлюз перестал его перечислять.` : "";

    return `<aside style="flex:0 1 330px;min-width:280px;background:${C.card};border:1px solid ${C.line};border-radius:10px;box-shadow:0 1px 2px rgba(24,39,58,.05)">
      <div style="padding:13px 15px;border-bottom:1px solid ${C.line};display:flex;align-items:center;justify-content:space-between">
        <span class="cap">Выбранный узел</span>${icon("i-eye", 14, C.faint)}
      </div>
      <div style="padding:15px;display:flex;flex-direction:column;gap:13px">
        <div style="display:flex;gap:11px;align-items:flex-start">
          <span style="width:34px;height:34px;border-radius:9px;background:${soft(st.color, 0.12)};color:${st.color};display:flex;align-items:center;justify-content:center;flex:0 0 auto">${icon(k.icon, 18)}</span>
          <span style="min-width:0"><span style="display:block;font-size:14.5px;font-weight:600">${esc(node.name)}</span>
            <span style="display:block;font-size:11.5px;color:${C.mut3};margin-top:2px">${k.label}</span></span>
        </div>
        ${pill(node.state, node.kind)}
        ${kv(fields)}
        ${warn ? `<div class="warnbox">${icon("i-alert", 14, C.stale)}<span>${esc(warn)}</span></div>` : ""}
        ${node.device_id ? `<div style="display:flex;flex-direction:column;gap:7px">
          ${btn("Открыть карточку устройства", { act: "open-device", data: `data-id="${esc(node.device_id)}"`, primary: true, ico: "i-chip" })}
          <div style="display:flex;gap:7px">
            ${btn("Обновить DPS", { act: "dev-refresh", data: `data-id="${esc(node.device_id)}"`, ico: "i-refresh" })}
            ${btn("Переподключить", { act: "dev-reconnect", data: `data-id="${esc(node.device_id)}"`, ico: "i-link" })}
          </div></div>` : ""}
      </div></aside>`;
  }

  // ================================================== 03 Карточка =========
  vDevice() {
    if (!this.s.deviceId) {
      const rows = this.rows();
      return `<div class="pad col14" style="max-width:1100px">
        ${cardOpen("Выберите устройство")}
        <div>${rows.map((r) => `<button class="gwrow" data-act="open-device" data-id="${esc(r.device_id)}">
          <span class="gwico" style="background:${soft((ST[r.state] || ST.config).color, .12)};color:${(ST[r.state] || ST.config).color}">
            ${icon(r.is_subdevice ? "i-zigbee" : "i-wifi", 17)}</span>
          <span class="gwmain"><span class="gwtop"><span class="gwname">${esc(r.name)}</span>${pill(r.state)}</span>
          <span class="gwmeta">${esc(r.host)} · ${r.entity_count} сущн.</span></span></button>`).join("")}</div>
        ${cardClose}</div>`;
    }
    const d = this.d.device;
    if (!d) return `<div class="pad muted">Загрузка устройства…</div>`;
    const r = d.device, st = ST[r.state] || ST.config;

    const tabs = DEVICE_TABS.map((t) => {
      const n = t.id === "entities" ? r.entity_count : t.id === "dps" ? d.dps.length : null;
      const on = this.s.deviceTab === t.id;
      return `<button data-act="dtab" data-tab="${t.id}" class="tab ${on ? "on" : ""}">${t.label}
        ${n !== null ? `<span class="cnt">${n}</span>` : ""}</button>`;
    }).join("");

    const body = {
      status: () => this.dvStatus(d), entities: () => this.dvEntities(d),
      dps: () => this.dvDps(d), diag: () => this.dvDiag(d), config: () => this.dvConfig(d),
    }[this.s.deviceTab]();

    return `<div style="display:flex;flex-direction:column">
      <div style="padding:16px 20px 0;max-width:1560px">
        <div class="crumbs">${icon("i-network", 12, C.mut3)}
          <button data-act="nav" data-nav="map" class="lnk">Карта связей</button>${icon("i-chev-r", 11, C.faint)}
          <span>${esc(r.name)}</span></div>
        <div class="dhead">
          <span class="dico" style="background:${soft(st.color, .12)};color:${st.color}">${icon(r.is_subdevice ? "i-zigbee" : "i-wifi", 22)}</span>
          <div style="min-width:0;flex:1">
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
              <span style="font-size:19px;font-weight:600;letter-spacing:-.01em">${esc(r.name)}</span>${pill(r.state)}
            </div>
            <div class="mono small mut" style="margin-top:3px">${esc(r.host)} · ${esc(r.protocol)}${r.node_id ? " · node " + esc(r.node_id) : ""} · ${esc(r.model || "Tuya")}</div>
          </div>
          <div style="display:flex;gap:7px;flex-wrap:wrap">
            ${btn("Обновить DPS", { act: "dev-refresh", data: `data-id="${esc(r.device_id)}"`, ico: "i-refresh" })}
            ${btn("Тест статуса", { act: "dev-test", data: `data-id="${esc(r.device_id)}"`, ico: "i-play" })}
            ${btn("Переподключить", { act: "dev-reconnect", data: `data-id="${esc(r.device_id)}"`, ico: "i-link" })}
            ${btn("Заменить устройство", { act: "replace-open",
              data: `data-old="${esc(r.device_id)}"`, ico: "i-refresh" })}
            ${btn("Убрать", { act: "remove-device", data: `data-id="${esc(r.device_id)}"`,
              danger: true, ico: "i-alert" })}
            ${btn("Перезагрузить запись", { act: "reload-entry", data: `data-entry="${esc(r.entry_id)}"`, danger: true })}
          </div>
        </div>
        <nav class="subnav">${tabs}</nav>
      </div>
      <div class="pad col14" style="max-width:1560px;padding-top:16px">
        ${this.s.replaceTarget === r.device_id ? this.replaceBox() : ""}
        ${body}
      </div>
    </div>`;
  }

  dvStatus(d) {
    const r = d.device;
    const conn = [
      ["Состояние", ST[r.state]?.label || r.state],
      ["Подключено", r.connected ? "да" : "нет"],
      ["Доступно для HA", r.available ? "да" : "нет"],
      ["Последний ответ", age(r.last_update_age) + (r.last_update_age !== null ? " назад" : "")],
      ["Попыток подряд", r.retries || 0],
      ["Последняя ошибка", r.last_error || "—"],
      ["Отладка устройства", r.enable_debug ? "включена" : "выключена"],
    ];
    const chain = [
      ["Запись конфигурации", String(r.entry_id || "").slice(-10)],
      ["Транспорт", r.is_subdevice ? "через шлюз Zigbee" : "прямое Wi-Fi подключение"],
      ["Шлюз", d.gateway ? d.gateway.name : "—"],
      ["Node ID", r.node_id || "—"],
      ["Дочерних устройств", d.children.length],
      ["Комната", r.area_name || "не назначена"],
    ];
    const kids = d.children.length ? `
      ${cardOpen(`Дочерние устройства · ${d.children.length}`)}
      <div>${d.children.map((k) => `<button class="gwrow" data-act="open-device" data-id="${esc(k.device_id)}">
        <span class="gwico" style="background:${soft((ST[k.state] || ST.config).color, .12)};color:${(ST[k.state] || ST.config).color}">${icon("i-chip", 16)}</span>
        <span class="gwmain"><span class="gwtop"><span class="gwname">${esc(k.name)}</span>${pill(k.state)}</span>
        <span class="gwmeta">node ${esc(k.node_id || "")} · ${k.entity_count} сущн.</span></span></button>`).join("")}</div>
      ${cardClose}` : "";

    return `<div class="row14">
      <div style="flex:1 1 420px;min-width:300px" class="col14">
        ${cardOpen("Соединение")}<div style="padding:14px">${kv(conn)}</div>${cardClose}
        ${kids}
      </div>
      <div style="flex:1 1 420px;min-width:300px" class="col14">
        ${cardOpen("Цепочка и связи")}<div style="padding:14px">${kv(chain)}</div>${cardClose}
        ${cardOpen("Результаты действий")}
        <div id="actionlog" style="padding:14px;display:flex;flex-direction:column;gap:8px">
          ${(this._actionLog || []).length ? (this._actionLog || []).map((a) => `
            <div style="display:flex;gap:9px;align-items:flex-start">
              ${icon(a.ok ? "i-check" : "i-alert", 13, a.ok ? C.ok : C.bad)}
              <span style="flex:1;font-size:12px;color:${C.ink2}">${esc(a.text)}
                <span class="mono small mut" style="display:block">${esc(a.time)}</span></span></div>`).join("")
            : `<span class="small mut">Действий пока не было. Кнопки сверху пишут сюда результат.</span>`}
        </div>${cardClose}
      </div></div>`;
  }

  dvEntities(d) {
    const map = {};
    (d.entity_configs || []).forEach((c) => (map[c.id] = c));
    const rows = d.device.entities.map((e) => {
      // Сопоставление строго по датапоинту из unique_id. Поиск по префиксу
      // платформы давал один и тот же DP всем каналам многоклавишного
      // выключателя.
      const cfg = (e.dp_id && map[e.dp_id]) || null;
      const mapping = cfg ? Object.entries(cfg.mapping).filter(([k]) => k !== "platform")
        .map(([k, v]) => `${k.replace(/_dp$/, "")}=${v}`).join(" · ") : "";
      const bad = e.state === "unavailable" || e.state === "unknown";
      return `<div class="erow">
        <span class="ename2">${esc(e.name)}<span class="mono small mut" style="display:block">${esc(e.entity_id)}</span></span>
        <span class="mono small">${esc(e.domain)}</span>
        <span class="mono small mut">${esc(mapping || "—")}</span>
        <span style="font-weight:600;color:${bad ? C.bad : C.ink}">${esc(e.state ?? "—")}</span>
        ${btn("Изменить", { act: "edit-entity", data: `data-eid="${esc(e.entity_id)}"`, small: true, ico: "i-pencil" })}
      </div>`;
    }).join("");
    return `${cardOpen(`Сущности устройства · ${d.device.entity_count}`)}
      <div style="overflow-x:auto"><div style="min-width:760px">
        <div class="ehead"><span>Сущность</span><span>Платформа</span><span>Сопоставление DP</span><span>Состояние HA</span><span></span></div>
        ${rows || empty("У устройства нет сущностей", "i-alert", C.mut)}
      </div></div>${cardClose}`;
  }

  dvDps(d) {
    const rows = d.dps.map((p) => `
      <div class="drow">
        <span class="mono" style="font-weight:600">${esc(p.dp)}</span>
        <span class="mono small mut">${esc(p.code || "—")}</span>
        <span class="mono" style="color:${C.ink}">${esc(JSON.stringify(p.value))}</span>
        <span class="small mut">${esc(p.used_by.join(", ") || "не используется")}</span>
        ${this.s.expert ? btn("Записать", { act: "write-dp", data: `data-dp="${esc(p.dp)}"`, small: true, ico: "i-terminal" }) : ""}
      </div>`).join("");

    const expert = this.s.expert ? `
      ${cardOpen("Экспертная запись raw DP · set_dp", `<span class="badge-w">только эксперт</span>`)}
      <div style="padding:14px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <input id="dpid" placeholder="DP, напр. 1" style="width:120px" class="inp">
        <input id="dpval" placeholder="значение: true / 42 / open" style="flex:1;min-width:180px" class="inp">
        ${btn("Записать DP", { act: "write-dp-form", danger: true, ico: "i-terminal" })}
        <span class="small mut" style="flex-basis:100%">Запись идёт прямо в устройство и записывается в журнал интеграции.</span>
      </div>${cardClose}`
      : `<div class="card" style="padding:14px;display:flex;align-items:center;gap:10px">
          ${icon("i-shield", 15, C.mut)}<span class="small mut" style="flex:1">Запись сырых DP скрыта. Включите EXPERT в шапке, чтобы получить доступ.</span>
          ${btn("Включить EXPERT", { act: "expert", small: true })}</div>`;

    return `${cardOpen(`Живые значения DPS · ${d.dps.length}`)}
      <div style="overflow-x:auto"><div style="min-width:700px">
        <div class="dhead2"><span>DP</span><span>Код</span><span>Значение</span><span>Привязка</span>${this.s.expert ? "<span></span>" : ""}</div>
        ${rows || empty("Устройство ещё не сообщило ни одного значения", "i-clock", C.mut)}
      </div></div>${cardClose}
      ${expert}`;
  }

  dvDiag(d) {
    const evs = (d.events || []).slice().reverse();
    const rows = evs.length ? evs.map((e) => {
      const m = EVENT_META[e.event] || { label: e.event, color: C.mut, icon: "i-dots" };
      return `<div class="inc" style="padding:10px 16px;border-bottom:1px solid ${C.div}">
        <span class="inct">${esc(new Date(e.ts).toLocaleString("ru-RU"))}</span>
        <span class="incico" style="background:${soft(m.color, .12)};color:${m.color}">${icon(m.icon, 12)}</span>
        <span class="incb"><span class="inct1">${esc(m.label)}</span>
          <span class="inct2">${esc(e.reason || "")}${e.disconnect_age_sec != null ? ` · ${Math.round(e.disconnect_age_sec)} с` : ""}</span></span>
      </div>`;
    }).join("") : empty("Событий доступности по этому устройству нет");

    return `${cardOpen("События доступности устройства",
      btn("Вся хронология", { act: "nav", data: 'data-nav="events"', small: true, ico: "i-chev-r" }))}
      <div>${rows}</div>${cardClose}
      ${cardOpen("Диагностические данные", btn("Копировать (redacted)", { act: "copy-diag", small: true, ico: "i-copy" }))}
      <div style="padding:14px"><pre class="pre">${esc(JSON.stringify(d.config, null, 2))}</pre></div>${cardClose}`;
  }

  dvConfig(d) {
    const c = d.config;
    const fields = Object.entries(c)
      .filter(([k]) => !["entities", "dps_strings"].includes(k))
      .map(([k, v]) => [k, typeof v === "object" ? JSON.stringify(v) : String(v)]);
    return `${cardOpen("Транспорт и устройство",
      `<span class="small mut">ключи и секреты не передаются в панель</span>`)}
      <div style="padding:14px">${kv(fields)}</div>${cardClose}
      ${cardOpen("Датапоинты устройства")}
      <div style="padding:14px"><pre class="pre">${esc((c.dps_strings || []).join("\n") || "—")}</pre></div>${cardClose}`;
  }

  // ================================================== 04 Добавление =======
  vAdd() {
    const d = this.d.discovered;
    if (!d) return `<div class="pad muted">Поиск устройств…</div>`;
    const found = d.devices || [];
    // «Новое» - это то, про что мы ничего не знаем. Шлюз, за которым уже
    // настроены узлы, новым не является, даже если сам он как устройство не
    // заведён: предложить «Добавить» на нём - значит завести второе
    // устройство на занятый адрес, которое интеграция не поднимет.
    const fresh = found.filter((f) => !f.configured && !f.is_known_gateway);
    const gateways = found.filter((f) => !f.configured && f.is_known_gateway);
    const known = found.filter((f) => f.configured);

    const suspects = this.d.replace?.credentials_suspects || [];
    const targets = this.d.replace?.targets || [];
    const suspectBox = suspects.length ? `
      ${cardOpen("Похоже, сменился ключ", `<span class="small" style="color:${C.stale}">${
        plural(suspects.length, "устройство", "устройства", "устройств")}</span>`)}
      <div style="padding:14px;display:flex;flex-direction:column;gap:10px">
        <span class="small mut">Эти устройства отвечают в сети, но интеграция к ним не
          подключается. Обычно так бывает после замены шлюза или повторной привязки:
          device_id прежний, сменился только local_key. Замена «сама на себя» ничего не
          двигает в Home Assistant — только обновляет учётные данные.</span>
        ${suspects.map((sid) => {
          const t = targets.find((x) => x.device_id === sid);
          return `<div class="frow">
            <span class="acell">${icon("i-alert", 14, C.stale)}
              <span class="aname">${esc(t?.name || sid)}</span></span>
            <span class="mono small mut">${esc(sid)}</span>
            <span class="small mut">${esc(t?.area_name || "без комнаты")}</span>
            ${btn("Обновить ключ", { act: "replace-open", small: true,
              data: `data-old="${esc(sid)}" data-with="${esc(sid)}"`, ico: "i-refresh" })}
          </div>`;
        }).join("")}
      </div>${cardClose}` : "";

    const replaceBox = this.s.replaceTarget ? this.replaceBox() : "";

    const replaceList = `
      ${cardOpen("Заменить существующее устройство",
        `<span class="small mut">поломка, замена изделия</span>`)}
      <div style="padding:14px;display:flex;flex-direction:column;gap:10px">
        <span class="small mut">Выберите, ЧТО заменяем. Новое изделие унаследует его
          личность: те же entity_id, комната, автоматизации и история.</span>
        <div>${targets.length ? targets.map((t) => `
          <div class="frow">
            <span class="acell">${icon(t.is_subdevice ? "i-zigbee" : "i-wifi", 14, C.mut)}
              <span class="aname">${esc(t.name)}</span></span>
            <span class="small mut">${esc(t.area_name || "без комнаты")}</span>
            <span class="small mut">${plural(t.entity_count, "сущность", "сущности", "сущностей")}</span>
            <span class="mono small mut">${esc(t.host || "")}</span>
            ${btn("Заменить", { act: "replace-open", small: true,
              data: `data-old="${esc(t.device_id)}"`, ico: "i-refresh" })}
          </div>`).join("") : empty("Настроенных устройств нет", "i-chip", C.mut)}</div>
      </div>${cardClose}`;

    const addForm = (f) => {
      if (this.s.addDevice !== f.device_id) return "";
      const configured = (this.d.overview?.devices || []);
      return `
        <div style="padding:12px 14px;background:${C.bg};border-top:1px solid ${C.line};
                    display:flex;flex-direction:column;gap:9px">
          <div class="row14">
            <div style="flex:1 1 240px;min-width:220px;display:flex;flex-direction:column;gap:8px">
              <input id="add-name" placeholder="Название, например «Реле кухня»" style="width:100%">
              <input id="add-key" placeholder="local_key устройства" style="width:100%">
              <input id="add-host" value="${esc(f.host || "")}" placeholder="адрес" style="width:100%">
            </div>
            <div style="flex:1 1 240px;min-width:220px;display:flex;flex-direction:column;gap:8px">
              <select id="add-template" style="width:100%">
                <option value="">— без образца: сущности настроите в мастере —</option>
                ${configured.map((c) => `<option value="${esc(c.device_id)}">
                  по образцу: ${esc(c.name)} · ${plural(c.entity_count, "сущность", "сущности", "сущностей")}
                </option>`).join("")}
              </select>
              <span class="small mut">Образец избавляет от настройки датапоинтов заново:
                если ставите такое же изделие, возьмите соседнее. Имена сущностей
                будут переименованы под новое устройство.</span>
            </div>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            ${btn("Добавить устройство", { act: "add-go", primary: true, ico: "i-check",
              data: `data-id="${esc(f.device_id)}" data-proto="${esc(f.protocol || "")}"` })}
            ${btn("Отмена", { act: "add-cancel", small: false })}
            <span class="small mut">Перед добавлением панель проверит связь с устройством.</span>
          </div>
        </div>`;
    };

    const rows = fresh.length ? fresh.map((f) => `
      <div class="frow">
        <span class="acell">${icon("i-wifi", 14, C.mut)}<span class="aname mono">${esc(f.device_id)}</span></span>
        <span class="mono small">${esc(f.host)}</span>
        <span class="mono small mut">протокол ${esc(f.protocol || "?")}</span>
        ${btn(this.s.addDevice === f.device_id ? "Свернуть" : "Добавить",
          { act: "add-open", data: `data-id="${esc(f.device_id)}"`,
            primary: this.s.addDevice !== f.device_id, small: true, ico: "i-plus" })}
      </div>${addForm(f)}`).join("") : empty("Новых устройств в сети не обнаружено", "i-search", C.mut);

    // Шлюзы, за которыми уже работают настроенные узлы: показываем как факт,
    // а не как предложение добавить.
    const gwRows = gateways.map((f) => `
      <div class="frow">
        <span class="acell">${icon("i-zigbee", 14, C.blue)}
          <span class="aname mono">${esc(f.device_id)}</span></span>
        <span class="mono small">${esc(f.host)}</span>
        <span class="small">${f.serves
          ? `за ним ${plural(f.serves, "настроенное устройство", "настроенных устройства", "настроенных устройств")}`
          : "упомянут как шлюз"}</span>
        <span class="small mut">${esc(f.serves_example || "")}</span>
      </div>`).join("");
    const gwBox = gateways.length ? `
      ${cardOpen(`Шлюзы, уже обслуживающие устройства · ${gateways.length}`)}
      <div style="padding:10px 14px 4px"><span class="small mut">
        Это не новые устройства: за ними уже работают настроенные узлы. Заводить
        их как отдельное устройство не нужно — на занятом адресе второе
        устройство не поднимется.</span></div>
      <div>${gwRows}</div>${cardClose}` : "";

    // Главное объяснение, из-за отсутствия которого список кажется неполным.
    const whyBox = `
      ${cardOpen("Почему в списке не все устройства")}
      <div style="padding:12px 14px;display:flex;flex-direction:column;gap:7px">
        <span class="small">Здесь только то, что <b>само объявляет себя в сети</b>
          по UDP. Так делают Wi-Fi-устройства и шлюзы.</span>
        <span class="small"><b>Узлы Zigbee за шлюзом не вещают никогда</b> — ни один
          из них тут не появится, сколько ни обновляй. Их список знает только
          шлюз и облачный аккаунт, поэтому добавляются они мастером интеграции
          с привязанным аккаунтом Tuya.</span>
        <span class="small mut">Настроенные устройства целиком видны на вкладках
          «Обзор» и «Карта связей», а не здесь.</span>
      </div>${cardClose}`;

    return `<div class="pad col16" style="max-width:1440px">
      <div class="row16">
        <div style="flex:2 1 520px;min-width:320px" class="col16">
          ${suspectBox}
          ${replaceBox}
          ${gwBox}
          ${replaceList}
          ${cardOpen(`Найденные устройства · ${fresh.length}`,
            btn("Сканировать снова", { act: "refresh", small: true, ico: "i-refresh" }))}
            <div>${rows}</div>
          ${cardClose}
          ${cardOpen(`Уже в системе · ${known.length}`)}
            <div>${known.length ? known.map((f) => `
              <div class="frow"><span class="acell">${icon("i-check", 14, C.ok)}
                <span class="aname mono">${esc(f.device_id)}</span></span>
                <span class="mono small">${esc(f.host)}</span><span class="small mut">настроено</span><span></span></div>`).join("")
              : empty("Ни одно из найденных устройств пока не настроено", "i-alert", C.mut)}</div>
          ${cardClose}
        </div>
        <aside style="flex:1 1 300px;min-width:280px" class="col16">
          ${whyBox}
          ${cardOpen("Как это работает")}
          <div style="padding:14px;display:flex;flex-direction:column;gap:10px">
            ${[["Узлы за шлюзом здесь не появятся", "Широковещание шлёт только само устройство. Zigbee-узел за шлюзом этого не делает никогда — его знает шлюз, а не сеть."],
               ["Устройства сами объявляют себя", "Tuya-устройства раз в несколько секунд шлют широковещательный пакет в локальную сеть. Панель показывает всё, что услышала."],
               ["Широковещание не проходит между подсетями", "Устройство в другом VLAN придётся добавить вручную и задать статический адрес."],
               ["Ключ берётся из облака", "Локальный ключ подставляется из привязанного облачного аккаунта Tuya и никогда не показывается в панели."]]
              .map(([t, b]) => `<div><div style="font-size:12.5px;font-weight:600;margin-bottom:3px">${t}</div>
                <div class="small mut" style="line-height:1.5">${b}</div></div>`).join("")}
          </div>${cardClose}
          ${cardOpen("Ручное добавление")}
          <div style="padding:14px;display:flex;flex-direction:column;gap:9px">
            <span class="small mut">Устройство из списка слева добавляется прямо здесь —
              по образцу соседнего изделия сущности переносятся сразу. Мастер интеграции
              нужен, только если набор датапоинтов уникален и образца нет.</span>
            ${btn("Открыть мастер интеграции", { act: "open-flow", ico: "i-plus" })}
          </div>${cardClose}
        </aside>
      </div></div>`;
  }

  // ================================================== 05 События ==========
  vEvents() {
    const rep = this.d.report?.entries || [];
    const logs = this.d.logs?.records || [];
    const q = this.s.eventSearch;

    const evs = rep.slice().reverse().filter((e) =>
      !q || JSON.stringify(e).toLowerCase().includes(q));

    const byDay = {};
    evs.forEach((e) => {
      const d = new Date(e.ts);
      const key = d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
      (byDay[key] = byDay[key] || []).push(e);
    });

    const timeline = Object.keys(byDay).length ? Object.entries(byDay).map(([day, items]) => `
      <div class="dayhdr">${esc(day)}</div>
      ${items.map((e) => {
        const m = EVENT_META[e.event] || { label: e.event, color: C.mut, icon: "i-dots" };
        return `<div class="tlrow">
          <span class="mono small mut" style="width:64px;flex:0 0 auto">${esc(new Date(e.ts).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" }))}</span>
          <span class="incico" style="background:${soft(m.color, .12)};color:${m.color}">${icon(m.icon, 12)}</span>
          <span style="flex:1;min-width:0">
            <span style="display:block;font-size:12.5px;color:${C.ink2}">${esc(m.label)}${e.reason ? " — " + esc(e.reason) : ""}</span>
            <span class="mono small mut" style="display:block;margin-top:2px">${esc(e.name || e.device_id)}${e.node_id ? " · node " + esc(e.node_id) : ""}</span>
          </span>
          ${e.device_id ? btn("Открыть", { act: "open-device", data: `data-id="${esc(e.device_id)}"`, small: true }) : ""}
        </div>`;
      }).join("")}`).join("") : empty("Событий за выбранный период нет");

    const logRows = logs.slice().reverse()
      .filter((r) => !q || r.message.toLowerCase().includes(q))
      .slice(0, 200)
      .map((r) => `<div class="logrow ${r.level.toLowerCase()}">
        <span class="mono small mut" style="width:64px;flex:0 0 auto">${hhmmss(new Date(r.time * 1000))}</span>
        <span class="lvl">${r.level}</span><span class="lmsg">${esc(r.message)}</span></div>`).join("");

    return `<div class="pad col14" style="max-width:1560px">
      <div class="mtools">
        <div class="search"><span>${icon("i-search", 14, C.mut)}</span>
          <input data-act="esearch" value="${esc(q)}" placeholder="Устройство, сущность, node ID, причина"></div>
        <span style="flex:1"></span>
        ${btn("Экспорт (redacted)", { act: "export", small: true, ico: "i-download" })}
        ${btn("Обновить", { act: "refresh", small: true, ico: "i-refresh" })}
      </div>
      <div class="row14">
        <div style="flex:1 1 620px;min-width:320px">
          ${cardOpen(`Хронология · ${evs.length}`, `<span class="small mut">источник: журнал доступности</span>`)}
          <div>${timeline}</div>${cardClose}
        </div>
        <div style="flex:1 1 420px;min-width:300px">
          ${cardOpen(`Журнал интеграции · ${logs.length}`, `<span class="small mut">в памяти с запуска</span>`)}
          <div style="padding:8px 6px">${logRows || empty("Записей нет — интеграция пишет только предупреждения", "i-list", C.mut)}</div>
          ${cardClose}
        </div>
      </div></div>`;
  }

  // ================================================== 06 Настройки ========
  // ==================================================== 06 Комнаты ========
  vRooms() {
    const d = this.d.areas;
    if (!d) return `<div class="pad muted">Загрузка комнат…</div>`;
    const areas = d.areas || [];
    const free = d.unassigned || [];

    const options = (selected) =>
      `<option value="">— без комнаты —</option>` +
      areas.map((a) => `<option value="${esc(a.area_id)}" ${a.area_id === selected ? "selected" : ""}>
         ${esc(a.name)}</option>`).join("");

    const deviceRow = (dev, areaId) => `
      <div class="frow">
        <span class="acell">${icon(dev.is_gateway ? "i-network" : "i-chip", 14,
          (ST[dev.state] || ST.config).color)}
          <span class="aname">${esc(dev.name)}</span></span>
        <span class="small mut">${plural(dev.entity_count, "сущность", "сущности", "сущностей")}</span>
        <span>${pill(dev.state)}</span>
        <select data-act="set-area" data-id="${esc(dev.device_id)}"
                ${dev.registry_id ? "" : "disabled title='У устройства нет сущностей'"}>
          ${options(areaId)}
        </select>
      </div>`;

    const cards = areas.map((a) => `
      ${cardOpen(esc(a.name), `<span class="small mut">${
        plural(a.devices.length, "устройство", "устройства", "устройств")}</span>`)}
      <div>${a.devices.length
        ? a.devices.map((dev) => deviceRow(dev, a.area_id)).join("")
        : empty("В этой комнате пока нет устройств интеграции", "i-home", C.mut)}</div>
      ${cardClose}`).join("");

    return `<div class="pad col16" style="max-width:1280px">
      ${cardOpen("Новая комната")}
      <div style="padding:14px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <input id="newroom" placeholder="Например: Гостиная" style="flex:1;min-width:220px">
        ${btn("Создать", { act: "create-room", primary: true, ico: "i-plus" })}
        <span class="small mut">Комната создаётся в Home Assistant и сразу берёт первое
          устройство без комнаты — дальше распределите остальные списками ниже.</span>
      </div>${cardClose}

      ${free.length ? `
        ${cardOpen("Без комнаты", `<span class="small" style="color:${C.stale}">${
          plural(free.length, "устройство", "устройства", "устройств")}</span>`)}
        <div>${free.map((dev) => deviceRow(dev, null)).join("")}</div>
        ${cardClose}` : ""}

      ${cards || empty("Комнаты ещё не заведены", "i-home", C.mut)}
    </div>`;
  }

  // ============================================ Замена устройства =========
  replaceBox(target) {
    /* Карточка замены: слева что заменяем, справа чем. */
    const r = this.d.replace;
    if (!r) return "";
    const targets = r.targets || [];
    const chosen = target || targets.find((t) => t.device_id === this.s.replaceTarget);
    const candidates = (r.candidates || []).filter((c) => !c.configured);
    const withId = this.s.replaceWith;
    const cand = candidates.find((c) => c.device_id === withId);

    if (!chosen) return "";
    const mismatch = cand && chosen.datapoints.length
      ? `<div class="warnbox">${icon("i-alert", 14, C.stale)}<span>Сверьте датапоинты:
          у заменяемого устройства сущности на DP ${esc(chosen.datapoints.join(", "))}.
          Если новое изделие другой модели, часть сущностей работать не будет.</span></div>`
      : "";

    return `
      ${cardOpen(`Замена: ${esc(chosen.name)}`,
        `<span class="small mut">${esc(chosen.device_id)}</span>`)}
      <div style="padding:14px;display:flex;flex-direction:column;gap:12px">
        <div class="row14">
          <div style="flex:1 1 300px;min-width:260px">
            <div class="cap" style="margin-bottom:6px">Что заменяем</div>
            ${kv([
              ["Комната", chosen.area_name || "не назначена"],
              ["Сущностей", chosen.entity_count],
              ["Адрес", chosen.host || "—"],
              ["Тип", chosen.is_subdevice ? "узел за шлюзом" : "прямое подключение"],
              ...(chosen.children.length ? [["Дочерних", chosen.children.length]] : []),
            ])}
            <div class="small mut" style="margin-top:8px">Сохранятся: ${
              chosen.entities.map((e) => esc(e.entity_id)).join(", ") || "—"}</div>
          </div>
          <div style="flex:1 1 300px;min-width:260px">
            <div class="cap" style="margin-bottom:6px">Чем заменяем</div>
            <select data-act="replace-pick" style="width:100%;margin-bottom:8px">
              <option value="">— выберите устройство из сети —</option>
              ${candidates.map((c) => `<option value="${esc(c.device_id)}" ${
                c.device_id === withId ? "selected" : ""}>${esc(c.device_id)} · ${esc(c.host)}</option>`).join("")}
            </select>
            <input id="rep-id" placeholder="или впишите device_id вручную"
                   value="${esc(withId || "")}" style="width:100%;margin-bottom:8px">
            <input id="rep-key" placeholder="local_key нового устройства"
                   value="" style="width:100%;margin-bottom:8px">
            <input id="rep-host" placeholder="адрес" value="${esc(cand?.host || chosen.host || "")}"
                   style="width:100%">
          </div>
        </div>
        ${mismatch}
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          ${btn("Заменить устройство", { act: "replace-go", data:
            `data-entry="${esc(chosen.entry_id)}" data-old="${esc(chosen.device_id)}"`,
            primary: true, ico: "i-refresh" })}
          ${btn("Отмена", { act: "replace-cancel" })}
          <span class="small mut">Home Assistant продолжит считать это тем же устройством:
            entity_id, комната, автоматизации и история сохранятся.</span>
        </div>
      </div>${cardClose}`;
  }

  homeSettings() {
    /* Настройки установки: пока не заданы, действуют значения по умолчанию. */
    const st = this.d.settings;
    if (!st) return "";
    const field = (id, label, value, hint, suffix = "с") => `
      <div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid ${C.div}">
        <span style="flex:1;min-width:0">
          <span style="display:block;font-size:12.5px;color:${C.ink2}">${label}</span>
          <span class="small mut">${hint}</span>
        </span>
        <input id="${id}" type="number" value="${esc(value)}"
               style="width:96px;text-align:right">
        <span class="small mut" style="width:16px">${suffix}</span>
      </div>`;

    return (st.entries || []).map((e) => `
      ${cardOpen(`Настройки дома · ${esc(e.title)}`,
        `<span class="small mut">по умолчанию ${st.defaults.grace_period}/${
          st.defaults.startup_grace}/${st.defaults.watchdog_interval} с</span>`)}
      <div style="padding:14px;display:flex;flex-direction:column;gap:4px">
        <div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid ${C.div}">
          <span style="flex:1">
            <span style="display:block;font-size:12.5px;color:${C.ink2}">Название дома</span>
            <span class="small mut">Показывается в панели; на работу не влияет.</span>
          </span>
          <input id="set-name-${esc(e.entry_id)}" value="${esc(e.home_name || "")}"
                 placeholder="${esc(e.title)}" style="width:200px">
        </div>
        ${field(`set-grace-${e.entry_id}`, "Окно недоступности", e.grace_period,
          "Сколько держать сущности доступными во время переподключения. Больше — меньше мусора в истории при микро-обрывах Wi-Fi.")}
        ${field(`set-start-${e.entry_id}`, "Окно при старте", e.startup_grace,
          "То же самое, но сразу после запуска Home Assistant, когда парк ещё поднимается.")}
        ${field(`set-dog-${e.entry_id}`, "Проверка шлюзов", e.watchdog_interval,
          "Как часто опрашивать шлюзы настоящим запросом: сокет остаётся открытым и после смерти сервиса за ним.")}
        <div style="display:flex;align-items:center;gap:10px;padding:9px 0;
                    border-bottom:1px solid ${C.div}">
          <span style="flex:1">
            <span style="display:block;font-size:12.5px;color:${
              e.lockdown ? C.bad : C.ink2};font-weight:${e.lockdown ? 600 : 400}">
              Изолированный режим</span>
            <span class="small mut">Полностью запрещает ИНТЕГРАЦИИ обмен с облаком Tuya.
              Управление устройствами идёт по локальной сети и не меняется.
              Сами хабы и Wi-Fi-устройства держат собственное соединение с облаком —
              его закрывают правилом на роутере; локальное управление при этом
              сохраняется.
              ${e.cloud_configured
                ? "Облачный аккаунт привязан: ключи и список устройств перестанут обновляться, замена устройства делается вручную в панели."
                : "Облачный аккаунт не привязан — режим просто закрывает путь наружу окончательно."}
              ${e.blocked_requests
                ? `<b style="color:${C.bad}">Заблокировано запросов с момента запуска: ${e.blocked_requests}.</b>`
                : ""}</span>
          </span>
          <input id="set-lock-${esc(e.entry_id)}" type="checkbox" ${e.lockdown ? "checked" : ""}
                 style="width:18px;height:18px">
        </div>
        <div style="display:flex;align-items:center;gap:10px;padding:9px 0">
          <span style="flex:1">
            <span style="display:block;font-size:12.5px;color:${C.ink2}">Подробный журнал</span>
            <span class="small mut">Включает отладку всей интеграции без правки configuration.yaml.</span>
          </span>
          <input id="set-debug-${esc(e.entry_id)}" type="checkbox" ${e.debug ? "checked" : ""}
                 style="width:18px;height:18px">
        </div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
          ${btn("Сохранить", { act: "save-settings", data: `data-entry="${esc(e.entry_id)}"`,
            primary: true, ico: "i-check" })}
          <span class="small mut">Запись перезагрузится — соединения пересоберутся.</span>
        </div>
      </div>${cardClose}`).join("");
  }

  vSettings() {
    const entries = this.d.overview?.entries || [];
    const s = this.sum();
    const cards = entries.map((e) => `
      ${cardOpen(esc(e.title), pill(e.problems ? "stale" : "online", "entry"))}
      <div style="padding:14px;display:flex;flex-direction:column;gap:12px">
        <div class="mini3">
          <div><b>${e.devices}</b><span>${plural(e.devices, "устройство", "устройства", "устройств")
            .replace(/^\d+\s/, "")}</span></div>
          <div><b>${e.entities}</b><span>${plural(e.entities, "сущность", "сущности", "сущностей")
            .replace(/^\d+\s/, "")}</span></div>
          <div><b style="color:${e.problems ? C.bad : C.ok}">${e.problems}</b><span>${
            plural(e.problems, "проблема", "проблемы", "проблем").replace(/^\d+\s/, "")}</span></div>
        </div>
        ${kv([["Идентификатор", e.entry_id], ["Облачный аккаунт", e.no_cloud ? "не привязан" : "привязан"]])}
        <div style="display:flex;gap:7px;flex-wrap:wrap">
          ${btn("Показать на карте", { act: "nav", data: 'data-nav="map"', ico: "i-network" })}
          ${btn("Перезагрузить запись", { act: "reload-entry", data: `data-entry="${esc(e.entry_id)}"`, danger: true, ico: "i-refresh" })}
        </div>
      </div>${cardClose}`).join("");

    return `<div class="pad col16" style="max-width:1440px">
      <div class="row16">
        <div style="flex:2 1 520px;min-width:320px" class="col16">
          ${this.homeSettings()}
          ${cards}
        </div>
        <aside style="flex:1 1 320px;min-width:290px" class="col16">
          ${cardOpen("Параметры восстановления")}
          <div style="padding:14px">${kv([
            ["Grace-период", "120 с"],
            ["Окно при старте", "300 с"],
            ["Отсчёт повторов", "1, 2, 5, 10, 20, 30, 60 с"],
            ["Watchdog шлюза", "каждые 30 с"],
            ["Порог «молчит»", "120 с"],
          ])}</div>${cardClose}
          ${cardOpen("Экспертный режим")}
          <div style="padding:14px;display:flex;flex-direction:column;gap:9px">
            <span class="small mut">Открывает запись сырых DP в карточке устройства. Каждая запись попадает в журнал интеграции с именем пользователя.</span>
            ${btn(this.s.expert ? "Выключить EXPERT" : "Включить EXPERT", { act: "expert", danger: this.s.expert })}
          </div>${cardClose}
          ${cardOpen("Опасные операции")}
          <div style="padding:14px;display:flex;flex-direction:column;gap:9px">
            <span class="small mut">Перезагрузка интеграции разрывает соединения со всеми ${s.total} устройствами и собирает их заново. Свет и техника продолжат работать от выключателей.</span>
            ${btn("Перезагрузить интеграцию BMS", { act: "reload-all", danger: true, ico: "i-alert" })}
          </div>${cardClose}
        </aside>
      </div></div>`;
  }

  // ------------------------------------------------------------ события --
  onInput(e) {
    const t = e.composedPath()[0];
    if (!t?.dataset) return;
    if (t.dataset.act === "search") { this.s.search = t.value.toLowerCase(); this.paint(); this.focus("search"); }
    if (t.dataset.act === "esearch") { this.s.eventSearch = t.value.toLowerCase(); this.paint(); this.focus("esearch"); }
    if (t.dataset.act === "entry") { this.s.entryFilter = t.value; this.paint(); }
    if (t.dataset.act === "replace-pick") {
      this.s.replaceWith = t.value || null;
      this.paint();
    }
    if (t.dataset.act === "set-area") this.assignArea(t.dataset.id, t.value);
  }

  async assignArea(deviceId, areaId) {
    try {
      await this.ws("assign_area", { device_id: deviceId, area_id: areaId || null });
      this.toast(areaId ? "Комната назначена" : "Устройство убрано из комнаты");
    } catch (err) {
      this.toast(err?.message || "Не удалось назначить комнату", true);
    }
    return this.refresh(true);
  }

  focus(act) {
    const el = this.shadowRoot.querySelector(`[data-act="${act}"]`);
    if (el) { el.focus(); const v = el.value; el.value = ""; el.value = v; }
  }

  async onClick(e) {
    const el = e.composedPath().find((n) => n?.dataset?.act);
    if (!el) return;
    const a = el.dataset.act;
    const id = el.dataset.id;

    if (a === "nav") return this.go(el.dataset.nav);
    if (a === "dtab") { this.s.deviceTab = el.dataset.tab; return this.paint(); }
    if (a === "refresh") return this.refresh();
    if (a === "expert") { this.s.expert = !this.s.expert; return this.paint(); }
    if (a === "incidents") { this.s.incidentsOnly = !this.s.incidentsOnly; return this.paint(); }
    if (a === "chip") {
      this.s.mapState = this.s.mapState === el.dataset.state ? null : el.dataset.state;
      return this.paint();
    }
    if (a === "filter-state") { this.s.mapState = el.dataset.state; return this.go("map"); }
    if (a === "toggle") {
      const n = el.dataset.node;
      this.s.collapsed.has(n) ? this.s.collapsed.delete(n) : this.s.collapsed.add(n);
      return this.paint();
    }
    if (a === "expand-all") { this.s.collapsed.clear(); return this.paint(); }
    if (a === "collapse-all") {
      const all = [];
      const walk = (n) => { if ((n.children || []).length) all.push(n.id); (n.children || []).forEach(walk); };
      (this.d.topology?.tree || []).forEach(walk);
      this.s.collapsed = new Set(all);
      return this.paint();
    }
    if (a === "select") {
      this.s.selected = el.dataset.node;
      return this.paint();
    }
    if (a === "open-device") {
      this.s.deviceId = id; this.s.deviceTab = "status"; this.d.device = null;
      return this.go("device");
    }
    if (a === "open-flow") {
      const entry = this.d.overview?.entries?.[0];
      window.history.pushState(null, "", "/config/integrations/integration/bms_integration");
      window.dispatchEvent(new CustomEvent("location-changed"));
      return;
    }
    if (a === "add-device") {
      this.toast("Устройство добавляется через мастер интеграции — открываю");
      return this.onClick({ composedPath: () => [{ dataset: { act: "open-flow" } }] });
    }
    if (a === "edit-entity") {
      window.history.pushState(null, "", "/config/integrations/integration/bms_integration");
      window.dispatchEvent(new CustomEvent("location-changed"));
      return;
    }
    if (a === "copy-diag") {
      try {
        await navigator.clipboard.writeText(JSON.stringify(this.d.device?.config || {}, null, 2));
        this.toast("Скопировано без секретов");
      } catch { this.toast("Буфер обмена недоступен", true); }
      return;
    }
    if (a === "export") {
      // Кнопка обещает «redacted» — значит адреса и идентификаторы устройств
      // в файл не попадают: такие выгрузки прикладывают к обращениям.
      const entries = (this.d.report?.entries || []).map((e) => {
        const { host, device_id, node_id, gateway_id, ...safe } = e;
        return safe;
      });
      const blob = new Blob([JSON.stringify(entries, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "bms-events.json";
      // Anchor must be in the document and the URL must outlive the click,
      // or Firefox and older WebKit never start the download.
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      return this.toast("Файл событий сохранён");
    }

    // ---- комнаты и замена ---------------------------------------------
    if (a === "lockdown-toggle") {
      const on = !el.dataset.on;
      if (!confirm(on
        ? "Включить изолированный режим?\n\nИнтеграции будет полностью запрещён обмен с облаком Tuya и Tuya IoT. Управление устройствами локальное и не изменится. Ключи и список устройств перестанут обновляться из облака."
        : "Снять изолированный режим и снова разрешить обмен с облаком Tuya?"))
        return;
      el.disabled = true;
      try {
        await this.ws("set_lockdown", { enabled: on });
        this.logAction(true, on ? "Изолированный режим включён" : "Изолированный режим снят");
        this.toast(on
          ? "Изолированный режим включён: обмен с облаком запрещён"
          : "Изолированный режим снят");
      } catch (err) {
        this.logAction(false, `Изолированный режим: ${err?.message || err}`);
        this.toast(err?.message || "Не получилось", true);
      }
      el.disabled = false;
      return this.refresh();
    }
    if (a === "create-room") {
      const name = this.shadowRoot.getElementById("newroom")?.value?.trim();
      if (!name) return this.toast("Впишите название комнаты", true);
      const first = (this.d.areas?.unassigned || [])[0];
      if (!first) return this.toast("Нет устройства без комнаты, к которому её привязать", true);
      try {
        await this.ws("assign_area", { device_id: first.device_id, new_area: name });
        this.toast(`Комната «${name}» создана`);
      } catch (err) { this.toast(err?.message || "Не удалось создать", true); }
      return this.refresh(true);
    }
    if (a === "add-open") {
      this.s.addDevice = this.s.addDevice === id ? null : id;
      return this.paint();
    }
    if (a === "add-cancel") { this.s.addDevice = null; return this.paint(); }
    if (a === "add-go") {
      const root = this.shadowRoot;
      const key = (root.getElementById("add-key")?.value || "").trim();
      if (!key) return this.toast("Нужен local_key устройства", true);
      const entryId = (this.d.overview?.entries || [])[0]?.entry_id;
      if (!entryId) return this.toast("Не найдена запись конфигурации", true);
      el.disabled = true;
      try {
        const res = await this.ws("add_device", {
          entry_id: entryId,
          device_id: id,
          local_key: key,
          name: root.getElementById("add-name")?.value || "",
          host: root.getElementById("add-host")?.value || "",
          protocol_version: el.dataset.proto || null,
          template_device_id: root.getElementById("add-template")?.value || null,
        });
        this.logAction(true, `Добавлено ${res.name}: сущностей ${res.entities}`);
        this.toast(res.entities
          ? `Добавлено, сущностей по образцу: ${res.entities}`
          : "Добавлено. Сущности настройте в мастере интеграции.");
        this.s.addDevice = null;
      } catch (err) {
        this.logAction(false, `Добавление ${id}: ${err?.message || err}`);
        this.toast(err?.message || "Не удалось добавить", true);
      }
      el.disabled = false;
      return this.refresh();
    }
    if (a === "remove-device") {
      const row = this.deviceRow(id);
      if (!confirm(`Убрать «${row?.name || id}» из интеграции?\n\nЕго сущности и запись в Home Assistant будут удалены вместе с историей привязок. Само устройство продолжит работать от выключателей.`))
        return;
      try {
        const res = await this.ws("remove_device", { entry_id: row?.entry_id, device_id: id });
        this.logAction(true, `Удалено ${res.name}: сущностей ${res.entities}`);
        this.toast("Устройство убрано");
        if (this.s.deviceId === id) this.s.screen = "overview";
      } catch (err) {
        this.logAction(false, `Удаление ${id}: ${err?.message || err}`);
        this.toast(err?.message || "Не удалось убрать", true);
      }
      return this.refresh();
    }
    if (a === "replace-open") {
      this.s.replaceTarget = el.dataset.old || id;
      this.s.replaceWith = el.dataset.with || null;
      return this.paint();
    }
    if (a === "replace-cancel") {
      this.s.replaceTarget = null; this.s.replaceWith = null;
      return this.paint();
    }
    if (a === "replace-go") {
      const root = this.shadowRoot;
      const newId = (root.getElementById("rep-id")?.value || this.s.replaceWith || "").trim();
      const key = (root.getElementById("rep-key")?.value || "").trim();
      const host = (root.getElementById("rep-host")?.value || "").trim();
      const oldId = el.dataset.old;
      if (!newId) return this.toast("Укажите device_id нового устройства", true);
      const same = newId === oldId;
      if (!same && !key)
        return this.toast("Для нового устройства нужен его local_key", true);
      if (!confirm(same
        ? `Обновить учётные данные устройства ${oldId}?`
        : `Выдать устройство ${newId} за ${oldId}?\n\nHome Assistant продолжит считать это тем же устройством: entity_id, комната, автоматизации и история сохранятся.`))
        return;
      el.disabled = true;
      try {
        const res = await this.ws("replace_device", {
          entry_id: el.dataset.entry, old_device_id: oldId, new_device_id: newId,
          local_key: key || null, host: host || null,
        });
        this.logAction(true, `Замена ${oldId} -> ${newId}: сущностей ${res.entities}`);
        this.toast(res.mode === "credentials"
          ? "Учётные данные обновлены"
          : `Заменено, перенесено сущностей: ${res.entities}`);
        this.s.replaceTarget = null; this.s.replaceWith = null;
      } catch (err) {
        this.logAction(false, `Замена ${oldId}: ${err?.message || err}`);
        this.toast(err?.message || "Замена не удалась", true);
      }
      el.disabled = false;
      return this.refresh();
    }
    if (a === "save-settings") {
      const root = this.shadowRoot;
      const num = (id2, fallback) => {
        const v = parseInt(root.getElementById(id2)?.value, 10);
        return Number.isFinite(v) ? v : fallback;
      };
      const entryId = el.dataset.entry;
      try {
        await this.ws("update_settings", {
          entry_id: entryId,
          home_name: root.getElementById(`set-name-${entryId}`)?.value || "",
          grace_period: num(`set-grace-${entryId}`, 120),
          startup_grace: num(`set-start-${entryId}`, 300),
          watchdog_interval: num(`set-dog-${entryId}`, 30),
          debug: !!root.getElementById(`set-debug-${entryId}`)?.checked,
          lockdown: !!root.getElementById(`set-lock-${entryId}`)?.checked,
        });
        this.toast("Настройки сохранены, запись перезагружается");
      } catch (err) { this.toast(err?.message || "Не удалось сохранить", true); }
      return this.refresh();
    }

    // ---- действия, меняющие состояние --------------------------------
    if (a === "dev-refresh" || a === "dev-reconnect" || a === "dev-test") {
      const row = this.deviceRow(id);
      const action = a === "dev-refresh" ? "refresh" : a === "dev-test" ? "status_test" : "reconnect";
      el.disabled = true;
      try {
        const res = await this.ws("action", { device_id: id, node_id: row?.node_id || null, action });
        this.logAction(true, `${el.textContent.trim()}: ${res.detail} (${res.took_ms} мс)`);
        this.toast(res.detail);
      } catch (err) {
        this.logAction(false, `${el.textContent.trim()}: ${err?.message || err}`);
        this.toast(err?.message || "Не получилось", true);
      }
      el.disabled = false;
      return this.refresh(true);
    }
    if (a === "reload-entry" || a === "reload-all") {
      const all = a === "reload-all";
      const what = all ? "всю интеграцию" : "эту запись";
      if (!confirm(`Перезагрузить ${what}? Соединения со всеми устройствами будут пересобраны — это занимает до минуты.`)) return;
      this.toast("Перезагрузка запущена…");
      try {
        await this.ws("reload", all ? {} : { entry_id: el.dataset.entry });
        this.toast("Перезагрузка выполнена");
      } catch (err) { this.toast(err?.message || "Не удалось", true); }
      return this.refresh();
    }
    if (a === "write-dp" || a === "write-dp-form") {
      const dp = a === "write-dp" ? el.dataset.dp : this.shadowRoot.getElementById("dpid")?.value;
      const value = a === "write-dp"
        ? prompt(`Новое значение DP ${dp}:`)
        : this.shadowRoot.getElementById("dpval")?.value;
      if (dp == null || value == null || value === "") return;
      const row = this.d.device?.device;
      if (!confirm(`Записать DP ${dp} = ${value} в «${row?.name}»? Команда уйдёт прямо в устройство.`)) return;
      try {
        await this.ws("set_dp", { device_id: row.device_id, node_id: row.node_id || null, dp: String(dp), value });
        this.logAction(true, `Запись DP ${dp} = ${value}`);
        this.toast("Записано");
      } catch (err) {
        this.logAction(false, `Запись DP ${dp}: ${err?.message || err}`);
        this.toast(err?.message || "Запись не прошла", true);
      }
      return this.refresh(true);
    }
  }

  logAction(ok, text) {
    this._actionLog = [{ ok, text, time: hhmmss(new Date()) }, ...(this._actionLog || [])].slice(0, 8);
  }
}

// ------------------------------------------------------------- спрайт ----
const SPRITE = `<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="i-grid" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 9.5V6a2.5 2.5 0 0 1 2.5-2.5h3.5M14.5 3.5H18A2.5 2.5 0 0 1 20.5 6v3.5M20.5 14.5V18a2.5 2.5 0 0 1-2.5 2.5h-3.5M9.5 20.5H6A2.5 2.5 0 0 1 3.5 18v-3.5"/><rect x="8.4" y="8.4" width="7.2" height="7.2" rx="1.6"/></symbol>
<symbol id="i-network" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="8.6" y="3.4" width="6.8" height="5" rx="1.4"/><rect x="3" y="15.6" width="6.8" height="5" rx="1.4"/><rect x="14.2" y="15.6" width="6.8" height="5" rx="1.4"/><path d="M12 8.4v3.4M6.4 15.6v-1.9a1.9 1.9 0 0 1 1.9-1.9h7.4a1.9 1.9 0 0 1 1.9 1.9v1.9"/></symbol>
<symbol id="i-chip" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="4.4" y="4.4" width="15.2" height="15.2" rx="4.2"/><rect x="9.1" y="9.1" width="5.8" height="5.8" rx="1.7"/></symbol>
<symbol id="i-plus" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5.2v13.6M5.2 12h13.6"/></symbol>
<symbol id="i-list" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8.8 6.4h11.2M8.8 12h11.2M8.8 17.6h7.4"/><circle cx="4.7" cy="6.4" r="1.15" fill="currentColor" stroke="none"/><circle cx="4.7" cy="12" r="1.15" fill="currentColor" stroke="none"/><circle cx="4.7" cy="17.6" r="1.15" fill="currentColor" stroke="none"/></symbol>
<symbol id="i-sliders" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.7 7.4h6.1M14.2 7.4h6.1M3.7 12h10.1M18.2 12h2.1M3.7 16.6h3.5M11.6 16.6h8.7"/><circle cx="12" cy="7.4" r="2.2"/><circle cx="16" cy="12" r="2.2"/><circle cx="9.4" cy="16.6" r="2.2"/></symbol>
<symbol id="i-pencil" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4.2 19.8l1.1-4.1L16.1 4.9a2.3 2.3 0 0 1 3.2 3.2L8.3 18.7l-4.1 1.1z"/><path d="M14.6 6.4l3.2 3.2"/></symbol>
<symbol id="i-refresh" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M19.7 12a7.7 7.7 0 1 1-2.3-5.5"/><path d="M19.7 4.3v4.3h-4.3"/></symbol>
<symbol id="i-chev-r" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M9.4 5.6l6.4 6.4-6.4 6.4"/></symbol>
<symbol id="i-chev-d" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M5.6 9.4l6.4 6.4 6.4-6.4"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="10.8" cy="10.8" r="6.4"/><path d="M15.5 15.5l4.3 4.3"/></symbol>
<symbol id="i-wifi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.6 9.2a12 12 0 0 1 16.8 0"/><path d="M6.9 12.7a7.4 7.4 0 0 1 10.2 0"/><path d="M10.2 16.2a3 3 0 0 1 3.6 0"/><circle cx="12" cy="19.2" r="1.2" fill="currentColor" stroke="none"/></symbol>
<symbol id="i-zigbee" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6.6 6.6h10.8L6.6 17.4h10.8"/></symbol>
<symbol id="i-shield" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.2l7.8 2.7v6.2c0 4.3-3.1 7.4-7.8 8.7-4.7-1.3-7.8-4.4-7.8-8.7V5.9z"/></symbol>
<symbol id="i-download" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.8v10.4M8.2 10.6L12 14.4l3.8-3.8"/><path d="M4.8 18.4h14.4"/></symbol>
<symbol id="i-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="8.8" y="8.8" width="11.4" height="11.4" rx="2.8"/><path d="M15.4 8.8V6.6a2.8 2.8 0 0 0-2.8-2.8H6.6a2.8 2.8 0 0 0-2.8 2.8v6a2.8 2.8 0 0 0 2.8 2.8h2.2"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4.8 12.6l4.6 4.6L19.2 7.4"/></symbol>
<symbol id="i-clock" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.4"/><path d="M12 7.2V12l3.4 2.1"/></symbol>
<symbol id="i-alert" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 4.3a2 2 0 0 1 3.4 0l7.4 13.3a2 2 0 0 1-1.7 3H4.6a2 2 0 0 1-1.7-3z"/><path d="M12 9.4v4.3"/><circle cx="12" cy="17.2" r="1" fill="currentColor" stroke="none"/></symbol>
<symbol id="i-octagon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.6"/><path d="M5.9 5.9l12.2 12.2"/></symbol>
<symbol id="i-terminal" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3.2" y="4.2" width="17.6" height="15.6" rx="3.4"/><path d="M7.6 9.6l2.8 2.4-2.8 2.4M13.2 14.4h3.6"/></symbol>
<symbol id="i-link" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9.6 14.4l4.8-4.8"/><path d="M10.9 6.9l2.1-2.1a4.3 4.3 0 0 1 6.1 6.1l-2.1 2.1M13.1 17.1l-2.1 2.1a4.3 4.3 0 0 1-6.1-6.1l2.1-2.1"/></symbol>
<symbol id="i-filter" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.8 5.6h16.4l-6.4 7.4v6.6l-3.6-2.1v-4.5z"/></symbol>
<symbol id="i-play" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8.2 5.4a1 1 0 0 1 1.5-.9l9.1 5.6a1 1 0 0 1 0 1.8l-9.1 5.6a1 1 0 0 1-1.5-.9z"/></symbol>
<symbol id="i-gear" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4.9"/><circle cx="12" cy="12" r="2.1"/><path d="M16.9 12h2M14.45 16.24l1 1.74M9.55 16.24l-1 1.74M7.1 12h-2M9.55 7.76l-1-1.74M14.45 7.76l1-1.74"/></symbol>
<symbol id="i-dots" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round"><path d="M4.4 12h.01M8.2 12h.01" stroke-width="2.6"/><path d="M12 12h.01M15.8 12h.01M19.6 12h.01" stroke-width="2.6" opacity=".35"/></symbol>
<symbol id="i-home" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.6 10.9L12 4.2l8.4 6.7v8a1.8 1.8 0 0 1-1.8 1.8H5.4a1.8 1.8 0 0 1-1.8-1.8z"/></symbol>
<symbol id="i-eye" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2.6 12.6S6 6.8 12 6.8s9.4 5.8 9.4 5.8"/><circle cx="12" cy="13.4" r="3.1"/></symbol>
</defs></svg>`;

// ---------------------------------------------------------------- стили --
const CSS = `
:host{display:block;background:${C.bg};min-height:100vh;
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue','Segoe UI',sans-serif;
  color:${C.ink};-webkit-font-smoothing:antialiased}
*{box-sizing:border-box}
button{font-family:inherit;transition:background .15s,border-color .15s,color .15s}
input,select{font-family:inherit}
.app{display:flex;flex-direction:column;min-height:100vh}
header{background:${C.card};border-bottom:1px solid ${C.div};display:flex;align-items:center;gap:16px;
  padding:9px 20px;flex-wrap:wrap;min-height:56px}
.brand{display:flex;align-items:center;gap:11px;min-width:0}
.logo{height:26px;width:auto;display:block}
.vr{width:1px;height:22px;background:#DDE3E9}
.t1{font-size:14px;font-weight:600;letter-spacing:-.01em;white-space:nowrap}
.t2{font-size:10.5px;color:${C.mut};letter-spacing:.04em;white-space:nowrap}
.hactions{display:flex;align-items:center;gap:8px;margin-left:auto;flex-wrap:wrap}
.chip-sel{display:flex;align-items:center;gap:8px;height:32px;padding:0 10px 0 11px;background:${C.card};
  border:1px solid #DDE3E9;border-radius:7px}
.chip-sel select{background:transparent;border:0;color:${C.ink};font-size:12.5px;font-weight:500;outline:none;cursor:pointer;max-width:180px}
.upd{display:flex;align-items:center;gap:7px;font-size:11.5px;color:${C.mut};white-space:nowrap}
.dot-live{width:6px;height:6px;border-radius:50%;background:${C.ok};box-shadow:0 0 0 3px ${soft(C.ok, .14)}}
nav{background:${C.card};border-bottom:1px solid ${C.div};display:flex;gap:2px;padding:0 14px;overflow-x:auto}
nav::-webkit-scrollbar{display:none}
.tab{display:inline-flex;align-items:center;gap:8px;height:42px;padding:0 12px;border:0;background:transparent;
  color:#5F6B77;font-size:12.5px;font-weight:500;cursor:pointer;white-space:nowrap;border-bottom:2px solid transparent}
.tab.on{color:${C.accent};font-weight:600;border-bottom-color:${C.accent}}
.badge-n{min-width:17px;height:17px;padding:0 5px;border-radius:9px;background:${soft(C.bad, .16)};color:${C.bad};
  font-size:10.5px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;font-family:${MONO}}
.badge-w{height:18px;padding:0 7px;border-radius:4px;background:${soft(C.warn, .14)};color:${C.warn};
  font-size:10.5px;font-weight:700;display:inline-flex;align-items:center;font-family:${MONO}}
.cnt{font-family:${MONO};font-size:11px;opacity:.65}
main{flex:1;min-height:0}
.pad{padding:20px 20px 40px}
.col14{display:flex;flex-direction:column;gap:14px}
.col16{display:flex;flex-direction:column;gap:16px}
.row14{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start}
.row16{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start}
.card{background:${C.card};border:1px solid ${C.line};border-radius:10px;box-shadow:0 1px 2px rgba(24,39,58,.05)}
.card.err{border-left:3px solid ${C.bad};padding:14px;display:flex;gap:10px;align-items:center;color:${C.bad}}
.cap{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:${C.mut2}}
.muted,.mut{color:${C.mut}}
.small{font-size:11.5px}
.mono{font-family:${MONO}}
.pre{margin:0;font-family:${MONO};font-size:11.5px;color:${C.ink2};white-space:pre-wrap;word-break:break-word;
  background:#F7F9FB;border:1px solid ${C.div};border-radius:8px;padding:11px;max-height:340px;overflow:auto}
.inp{height:32px;padding:0 10px;border:1px solid #DDE3E9;border-radius:7px;background:${C.card};
  color:${C.ink};font-size:12.5px;outline:none}
.inp:focus{border-color:${C.accent}}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:10px}
.tile{background:${C.card};border:1px solid ${C.line};border-radius:10px;box-shadow:0 1px 2px rgba(24,39,58,.05);
  padding:13px 14px;display:flex;flex-direction:column;gap:9px;position:relative;overflow:hidden}
.tile:before{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--tc)}
.tl{display:flex;align-items:center;gap:7px}
.tl span{font-size:10.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:${C.mut2}}
.tv{display:flex;align-items:baseline;gap:8px}
.tv>span:first-child{font-family:${MONO};font-size:27px;font-weight:600;line-height:1;letter-spacing:-.02em}
.tn{font-size:11px;color:${C.mut}}
.hbar{display:flex;height:9px;border-radius:5px;overflow:hidden;gap:1.5px;background:#F2F5F8}
.hgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:2px;margin-top:14px}
.hrow{display:flex;align-items:center;gap:10px;padding:7px 8px;border:0;background:transparent;border-radius:6px;
  cursor:pointer;text-align:left;width:100%}
.hrow:hover{background:#F4F6F8}
.hd{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
.hl{font-size:12.5px;color:${C.ink2};flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hn{font-family:${MONO};font-size:13px;font-weight:600;color:${C.ink}}
.gwrow{display:flex;align-items:center;gap:13px;padding:13px 16px;border:0;border-top:1px solid ${C.div};
  background:transparent;cursor:pointer;text-align:left;width:100%}
.gwrow:hover{background:#F4F6F8}
.gwico{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;flex:0 0 auto}
.gwmain{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
.gwtop{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.gwname{font-size:13px;font-weight:600;color:${C.ink};overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gwmeta{font-size:11.5px;color:${C.mut};font-family:${MONO}}
.gwaff{text-align:right;flex:0 0 auto}
.gwaff>span:first-child{display:block;font-family:${MONO};font-size:14px;font-weight:600}
.gwaff>span:last-child{display:block;font-size:10px;color:${C.mut};letter-spacing:.04em}
.inc{display:flex;gap:11px;padding:9px 16px;align-items:flex-start}
.inct{font-family:${MONO};font-size:11px;color:${C.mut};padding-top:2px;flex:0 0 auto;width:44px}
.incico{width:22px;height:22px;border-radius:6px;display:flex;align-items:center;justify-content:center;flex:0 0 auto}
.incb{flex:1;min-width:0}
.inct1{display:block;font-size:12.5px;color:#303D4A;line-height:1.35}
.inct2{display:block;font-size:11px;color:${C.mut};font-family:${MONO};margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ahead,.arow{display:grid;grid-template-columns:minmax(190px,1.5fr) minmax(130px,1fr) 190px minmax(170px,1.2fr) 90px 96px;gap:12px}
.ahead{padding:9px 16px;background:#F7F9FB;border-bottom:1px solid ${C.line};font-size:10.5px;font-weight:600;
  letter-spacing:.07em;text-transform:uppercase;color:${C.mut}}
.arow{padding:0 16px;border-bottom:1px solid ${C.div};align-items:center;min-height:46px}
.arow:hover{background:#F4F6F8}
.acell{display:flex;align-items:center;gap:9px;min-width:0}
.aname{font-size:12.5px;font-weight:500;color:${C.ink};overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.amono{font-size:11.5px;color:${C.mut2};font-family:${MONO};overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.areason{font-size:11.5px;color:${C.mut2};overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mtools{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.search{display:flex;align-items:center;gap:8px;height:34px;padding:0 11px;background:${C.card};
  border:1px solid #DDE3E9;border-radius:7px;flex:1 1 220px;max-width:340px}
.search input{flex:1;min-width:0;background:transparent;border:0;outline:none;color:${C.ink};font-size:12.5px}
.chips{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 10px;border-radius:14px;
  border:1px solid #DDE3E9;background:${C.card};font-size:11.5px;cursor:pointer;white-space:nowrap}
.thead{display:flex;align-items:center;padding:9px 14px;background:#F7F9FB;border-bottom:1px solid ${C.line};
  font-size:10.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:${C.mut}}
.trow{display:flex;align-items:center;min-height:44px;padding:0 14px;border-bottom:1px solid ${C.bg};
  cursor:pointer;position:relative}
.trow:hover{background:#F4F7FA}
.trow.sel{background:${soft(C.accent, .05)}}
.tmark{position:absolute;left:0;top:0;bottom:0;width:2px}
.tname{flex:1;min-width:0;display:flex;align-items:center;gap:8px}
.tchev{width:18px;height:18px;flex:0 0 auto;border:0;background:transparent;padding:0;color:#6E7985;cursor:pointer;
  display:flex;align-items:center;justify-content:center;border-radius:4px}
.tchev:hover{background:${C.line};color:${C.ink}}
.tdot{width:18px;flex:0 0 auto;display:flex;justify-content:center}
.tdot:after{content:'';width:5px;height:5px;border-radius:50%;background:#D0D8DF}
.tlabel{min-width:0;display:flex;flex-direction:column;gap:1px;padding:6px 0}
.tmeta{font-size:11px;color:${C.mut3};overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tval{width:126px;flex:0 0 auto;font-family:${MONO};font-size:11.5px;color:${C.ink3};
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tnode{width:104px;flex:0 0 auto;font-family:${MONO};font-size:11.5px;color:${C.mut3};
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tfoot{padding:10px 14px;background:#F7F9FB;border-top:1px solid ${C.line}}
.warnbox{display:flex;gap:9px;padding:10px 11px;border-radius:8px;background:${soft(C.stale, .09)};
  border:1px solid ${soft(C.stale, .3)};font-size:11.5px;color:#7A5D00;line-height:1.5}
.crumbs{display:flex;align-items:center;gap:7px;font-size:11.5px;color:${C.mut3};margin-bottom:11px;flex-wrap:wrap}
.lnk{border:0;background:transparent;color:${C.accent};font-size:11.5px;cursor:pointer;padding:0}
.dhead{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding-bottom:14px}
.dico{width:44px;height:44px;border-radius:11px;display:flex;align-items:center;justify-content:center;flex:0 0 auto}
.subnav{display:flex;gap:2px;border-bottom:1px solid ${C.line};overflow-x:auto}
.subnav .tab{height:38px}
.ehead,.erow{display:grid;grid-template-columns:minmax(200px,2fr) 110px minmax(150px,1.4fr) 120px 110px;gap:12px}
.ehead{padding:9px 16px;background:#F7F9FB;border-bottom:1px solid ${C.line};font-size:10.5px;font-weight:600;
  letter-spacing:.07em;text-transform:uppercase;color:${C.mut}}
.erow{padding:9px 16px;border-bottom:1px solid ${C.div};align-items:center;font-size:12.5px}
.erow:hover{background:#F4F6F8}
.ename2{min-width:0;overflow:hidden}
.dhead2,.drow{display:grid;grid-template-columns:70px 150px minmax(120px,1fr) minmax(160px,1.2fr) auto;gap:12px}
.dhead2{padding:9px 16px;background:#F7F9FB;border-bottom:1px solid ${C.line};font-size:10.5px;font-weight:600;
  letter-spacing:.07em;text-transform:uppercase;color:${C.mut}}
.drow{padding:9px 16px;border-bottom:1px solid ${C.div};align-items:center;font-size:12px}
.drow:hover{background:#F4F6F8}
.frow{display:grid;grid-template-columns:minmax(200px,2fr) 140px minmax(120px,1fr) auto;gap:12px;
  padding:11px 16px;border-bottom:1px solid ${C.div};align-items:center}
.frow:hover{background:#F4F6F8}
.dayhdr{padding:9px 16px;background:#F7F9FB;border-bottom:1px solid ${C.line};font-size:11px;font-weight:600;
  letter-spacing:.06em;text-transform:uppercase;color:${C.mut}}
.tlrow{display:flex;gap:11px;padding:10px 16px;border-bottom:1px solid ${C.div};align-items:center}
.tlrow:hover{background:#F4F6F8}
.logrow{display:flex;gap:10px;padding:5px 10px;border-radius:6px;align-items:baseline;font-family:${MONO};font-size:11.5px}
.logrow:nth-child(odd){background:#F7F9FB}
.lvl{flex:0 0 auto;width:58px;font-weight:700;color:${C.mut}}
.logrow.warning .lvl{color:${C.warn}}
.logrow.error .lvl{color:${C.bad}}
.logrow.info .lvl{color:${C.blue}}
.lmsg{white-space:pre-wrap;word-break:break-word;color:${C.ink2}}
.mini3{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;text-align:center}
.mini3 div{background:#F7F9FB;border:1px solid ${C.div};border-radius:8px;padding:10px 6px}
.mini3 b{display:block;font-family:${MONO};font-size:19px;font-weight:600}
.mini3 span{font-size:10.5px;color:${C.mut}}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:${C.bg}}
::-webkit-scrollbar-thumb{background:#DDE3E9;border-radius:6px;border:2px solid ${C.bg}}
::-webkit-scrollbar-thumb:hover{background:#ABB5BF}
`;

// Регистрация имени - один раз на документ. При обновлении интеграции Home
// Assistant подгружает новый модуль в ту же страницу, и повторный define
// бросает исключение, обрывая выполнение остатка модуля: до полной
// перезагрузки страницы продолжал бы работать старый класс.
if (!customElements.get(PANEL_TAG)) {
  customElements.define(PANEL_TAG, BmsControlCenter);
}