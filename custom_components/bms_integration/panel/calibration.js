/**
 * Калибровка цвета RGB-ленты камерой телефона - как Apple TV с телевизором.
 *
 * Лента по команде сервера показывает ряд цветов, камера телефона меряет их
 * на освещённой стене, сервер считает поправку, затем лента показывает
 * проверочные цвета уже с поправкой, и сервер говорит, насколько точно вышло.
 * Контроллеры передают цвет по-разному, поэтому поправка своя у каждой ленты.
 *
 * Панель грузит модуль лениво и даёт ему только ws(name, payload),
 * toast(text, bad) и onClose(changed). Сверху - чистые функции замера: они
 * проверяются в node (tests/test_calibration_js.mjs) без браузера.
 */

// ====================================================== чистые функции ==
export function srgbToLinear(v) {
  if (!(v > 0)) return 0;
  if (v >= 1) return 1;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}
const LIN = Float64Array.from({ length: 256 }, (_, i) => srgbToLinear(i / 255));

// Центральная половина кадра: края у телефонной камеры темнее
// (виньетирование) и чаще ловят посторонний свет.
export const CENTER = Object.freeze({ x: 0.25, y: 0.25, w: 0.5, h: 0.5 });
// Канал «в потолке» уже не говорит, какой он на самом деле, - соотношение
// каналов у такого пикселя врёт. Почти чёрное - шум матрицы, а не свет ленты.
const CLIP = 0.97;
const DARK_Y = 0.002;

export function averageRegion(data, width, height, region = CENTER) {
  const r0 = region || CENTER;
  const x0 = Math.max(0, Math.floor(r0.x * width)), x1 = Math.min(width, Math.ceil((r0.x + r0.w) * width));
  const y0 = Math.max(0, Math.floor(r0.y * height)), y1 = Math.min(height, Math.ceil((r0.y + r0.h) * height));
  const total = Math.max(0, x1 - x0) * Math.max(0, y1 - y0);
  if (!total) return { rgb: null, coverage: 0, clipped: 0, dark: 0 };
  const top = CLIP * 255;
  let used = 0, clipped = 0, dark = 0, sr = 0, sg = 0, sb = 0;
  for (let y = y0; y < y1; y++) {
    for (let i = (y * width + x0) * 4, end = (y * width + x1) * 4; i < end; i += 4) {
      const r = data[i], g = data[i + 1], b = data[i + 2];
      if (r >= top || g >= top || b >= top) { clipped++; continue; }
      const lr = LIN[r], lg = LIN[g], lb = LIN[b];
      if (0.2126 * lr + 0.7152 * lg + 0.0722 * lb < DARK_Y) { dark++; continue; }
      sr += lr; sg += lg; sb += lb; used++;
    }
  }
  return { rgb: used ? [sr / used, sg / used, sb / used] : null,
           coverage: used / total, clipped: clipped / total, dark: dark / total };
}

export function chromaticity(rgb) {
  if (!rgb) return null;
  const sum = rgb[0] + rgb[1] + rgb[2];
  return sum > 0 ? [rgb[0] / sum, rgb[1] / sum] : null;
}

const maxDiff = (a, b) => Math.max(Math.abs(a[0] - b[0]), Math.abs(a[1] - b[1]));

export function isSettled(history, { tolerance = 0.004, need = 4 } = {}) {
  const n = Math.max(1, need | 0);
  if (!Array.isArray(history) || history.length < n) return false;
  const tail = history.slice(-n);
  if (tail.some((c) => !c)) return false;
  const mean = [0, 1].map((k) => tail.reduce((s, c) => s + c[k], 0) / n);
  return tail.every((c) => maxDiff(c, mean) <= tolerance + 1e-12);
}

const realSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Дождаться, пока цвет на стене перестанет меняться, и усреднить кадры.
 *
 * Камера с автоэкспозицией и лента с плавным переходом успокаиваются не
 * сразу. changedFrom - цветность прошлого замера: пока картинка с ним
 * совпадает, это ещё старый цвет (команда до ленты не дошла), и признать его
 * «устоявшимся» - значит записать прошлый цвет под новым именем.
 */
export async function measureUntilSettled(sampleFn, opts = {}) {
  const {
    minWaitMs = 700, intervalMs = 150, timeoutMs = 5000, average = 8,
    tolerance = 0.004, need = 4, minCoverage = 0.1,
    changedFrom = null, changeThreshold = 0.01, changeTimeoutMs = 2500,
    sleep = realSleep, now = () => Date.now(),
  } = opts;
  const usable = (s) => !!(s && s.rgb && s.coverage >= minCoverage);
  const start = now();
  await sleep(minWaitMs);
  const history = [];
  let settled = false, seen = !changedFrom, waited = false, last = null;
  for (;;) {
    last = await sampleFn();
    const c = usable(last) ? chromaticity(last.rgb) : null;
    if (!seen && !waited) {
      if (c && maxDiff(c, changedFrom) > changeThreshold) seen = true;
      else if (now() - start >= changeTimeoutMs) waited = true;
    }
    if (seen || waited) {
      history.push(c);
      if (isSettled(history, { tolerance, need })) { settled = true; break; }
    }
    if (now() - start >= timeoutMs) break;
    await sleep(intervalMs);
  }
  const sum = [0, 0, 0];
  let n = 0, cov = 0;
  for (let i = 0; i < average; i++) {
    await sleep(intervalMs);
    last = await sampleFn();
    if (!usable(last)) continue;
    sum[0] += last.rgb[0]; sum[1] += last.rgb[1]; sum[2] += last.rgb[2];
    cov += last.coverage; n++;
  }
  return { rgb: n ? sum.map((v) => v / n) : null, settled, changed: changedFrom ? seen : null,
           coverage: n ? cov / n : 0, used: n, last };
}

// Доля пересвеченных точек, при которой в рамке светится сама лента. С
// объекта: камеру держали так, что светящийся профиль шёл через весь кадр.
export const STRIP_IN_FRAME = 0.03;

export function exposureState(res, okCoverage = 0.5) {
  if (!res || (!res.coverage && !res.clipped && !res.dark)) return "none";
  if (res.coverage >= okCoverage) return res.clipped > STRIP_IN_FRAME ? "strip" : "ok";
  return res.clipped > res.dark ? "bright" : "dark";
}

export function snapToRange(target, range) {
  if (!range || !Number.isFinite(range.min) || !Number.isFinite(range.max) || range.max < range.min) return null;
  const step = range.step > 0 ? range.step : 0;
  let v = Math.min(range.max, Math.max(range.min, target));
  if (step) {
    v = range.min + Math.round((v - range.min) / step) * step;
    if (v > range.max) v -= step;
  }
  return Math.round(v * 1000) / 1000;
}

// Без фиксированного баланса белого камера подстраивается под ленту, и
// замер теряет смысл. Chrome на Android даёт выставить 6500 K; Safari на
// iPhone умеет только «заморозить» баланс, подобранный по белому экрану.
export function whiteBalancePlan(caps, target = 6500) {
  const modes = Array.isArray(caps?.whiteBalanceMode) ? caps.whiteBalanceMode : [];
  if (!modes.includes("manual")) return { method: null };
  const kelvin = snapToRange(target, caps.colorTemperature);
  return kelvin > 0 ? { method: "6500k", colorTemperature: kelvin } : { method: "reference" };
}

export function whiteBalanceHeld(settings, plan, step = 0) {
  if (!settings || !plan?.method) return false;
  if (plan.method === "6500k") {
    return settings.whiteBalanceMode === "manual" && Number.isFinite(settings.colorTemperature)
      && Math.abs(settings.colorTemperature - plan.colorTemperature) <= Math.max(step || 0, 100);
  }
  // Если браузер о режиме молчит, проверить нечем: отказ applyConstraints
  // к этому моменту уже отсеян.
  return !("whiteBalanceMode" in settings) || settings.whiteBalanceMode === "manual";
}

// hs - как в Home Assistant: оттенок 0..360, насыщенность 0..100.
export function hsToCss(hs) {
  const h = ((((Number(hs?.[0]) || 0) % 360) + 360) % 360) / 60;
  const s = Math.min(1, Math.max(0, (Number(hs?.[1]) || 0) / 100));
  const f = (n) => { const k = (n + h) % 6; return Math.round(255 * (1 - s * Math.max(0, Math.min(k, 4 - k, 1)))); };
  return `rgb(${f(5)},${f(3)},${f(1)})`;
}

const NAMES = { white: "белый", white_mode: "белый режим лампы", red: "красный", green: "зелёный", blue: "синий", cyan: "голубой",
  sky: "голубой", magenta: "пурпурный", pink: "розовый", yellow: "жёлтый", orange: "оранжевый",
  purple: "фиолетовый" };
const HUES = [[15, "красный"], [45, "оранжевый"], [70, "жёлтый"], [160, "зелёный"], [200, "голубой"],
  [255, "синий"], [290, "фиолетовый"], [345, "розовый"], [361, "красный"]];

export function colourName(key, hs) {
  const k = String(key || "");
  if (k === "white_mode") return NAMES.white_mode;
  const soft = k.endsWith("_soft");
  const known = NAMES[soft ? k.slice(0, -5) : k];
  if (known) return soft ? `светло-${known}` : known;
  const h = (((Number(hs?.[0]) || 0) % 360) + 360) % 360, s = Number(hs?.[1]) || 0;
  if (s < 15) return "белый";
  const name = HUES.find(([edge]) => h < edge)[1];
  return s < 70 ? `светло-${name}` : name;
}

// Образец цвета приходит от сервера готовым; попадает в style - поэтому
// только строгий #rrggbb, остальное рисуем сами.
const HEX = /^#[0-9a-f]{6}$/i;
export function swatchOf(item) {
  return HEX.test(item?.swatch || "") ? item.swatch : hsToCss(item?.hs);
}

// ------------------------------------------- что показать на ленте --
// Тёплый белый, по которому владелец заметил беду: пресет 3000 K выходил белым.
export const WARM_KELVIN = 3000;

/** Цвет замера или проверки -> что показать: {kelvin} или {hs}; hs null - белый режим лампы. */
export function itemTarget(item) {
  const k = item?.kelvin;
  if (k !== null && k !== undefined && Number.isFinite(Number(k)) && Number(k) > 0) return { kelvin: Number(k) };
  return { hs: Array.isArray(item?.hs) ? [Number(item.hs[0]) || 0, Number(item.hs[1]) || 0] : null };
}

/** Команда «show»: ровно одно из hs / kelvin - сервер отказывает, если оба. */
export function showPayload(entityId, target, calibration = null) {
  const payload = { entity_id: entityId };
  if (target?.kelvin !== null && target?.kelvin !== undefined) payload.kelvin = target.kelvin;
  else payload.hs = target?.hs ?? null;
  if (calibration) payload.calibration = calibration;
  return payload;
}

const sameHs = (a, b) => (a === null && b === null)
  || (!!a && !!b && Math.abs(a[0] - b[0]) < 0.5 && Math.abs(a[1] - b[1]) < 0.5);

export function sameTarget(a, b) {
  if (!a || !b) return false;
  const ka = a.kelvin ?? null, kb = b.kelvin ?? null;
  if (ka !== null || kb !== null) return ka !== null && kb !== null && Math.abs(ka - kb) < 1;
  return sameHs(a.hs ?? null, b.hs ?? null);
}

/**
 * Сравнение, как у Apple TV («Оригинал» / «Сбалансированный»): человек
 * переключает и смотрит на стену. «Как было» - то, что лента делает сама:
 * белый - её белый режим, если он есть, иначе белый из трёх цветов без
 * поправки; тёплый - её собственные 3000 K.
 */
export function previewPayload(entityId, { corrected, warm }, { calibration = null, whiteMode = false } = {}) {
  const cal = corrected ? calibration : null;
  if (warm) return showPayload(entityId, { kelvin: WARM_KELVIN }, cal);
  if (corrected) return showPayload(entityId, { hs: [0, 0] }, cal);
  return showPayload(entityId, { hs: whiteMode ? null : [0, 0] }, null);
}

export const samePayload = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// -------------------------------------------- подстройка белого на глаз --
const isRange = (r) => !!r && Number.isFinite(r.min) && Number.isFinite(r.max) && r.max > r.min;

/** Диапазоны ползунков от сервера; нет их (старый сервер) - нет и подстройки. */
export function whiteRange(info) {
  const r = info?.white_range;
  if (!isRange(r?.kelvin) || !isRange(r?.tint)) return null;
  const one = (x) => ({ min: x.min, max: x.max, step: x.step > 0 ? x.step : 0 });
  return { kelvin: one(r.kelvin), tint: one(r.tint) };
}

/** Только в пределы. Белый по умолчанию бывает не на сетке ползунка
 *  (оттенок 0,0032 при шаге 0,001): привязка к шагу тихо меняла бы его. */
export function clampRange(value, range) {
  const v = Number(value);
  if (!isRange(range) || !Number.isFinite(v)) return null;
  return Math.min(range.max, Math.max(range.min, v));
}

/** Как snapToRange, но без округления до тысячных: шаг оттенка бывает мельче. */
export function fitRange(value, range) {
  const v0 = Number(value);
  if (!isRange(range) || !Number.isFinite(v0)) return null;
  let v = Math.min(range.max, Math.max(range.min, v0));
  if (range.step > 0) {
    v = range.min + Math.round((v - range.min) / range.step) * range.step;
    if (v > range.max + 1e-9) v -= range.step;
  }
  return Math.round(v * 1e9) / 1e9;
}

const finiteWhite = (w) => !!w && Number.isFinite(w.kelvin) && Number.isFinite(w.tint);

export function whiteDefault(info, range = whiteRange(info)) {
  const d = finiteWhite(info?.white_default) ? info.white_default : { kelvin: 6500, tint: 0 };
  if (!range) return { kelvin: d.kelvin, tint: d.tint };
  return { kelvin: clampRange(d.kelvin, range.kelvin), tint: clampRange(d.tint, range.tint) };
}

export function whiteOf(calibration, fallback) {
  const w = calibration?.white;
  return finiteWhite(w) ? { kelvin: w.kelvin, tint: w.tint } : { ...fallback };
}

/** Копия поправки с другим белым. Ответ сервера не трогаем: «Отмена» и
 *  «Сбросить» должны вернуть ровно то, что он прислал. */
export function withWhite(calibration, white, range) {
  const copy = JSON.parse(JSON.stringify(calibration));
  copy.white = range
    ? { kelvin: clampRange(white.kelvin, range.kelvin), tint: clampRange(white.tint, range.tint) }
    : { kelvin: white.kelvin, tint: white.tint };
  return copy;
}

export const sameWhite = (a, b) => !!a && !!b
  && Math.abs(a.kelvin - b.kelvin) < 0.5 && Math.abs(a.tint - b.tint) < 1e-6;

/** Кнопки «‹ ›» у ползунка: около двадцати касаний на весь ход. */
export function nudge(value, dir, range) {
  if (!isRange(range)) return value;
  const span = range.max - range.min;
  const step = range.step > 0 ? range.step : span / 100;
  const big = Math.max(step, step * Math.round(span / 20 / step));
  return fitRange(Number(value) + Math.sign(dir) * big, range);
}

function shiftWords(value, centre, range, less, more, neutral) {
  const half = (range.step > 0 ? range.step : (range.max - range.min) / 1000) / 2;
  if (Math.abs(value - centre) <= half) return neutral;
  const span = value < centre ? centre - range.min : range.max - centre;
  const f = span > 0 ? Math.abs(value - centre) / span : 1;
  const word = value < centre ? less : more;
  if (f < 0.34) return `Чуть ${word}`;
  if (f < 0.67) return word[0].toUpperCase() + word.slice(1);
  return `Намного ${word}`;
}

/** Положение ползунков словами - от белого по умолчанию (нейтрального). */
export function whiteWords(white, def, range) {
  return {
    kelvin: shiftWords(white.kelvin, def.kelvin, range.kelvin, "теплее", "холоднее", "Нейтральный"),
    tint: shiftWords(white.tint, def.tint, range.tint, "розовее", "зеленее", "Без оттенка"),
  };
}

/**
 * Предпросмотр на ленте: последнее значение побеждает.
 *
 * Ползунок присылает десятки значений в секунду, а лента отвечает за сотни
 * миллисекунд. Очередь команд проигрывала бы на стене всю историю движения
 * пальца с опозданием в секунды. Поэтому: дребезг delay мс, в пути не больше
 * одной команды, а ждёт только самое новое значение - промежуточные
 * выбрасываются. Таймеры подставляются, чтобы проверять без часов.
 *
 * schedule(v) - после дребезга; now(v) - сразу (кнопки); cancel() - забыть
 * ждущее и вернуть промис команды, что уже в пути: «end» должен идти после неё.
 * send(v, live) получает live(): false - отправку отменили, пока она ждала.
 */
export function latestWins(send, opts = {}) {
  const {
    delay = 250, equals = (a, b) => a === b,
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
    // live() - не отменили ли отправку, пока она ждала своей очереди.
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
const TAG = "bms-color-calibration";
const EMPTY = Object.freeze({ rgb: null, coverage: 0, clipped: 0, dark: 0 });
const LIVE = ["camera", "reference", "aim", "measuring"];
const METER = {
  pending: "Включаю белый свет ленты…",
  none: "Нет изображения с камеры",
  dark: "Слишком темно — наведите на место, ярче освещённое лентой",
  bright: "Пересвет — наведите на бумагу, а не на саму ленту",
  strip: "В кадре светится сама лента — наведите камеру на бумагу так, чтобы ленты не было видно",
  ok: "Хорошо — можно измерять",
  measuring: "Кадр хороший",
};
const VERDICT = { excellent: "ok", good: "ok", fair: "warn", poor: "bad" };

class Cancelled extends Error {}

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const btn = (label, act, kind = "", extra = "") =>
  `<button type="button" class="b ${kind}" data-act="${act}" ${extra}>${label}</button>`;
// Сегменты - кнопки-переключатели (aria-pressed) в группе: каждая в Tab,
// пробел и Enter работают сами, и считыватель экрана называет выбранную.
const seg = (label, group, options, value, off) =>
  `<div class="seg" role="group" aria-label="${esc(label)}">${options.map(([v, text]) =>
    `<button type="button" data-act="seg" data-g="${group}" data-v="${v}" aria-pressed="${v === value}" ${off}>${text}</button>`).join("")}</div>`;
const METHODS = ["6500k", "reference"];
// «Оранжевый» -> «оранжевый» внутри фразы, но «RGB» остаётся «RGB».
const lower1 = (t) => (/^\p{Lu}\p{Ll}/u.test(t) ? t[0].toLowerCase() + t.slice(1) : t);
// «3000 K» не рвётся между числом и единицей: на узком телефоне «K»
// уезжала на отдельную строку.
const NB = "\u00a0";
const nbK = (t) => String(t ?? "").replace(/(\d) (K|К)(?!\p{L})/gu, `$1${NB}$2`);
const WARM = `Тёплый ${WARM_KELVIN}${NB}K`;

function errorText(err) {
  if (err?.code === "unknown_command") {
    return "Сервер не знает калибровку цвета — обновите интеграцию BMS и перезапустите Home Assistant.";
  }
  if (err === 3 || err?.code === 3) return "Пропала связь с Home Assistant. Проверьте сеть и нажмите «Повторить».";
  return err?.message || (typeof err === "string" ? err : "") || "Неизвестная ошибка";
}

function cameraErrorText(err) {
  const name = err?.name || "";
  if (name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError") {
    return "Нет доступа к камере. Разрешите камеру для этого сайта (iPhone: Настройки → Safari → Камера; "
      + "Android: значок слева от адреса → Разрешения) и нажмите «Повторить».";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError" || name === "DevicesNotFoundError") {
    return "Камера не найдена. Откройте калибровку на телефоне с основной камерой.";
  }
  if (name === "NotReadableError" || name === "TrackStartError" || name === "AbortError") {
    return "Камера занята другим приложением. Закройте его и нажмите «Повторить».";
  }
  return `Не удалось включить камеру: ${err?.message || name || err}`;
}

// Каждый applyConstraints заменяет весь набор ограничений: без этих двух
// браузер вправе сменить разрешение посреди замера.
const BASE = Object.freeze({ width: { ideal: 640 }, height: { ideal: 480 } });
const exact = (c) => Object.fromEntries(Object.entries(c).map(([k, v]) => [k, { exact: v }]));
const shortDate = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleDateString("ru-RU");
};

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
.stage{position:relative;width:min(100%,calc(46vh * var(--ar,1.3333)));aspect-ratio:var(--ar,1.3333);margin:0 auto;background:#000;border-radius:14px;overflow:hidden;flex:none}
.stage[hidden]{display:none}
video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.ring{position:absolute;left:25%;top:25%;width:50%;height:50%;border:2px solid rgba(255,255,255,.92);border-radius:12px;box-shadow:0 0 0 999px rgba(0,0,0,.3)}
.meter{position:absolute;left:50%;bottom:10px;transform:translateX(-50%);width:max-content;max-width:calc(100% - 20px);display:flex;align-items:center;gap:8px;
  padding:6px 12px;border-radius:999px;background:rgba(13,19,25,.86);font-size:13.5px;font-weight:600}
.meter::before{content:"";width:10px;height:10px;border-radius:50%;background:var(--mc,#9AA7B4);flex:none}
.meter:empty,.meter[hidden]{display:none}
.meter[data-state=ok]{--mc:#2BC275}.meter[data-state=dark],.meter[data-state=bright]{--mc:#F0B429}.meter[data-state=none]{--mc:#FF5A6A}
h2{font-size:21px;line-height:1.25;margin:0;font-weight:700;outline:none}
p{margin:0}.mut{color:#9AA7B4;font-size:14px}
ol{margin:0;padding:0;list-style:none;counter-reset:s;display:flex;flex-direction:column;gap:10px}
ol li{counter-increment:s;display:flex;gap:12px;align-items:flex-start}
ol li::before{content:counter(s);flex:none;width:26px;height:26px;border-radius:50%;background:#1C2733;color:#8DBBFF;display:grid;place-items:center;font-size:13px;font-weight:700}
.acts{display:flex;flex-direction:column;gap:10px}
.b{min-height:48px;width:100%;padding:10px 18px;border-radius:12px;border:1px solid #2B3744;background:#18212B;color:#EEF2F6;
  font:inherit;font-size:16px;font-weight:600;cursor:pointer;touch-action:manipulation}
.b.pri{background:#006FFF;border-color:#006FFF;color:#fff}
.b.dan{background:transparent;border-color:#6A2B35;color:#FF8592}
.b.link{background:transparent;border-color:transparent;color:#8DBBFF;min-height:44px}
.b:disabled{opacity:.45;cursor:not-allowed}.b[hidden]{display:none}
button:focus-visible,input:focus-visible,[tabindex="0"]:focus-visible{outline:3px solid #8DBBFF;outline-offset:2px}
.card{background:#141C25;border:1px solid #222D39;border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:10px}
.row{display:flex;align-items:center;gap:12px;min-width:0}.grow{flex:1;min-width:0}
.chk .grow{min-width:auto}.chk .mut{flex:0 1 auto;min-width:0;text-align:right}
.sw{width:28px;height:28px;border-radius:50%;border:1px solid rgba(255,255,255,.3);flex:none}
.track{height:8px;border-radius:4px;background:#222D39;overflow:hidden}
.track span{display:block;height:100%;width:0;background:#2F87FF;transition:width .3s}
.note{border-left:3px solid #F0B429;padding:2px 0 2px 10px;color:#E8D6A8;font-size:14px}
.err{border-left:3px solid #FF5A6A;padding:2px 0 2px 10px;color:#FFB3BB;font-size:14px}
.ok{color:#2BC275}.warn{color:#F0B429}.bad{color:#FF5A6A}
.lh{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#9AA7B4}
.seg{display:flex;gap:4px;padding:4px;border-radius:14px;background:#0D1319;border:1px solid #222D39}
.seg button{flex:1 1 0;min-width:0;min-height:44px;padding:6px 8px;border:0;border-radius:10px;background:transparent;color:#C9D3DD;
  font:inherit;font-size:15px;font-weight:600;line-height:1.2;cursor:pointer;touch-action:manipulation}
.seg button[aria-pressed=true]{background:#2B3744;color:#fff;box-shadow:0 1px 3px rgba(0,0,0,.45)}
.seg button:disabled{opacity:.45;cursor:not-allowed}
.sl{display:flex;flex-direction:column;gap:4px}
.sl .val{font-weight:650}
.slr{display:flex;align-items:center;gap:6px}
.nb{width:48px;height:48px;flex:none;border-radius:12px;border:1px solid #2B3744;background:#18212B;color:#EEF2F6;
  display:grid;place-items:center;cursor:pointer;touch-action:manipulation;padding:0}
.nb:disabled{opacity:.45;cursor:not-allowed}
.ends{display:flex;justify-content:space-between;padding:0 54px;font-size:13px;color:#9AA7B4}
input[type=range]{-webkit-appearance:none;appearance:none;flex:1;min-width:0;height:48px;margin:0;background:transparent;cursor:pointer}
input[type=range]:disabled{opacity:.45;cursor:not-allowed}
input[type=range]::-webkit-slider-runnable-track{height:12px;border-radius:6px;background:var(--track)}
input[type=range]::-moz-range-track{height:12px;border-radius:6px;background:var(--track)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:32px;height:32px;margin-top:-10px;border-radius:50%;
  background:#fff;border:3px solid #006FFF;box-shadow:0 1px 4px rgba(0,0,0,.55)}
input[type=range]::-moz-range-thumb{width:26px;height:26px;border-radius:50%;background:#fff;border:3px solid #006FFF}
.rng.k{--track:linear-gradient(90deg,#FFB061,#FFF3E3 var(--mid,50%),#C4D9FF)}
.rng.t{--track:linear-gradient(90deg,#FFADD5,#F3F3F3 var(--mid,50%),#B4EFBE)}
.pv{display:flex;align-items:center;gap:10px;min-height:24px}
.pv .mut{flex:1}
.pv .b.link{width:auto;flex:none;padding:6px 10px}
.white{position:fixed;inset:0;z-index:2;background:#fff;display:flex;align-items:flex-end;justify-content:center;
  padding-bottom:calc(28px + env(safe-area-inset-bottom));cursor:pointer;outline:none}
.white[hidden]{display:none}.white span{color:#BDBDBD;font:13px -apple-system,Roboto,sans-serif;transition:opacity .6s}
.white.quiet span{opacity:0}
.toast{position:fixed;left:50%;bottom:calc(20px + env(safe-area-inset-bottom));transform:translateX(-50%);z-index:3;max-width:calc(100% - 32px);
  background:#EEF2F6;color:#0D1319;padding:10px 16px;border-radius:10px;font-size:14px;font-weight:600}
.toast[hidden]{display:none}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
`;

const SHELL = `<style>${CSS}</style>
<div class="wrap" role="dialog" aria-modal="true" aria-labelledby="t">
  <div class="bar"><div class="ttl"><div class="h" id="t">Калибровка цвета</div><div class="sub" id="name"></div></div>
    <button type="button" class="x" data-act="close" aria-label="Закрыть калибровку">
      <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>
    </button></div>
  <div class="body">
    <div class="stage" id="stage" hidden><video id="v" muted playsinline autoplay aria-hidden="true"></video>
      <div class="ring" aria-hidden="true"></div><div class="meter" id="meter" role="status"></div></div>
    <div id="c"></div>
  </div>
</div>
<div class="white" id="white" hidden data-act="white-exit" role="button" tabindex="0"
  aria-label="Белый экран. Коснитесь, чтобы выйти"><span>Коснитесь, чтобы выйти</span></div>
<div class="toast" id="toast" role="status" hidden></div>
<div class="sr" id="sr" role="status" aria-live="polite"></div>`;

function defineOverlay() {
  if (customElements.get(TAG)) return;
  customElements.define(TAG, class extends HTMLElement {
    setup(o) {
      this.o = o;
      this.id = o.entityId;
      this.info = o.info || null;
      this.step = this.info ? "intro" : "loading";
      Object.assign(this, { method: null, stream: null, track: null, begun: false, begunOk: false, token: null, inflight: null,
        stopping: null, changed: false, result: null, aim: "pending", aimReady: false, err: "", note: "",
        adj: null, ab: { corrected: true, warm: false }, pv: { sending: false, waiting: false }, pvErr: "", paused: false });
      // Один предпросмотр на окно: подстройка и сравнение «как было / с
      // поправкой» шлют на ленту через него, и в пути всегда не больше одной команды.
      this.preview = latestWins((payload, live) => this.previewSend(payload, live), {
        delay: 250, equals: samePayload,
        onState: (st) => { this.pv = st; this.paintPreview(); },
        onSent: () => { this.pvErr = ""; this.paused = false; this.paintPreview(); },
        onError: (err) => {
          if (err instanceof Cancelled || this.closed) return;
          console.warn("BMS калибровка: предпросмотр", err);
          this.pvErr = `Лента не ответила: ${errorText(err)}`;
          if (this.shadowRoot) this.$("sr").textContent = this.pvErr;
          this.paintPreview();
        },
      });
    }

    get wr() { return whiteRange(this.info); }
    get whiteDef() { return whiteDefault(this.info, this.wr); }
    hasWhiteMode() { return (this.info?.samples || []).some((s) => s.key === "white_mode"); }
    resultCal() { return this.result ? this.result.adjusted || this.result.calibration : null; }

    connectedCallback() {
      if (this.shadowRoot) return;
      const root = this.attachShadow({ mode: "open" });
      root.innerHTML = SHELL;
      this.$ = (id) => root.getElementById(id);
      this.$("name").textContent = this.o.name || this.id;
      const video = this.$("v");
      video.muted = true;
      video.setAttribute("webkit-playsinline", "");
      // Телефон повернули - кадр сменил стороны; рамка замера должна
      // по-прежнему совпадать с центральной половиной кадра.
      video.addEventListener("resize", () => this.fitStage());
      root.addEventListener("click", (e) => this.onClick(e));
      root.addEventListener("input", (e) => this.onInput(e));
      root.addEventListener("keydown", (e) => this.onKey(e));
      this._onVis = () => { if (document.visibilityState === "hidden") this.interrupt(); };
      this._onHide = () => this.interrupt();
      document.addEventListener("visibilitychange", this._onVis);
      window.addEventListener("pagehide", this._onHide);
      this.render();
      if (!this.info) this.loadInfo();
    }

    disconnectedCallback() { this.close(); }

    // ---------------------------------------------------------- отрисовка --
    go(step) { this.step = step; this.render(); }

    render(focus = true) {
      if (!this.shadowRoot) return;
      this.$("c").innerHTML = this.view();
      const live = LIVE.includes(this.step) && !!this.stream;
      this.$("stage").hidden = !live;
      if (live) this.$("v").play?.()?.catch?.(() => {});
      this.$("meter").hidden = this.step !== "aim" && this.step !== "measuring";
      if (focus === true) this.$("c").querySelector("h2")?.focus();
      // Перерисовка по месту (ошибка сохранения и т. п.): фокус возвращается
      // на ту же кнопку, а не теряется на странице под окном.
      else if (focus) this.$("c").querySelector(focus)?.focus();
      if (this.step === "aim" || this.step === "measuring") this.paintMeter();
      if (this.step === "measuring") this.paintProgress();
      if (this.step === "adjust") this.paintAdjust();
      if (this.step === "result" || this.step === "adjust") this.paintPreview();
    }

    view() {
      const i = this.info, s = this.step;
      const head = (t) => `<h2 tabindex="-1">${t}</h2>`;
      const close = btn("Закрыть", "close");
      const retry = btn("Повторить", "start", "pri");
      const error = this.err ? `<p class="err">${esc(this.err)}</p>` : "";
      if (s === "loading") return `${head("Загрузка…")}<p class="mut">Узнаю, умеет ли лента калибровку.</p>`;
      if (s === "fatal") return `${head("Калибровка недоступна")}${error}<div class="acts">${close}</div>`;
      if (s === "intro") {
        if (!i.supported) {
          return `${head("Калибровка недоступна")}<p>${esc(i.reason || "Эта лента не умеет менять цвет.")}</p><div class="acts">${close}</div>`;
        }
        if (!(i.samples || []).length) {
          return `${head("Калибровка недоступна")}<p>Сервер не прислал цвета для замера — обновите интеграцию BMS.</p><div class="acts">${close}</div>`;
        }
        return `${head("Подстроим цвет ленты под настоящий белый")}
          <p>Камера телефона измерит, как лента показывает цвета, и интеграция поправит их — белый станет нейтральным дневным. Займёт около минуты; потом лента вернётся как была.</p>
          <ol>
            <li>Выключите остальной свет и задёрните шторы.</li>
            <li>Положите белый лист бумаги в 10–20 см от ленты и наведите основную (заднюю) камеру на бумагу так, чтобы самой ленты в кадре не было. Дерево и цветные стены окрашивают свет — калибровка подгонит ленту под их цвет.</li>
            <li>Держите телефон неподвижно, пока идёт замер.</li>
            <li>Отверните экран телефона от стены — его свет портит замер.</li>
          </ol>
          <p class="mut">На iPhone понадобится второй экран с белым фоном: ноутбук, телевизор или другой телефон. На втором телефоне откройте эту же калибровку и нажмите «Белый экран для второго телефона».</p>
          ${i.calibrated ? `<p class="mut">Сейчас калибровка сохранена${i.calibrated_at ? ` (${esc(shortDate(i.calibrated_at))})` : ""}.${
            this.canAdjustSaved() ? " Белый можно подстроить на глаз — без камеры." : ""}</p>` : ""}
          ${error}
          <div class="acts">${btn("Начать", "start", "pri")}
            ${this.canAdjustSaved() ? btn("Подстроить белый на глаз", "adjust-saved") : ""}
            ${btn("Белый экран для второго телефона", "white")}
            ${i.calibrated ? btn("Сбросить калибровку", "reset-ask", "dan") : ""}</div>`;
      }
      if (s === "adjust") return this.viewAdjust(error);
      if (s === "reset") {
        return `${head("Сбросить калибровку?")}<p>Лента вернётся к цветам контроллера без поправки.</p>${error}
          <div class="acts">${btn(this.saving ? "Сбрасываю…" : "Сбросить", "reset", "dan", this.saving ? "disabled" : "")}
          ${btn("Отмена", "intro", "", this.saving ? "disabled" : "")}</div>`;
      }
      if (s === "insecure") {
        return `${head("Нужен защищённый адрес")}<p>Камера в браузере работает только по защищённому адресу (https). Откройте эту страницу по адресу объекта https://… или в приложении Home Assistant.</p><div class="acts">${close}</div>`;
      }
      if (s === "camera") return `${head("Включаю камеру…")}<p class="mut">Если браузер спросит доступ к камере — разрешите.</p><div class="acts">${btn("Отмена", "close")}</div>`;
      if (s === "camera-error") return `${head("Камера недоступна")}${error}<div class="acts">${retry}${close}</div>`;
      if (s === "wb-unsupported") {
        return `${head("Этот телефон не подходит")}<p>Этот телефон не даёт зафиксировать баланс белого камеры — без этого замер неточен. Обновите iOS / Chrome или возьмите другой телефон.</p><div class="acts">${close}</div>`;
      }
      if (s === "reference") {
        return `${head("Запомним белый")}
          ${this.note ? `<p class="note">${esc(this.note)}</p>` : ""}
          <p>Наведите камеру на белый экран ноутбука, телевизора или второго телефона (белая страница, яркость повыше) и нажмите «Запомнить белый».</p>
          <p class="mut">На том экране выключите ночной режим (Night Shift, True Tone, «Защита глаз»). На втором телефоне белый экран открывается здесь же: «Калибровка цвета» → «Белый экран для второго телефона».</p>
          ${error}
          <div class="acts">${btn(this.busy ? "Запоминаю…" : "Запомнить белый", "remember", "pri", this.busy ? "disabled" : "")}${btn("Отмена", "close")}</div>`;
      }
      if (s === "aim") {
        return `${head("Наведите на стену")}
          <p>Лента светит белым. Наведите камеру на белый лист бумаги, освещённый лентой, так, чтобы рамка была заполнена светом, а самой ленты в кадре не было.</p>
          ${this.front ? `<p class="note">Включилась фронтальная камера. Для замера нужна основная (задняя) камера телефона.</p>` : ""}
          ${this.wbNote ? `<p class="mut">${esc(this.wbNote)}.</p>` : ""}
          <div class="acts">${btn("Измерить", "measure", "pri", "disabled")}${btn("Всё равно измерить", "measure", "", "hidden data-force=\"1\"")}${btn("Отмена", "close")}</div>`;
      }
      if (s === "measuring") {
        return `${head("Идёт замер")}
          <div class="card"><div class="row"><span class="sw" data-p="sw"></span><span class="grow" data-p="label"></span></div>
            <div class="track" role="progressbar" aria-label="Ход замера" aria-valuemin="0" data-p="track"><span data-p="bar"></span></div></div>
          <p class="mut">Держите телефон неподвижно и не загораживайте стену.</p>
          <div class="acts">${btn("Отменить", "close")}</div>`;
      }
      if (s === "result") return this.viewResult(error);
      if (s === "interrupted" || s === "error") {
        const title = s === "error" ? "Не получилось" : "Замер прерван";
        const text = s === "error" ? error
          : `<p>Экран погас или приложение было свёрнуто — камера отключилась.</p>`;
        return `${head(title)}${text}${this.restoreNote ? `<p class="mut">${esc(this.restoreNote)}</p>` : ""}
          <div class="acts">${retry}${close}</div>`;
      }
      return "";
    }

    viewResult(error) {
      const { ev, whiteBefore } = this.result;
      const checks = Object.fromEntries((this.info.checks || []).map((c) => [c.key, c]));
      const tone = VERDICT[ev.verdict] || "warn";
      const whiteAfter = (ev.results || []).find((r) => r.key === "white" || sameHs(checks[r.key]?.hs, [0, 0]));
      // Название цвета не сжимается мельче своего слова, а оценка при
      // нехватке места переносится: на узком телефоне они наезжали друг на друга.
      const rows = (ev.results || []).map((r) => `<div class="row chk">
          <span class="sw" style="background:${swatchOf(checks[r.key])}"></span>
          <span class="grow">${esc(nbK(r.label || checks[r.key]?.label || colourName(r.key, checks[r.key]?.hs)))}</span>
          <span class="mut">${esc(r.words)}</span></div>`).join("");
      const unsteady = this.result.unsteady;
      const off = this.saving ? "disabled" : "";
      return `<h2 tabindex="-1" class="${tone}">${esc(ev.verdict_words || "Готово")}</h2>
        ${whiteBefore || whiteAfter ? `<div class="card"><div class="row"><span class="sw" style="background:#fff"></span><div class="grow">
          ${whiteBefore ? `<div>Белый до калибровки: <b>${esc(whiteBefore.words)}</b></div>` : ""}
          ${whiteAfter ? `<div>Белый после: <b>${esc(whiteAfter.words)}</b></div>` : ""}</div></div></div>` : ""}
        ${this.result.warning ? `<p class="note">${esc(this.result.warning)}</p>` : ""}
        ${this.result.adjusted ? `<p class="note">Белый подстроен вручную — оценка проверки была до подстройки.</p>` : ""}
        ${rows ? `<div class="card"><div class="lh">Проверка цветов</div>${rows}</div>` : ""}
        ${ev.verdict === "poor" ? `<p class="note">Результат слабый. Проверьте, что в комнате темно, стена белая и телефон неподвижен, и нажмите «Повторить».</p>` : ""}
        ${unsteady ? `<p class="note">Часть замеров была неустойчивой — в следующий раз держите телефон неподвижнее.</p>` : ""}
        <div class="card"><div class="lh" id="cmp">Сравните на стене</div>
          ${seg("Поправка", "corrected", [["0", "Как было"], ["1", "С поправкой"]], this.ab.corrected ? "1" : "0", off)}
          ${seg("Цвет на ленте", "warm", [["0", "Белый"], ["1", WARM]], this.ab.warm ? "1" : "0", off)}
          ${this.viewPreviewLine()}</div>
        ${error}
        <div class="acts">${btn(this.saving ? "Сохраняю…" : "Сохранить", "save", "pri", off)}
          ${this.wr ? btn("Подстроить белый на глаз", "adjust", "", off) : ""}
          ${btn("Повторить", "again", "", off)}${btn("Отмена", "close", "", off)}</div>`;
    }

    viewPreviewLine() {
      return `<div class="pv"><p class="mut" data-p="pv"></p>${btn("Показать снова", "pv-again", "link", "hidden")}</div>
        <p class="err" data-p="pverr" hidden></p>`;
    }

    viewAdjust(error) {
      const a = this.adj, r = this.wr, def = this.whiteDef;
      const off = this.saving ? "disabled" : "";
      const saved = a.from === "saved";
      const slider = (k, cls, label, less, more) => {
        const rg = r[k], pct = Math.round((100 * (def[k] - rg.min)) / (rg.max - rg.min));
        return `<div class="sl">
          <div class="row"><span class="grow val" data-p="${k}-w"></span>${k === "kelvin" ? `<span class="mut" data-p="kelvin-h"></span>` : ""}</div>
          <div class="slr">
            <button type="button" class="nb" data-act="nudge" data-k="${k}" data-d="-1" aria-label="${less}" ${off}>
              <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
            <input type="range" class="rng ${cls}" data-k="${k}" min="${rg.min}" max="${rg.max}" step="${rg.step || "any"}"
              value="${a.white[k]}" style="--mid:${Math.min(100, Math.max(0, pct))}%" aria-label="${label}" ${off}>
            <button type="button" class="nb" data-act="nudge" data-k="${k}" data-d="1" aria-label="${more}" ${off}>
              <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
          </div>
          <div class="ends" aria-hidden="true"><span>${less}</span><span>${more}</span></div></div>`;
      };
      return `<h2 tabindex="-1">Подстройка белого на глаз</h2>
        <p class="mut">Смотрите на стену, а не на экран, и двигайте ползунки, пока белый не станет на вид нейтральным.</p>
        ${seg("Что показать на ленте", "warm", [["0", "Белый"], ["1", WARM]], a.warm ? "1" : "0", off)}
        <div class="card">${slider("kelvin", "k", "Теплее или холоднее", "Теплее", "Холоднее")}
          ${slider("tint", "t", "Розовее или зеленее", "Розовее", "Зеленее")}
          ${this.viewPreviewLine()}</div>
        ${error}
        <div class="acts">${saved ? btn(this.saving ? "Сохраняю…" : "Сохранить", "save", "pri", off) : btn("Готово", "adj-done", "pri", off)}
          ${btn("Сбросить", "adj-reset", "", `aria-label="Сбросить подстройку белого" ${off}`)}
          ${btn("Отмена", "adj-cancel", "", off)}</div>`;
    }

    /** Подписи ползунков - по месту: перерисовка всего экрана оборвала бы
     *  перетаскивание пальцем. sync - выставить и сами ползунки (сброс, «‹ ›»). */
    paintAdjust(sync = false) {
      if (this.step !== "adjust" || !this.adj || !this.shadowRoot) return;
      const c = this.$("c"), a = this.adj, words = whiteWords(a.white, this.whiteDef, this.wr);
      const q = (k) => c.querySelector(`[data-p="${k}"]`);
      const hint = `≈${NB}${Math.round(a.white.kelvin)}${NB}K`;
      if (q("kelvin-w")) q("kelvin-w").textContent = words.kelvin;
      if (q("kelvin-h")) q("kelvin-h").textContent = hint;
      if (q("tint-w")) q("tint-w").textContent = words.tint;
      for (const input of c.querySelectorAll("input[data-k]")) {
        const k = input.dataset.k;
        if (sync) input.value = String(a.white[k]);
        input.setAttribute("aria-valuetext", k === "kelvin" ? `${words.kelvin}, ${hint}` : words.tint);
      }
    }

    paintSeg() {
      const c = this.$("c");
      for (const b of c.querySelectorAll('[data-act="seg"]')) {
        const on = this.step === "adjust" ? this.adj?.warm : this.ab[b.dataset.g];
        b.setAttribute("aria-pressed", String((b.dataset.v === "1") === !!on));
      }
    }

    pvShownText() {
      if (this.step === "adjust") {
        return `Лента показывает ${this.adj?.warm ? `тёплый белый ${WARM_KELVIN}${NB}K` : "белый"} с этой подстройкой.`;
      }
      return `Лента показывает ${this.ab.warm ? `тёплый белый ${WARM_KELVIN}${NB}K` : "белый"} ${
        this.ab.corrected ? "с поправкой" : "как было, без поправки"}.`;
    }

    paintPreview() {
      if (!this.shadowRoot || (this.step !== "result" && this.step !== "adjust")) return;
      const c = this.$("c"), line = c.querySelector('[data-p="pv"]'), bad = c.querySelector('[data-p="pverr"]');
      if (!line) return;
      const busy = this.pv.sending || this.pv.waiting;
      const text = busy ? "Обновляю ленту…"
        : this.paused ? "Экран телефона гас — лента вернулась как была."
        : this.pvErr ? "" : this.pvShownText();
      line.textContent = text;
      line.hidden = !text;
      bad.textContent = this.pvErr;
      bad.hidden = busy || !this.pvErr;
      const again = c.querySelector('[data-act="pv-again"]');
      if (again) again.hidden = busy || this.saving || !(this.paused || this.pvErr);
      for (const g of c.querySelectorAll(".seg")) g.setAttribute("aria-busy", String(busy));
    }

    paintMeter() {
      const m = this.$("meter");
      const state = this.step === "aim" && !this.aimReady ? "pending" : this.aim;
      const text = state === "ok" && this.step === "measuring" ? METER.measuring : METER[state];
      if (m.textContent !== text) { m.dataset.state = state; m.textContent = text; }
      if (this.step !== "aim") return;
      const [go, force] = this.$("c").querySelectorAll('[data-act="measure"]');
      if (go) go.disabled = !(this.aimReady && state === "ok");
      if (force) force.hidden = !this.aimReady || state === "ok";
    }

    paintProgress() {
      const p = this.prog, c = this.$("c");
      if (!p) return;
      const q = (k) => c.querySelector(`[data-p="${k}"]`);
      if (!q("label")) return;
      q("label").textContent = p.label;
      q("sw").style.background = p.swatch || "transparent";
      q("sw").style.visibility = p.swatch ? "visible" : "hidden";
      q("bar").style.width = `${Math.round((100 * p.done) / Math.max(1, p.total))}%`;
      q("track").setAttribute("aria-valuemax", String(p.total));
      q("track").setAttribute("aria-valuenow", String(p.done));
    }

    progress(done, total, label, swatch = "") {
      this.prog = { done, total, label, swatch };
      if (this.step !== "measuring") this.go("measuring"); else this.paintProgress();
      this.$("sr").textContent = label;
    }

    notify(text) {
      const t = this.$("toast");
      t.textContent = text; t.hidden = false;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { t.hidden = true; }, 3000);
    }

    fitStage() {
      const v = this.$("v");
      if (v.videoWidth && v.videoHeight) this.$("stage").style.setProperty("--ar", String(v.videoWidth / v.videoHeight));
    }

    // ------------------------------------------------------------ события --
    onClick(e) {
      const el = e.composedPath().find((n) => n?.dataset?.act);
      if (!el || el.disabled) return;
      const a = el.dataset.act;
      if (a === "close") return this.close();
      if (a === "start") return this.start();
      if (a === "white") return this.showWhite();
      if (a === "white-exit") return this.exitWhite();
      if (a === "reset-ask") { this.err = ""; return this.go("reset"); }
      if (a === "reset") return this.reset();
      if (a === "intro") { this.err = ""; return this.go("intro"); }
      if (a === "remember") return this.rememberWhite();
      if (a === "measure") return this.measure();
      if (a === "save") return this.save();
      if (a === "again") return this.again();
      if (a === "adjust") return this.adjustResult();
      if (a === "adjust-saved") return this.adjustSaved();
      if (a === "adj-done") return this.adjustLeave(true);
      if (a === "adj-cancel") return this.adjustLeave(false);
      if (a === "adj-reset") return this.setWhite(this.whiteDef, true);
      if (a === "nudge") {
        const k = el.dataset.k;
        if (this.adj && this.wr?.[k]) this.setWhite({ [k]: nudge(this.adj.white[k], Number(el.dataset.d), this.wr[k]) }, true);
        return;
      }
      if (a === "seg") return this.onSeg(el.dataset.g, el.dataset.v === "1");
      if (a === "pv-again") return this.preview.now(this.previewNowPayload());
    }

    onInput(e) {
      const el = e.composedPath()[0];
      const k = el?.dataset?.k;
      if (this.step !== "adjust" || !this.adj || !k || el.disabled) return;
      this.setWhite({ [k]: Number(el.value) }, false);
    }

    onSeg(group, on) {
      if (this.saving) return;
      if (this.step === "adjust" && this.adj && group === "warm") {
        if (this.adj.warm === on) return;
        this.adj.warm = on;
      } else if (this.step === "result" && (group === "warm" || group === "corrected")) {
        if (this.ab[group] === on) return;
        this.ab = { ...this.ab, [group]: on };
      } else return;
      this.paintSeg();
      this.preview.now(this.previewNowPayload());
    }

    onKey(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        if (!this.$("white").hidden) this.exitWhite();
        // Подстройка - слой поверх результата или вступления: Esc закрывает её, а не всё окно.
        else if (this.step === "adjust" && !this.saving) this.adjustLeave(false);
        else if (!this.saving) this.close();
        return;
      }
      if ((e.key === "Enter" || e.key === " ") && e.target === this.$("white")) { e.preventDefault(); return this.exitWhite(); }
      if (e.key !== "Tab" || !this.$("white").hidden) return;
      // Фокус не уходит за окно калибровки на страницу под ним.
      const items = [...this.shadowRoot.querySelectorAll(".wrap button:not([disabled]), .wrap input:not([disabled])")]
        .filter((b) => !b.hidden && b.getClientRects().length);
      if (!items.length) return;
      const active = this.shadowRoot.activeElement, first = items[0], last = items[items.length - 1];
      if (e.shiftKey && (active === first || !items.includes(active))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (active === last || !items.includes(active))) { e.preventDefault(); first.focus(); }
    }

    // --------------------------------------------------------- служебное --
    newToken() { this.cancelToken(); return (this.token = { cancelled: false }); }
    cancelToken() { if (this.token) this.token.cancelled = true; this.token = null; }

    sleep(ms, token) {
      return new Promise((resolve, reject) =>
        setTimeout(() => (token.cancelled ? reject(new Cancelled()) : resolve()), ms));
    }

    async call(type, payload, token) {
      if (token.cancelled) throw new Cancelled();
      const p = Promise.resolve().then(() => this.o.ws(type, payload));
      this.inflight = p;
      let res;
      try { res = await p; } finally { if (this.inflight === p) this.inflight = null; }
      if (token.cancelled) throw new Cancelled();
      return res;
    }

    async show(target, calibration, token) {
      this.shown = null;
      await this.call("color_calibration/show", showPayload(this.id, target, calibration), token);
      this.shown = { target, cal: !!calibration };
    }

    /** Отправка предпросмотра (её зовёт только latestWins). После «end»
     *  (экран гас) сессия открывается заново: «begin» снимает то, что лента
     *  показывает сейчас, - это уже возвращённое прежнее состояние. */
    async previewSend(payload, live = () => true) {
      if (this.stopping) await this.stopping;
      // Отменённая отправка, проснувшись после «end», не должна открыть
      // сессию снова: лента ушла бы на предпросмотр уже после возврата.
      if (this.closed || this.saving || !live()) throw new Cancelled();
      let token = this.token;
      if (!this.begunOk) {
        token ||= this.newToken();
        this.begun = true;
        this.lockWake();
        await this.call("color_calibration/begin", { entity_id: this.id }, token);
        this.begunOk = true;
      }
      if (!token || !live()) throw new Cancelled();
      // Замер после предпросмотра не должен считать, что на ленте его цвет.
      this.shown = null;
      return this.call("color_calibration/show", payload, token);
    }

    /** Что сейчас должно быть на ленте по выбору на экране. */
    previewNowPayload() {
      if (this.step === "adjust" && this.adj) {
        return previewPayload(this.id, { corrected: true, warm: this.adj.warm }, { calibration: this.adj.cal });
      }
      return previewPayload(this.id, this.ab, { calibration: this.resultCal(), whiteMode: this.hasWhiteMode() });
    }

    grab() {
      const video = this.$("v");
      const vw = video.videoWidth, vh = video.videoHeight;
      if (!vw || !vh || video.readyState < 2) return EMPTY;
      const w = 160, h = Math.max(1, Math.round((160 * vh) / vw));
      const cv = (this.canvas ||= document.createElement("canvas"));
      if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
      this.ctx ||= cv.getContext("2d", { willReadFrequently: true, colorSpace: "srgb" });
      if (!this.ctx) return EMPTY;
      try {
        this.ctx.drawImage(video, 0, 0, w, h);
        return averageRegion(this.ctx.getImageData(0, 0, w, h, { colorSpace: "srgb" }).data, w, h, CENTER);
      } catch {
        return EMPTY;
      }
    }

    startMeter() {
      this.stopMeter();
      this._meter = setInterval(() => {
        const res = this.grab();
        this.aim = exposureState(res);
        if (this.aim === "ok") this.live = chromaticity(res.rgb);
        if (this.step === "aim" || this.step === "measuring") this.paintMeter();
      }, 250);
    }
    stopMeter() { clearInterval(this._meter); this._meter = null; }

    async lockWake() {
      if (this.wake || !navigator.wakeLock?.request) return;
      try {
        const lock = await navigator.wakeLock.request("screen");
        if (this.closed) return lock.release().catch(() => {});
        this.wake = lock;
        lock.addEventListener?.("release", () => { if (this.wake === lock) this.wake = null; });
      } catch { /* без блокировки экран может погаснуть - тогда замер честно прервётся */ }
    }
    releaseWake() { const w = this.wake; this.wake = null; w?.release?.().catch?.(() => {}); }

    stopCamera() {
      for (const t of this.stream?.getTracks?.() || []) t.stop();
      if (this.shadowRoot) this.$("v").srcObject = null;
      this.stream = this.track = null;
      this.method = null;
      this.exposureLocked = false;
    }

    /** Остановить всё и вернуть ленту. Ждёт команду, что уже в пути: иначе
     *  «end» обгоняет её на сервере, и лента остаётся на цвете замера. */
    stopSession() {
      if (this.stopping) return this.stopping;
      // Ждущий предпросмотр выбрасывается, а уже отправленный «end» дождётся ниже.
      this.preview.cancel();
      this.cancelToken();
      this.stopMeter();
      this.stopCamera();
      this.releaseWake();
      if (!this.begun) return Promise.resolve({ ended: false, error: "" });
      this.begun = false;
      this.begunOk = false;
      const pending = this.inflight;
      this.stopping = (async () => {
        if (pending) await Promise.race([pending.catch(() => {}), realSleep(4000)]);
        try {
          await this.o.ws("color_calibration/end", { entity_id: this.id, restore: true });
          return { ended: true, error: "" };
        } catch (err) {
          console.warn("BMS калибровка: лента не вернулась в прежнее состояние", err);
          return { ended: true, error: errorText(err) };
        }
      })().finally(() => { this.stopping = null; });
      return this.stopping;
    }

    // ------------------------------------------------------------ сценарий --
    async loadInfo() {
      try {
        this.info = await this.o.ws("color_calibration/info", { entity_id: this.id });
        this.go("intro");
      } catch (err) {
        this.err = errorText(err);
        this.go("fatal");
      }
    }

    async start() {
      if (this.stopping) await this.stopping;
      if (this.closed) return;
      Object.assign(this, { err: "", note: "", wbNote: "", restoreNote: "", result: null });
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) return this.go("insecure");
      const token = this.newToken();
      this.go("camera");
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: "environment" }, width: { ideal: 640 }, height: { ideal: 480 } }, audio: false });
      } catch (err) {
        if (token.cancelled) return;
        console.warn("BMS калибровка: камера", err);
        this.err = cameraErrorText(err);
        return this.go("camera-error");
      }
      if (token.cancelled) { stream.getTracks().forEach((t) => t.stop()); return; }
      this.stream = stream;
      this.track = stream.getVideoTracks()[0] || null;
      const video = this.$("v");
      video.srcObject = stream;
      this.render(false);
      this.track?.addEventListener("ended", () => {
        if (this.stream !== stream) return;
        // Готовый результат камера уже не нужна: сохранить его можно и так.
        if (this.step === "result" || this.step === "adjust") return this.stopCamera();
        this.fail(new Error("Камера отключилась. Закройте другие приложения с камерой и нажмите «Повторить»."));
      });
      try { await video.play(); } catch { /* muted + playsinline: кадры пойдут и без play() */ }
      try {
        for (let t = 0; !video.videoWidth && t < 40; t++) await this.sleep(100, token);
      } catch { return; }
      if (!video.videoWidth) return this.fail(new Error("Камера включилась, но не присылает изображение. Нажмите «Повторить»."));
      this.fitStage();
      this.front = this.track?.getSettings?.().facingMode === "user";
      this.lockWake();
      return this.lockWhiteBalance(token);
    }

    async lockWhiteBalance(token) {
      const track = this.track;
      const caps = () => (track.getCapabilities ? track.getCapabilities() : {}) || {};
      let c = caps();
      // Chrome на части телефонов отдаёт возможности камеры не с первого кадра.
      if (!c.whiteBalanceMode) { try { await this.sleep(500, token); } catch { return; } c = caps(); }
      console.info("BMS калибровка: возможности камеры", c, track.getSettings?.());
      const plan = whiteBalancePlan(c);
      if (plan.method === "6500k") {
        const why = await this.applyWB({ whiteBalanceMode: "manual", colorTemperature: plan.colorTemperature }, plan,
          c.colorTemperature?.step);
        if (token.cancelled) return;
        if (!why) {
          this.method = "6500k";
          this.kelvin = plan.colorTemperature;
          this.wbNote = `Баланс белого камеры зафиксирован: ${Math.round(track.getSettings().colorTemperature)} K`;
          return this.goAim();
        }
        console.warn("BMS калибровка: 6500 K не выставились", why);
        // Неудачная попытка могла оставить «ручной» баланс на чужой
        // температуре - тогда «Запомнить белый» сразу «удалась» бы, так и не
        // подстроившись под белый экран. Возвращаем камере автобаланс.
        try { await track.applyConstraints({ ...BASE, advanced: [{ whiteBalanceMode: "continuous" }] }); } catch { /* уже авто */ }
        if (token.cancelled) return;
        this.note = `Камера не приняла 6500 K (${why}). Зафиксируем баланс белого по белому экрану.`;
        return this.go("reference");
      }
      if (plan.method === "reference") return this.go("reference");
      console.warn("BMS калибровка: баланс белого не фиксируется", c);
      await this.stopSession();
      return this.go("wb-unsupported");
    }

    /** Пусто - баланс держится; иначе причина словами. */
    async applyWB(constraint, plan, step) {
      const track = this.track;
      if (!track) return "камера отключилась";
      let why = "";
      // Сначала как advanced - так эти ограничения описаны в стандарте; часть
      // браузеров молча пропускает advanced, тогда то же самое обязательным.
      for (const c of [{ ...BASE, advanced: [constraint] }, { ...BASE, ...exact(constraint) }]) {
        try {
          await track.applyConstraints(c);
          const st = track.getSettings ? track.getSettings() : {};
          if (whiteBalanceHeld(st, plan, step)) return "";
          why = `режим «${st.whiteBalanceMode || "неизвестен"}»${st.colorTemperature ? `, ${Math.round(st.colorTemperature)} K` : ""}`;
        } catch (err) {
          why = err?.message || err?.name || String(err);
        }
      }
      return why || "неизвестная причина";
    }

    async rememberWhite() {
      if (this.busy) return;
      this.busy = true; this.err = ""; this.render(false);
      const why = await this.applyWB({ whiteBalanceMode: "manual" }, { method: "reference" });
      this.busy = false;
      if (this.step !== "reference") return;
      if (why) { this.err = `Камера не зафиксировала баланс белого: ${why}`; return this.render(false); }
      this.method = "reference";
      this.wbNote = "Баланс белого камеры зафиксирован по белому экрану";
      this.goAim();
    }

    /** Экспозиция, снятая по белому, держит в кадре все цвета ленты: каждый
     *  основной темнее белого. На автоэкспозиции камера «вытягивает» синий
     *  до пересвета. Не дал браузер - остаёмся на авто: сервер считает только
     *  соотношения внутри кадра. Только для 6500 K: «заморозку» по белому
     *  экрану повторное ограничение баланса сбросило бы на свет ленты. */
    async lockExposure(token) {
      const track = this.track;
      const caps = track?.getCapabilities?.() || {}, st = track?.getSettings?.() || {};
      if (this.method !== "6500k" || !caps.exposureMode?.includes?.("manual") || !(st.exposureTime > 0)) return;
      try {
        await track.applyConstraints({ ...BASE, advanced: [{ whiteBalanceMode: "manual", colorTemperature: this.kelvin,
          exposureMode: "manual", exposureTime: st.exposureTime }] });
        await this.sleep(400, token);
        if (track.getSettings().exposureMode === "manual" && exposureState(this.grab()) === "ok") {
          this.exposureLocked = true;
          return;
        }
      } catch (err) {
        if (err instanceof Cancelled) throw err;
        console.warn("BMS калибровка: экспозиция не зафиксировалась", err);
      }
      await this.unlockExposure();
    }

    async unlockExposure() {
      this.exposureLocked = false;
      try {
        await this.track?.applyConstraints({ ...BASE, advanced: [{ whiteBalanceMode: "manual",
          colorTemperature: this.kelvin, exposureMode: "continuous" }] });
      } catch { /* камера осталась как была - замер всё равно идёт */ }
    }

    async goAim() {
      const token = this.newToken();
      if (this.exposureLocked) await this.unlockExposure();
      this.aimReady = false; this.aim = "pending";
      this.go("aim");
      this.startMeter();
      try {
        // begin до первого show: сервер запоминает, что лента показывала, и
        // повторный begin этот снимок не перезаписывает.
        this.begun = true;
        await this.call("color_calibration/begin", { entity_id: this.id }, token);
        this.begunOk = true;
        await this.show({ hs: [0, 0] }, null, token);
        this.aimReady = true;
        this.paintMeter();
      } catch (err) {
        if (!(err instanceof Cancelled)) this.fail(err);
      }
    }

    async measureColour(target, calibration, name, token, { allowDark = false } = {}) {
      const same = this.shown && sameTarget(this.shown.target, target) && this.shown.cal === !!calibration;
      await this.show(target, calibration, token);
      const run = (changedFrom) => measureUntilSettled(() => this.grab(),
        { sleep: (ms) => this.sleep(ms, token), changedFrom });
      let m = await run(same ? null : this.last);
      if (!m.rgb && this.exposureLocked) {
        // Белый режим лампы бывает ярче белого из RGB, и на экспозиции,
        // снятой по белому, он в пересвете. Отдаём экспозицию камере.
        await this.unlockExposure();
        m = await run(null);
      }
      if (!m.rgb && allowDark && exposureState(m.last) !== "bright") {
        // Белый режим не светит вовсе: контроллер RGB+W, а белых светодиодов
        // на ленте нет. Это ответ, а не сбой - сервер соберёт белый из RGB.
        return [0, 0, 0];
      }
      if (!m.rgb) {
        throw new Error(exposureState(m.last) === "bright"
          ? `Не удалось измерить «${name}»: пересвет. Наведите камеру на стену, а не на саму ленту, и нажмите «Повторить».`
          : `Не удалось измерить «${name}»: слишком темно. Наведите камеру на место стены, ярче освещённое лентой, и нажмите «Повторить».`);
      }
      if (!m.settled) this.unsteady++;
      if (m.changed === false) console.info("BMS калибровка: цвет на стене не сменился", name, target);
      this.last = chromaticity(m.rgb);
      return m.rgb.map((v) => Math.round(v * 1e6) / 1e6);
    }

    async measure() {
      if (!this.aimReady || this.step !== "aim") return;
      const token = this.newToken();
      const S = this.info.samples || [], K = this.info.checks || [];
      const total = S.length + 1 + K.length;
      const method = this.method;
      let done = 0;
      this.unsteady = 0;
      this.last = this.live || null;
      try {
        this.progress(0, total, "Готовлю камеру…");
        await this.lockExposure(token);
        const samples = {};
        for (const [n, s] of S.entries()) {
          // hs: null - лампа в собственном белом режиме (white_mode): сервер
          // сам включает его, поправка к нему не применяется. kelvin - что
          // лента делает сама на эту температуру, тоже без поправки.
          const name = nbK(lower1(s.label || colourName(s.key, s.hs)));
          this.progress(done, total, `Замер ${n + 1} из ${S.length}: ${name}`, swatchOf(s));
          samples[s.key] = await this.measureColour(itemTarget(s), null, name, token,
            { allowDark: s.key === "white_mode" });
          done++;
        }
        this.progress(done, total, "Считаю поправку…");
        const fit = await this.call("color_calibration/fit", { samples }, token);
        this.progress(done, total, "Проверяю белый с поправкой…", hsToCss([0, 0]));
        const white = await this.measureColour({ hs: [0, 0] }, fit.calibration, "белый", token);
        done++;
        const { calibration } = await this.call("color_calibration/refine_white",
          { calibration: fit.calibration, measured: white }, token);
        const measured = {};
        for (const [n, c] of K.entries()) {
          // Проверка с kelvin - эта температура через поправку: как её покажет
          // пресет Home Assistant после сохранения.
          const name = nbK(lower1(c.label || colourName(c.key, c.hs)));
          this.progress(done, total, `Проверка ${n + 1} из ${K.length}: ${name}`, swatchOf(c));
          measured[c.key] = await this.measureColour(itemTarget(c), calibration, name, token);
          done++;
        }
        this.progress(total, total, "Оцениваю результат…");
        const ev = await this.call("color_calibration/evaluate", { calibration, measured }, token);
        this.result = { calibration, method, ev, whiteBefore: fit.white_before || null,
                        warning: fit.warning || null,
                        unsteady: this.unsteady > 0, adjusted: null };
        // Лента остаётся на белом с поправкой: результат видно глазами, а
        // переключатели ниже дают сравнить с тем, что было.
        Object.assign(this, { ab: { corrected: true, warm: false }, pvErr: "", paused: false });
        this.stopMeter();
        this.go("result");
        this.$("sr").textContent = ev.verdict_words || "Замер окончен";
        this.preview.now(this.previewNowPayload());
      } catch (err) {
        if (!(err instanceof Cancelled)) this.fail(err);
      }
    }

    fail(err) {
      if (this.closed) return;
      console.error("BMS калибровка:", err);
      this.err = errorText(err);
      this.restoreNote = this.begun ? "Возвращаю ленту в прежнее состояние…" : "";
      this.go("error");
      this.stopSession().then((r) => {
        if (!r.ended) return;
        this.restoreNote = r.error ? `Ленту не удалось вернуть: ${r.error}` : "Лента возвращена в прежнее состояние.";
        if (this.step === "error") this.render(false);
      });
    }

    /** Экран погас или приложение свернули: камеры уже нет, лента в замере. */
    interrupt() {
      if (this.closed || (!this.stream && !this.begun)) return;
      // Результат и подстройка остаются на экране: камера им не нужна, а
      // ленту можно показать снова кнопкой «Показать снова».
      const keep = ["result", "error", "adjust"].includes(this.step);
      const wasBegun = this.begun;
      this.restoreNote = wasBegun ? "Возвращаю ленту в прежнее состояние…" : "";
      this.stopSession().then((r) => {
        if (!r.ended) return;
        if (r.error && (this.step === "result" || this.step === "adjust")) {
          this.pvErr = `Ленту не удалось вернуть: ${r.error}`;
          return this.paintPreview();
        }
        if (this.step !== "interrupted") return;
        this.restoreNote = r.error ? `Ленту не удалось вернуть: ${r.error}` : "Лента возвращена в прежнее состояние.";
        this.render(false);
      });
      if (!keep) return this.go("interrupted");
      if (wasBegun && (this.step === "result" || this.step === "adjust")) { this.paused = true; this.paintPreview(); }
    }

    // ------------------------------------------------- подстройка белого --
    canAdjustSaved() {
      const i = this.info;
      return !!(i?.calibrated && i.calibration && typeof i.calibration === "object" && this.wr);
    }

    /** Подстройка поверх готового замера. Поправка от сервера не меняется:
     *  правится её копия, «Отмена» её просто выбрасывает. */
    adjustResult() {
      if (!this.result || this.saving || !this.wr) return;
      const base = this.result.calibration;
      const white = whiteOf(this.resultCal(), this.whiteDef);
      this.adj = { from: "result", base, white, warm: this.ab.warm, cal: withWhite(base, white, this.wr) };
      this.err = "";
      this.go("adjust");
      this.preview.now(this.previewNowPayload());
    }

    /** Подстройка сохранённой калибровки - без камеры: человек смотрит на стену сам. */
    async adjustSaved() {
      if (this.stopping) await this.stopping;
      if (this.closed || this.step !== "intro" || !this.canAdjustSaved()) return;
      const base = this.info.calibration;
      const white = whiteOf(base, this.whiteDef);
      Object.assign(this, { err: "", pvErr: "", paused: false, result: null });
      this.adj = { from: "saved", base, white, warm: false, cal: withWhite(base, white, this.wr) };
      this.go("adjust");
      // «begin» уходит первым в previewSend - до первого show.
      this.preview.now(this.previewNowPayload());
    }

    setWhite(part, sync) {
      const a = this.adj, r = this.wr;
      if (this.step !== "adjust" || !a || !r || this.saving) return;
      const next = { ...a.white, ...part };
      const white = { kelvin: clampRange(next.kelvin, r.kelvin), tint: clampRange(next.tint, r.tint) };
      if (white.kelvin === null || white.tint === null) return;
      const moved = !sameWhite(white, a.white);
      a.white = white;
      a.cal = withWhite(a.base, white, r);
      this.paintAdjust(sync);
      if (!moved && !sync) return;
      // Ползунок - с дребезгом; кнопки и сброс - сразу.
      if (sync) this.preview.now(this.previewNowPayload()); else this.preview.schedule(this.previewNowPayload());
    }

    adjustLeave(keep) {
      const a = this.adj;
      if (!a || this.saving || this.step !== "adjust") return;
      this.adj = null;
      this.err = "";
      if (a.from === "result" && this.result) {
        if (keep) {
          const measured = whiteOf(this.result.calibration, this.whiteDef);
          this.result.adjusted = sameWhite(a.white, measured) ? null : a.cal;
        }
        this.ab = { corrected: true, warm: a.warm };
        this.step = "result";
        this.render('[data-act="adjust"]');
        if (keep && this.result.adjusted) this.$("sr").textContent = "Белый подстроен вручную";
        return this.preview.now(this.previewNowPayload());
      }
      // Из вступления: лента возвращается как была, сохранённая калибровка не тронута.
      this.step = "intro";
      this.render('[data-act="adjust-saved"]');
      this.stopSession().then((r) => {
        if (!r.error || this.closed) return;
        this.err = `Ленту не удалось вернуть в прежнее состояние: ${r.error}`;
        if (this.step === "intro") this.render(false);
      });
    }

    async again() {
      if (this.againing || this.saving || !this.result) return;
      this.againing = true;
      // Предпросмотр в пути догоняет замер на сервере: сначала дождаться его.
      try { await this.preview.cancel(); } finally { this.againing = false; }
      if (this.closed || this.step !== "result") return;
      Object.assign(this, { result: null, adj: null, err: "", pvErr: "", paused: false });
      return this.stream ? this.goAim() : this.start();
    }

    async save() {
      if (this.saving) return;
      const saved = this.step === "adjust" && this.adj?.from === "saved";
      if (!saved && !this.result) return;
      const calibration = saved ? this.adj.cal : this.resultCal();
      const method = saved ? this.info?.method : this.result.method;
      this.saving = true; this.err = ""; this.render(false);
      const payload = { entity_id: this.id, calibration };
      if (METHODS.includes(method)) payload.method = method;
      try {
        await this.o.ws("color_calibration/save", payload);
      } catch (err) {
        this.saving = false; this.err = errorText(err);
        return this.render('[data-act="save"]');
      }
      this.changed = true;
      if (this.closed) { this.o.toast?.("Калибровка сохранена"); return this.o.onClose?.(true); }
      const r = await this.stopSession();
      this.o.toast?.(r.error ? `Калибровка сохранена, но ленту не удалось вернуть: ${r.error}` : "Калибровка сохранена", !!r.error);
      this.close();
    }

    async reset() {
      if (this.saving) return;
      this.saving = true; this.err = ""; this.render(false);
      const payload = { entity_id: this.id, calibration: null };
      if (["6500k", "reference"].includes(this.info?.method)) payload.method = this.info.method;
      try {
        await this.o.ws("color_calibration/save", payload);
      } catch (err) {
        this.saving = false; this.err = errorText(err);
        return this.render(false);
      }
      this.saving = false;
      this.changed = true;
      this.info = { ...this.info, calibrated: false, calibrated_at: null, method: null, calibration: null };
      this.go("intro");
      this.notify("Калибровка сброшена");
    }

    showWhite() {
      const el = this.$("white");
      el.hidden = false;
      el.classList.remove("quiet");
      el.focus();
      clearTimeout(this._whiteTimer);
      this._whiteTimer = setTimeout(() => el.classList.add("quiet"), 2500);
      try { el.requestFullscreen?.()?.catch?.(() => {}); } catch { /* iPhone: только на весь экран страницы */ }
      this.lockWake();
    }

    exitWhite() {
      const el = this.shadowRoot && this.$("white");
      if (!el || el.hidden) return;
      el.hidden = true;
      clearTimeout(this._whiteTimer);
      if (document.fullscreenElement) document.exitFullscreen?.()?.catch?.(() => {});
      if (!this.stream) this.releaseWake();
      this.$("c").querySelector('[data-act="white"]')?.focus();
    }

    close() {
      if (this.closed) return;
      this.closed = true;
      this.exitWhite();
      clearTimeout(this._toastTimer);
      document.removeEventListener("visibilitychange", this._onVis);
      window.removeEventListener("pagehide", this._onHide);
      const stopped = this.stopSession();
      if (this.isConnected) this.remove();
      stopped.then((r) => {
        if (r.error) this.o.toast?.(`Ленту не удалось вернуть в прежнее состояние: ${r.error}`, true);
      });
      try { this.o.onClose?.(this.changed); } catch (err) { console.error("BMS калибровка: onClose", err); }
    }
  });
}

/**
 * Открыть калибровку поверх страницы.
 * opts: { entityId, name, info?, ws(name, payload), toast(text, bad), onClose(changed), mount? }
 * Возвращает элемент; его close() закрывает окно и возвращает ленту.
 */
export function openCalibration(opts) {
  defineOverlay();
  const el = document.createElement(TAG);
  el.setup(opts);
  (opts.mount || document.body).appendChild(el);
  return el;
}
