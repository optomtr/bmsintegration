/**
 * Ручная калибровка цвета ленты - на глаз, без камеры.
 *
 * Камера дала владельцу «не то», и он попросил подгонять цвета сам. Он
 * выбирает, каким цвет должен быть, лента показывает команду, он двигает
 * ползунки, пока лента не станет такой же, как кружок на экране, и сохраняет
 * это точкой. Цвета между точками сервер берёт плавно - и любой цвет, и белый,
 * и температуру. Пока есть хоть одна ручная точка, калибровка камерой не
 * используется.
 *
 * Панель грузит модуль лениво и даёт ему только ws(name, payload),
 * toast(text, bad) и onClose(changed). Сверху - чистые функции: они
 * проверяются в node (tests/test_manual_colors_js.mjs) без браузера.
 * Модуль самостоятельный, как calibration.js: относительный import того
 * файла пришёл бы без ?v= панели и мог бы достаться из вчерашнего кэша.
 */

// ====================================================== чистые функции ==
export const DEFAULT_MAX_POINTS = 24;
const LABEL_MAX = 60;
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const round1 = (v) => Math.round(v * 10) / 10;

/** Оттенок по кругу: 360° - это снова 0°. Не число - 0. */
export function wrapHue(h) {
  const n = Number(h);
  if (!Number.isFinite(n)) return 0;
  const v = round1(((n % 360) + 360) % 360);
  return v >= 360 ? 0 : v;
}

export function clampSat(s) {
  const n = Number(s);
  return Number.isFinite(n) ? round1(Math.min(100, Math.max(0, n))) : 0;
}

/** Кнопки «‹ ›»: шаг 1° / 1 %. Оттенок идёт по кругу (359° -> 0°),
 *  насыщенность упирается в 0 и 100. */
export const stepHue = (h, dir) => wrapHue(Number(h) + Math.sign(Number(dir) || 0));
export const stepSat = (s, dir) => clampSat(Number(s) + Math.sign(Number(dir) || 0));

/** hs от сервера - только два конечных числа; остальное не цвет. */
export function normHs(hs) {
  if (!Array.isArray(hs) || hs.length < 2 || !isNum(hs[0]) || !isNum(hs[1])) return null;
  return [wrapHue(hs[0]), clampSat(hs[1])];
}

// hs - как в Home Assistant: оттенок 0..360, насыщенность 0..100, яркость полная.
export function hsToCss(hs) {
  const h = ((((Number(hs?.[0]) || 0) % 360) + 360) % 360) / 60;
  const s = Math.min(1, Math.max(0, (Number(hs?.[1]) || 0) / 100));
  const f = (n) => { const k = (n + h) % 6; return Math.round(255 * (1 - s * Math.max(0, Math.min(k, 4 - k, 1)))); };
  return `rgb(${f(5)},${f(3)},${f(1)})`;
}

/** Одна цель: насыщенность и оттенок в пределах полградуса; у белого
 *  оттенка нет - любые два почти белых считаются одной целью. */
export function sameHs(a, b) {
  if (!a || !b) return false;
  if (Math.abs(a[1] - b[1]) >= 0.5) return false;
  if (a[1] < 0.5 && b[1] < 0.5) return true;
  const d = Math.abs(wrapHue(a[0]) - wrapHue(b[0]));
  return Math.min(d, 360 - d) < 0.5;
}

const HUES = [[15, "красный"], [45, "оранжевый"], [70, "жёлтый"], [160, "зелёный"], [200, "голубой"],
  [255, "синий"], [290, "фиолетовый"], [345, "розовый"], [361, "красный"]];

/** Название цвета для своей точки: «Оранжевый», «Светло-синий», «Белый». */
export function colourLabel(hs) {
  const h = wrapHue(hs?.[0]), s = clampSat(hs?.[1]);
  if (s < 15) return "Белый";
  const name = HUES.find(([edge]) => h < edge)[1];
  return s < 70 ? `Светло-${name}` : name[0].toUpperCase() + name.slice(1);
}

const cleanLabel = (v) => (typeof v === "string" ? v.trim().slice(0, LABEL_MAX) : "");

export function normPoint(p) {
  const target = normHs(p?.target), device = normHs(p?.device);
  if (!target || !device) return null;
  return { target, device, label: cleanLabel(p.label) || colourLabel(target) };
}

export const normPoints = (list) => (Array.isArray(list) ? list : []).map(normPoint).filter(Boolean);

// Образец приходит от сервера готовым; попадает в style - поэтому только
// строгий #rrggbb, остальное рисуем сами.
const HEX = /^#[0-9a-f]{6}$/i;

export function normPresets(list) {
  const out = [];
  for (const p of Array.isArray(list) ? list : []) {
    const target = normHs(p?.target);
    if (!target) continue;
    out.push({ key: typeof p.key === "string" ? p.key : "", label: cleanLabel(p.label) || colourLabel(target), target,
      swatch: typeof p.swatch === "string" && HEX.test(p.swatch) ? p.swatch : hsToCss(target) });
  }
  return out;
}

/** «1 цвет», «3 цвета», «24 цвета», «5 цветов». */
export function colours(n) {
  const m10 = n % 10, m100 = n % 100;
  const w = m10 === 1 && m100 !== 11 ? "цвет" : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? "цвета" : "цветов";
  return `${n} ${w}`;
}

export function maxPoints(info) {
  const m = Number(info?.max_points);
  return Number.isInteger(m) && m > 0 ? m : DEFAULT_MAX_POINTS;
}

/** Кружок «так должно выглядеть»: у пресета свой образец (тёплый белый по
 *  кельвинам точнее, чем hs), у своего цвета - круг Home Assistant. */
export function targetSwatch(target, presets) {
  const p = (presets || []).find((x) => sameHs(x.target, target));
  return p ? p.swatch : hsToCss(target);
}

export const findPoint = (points, target) => (points || []).findIndex((p) => sameHs(p.target, target));
export const canAdd = (points, max = DEFAULT_MAX_POINTS) => (points || []).length < max;
export const freePresets = (presets, points) => (presets || []).filter((p) => findPoint(points, p.target) < 0);

/**
 * Точку - в черновик. index - правится эта точка; без него точка с той же
 * целью заменяется, а не дублируется: две точки на одну цель сервер не
 * различит. Возвращает { points, index } или null - список полон.
 */
export function upsertPoint(points, point, index = null, max = DEFAULT_MAX_POINTS) {
  const list = (points || []).slice();
  let at = Number.isInteger(index) && index >= 0 && index < list.length ? index : findPoint(list, point.target);
  if (at < 0) {
    if (list.length >= max) return null;
    list.push(point);
    return { points: list, index: list.length - 1 };
  }
  list[at] = point;
  for (let i = list.length - 1; i >= 0; i--) {
    if (i !== at && sameHs(list[i].target, point.target)) { list.splice(i, 1); if (i < at) at--; }
  }
  return { points: list, index: at };
}

export function removePoint(points, index) {
  const list = (points || []).slice();
  if (Number.isInteger(index) && index >= 0 && index < list.length) list.splice(index, 1);
  return list;
}

const pointKey = (p) => `${p.target[0]},${p.target[1]}|${p.device[0]},${p.device[1]}|${p.label}`;
export const samePoints = (a, b) => (a || []).length === (b || []).length
  && (a || []).every((p, i) => pointKey(p) === pointKey(b[i]));

/** Копии, а не ссылки: черновик правится, пока запрос ещё в пути. */
export const pointsPayload = (points) => (points || []).map((p) =>
  ({ target: [p.target[0], p.target[1]], device: [p.device[0], p.device[1]], label: p.label }));
export const showPayload = (entityId, hs) => ({ entity_id: entityId, hs: [hs[0], hs[1]] });
export const commandPayload = (entityId, target, points) =>
  ({ entity_id: entityId, target: [target[0], target[1]], points: pointsPayload(points) });

/**
 * Предпросмотр на ленте: последнее значение побеждает. Тот же планировщик,
 * что в calibration.js (там его история и проверки).
 *
 * Ползунок присылает десятки значений в секунду, а лента отвечает за сотни
 * миллисекунд. Очередь команд проигрывала бы на стене всю историю движения
 * пальца с опозданием в секунды. Поэтому: дребезг delay мс, в пути не больше
 * одной команды, а ждёт только самое новое значение.
 *
 * schedule(v) - после дребезга; now(v) - сразу (кнопки); cancel() - забыть
 * ждущее и вернуть промис команды, что уже в пути: «end» должен идти после неё.
 * send(v, live) получает live(): false - отправку отменили, пока она ждала.
 */
export function latestWins(send, opts = {}) {
  const {
    delay = 200, equals = (a, b) => a === b,
    onState = () => {}, onSent = () => {}, onError = () => {},
    setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (h) => clearTimeout(h),
  } = opts;
  let timer = null, latest, has = false, ready = false;
  let flying = null, shown, hasShown = false, epoch = 0;

  const report = () => onState({ sending: !!flying, waiting: has });
  const drop = () => {
    if (timer !== null) { clearTimer(timer); timer = null; }
    latest = undefined; has = false; ready = false;
  };

  function pump() {
    if (flying || !has || !ready) return;
    const value = latest;
    drop();
    // То же, что уже на ленте, не шлём. После ошибки лента «неизвестна» -
    // и повтор того же значения уходит.
    if (hasShown && equals(value, shown)) return;
    const mine = epoch;
    const promise = Promise.resolve().then(() => send(value, () => mine === epoch));
    flying = promise;
    promise.then(
      () => { if (mine === epoch) { shown = value; hasShown = true; onSent(value); } },
      (err) => { if (mine === epoch) { hasShown = false; onError(err, value); } },
    ).finally(() => {
      if (flying === promise) flying = null;
      pump();
      report();
    });
  }

  function put(value, immediate) {
    drop();
    latest = value; has = true;
    if (immediate || !(delay > 0)) ready = true;
    else timer = setTimer(() => { timer = null; ready = true; pump(); report(); }, delay);
    pump();
    report();
  }

  return {
    schedule: (value) => put(value, false),
    now: (value) => put(value, true),
    cancel() {
      epoch++;
      drop();
      hasShown = false;
      report();
      return flying ? flying.then(() => {}, () => {}) : Promise.resolve();
    },
    get state() { return { sending: !!flying, waiting: has }; },
  };
}

// =========================================================== интерфейс ==
const TAG = "bms-manual-colors";
// Сервер сам возвращает ленту, если от экрана калибровки тихо 10 минут
// (calibration_ws.ABANDONED_AFTER), и забывает снимок. После долгой паузы
// «begin» уходит снова: он же продлевает этот срок и, если снимок уже
// забыт, снимает новый - с ленты, которую сервер только что вернул.
const KEEPALIVE_MS = 5 * 60 * 1000;
const PREVIEW_STEPS = ["edit", "test"];

class Cancelled extends Error {}

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const btn = (label, act, kind = "", extra = "") =>
  `<button type="button" class="b ${kind}" data-act="${act}" ${extra}>${label}</button>`;
// «3000 K» не рвётся между числом и единицей на узком телефоне.
const NB = " ";
const nbK = (t) => String(t ?? "").replace(/(\d) (K|К)(?!\p{L})/gu, `$1${NB}$2`);
const realSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const hueText = (h) => `${Math.round(wrapHue(h)) % 360}°`;
const satText = (s) => `${Math.round(clampSat(s))}%`;
const hsWords = (hs) => `оттенок ${hueText(hs[0])}, насыщенность ${satText(hs[1])}`;
const ARROW = `<svg class="arr" width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h14M13 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const CHEV_L = `<svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const CHEV_R = `<svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

function errorText(err) {
  if (err?.code === "unknown_command") {
    return "Сервер не знает ручную калибровку — обновите интеграцию BMS и перезапустите Home Assistant.";
  }
  if (err === 3 || err?.code === 3) return "Пропала связь с Home Assistant. Проверьте сеть и попробуйте ещё раз.";
  return err?.message || (typeof err === "string" ? err : "") || "Неизвестная ошибка";
}

const CSS = `
:host{all:initial;display:block;position:fixed;inset:0;z-index:2147483000}
*{box-sizing:border-box}
.wrap{position:absolute;inset:0;display:flex;flex-direction:column;overflow-y:auto;overscroll-behavior:contain;
  background:#0D1319;color:#EEF2F6;font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-text-size-adjust:100%;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}
.bar{display:flex;align-items:center;gap:12px;padding:8px 6px 8px 16px;border-bottom:1px solid #1F2933;position:sticky;top:0;background:#0D1319;z-index:1}
.ttl{flex:1;min-width:0}.h{font-size:16px;font-weight:650}
.sub{font-size:13px;color:#9AA7B4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.x{width:44px;height:44px;border-radius:12px;border:0;background:transparent;color:#C9D3DD;display:grid;place-items:center;cursor:pointer;flex:none}
.x:hover{background:#1A232D}
.body{width:100%;max-width:560px;margin:0 auto;padding:16px;display:flex;flex-direction:column;gap:16px}
#c{display:flex;flex-direction:column;gap:14px}
h2{font-size:21px;line-height:1.25;margin:0;font-weight:700;outline:none;overflow-wrap:anywhere}
p{margin:0}.mut{color:#9AA7B4;font-size:14px}
.acts{display:flex;flex-direction:column;gap:10px}
.b{min-height:48px;width:100%;padding:10px 18px;border-radius:12px;border:1px solid #2B3744;background:#18212B;color:#EEF2F6;
  font:inherit;font-size:16px;font-weight:600;cursor:pointer;touch-action:manipulation}
.b.pri{background:#006FFF;border-color:#006FFF;color:#fff}
.b.dan{background:transparent;border-color:#6A2B35;color:#FF8592}
.b.link{background:transparent;border-color:transparent;color:#8DBBFF;min-height:44px}
.b:disabled{opacity:.45;cursor:not-allowed}.b[hidden]{display:none}
button:focus-visible,input:focus-visible,[tabindex="0"]:focus-visible{outline:3px solid #8DBBFF;outline-offset:2px}
.card{background:#141C25;border:1px solid #222D39;border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:12px}
.row{display:flex;align-items:center;gap:12px;min-width:0}.grow{flex:1;min-width:0}
.sw{width:28px;height:28px;border-radius:50%;border:1px solid rgba(255,255,255,.3);flex:none;display:inline-block}
.rainbow{background:conic-gradient(#f00,#ff0,#0f0,#0ff,#00f,#f0f,#f00)}
.note{border-left:3px solid #F0B429;padding:2px 0 2px 10px;color:#E8D6A8;font-size:14px}
.err{border-left:3px solid #FF5A6A;padding:2px 0 2px 10px;color:#FFB3BB;font-size:14px}
.err[hidden],.mut[hidden]{display:none}
.lh{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#9AA7B4}
.pts{display:flex;flex-direction:column}
.pt{display:flex;flex-direction:column;gap:10px;padding:12px 0;border-top:1px solid #222D39}
.pt:first-child{border-top:0;padding-top:0}.pt:last-child{padding-bottom:0}
.pair{display:inline-flex;align-items:center;gap:4px;flex:none;color:#9AA7B4}
.lbl{font-weight:600;overflow-wrap:anywhere}
.btns{display:flex;gap:8px}
.btns .b{min-height:44px;padding:8px 12px;font-size:15px;flex:1 1 0}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:8px;min-height:44px;max-width:100%;padding:6px 14px 6px 8px;border-radius:999px;
  border:1px solid #2B3744;background:#18212B;color:#EEF2F6;font:inherit;font-size:15px;font-weight:600;line-height:1.2;
  text-align:left;cursor:pointer;touch-action:manipulation}
.chip .sw{width:26px;height:26px}
.chip[aria-pressed=true]{border-color:#006FFF;background:#12263F;box-shadow:inset 0 0 0 1px #006FFF}
.chip:disabled{opacity:.45;cursor:not-allowed}
.cmp{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:0}
.cmp figure{margin:0;display:flex;flex-direction:column;align-items:center;gap:8px;text-align:center;min-width:0}
.big{width:min(26vw,104px);aspect-ratio:1;border-radius:50%;border:1px solid rgba(255,255,255,.3);display:block}
.big.wait{background:transparent!important;border:2px dashed #3A4756}
.cmp figcaption{font-size:14px;color:#C9D3DD;font-weight:600}
.sl{display:flex;flex-direction:column;gap:4px}
.sl .val{font-weight:650}.num{font-weight:650;font-variant-numeric:tabular-nums}
.slr{display:flex;align-items:center;gap:6px}
.nb{width:48px;height:48px;flex:none;border-radius:12px;border:1px solid #2B3744;background:#18212B;color:#EEF2F6;
  display:grid;place-items:center;cursor:pointer;touch-action:manipulation;padding:0}
.nb:disabled{opacity:.45;cursor:not-allowed}
input[type=range]{-webkit-appearance:none;appearance:none;flex:1;min-width:0;height:48px;margin:0;background:transparent;cursor:pointer}
input[type=range]:disabled{opacity:.45;cursor:not-allowed}
input[type=range]::-webkit-slider-runnable-track{height:12px;border-radius:6px;background:var(--track)}
input[type=range]::-moz-range-track{height:12px;border-radius:6px;background:var(--track)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:32px;height:32px;margin-top:-10px;border-radius:50%;
  background:#fff;border:3px solid #0D1319;box-shadow:0 0 0 2px #fff,0 1px 4px rgba(0,0,0,.55)}
input[type=range]::-moz-range-thumb{width:26px;height:26px;border-radius:50%;background:#fff;border:3px solid #0D1319}
.rng.h{--track:linear-gradient(90deg,#f00,#ff0 16.667%,#0f0 33.333%,#0ff 50%,#00f 66.667%,#f0f 83.333%,#f00)}
.rng.s{--track:linear-gradient(90deg,#fff,var(--hue,#f00))}
.pv{display:flex;align-items:center;gap:10px;min-height:24px}
.pv .mut{flex:1}
.pv .b.link{width:auto;flex:none;padding:6px 10px}
.toast{position:fixed;left:50%;bottom:calc(20px + env(safe-area-inset-bottom));transform:translateX(-50%);z-index:3;max-width:calc(100% - 32px);
  background:#EEF2F6;color:#0D1319;padding:10px 16px;border-radius:10px;font-size:14px;font-weight:600}
.toast[hidden]{display:none}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
`;

const SHELL = `<style>${CSS}</style>
<div class="wrap" role="dialog" aria-modal="true" aria-labelledby="t">
  <div class="bar"><div class="ttl"><div class="h" id="t">Ручная калибровка</div><div class="sub" id="name"></div></div>
    <button type="button" class="x" data-act="close" aria-label="Закрыть ручную калибровку">
      <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>
    </button></div>
  <div class="body"><div id="c"></div></div>
</div>
<div class="toast" id="toast" role="status" hidden></div>
<div class="sr" id="sr" role="status" aria-live="polite"></div>`;

function defineOverlay() {
  if (customElements.get(TAG)) return;
  customElements.define(TAG, class extends HTMLElement {
    setup(o) {
      this.o = o;
      this.id = o.entityId;
      Object.assign(this, { step: "loading", info: null, presets: [], max: DEFAULT_MAX_POINTS, saved: [], draft: [],
        e: null, c: null, t: null, back: "list", err: "", saving: false, changed: false, closed: false,
        begun: false, begunOk: false, beginning: null, touched: 0, token: null, inflight: null, stopping: null,
        pv: { sending: false, waiting: false }, pvErr: "", paused: false });
      // Один предпросмотр на окно: в пути всегда не больше одной команды ленте.
      this.preview = latestWins((v, live) => this.previewSend(v, live), {
        delay: 200, equals: (a, b) => JSON.stringify(a) === JSON.stringify(b),
        onState: (st) => { this.pv = st; this.paintPreview(); },
        onSent: () => { this.pvErr = ""; this.paused = false; this.paintPreview(); },
        onError: (err) => {
          if (err instanceof Cancelled || this.closed) return;
          console.warn("BMS ручная калибровка: предпросмотр", err);
          this.pvErr = `Лента не ответила: ${errorText(err)}`;
          if (this.shadowRoot) this.$("sr").textContent = this.pvErr;
          this.paintPreview();
        },
      });
    }

    connectedCallback() {
      if (this.shadowRoot) return;
      const root = this.attachShadow({ mode: "open" });
      root.innerHTML = SHELL;
      this.$ = (id) => root.getElementById(id);
      this.$("name").textContent = this.o.name || this.id;
      root.addEventListener("click", (e) => this.onClick(e));
      root.addEventListener("input", (e) => this.onSlide(e, "drag"));
      root.addEventListener("change", (e) => this.onSlide(e, "release"));
      root.addEventListener("keydown", (e) => this.onKey(e));
      this._onVis = () => (document.visibilityState === "hidden" ? this.interrupt() : this.resume());
      this._onHide = () => this.interrupt();
      document.addEventListener("visibilitychange", this._onVis);
      window.addEventListener("pagehide", this._onHide);
      // Человек смотрит на ленту, а не на телефон: экран не должен гаснуть.
      this.lockWake();
      this.render();
      this.loadInfo();
    }

    disconnectedCallback() { this.close(); }

    // ---------------------------------------------------------- отрисовка --
    go(step, focus = true) { this.step = step; this.render(focus); }

    render(focus = true) {
      if (!this.shadowRoot) return;
      const c = this.$("c");
      c.innerHTML = this.view();
      // Перерисовка по месту: фокус возвращается на ту же кнопку, а не
      // теряется на странице под окном.
      const el = typeof focus === "string" ? c.querySelector(focus) : null;
      if (el) el.focus();
      else if (focus) c.querySelector("h2")?.focus();
      if (["edit", "custom", "test"].includes(this.step)) this.paintHs(true);
      if (PREVIEW_STEPS.includes(this.step)) this.paintPreview();
    }

    view() {
      const s = this.step, head = (t) => `<h2 tabindex="-1">${t}</h2>`;
      const error = this.err ? `<p class="err">${esc(this.err)}</p>` : "";
      const off = this.saving ? "disabled" : "";
      if (s === "loading") return `${head("Загрузка…")}<p class="mut">Узнаю, какие цвета уже настроены.</p>`;
      if (s === "fatal") {
        return `${head("Ручная калибровка недоступна")}${error}
          <div class="acts">${btn("Повторить", "retry", "pri")}${btn("Закрыть", "close-now")}</div>`;
      }
      if (s === "unsupported") {
        return `${head("Ручная калибровка недоступна")}<p>${esc(this.info?.reason || "Эта лента не умеет менять цвет.")}</p>
          <div class="acts">${btn("Закрыть", "close-now")}</div>`;
      }
      if (s === "list") return this.viewList(error, off);
      if (s === "edit") return this.viewEdit(error);
      if (s === "custom") return this.viewCustom();
      if (s === "test") return this.viewTest();
      if (s === "confirm-reset") {
        const cam = !!this.info?.has_camera_calibration;
        return `${head("Сбросить ручную калибровку?")}
          <p>Все ваши цвета удалятся${cam ? ", и снова заработает калибровка камерой." : ", и лента вернётся к цветам без поправки."}</p>
          ${error}
          <div class="acts">${btn(this.saving ? "Сбрасываю…" : "Сбросить", "reset", "dan", off)}${btn("Отмена", "list", "", off)}</div>`;
      }
      if (s === "confirm-close") {
        return `${head("Закрыть без сохранения?")}<p>Изменения в списке цветов пропадут, лента вернётся как была.</p>
          <div class="acts">${btn("Закрыть без сохранения", "close-now", "dan")}${btn("Вернуться", "close-back")}</div>`;
      }
      return "";
    }

    viewList(error, off) {
      const d = this.draft, cam = !!this.info?.has_camera_calibration, dirty = this.isDirty();
      const rows = d.map((p, i) => {
        const label = esc(nbK(p.label));
        return `<div class="pt">
          <div class="row">
            <span class="pair" role="img" aria-label="Должно быть: ${esc(p.label)}. Лента получает: ${hsWords(p.device)}">
              <span class="sw" style="background:${targetSwatch(p.target, this.presets)}"></span>${ARROW}
              <span class="sw" style="background:${hsToCss(p.device)}"></span></span>
            <span class="grow lbl">${label}</span></div>
          <div class="btns">${btn("Изменить", "edit", "", `data-i="${i}" aria-label="Изменить: ${label}" ${off}`)}
            ${btn("Удалить", "remove", "dan", `data-i="${i}" aria-label="Удалить: ${label}" ${off}`)}</div></div>`;
      }).join("");
      const free = freePresets(this.presets, d);
      const add = !canAdd(d, this.max)
        ? `<p class="note">Это предел — ${colours(this.max)}. Удалите лишний, чтобы добавить новый.</p>`
        : `<div class="chips" role="group" aria-labelledby="add-h">${free.map((p) =>
            `<button type="button" class="chip" data-act="add-preset" data-k="${this.presets.indexOf(p)}" ${off}>
              <span class="sw" style="background:${p.swatch}"></span>${esc(nbK(p.label))}</button>`).join("")}
            <button type="button" class="chip" data-act="custom" ${off}><span class="sw rainbow"></span>Свой цвет</button></div>`;
      return `<h2 tabindex="-1">Ваши цвета</h2>
        <p>Выберите цвет, подгоните ползунками, пока лента не станет такой же, как кружок на экране, и сохраните. Цвета между вашими лента возьмёт плавно.</p>
        ${cam ? `<p class="note">Пока здесь есть цвета, калибровка камерой не используется.</p>` : ""}
        <div class="card"><div class="row"><span class="lh grow">Цвета</span>${d.length ? `<span class="mut">${d.length} из ${this.max}</span>` : ""}</div>
          ${rows ? `<div class="pts">${rows}</div>` : `<p class="mut">Пока нет ни одного цвета. Начните с белого.</p>`}</div>
        <div class="card"><div class="lh" id="add-h">Добавить цвет</div>${add}</div>
        ${btn("Проверить любой цвет", "test", "", off)}
        ${dirty ? `<p class="note">Изменения ещё не сохранены — нажмите «Сохранить всё».</p>` : ""}
        ${error}
        <div class="acts">${dirty ? btn(this.saving ? "Сохраняю…" : "Сохранить всё", "save-all", "pri", off) : ""}
          ${this.saved.length ? btn("Сбросить ручную калибровку", "reset-ask", "dan", off) : ""}
          ${btn(dirty ? "Закрыть без сохранения" : "Закрыть", "close", "", off)}</div>`;
    }

    slider(k, off = "") {
      const hue = k === "h", label = hue ? "Оттенок" : "Насыщенность", unit = hue ? "°" : "%";
      return `<div class="sl">
        <div class="row"><span class="grow val" id="${k}-l">${label}</span><span class="num" data-p="${k}-v"></span></div>
        <div class="slr">
          <button type="button" class="nb" data-act="step" data-k="${k}" data-d="-1" aria-label="${label}: меньше на 1${unit}" ${off}>${CHEV_L}</button>
          <input type="range" class="rng ${k}" data-k="${k}" min="0" max="${hue ? 360 : 100}" step="1" value="0" aria-labelledby="${k}-l" ${off}>
          <button type="button" class="nb" data-act="step" data-k="${k}" data-d="1" aria-label="${label}: больше на 1${unit}" ${off}>${CHEV_R}</button>
        </div></div>`;
    }

    viewPreviewLine() {
      return `<div class="pv"><p class="mut" data-p="pv"></p>${btn("Показать снова", "pv-again", "link", "hidden")}</div>
        <p class="err" data-p="pverr" hidden></p>`;
    }

    viewEdit(error) {
      const e = this.e, off = e.loading ? "disabled data-wait" : "";
      return `<h2 tabindex="-1">${esc(nbK(e.label))}</h2>
        <p class="mut">Смотрите на ленту и двигайте ползунки, пока она не станет такой же, как кружок «Так должно выглядеть».</p>
        <div class="cmp">
          <figure><span class="big" role="img" style="background:${targetSwatch(e.target, this.presets)}" aria-label="Так должно выглядеть: ${esc(e.label)}"></span>
            <figcaption>Так должно выглядеть</figcaption></figure>
          <figure><span class="big" role="img" data-p="dev"></span><figcaption>Что получает лента</figcaption></figure>
        </div>
        <div class="card">${this.slider("h", off)}${this.slider("s", off)}${this.viewPreviewLine()}</div>
        ${e.note ? `<p class="note">${esc(e.note)}</p>` : ""}${error}
        <div class="acts">${btn("Сохранить цвет", "edit-save", "pri", off)}${btn("Как без поправки", "as-is", "", off)}
          ${btn("Отмена", "back")}</div>`;
    }

    viewCustom() {
      return `<h2 tabindex="-1">Свой цвет</h2>
        <p class="mut">Выберите, каким цвет должен быть. Потом подгоните под него ленту.</p>
        <div class="cmp"><figure style="grid-column:1/-1"><span class="big" role="img" data-p="tgt"></span>
          <figcaption>Так должно выглядеть</figcaption></figure></div>
        <div class="card">${this.slider("h")}${this.slider("s")}</div>
        <div class="acts">${btn("Дальше", "custom-next", "pri")}${btn("Отмена", "back")}</div>`;
    }

    viewTest() {
      const t = this.t;
      return `<h2 tabindex="-1">Проверить любой цвет</h2>
        <p class="mut">Лента покажет цвет так, как после сохранения ваших цветов — даже ещё не сохранённых. Так видно, как выходят цвета между ними.</p>
        <div class="chips" role="group" aria-label="Готовые цвета">${this.presets.map((p, i) =>
          `<button type="button" class="chip" data-act="test-preset" data-k="${i}" aria-pressed="${t.preset === i}">
            <span class="sw" style="background:${p.swatch}"></span>${esc(nbK(p.label))}</button>`).join("")}</div>
        <div class="cmp">
          <figure><span class="big" role="img" data-p="tgt"></span><figcaption>Так должно выглядеть</figcaption></figure>
          <figure><span class="big" role="img" data-p="dev"></span><figcaption>Что получает лента</figcaption></figure>
        </div>
        <div class="card"><div class="lh">Или свой цвет</div>${this.slider("h")}${this.slider("s")}${this.viewPreviewLine()}</div>
        <div class="acts">${btn("Назад к списку", "back")}</div>`;
    }

    /** Подписи и кружки - по месту: перерисовка всего экрана оборвала бы
     *  перетаскивание пальцем. sync - выставить и сами ползунки (кнопки). */
    paintHs(sync = false) {
      const hs = this.cur();
      if (!hs || !this.shadowRoot) return;
      const c = this.$("c"), q = (k) => c.querySelector(`[data-p="${k}"]`);
      for (const input of c.querySelectorAll("input[data-k]")) {
        const hue = input.dataset.k === "h";
        if (sync) input.value = String(hue ? hs[0] : hs[1]);
        input.setAttribute("aria-valuetext", hue ? `${hueText(hs[0])}, ${colourLabel([hs[0], 100]).toLowerCase()}` : satText(hs[1]));
        if (!hue) input.style.setProperty("--hue", hsToCss([hs[0], 100]));
      }
      if (q("h-v")) q("h-v").textContent = hueText(hs[0]);
      if (q("s-v")) q("s-v").textContent = satText(hs[1]);
      const paint = (el, col, label) => {
        if (!el) return;
        el.classList.toggle("wait", !col);
        el.style.background = col ? hsToCss(col) : "";
        el.setAttribute("aria-label", label);
      };
      if (this.step === "edit") {
        paint(q("dev"), this.e.loading ? null : hs, this.e.loading ? "Что получает лента: считаю…" : `Что получает лента: ${hsWords(hs)}`);
      } else if (this.step === "custom") {
        paint(q("tgt"), hs, `Так должно выглядеть: ${colourLabel(hs).toLowerCase()}`);
      } else if (this.step === "test") {
        const t = this.t, preset = this.presets[t.preset];
        const tgt = q("tgt");
        paint(tgt, hs, `Так должно выглядеть: ${preset ? preset.label : colourLabel(hs).toLowerCase()}`);
        if (tgt && preset) tgt.style.background = preset.swatch;
        paint(q("dev"), t.device, t.device ? `Что получает лента: ${hsWords(t.device)}` : "Что получает лента: считаю…");
        for (const b of c.querySelectorAll('[data-act="test-preset"]')) b.setAttribute("aria-pressed", String(Number(b.dataset.k) === t.preset));
      }
    }

    paintPreview() {
      if (!this.shadowRoot || !PREVIEW_STEPS.includes(this.step)) return;
      const c = this.$("c"), line = c.querySelector('[data-p="pv"]'), bad = c.querySelector('[data-p="pverr"]');
      if (!line) return;
      const busy = this.pv.sending || this.pv.waiting;
      const text = this.step === "edit" && this.e?.loading ? "Считаю, с чего начать…"
        : busy ? "Обновляю ленту…"
        : this.paused ? "Экран телефона гас — лента вернулась как была."
        : this.pvErr ? ""
        : this.step === "edit" ? "Лента показывает этот вариант."
        : "Лента показывает этот цвет так, как после сохранения.";
      line.textContent = text;
      line.hidden = !text;
      bad.textContent = this.pvErr;
      bad.hidden = busy || !this.pvErr;
      const again = c.querySelector('[data-act="pv-again"]');
      if (again) again.hidden = busy || !(this.paused || this.pvErr);
    }

    notify(text) {
      const t = this.$("toast");
      t.textContent = text; t.hidden = false;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { t.hidden = true; }, 3000);
    }

    say(text) { if (this.shadowRoot) this.$("sr").textContent = text; }

    // ------------------------------------------------------------ события --
    onClick(e) {
      const el = e.composedPath().find((n) => n?.dataset?.act);
      if (!el || el.disabled) return;
      const a = el.dataset.act, i = Number(el.dataset.i), k = Number(el.dataset.k);
      if (a === "close") return this.askClose();
      if (a === "close-now") return this.close();
      if (a === "close-back") return this.goBack();
      if (a === "retry") return this.loadInfo();
      if (a === "list") { this.err = ""; return this.go("list", '[data-act="reset-ask"]'); }
      if (a === "edit") return this.startEdit(i);
      if (a === "remove") return this.removeAt(i);
      if (a === "add-preset" && this.presets[k]) {
        const p = this.presets[k];
        return this.startNew(p.target, p.label, `[data-act="add-preset"][data-k="${k}"]`);
      }
      if (a === "custom") return this.startCustom();
      if (a === "custom-next") return this.startNew(this.c.slice(), colourLabel(this.c), '[data-act="custom"]');
      if (a === "step") {
        const hs = this.cur();
        if (!hs) return;
        const part = el.dataset.k === "h" ? { h: stepHue(hs[0], Number(el.dataset.d)) } : { s: stepSat(hs[1], Number(el.dataset.d)) };
        return this.setHs(part, "button");
      }
      if (a === "as-is" && this.e) return this.setHs({ h: this.e.target[0], s: this.e.target[1] }, "button");
      if (a === "edit-save") return this.commitEdit();
      if (a === "back") return this.backToList();
      if (a === "test") return this.startTest();
      if (a === "test-preset") return this.pickTestPreset(k);
      if (a === "save-all") return this.saveAll();
      if (a === "reset-ask") { this.err = ""; return this.go("confirm-reset"); }
      if (a === "reset") return this.reset();
      if (a === "pv-again") { const v = this.previewValue(); if (v) this.preview.now(v); }
    }

    onSlide(e, how) {
      const el = e.composedPath()[0];
      const k = el?.dataset?.k;
      if (!k || el.type !== "range" || el.disabled) return;
      this.setHs({ [k]: Number(el.value) }, how);
    }

    onKey(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        if (this.saving) return;
        // Подэкраны - слой поверх списка: Esc возвращает к списку, а не закрывает всё.
        if (["edit", "custom", "test"].includes(this.step)) return this.backToList();
        if (this.step === "confirm-reset") { this.err = ""; return this.go("list", '[data-act="reset-ask"]'); }
        if (this.step === "confirm-close") return this.goBack();
        return this.askClose();
      }
      if (e.key !== "Tab") return;
      // Фокус не уходит за окно калибровки на страницу под ним.
      const items = [...this.shadowRoot.querySelectorAll(".wrap button:not([disabled]), .wrap input:not([disabled])")]
        .filter((b) => !b.hidden && b.getClientRects().length);
      if (!items.length) return;
      const active = this.shadowRoot.activeElement, first = items[0], last = items[items.length - 1];
      if (e.shiftKey && (active === first || !items.includes(active))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (active === last || !items.includes(active))) { e.preventDefault(); first.focus(); }
    }

    // ----------------------------------------------------------- ползунки --
    /** hs, которым сейчас управляют ползунки: команда ленте при правке, цель
     *  своего цвета, цель проверки. */
    cur() {
      if (this.step === "edit") return this.e?.device || null;
      if (this.step === "custom") return this.c;
      if (this.step === "test") return this.t?.target || null;
      return null;
    }

    /** Что должно быть на ленте по этому экрану. */
    previewValue() {
      if (this.step === "edit" && this.e && !this.e.loading) return { hs: this.e.device.slice() };
      if (this.step === "test" && this.t) return { target: this.t.target.slice(), points: pointsPayload(this.draft) };
      return null;
    }

    setHs(part, how) {
      const hs = this.cur();
      if (!hs || this.saving || (this.step === "edit" && this.e.loading)) return;
      const next = [part.h === undefined ? hs[0] : wrapHue(part.h), part.s === undefined ? hs[1] : clampSat(part.s)];
      const moved = next[0] !== hs[0] || next[1] !== hs[1];
      if (!moved && how === "drag") return;
      if (this.step === "edit") { this.e.device = next; if (moved) this.e.touched = true; }
      else if (this.step === "custom") this.c = next;
      else if (this.step === "test" && moved) {
        this.t.target = next;
        this.t.device = null;
        this.t.preset = this.presets.findIndex((p) => sameHs(p.target, next));
      }
      this.paintHs(how === "button");
      const v = this.previewValue();
      if (!v) return;
      // Палец на ползунке - с дребезгом; отпустил или нажал кнопку - сразу.
      if (how === "drag") this.preview.schedule(v); else this.preview.now(v);
    }

    // ------------------------------------------------------------- лента --
    newToken() { this.cancelToken(); return (this.token = { cancelled: false }); }
    cancelToken() { if (this.token) this.token.cancelled = true; this.token = null; }

    async call(type, payload, token) {
      if (token.cancelled) throw new Cancelled();
      const p = Promise.resolve().then(() => this.o.ws(type, payload));
      this.inflight = p;
      let res;
      try { res = await p; } finally { if (this.inflight === p) this.inflight = null; }
      if (token.cancelled) throw new Cancelled();
      return res;
    }

    /** «begin» до первого show: сервер запоминает, что лента показывала, и
     *  «end» возвращает именно это. Повторный «begin» снимок не перезаписывает. */
    ensureBegun(token) {
      if (this.begunOk && Date.now() - this.touched < KEEPALIVE_MS) return Promise.resolve();
      if (!this.beginning) {
        this.begun = true;
        const p = this.call("color_calibration/begin", { entity_id: this.id }, token)
          .then(() => { this.begunOk = true; this.touched = Date.now(); })
          .finally(() => { if (this.beginning === p) this.beginning = null; });
        this.beginning = p;
      }
      return this.beginning;
    }

    openSession() {
      this.ensureBegun(this.token || this.newToken()).catch((err) => {
        // Не вышло сразу - повторится перед первым показом, и ошибка будет видна там.
        if (!(err instanceof Cancelled)) console.warn("BMS ручная калибровка: begin", err);
      });
    }

    /** Отправка предпросмотра (её зовёт только latestWins). */
    async previewSend(v, live) {
      if (this.stopping) await this.stopping;
      // Отменённая отправка, проснувшись после «end», не должна открыть
      // сессию снова: лента ушла бы на предпросмотр уже после возврата.
      if (this.closed || this.saving || !live()) throw new Cancelled();
      const token = this.token || this.newToken();
      await this.ensureBegun(token);
      if (!live()) throw new Cancelled();
      let hs = v.hs;
      if (v.target) {
        const res = await this.call("color_points/command", commandPayload(this.id, v.target, v.points), token);
        hs = normHs(res?.device);
        if (!hs) throw new Error("Сервер не прислал команду для этого цвета.");
        if (!live()) throw new Cancelled();
        if (this.step === "test" && this.t && sameHs(this.t.target, v.target)) { this.t.device = hs; this.paintHs(); }
      }
      await this.call("color_calibration/show", showPayload(this.id, hs), token);
      this.touched = Date.now();
    }

    /** Вернуть ленту. Ждёт команду, что уже в пути: иначе «end» обгоняет её
     *  на сервере, и лента остаётся на пробном цвете. */
    stopSession() {
      if (this.stopping) return this.stopping;
      const flying = this.preview.cancel();
      this.cancelToken();
      this.beginning = null;
      if (!this.begun) return Promise.resolve({ ended: false, error: "" });
      this.begun = false;
      this.begunOk = false;
      const pending = this.inflight;
      this.stopping = (async () => {
        await Promise.race([Promise.all([flying, pending ? pending.catch(() => {}) : null]), realSleep(4000)]);
        try {
          await this.o.ws("color_calibration/end", { entity_id: this.id, restore: true });
          return { ended: true, error: "" };
        } catch (err) {
          console.warn("BMS ручная калибровка: лента не вернулась в прежнее состояние", err);
          return { ended: true, error: errorText(err) };
        }
      })().finally(() => { this.stopping = null; });
      return this.stopping;
    }

    /** Экран погас или приложение свернули: ленту возвращаем сразу. */
    interrupt() {
      if (this.closed || !this.begun) return;
      const live = PREVIEW_STEPS.includes(this.step);
      this.stopSession().then((r) => {
        if (!r.error || this.closed || !PREVIEW_STEPS.includes(this.step)) return;
        this.pvErr = `Ленту не удалось вернуть: ${r.error}`;
        this.paintPreview();
      });
      if (live) { this.paused = true; this.paintPreview(); }
    }

    /** Вернулись на экран: правка снова на ленте - лента должна показывать
     *  то, что на ползунках, пока они открыты. */
    resume() {
      if (this.closed) return;
      this.lockWake();
      if (!this.paused || !PREVIEW_STEPS.includes(this.step)) return;
      const v = this.previewValue();
      if (v) this.preview.now(v);
    }

    async lockWake() {
      if (this.wake || !navigator.wakeLock?.request || document.visibilityState === "hidden") return;
      try {
        const lock = await navigator.wakeLock.request("screen");
        if (this.closed) return lock.release().catch(() => {});
        this.wake = lock;
        lock.addEventListener?.("release", () => { if (this.wake === lock) this.wake = null; });
      } catch { /* без блокировки экран может погаснуть - лента тогда вернётся сама */ }
    }
    releaseWake() { const w = this.wake; this.wake = null; w?.release?.().catch?.(() => {}); }

    // ------------------------------------------------------------ сценарий --
    async loadInfo() {
      this.err = "";
      this.go("loading");
      try {
        const info = await this.o.ws("color_points/get", { entity_id: this.id });
        if (this.closed) return;
        this.info = info && typeof info === "object" ? info : {};
        if (!this.info.supported) return this.go("unsupported");
        this.presets = normPresets(this.info.presets);
        this.max = maxPoints(this.info);
        this.saved = normPoints(this.info.points);
        this.draft = this.saved.slice();
        this.go("list");
        this.openSession();
      } catch (err) {
        if (this.closed) return;
        this.err = errorText(err);
        this.go("fatal");
      }
    }

    isDirty() {
      return !samePoints(this.draft, this.saved) || (this.step === "edit" && !!this.e?.touched);
    }

    async startNew(target, label, from) {
      if (this.saving) return;
      const found = findPoint(this.draft, target);
      if (found >= 0) return this.startEdit(found, from);
      if (!canAdd(this.draft, this.max)) return;
      const e = { index: null, target: target.slice(), label, device: target.slice(), loading: true, note: "", touched: false, from };
      Object.assign(this, { e, err: "", pvErr: "", paused: false });
      this.go("edit");
      // Начало - то, что поправка из уже подобранных цветов сама шлёт на эту
      // цель: обычно остаётся чуть подвинуть, а не искать с нуля.
      try {
        const res = await this.o.ws("color_points/command", commandPayload(this.id, target, this.draft));
        const hs = normHs(res?.device);
        if (!hs) throw new Error("Сервер не прислал начальное положение.");
        if (this.e !== e || this.closed) return;
        e.device = hs;
      } catch (err) {
        if (this.e !== e || this.closed) return;
        e.note = `Не удалось рассчитать начальное положение: ${errorText(err)} Начинаю с цвета без поправки.`;
      }
      e.loading = false;
      if (this.step !== "edit") return;   // открыто «Закрыть без сохранения?» - отрисуется при возврате
      // По месту, а не render(): фокус остаётся там, где был.
      for (const el of this.$("c").querySelectorAll("[data-wait]")) { el.disabled = false; el.removeAttribute("data-wait"); }
      if (e.note) this.render(false);
      this.paintHs(true);
      this.paintPreview();
      this.preview.now(this.previewValue());
    }

    startEdit(i, from = `[data-act="edit"][data-i="${i}"]`) {
      const p = this.draft[i];
      if (!p || this.saving) return;
      this.e = { index: i, target: p.target.slice(), label: p.label, device: p.device.slice(), loading: false, note: "", touched: false, from };
      Object.assign(this, { err: "", pvErr: "", paused: false });
      this.go("edit");
      this.preview.now(this.previewValue());
    }

    commitEdit() {
      const e = this.e;
      if (!e || e.loading || this.saving) return;
      const point = normPoint({ target: e.target, device: e.device, label: e.label });
      const res = point && upsertPoint(this.draft, point, e.index, this.max);
      if (!res) { this.err = `Больше не поместится: предел — ${colours(this.max)}.`; return this.render('[data-act="edit-save"]'); }
      this.draft = res.points;
      this.leaveSub(`[data-act="edit"][data-i="${res.index}"]`);
      this.say(`«${point.label}» в списке. Чтобы применить, нажмите «Сохранить всё».`);
    }

    removeAt(i) {
      const p = this.draft[i];
      if (!p || this.saving) return;
      this.draft = removePoint(this.draft, i);
      const next = Math.min(i, this.draft.length - 1);
      this.err = "";
      this.go("list", next >= 0 ? `[data-act="edit"][data-i="${next}"]` : '[data-act="add-preset"], [data-act="custom"]');
      this.say(`«${p.label}» убран из списка`);
    }

    startCustom() {
      if (this.saving || !canAdd(this.draft, this.max)) return;
      this.c = [0, 100];
      this.err = "";
      this.go("custom");
    }

    startTest() {
      if (this.saving) return;
      const i = this.presets.length ? 0 : -1;
      this.t = { target: i >= 0 ? this.presets[0].target.slice() : [0, 0], preset: i, device: null };
      Object.assign(this, { err: "", pvErr: "", paused: false });
      this.go("test");
      this.preview.now(this.previewValue());
    }

    pickTestPreset(k) {
      const p = this.presets[k];
      if (!p || !this.t || this.step !== "test") return;
      Object.assign(this.t, { target: p.target.slice(), preset: k, device: null });
      this.paintHs(true);
      this.preview.now(this.previewValue());
    }

    /** С подэкрана на список. Лента остаётся на последнем показанном цвете:
     *  каждый возврат «как было» между правками мигал бы лентой. Ждущий
     *  предпросмотр выбрасываем - иначе он догнал бы список. */
    leaveSub(focus) {
      this.preview.cancel();
      Object.assign(this, { e: null, c: null, t: null, err: "", pvErr: "", paused: false });
      this.go("list", focus || true);
    }

    backToList() {
      const from = this.step === "edit" ? this.e?.from : this.step === "test" ? '[data-act="test"]' : '[data-act="custom"]';
      this.leaveSub(from);
    }

    askClose() {
      if (this.saving) return;
      if (!["list", "edit", "custom", "test", "confirm-reset"].includes(this.step) || !this.isDirty()) return this.close();
      this.back = this.step;
      this.err = "";
      this.go("confirm-close");
    }

    goBack() {
      const step = this.back || "list";
      this.back = "list";
      this.go(step);
      if (step === "edit" && this.e?.loading) return;
      const v = this.previewValue();
      if (v) this.preview.now(v);
    }

    async saveAll() {
      if (this.saving) return;
      this.saving = true; this.err = ""; this.render(false);
      try {
        await this.o.ws("color_points/save", { entity_id: this.id, points: pointsPayload(this.draft) });
      } catch (err) {
        this.saving = false; this.err = errorText(err);
        return this.render('[data-act="save-all"]');
      }
      this.changed = true;
      if (this.closed) { this.o.toast?.("Ручная калибровка сохранена"); return this.o.onClose?.(true); }
      const r = await this.stopSession();
      this.o.toast?.(r.error ? `Ручная калибровка сохранена, но ленту не удалось вернуть: ${r.error}` : "Ручная калибровка сохранена", !!r.error);
      this.close();
    }

    async reset() {
      if (this.saving) return;
      this.saving = true; this.err = ""; this.render(false);
      try {
        await this.o.ws("color_points/save", { entity_id: this.id, points: [] });
      } catch (err) {
        this.saving = false; this.err = errorText(err);
        return this.render('[data-act="reset"]');
      }
      Object.assign(this, { saving: false, changed: true, saved: [], draft: [] });
      if (this.closed) return this.o.onClose?.(true);
      this.go("list");
      this.notify("Ручная калибровка сброшена");
    }

    close() {
      if (this.closed) return;
      this.closed = true;
      clearTimeout(this._toastTimer);
      document.removeEventListener("visibilitychange", this._onVis);
      window.removeEventListener("pagehide", this._onHide);
      const stopped = this.stopSession();
      this.releaseWake();
      if (this.isConnected) this.remove();
      stopped.then((r) => {
        if (r.error) this.o.toast?.(`Ленту не удалось вернуть в прежнее состояние: ${r.error}`, true);
      });
      try { this.o.onClose?.(this.changed); } catch (err) { console.error("BMS ручная калибровка: onClose", err); }
    }
  });
}

/**
 * Открыть ручную калибровку поверх страницы.
 * opts: { entityId, name, ws(name, payload), toast(text, bad), onClose(changed), mount? }
 * Возвращает элемент; его close() закрывает окно и возвращает ленту.
 */
export function openManualColors(opts) {
  defineOverlay();
  const el = document.createElement(TAG);
  el.setup(opts);
  (opts.mount || document.body).appendChild(el);
  return el;
}
