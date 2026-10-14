/**
 * Калибровка цвета: чистые функции замера (panel/calibration.js) и крючок в
 * карточке устройства (panel/panel.js).
 *
 * Камера, лента и браузер здесь не нужны: замер кадра, ожидание «цвет
 * устоялся» и выбор баланса белого - обычные функции, и именно в них живут
 * ошибки, которые на объекте выглядят как «калибровка сделала хуже».
 *
 *   node tests/test_calibration_js.mjs
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const PANEL_DIR = join(HERE, "..", "custom_components", "bms_integration", "panel");

// calibration.js - ES-модуль в файле .js без package.json рядом. Разные
// версии node по-разному угадывают тип такого файла (а `node --check` на нём
// молча пропускает синтаксическую ошибку), поэтому грузим копию как .mjs.
const tmp = mkdtempSync(join(tmpdir(), "bms-calibration-"));
let cal;
try {
  const copy = join(tmp, "calibration.mjs");
  writeFileSync(copy, readFileSync(join(PANEL_DIR, "calibration.js")));
  cal = await import(pathToFileURL(copy).href);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log(`  ✓ ${name}`); }
  catch (err) { failed++; console.error(`  ✗ ${name}: ${err?.message || err}`); }
}
function ok(cond, msg) { if (!cond) throw new Error(msg); }
function eq(a, b, msg = "") {
  if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${msg} ожидалось ${JSON.stringify(b)}, получено ${JSON.stringify(a)}`);
}
function near(a, b, eps = 1e-9, msg = "") {
  const as = Array.isArray(a) ? a : [a], bs = Array.isArray(b) ? b : [b];
  ok(as.length === bs.length && as.every((v, i) => Math.abs(v - bs[i]) <= eps),
    `${msg} ожидалось ${JSON.stringify(b)}, получено ${JSON.stringify(a)}`);
}

// Кадр RGBA, как ImageData.data.
function image(w, h, colourAt) {
  const data = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const [r, g, b] = colourAt(x, y), i = (y * w + x) * 4;
    data[i] = r; data[i + 1] = g; data[i + 2] = b; data[i + 3] = 255;
  }
  return data;
}
const FULL = { x: 0, y: 0, w: 1, h: 1 };
const lin = (v8) => cal.srgbToLinear(v8 / 255);

console.log("srgbToLinear");
await test("концы и стык двух ветвей формулы", () => {
  near(cal.srgbToLinear(0), 0); near(cal.srgbToLinear(1), 1);
  near(cal.srgbToLinear(0.04045), 0.04045 / 12.92, 1e-12);
  near(cal.srgbToLinear(0.5), 0.214041, 1e-6);
});
await test("монотонна и не выходит за 0..1", () => {
  let prev = -1;
  for (let i = 0; i <= 100; i++) { const v = cal.srgbToLinear(i / 100); ok(v >= prev, `спад на ${i}`); prev = v; }
  eq([cal.srgbToLinear(-1), cal.srgbToLinear(2), cal.srgbToLinear(NaN)], [0, 1, 0]);
});

console.log("averageRegion");
await test("ровный кадр: линейное среднее, всё покрыто", () => {
  const res = cal.averageRegion(image(4, 4, () => [128, 64, 32]), 4, 4, FULL);
  near(res.rgb, [lin(128), lin(64), lin(32)], 1e-12);
  eq([res.coverage, res.clipped, res.dark], [1, 0, 0]);
});
await test("по умолчанию меряется только центральная половина кадра", () => {
  const inner = (x, y) => x >= 2 && x < 6 && y >= 2 && y < 6;
  const res = cal.averageRegion(image(8, 8, (x, y) => (inner(x, y) ? [100, 150, 200] : [10, 200, 30])), 8, 8);
  near(res.rgb, [lin(100), lin(150), lin(200)], 1e-12, "край кадра попал в замер:");
  eq(res.coverage, 1);
});
await test("усредняется свет, а не закодированные числа", () => {
  const res = cal.averageRegion(image(2, 1, (x) => (x ? [200, 200, 200] : [50, 50, 50])), 2, 1, FULL);
  const mean = (lin(50) + lin(200)) / 2;
  near(res.rgb, [mean, mean, mean], 1e-12);
  ok(Math.abs(mean - lin(125)) > 0.01, "проверка не различает линейное и гамма-среднее");
});
await test("пересвеченный канал (>= 0.97) отбрасывает пиксель, 247 ещё нет", () => {
  const at = (v) => cal.averageRegion(image(1, 1, () => [v, 40, 40]), 1, 1, FULL);
  eq(at(247).clipped, 0, "247:"); eq(at(248).clipped, 1, "248:");
  eq(cal.averageRegion(image(1, 1, () => [40, 40, 255]), 1, 1, FULL).clipped, 1, "синий в потолке:");
});
await test("почти чёрное (яркость < 0.002) - шум, а не свет", () => {
  eq(cal.averageRegion(image(1, 1, () => [6, 6, 6]), 1, 1, FULL).dark, 1, "серый 6:");
  eq(cal.averageRegion(image(1, 1, () => [7, 7, 7]), 1, 1, FULL).dark, 0, "серый 7:");
});
await test("доли пересвета, темноты и покрытия считаются от всей области", () => {
  const px = [[255, 255, 255], [0, 0, 0], [100, 50, 20], [120, 60, 30]];
  const res = cal.averageRegion(image(2, 2, (x, y) => px[y * 2 + x]), 2, 2, FULL);
  eq([res.coverage, res.clipped, res.dark], [0.5, 0.25, 0.25]);
  near(res.rgb, [(lin(100) + lin(120)) / 2, (lin(50) + lin(60)) / 2, (lin(20) + lin(30)) / 2], 1e-12);
});
await test("ни одного годного пикселя - rgb null, а не нули", () => {
  const res = cal.averageRegion(image(2, 2, () => [255, 255, 255]), 2, 2, FULL);
  eq([res.rgb, res.coverage, res.clipped], [null, 0, 1]);
  eq(cal.averageRegion(new Uint8ClampedArray(0), 0, 0).rgb, null, "пустой кадр:");
});

console.log("chromaticity / isSettled");
await test("цветность - доли каналов", () => {
  near(cal.chromaticity([1, 1, 1]), [1 / 3, 1 / 3], 1e-12);
  near(cal.chromaticity([2, 1, 1]), [0.5, 0.25], 1e-12);
  eq([cal.chromaticity([0, 0, 0]), cal.chromaticity(null)], [null, null]);
});
await test("устоялось: последние need в пределах допуска от среднего", () => {
  const c = [0.4, 0.35];
  ok(!cal.isSettled([]), "пустая история");
  ok(!cal.isSettled([c, c, c]), "мало точек");
  ok(cal.isSettled([c, c, c, c]), "четыре одинаковых");
  ok(cal.isSettled([[0.403, 0.35], [0.397, 0.35], [0.403, 0.35], [0.397, 0.35]]), "дрожь ±0.003");
  ok(!cal.isSettled([c, c, c, [0.41, 0.35]]), "последняя ушла на 0.01");
  ok(!cal.isSettled([c, c, null, c]), "провал кадра в хвосте");
  ok(cal.isSettled([[0.1, 0.1], [0.9, 0.9], c, c, c, c]), "старые точки не в счёт");
  ok(cal.isSettled([c, c], { need: 2 }) && !cal.isSettled([[0.4, 0.35], [0.41, 0.35]], { need: 2, tolerance: 0.001 }),
    "need и tolerance задаются");
});

console.log("measureUntilSettled (часы подставные)");
function clock() {
  let t = 0;
  return { now: () => t, sleep: async (ms) => { t += ms; }, get t() { return t; } };
}
const frame = (rgb, coverage = 1) => ({ rgb, coverage, clipped: 0, dark: 1 - coverage });
// Цвет с заданной цветностью: удобно сравнивать результат по каналам.
const A = [0.5, 0.3, 0.2], B = [0.2, 0.3, 0.5];

await test("ждёт minWait, затем переход, затем устоявшийся цвет", async () => {
  const k = clock(), seq = [[0.9, 0.05, 0.05], [0.7, 0.2, 0.1], [0.6, 0.25, 0.15]];
  let calls = 0, first = null;
  const res = await cal.measureUntilSettled(() => { first ??= k.t; return frame(seq[calls++] || A); }, k);
  eq(first, 700, "первый кадр не после minWait:");
  ok(res.settled, "не устоялось");
  near(res.rgb, A, 1e-12); eq(res.used, 8);
});
await test("в среднее идут только кадры ПОСЛЕ того, как устоялось", async () => {
  let calls = 0;
  const res = await cal.measureUntilSettled(() => frame(++calls <= 4 ? A : B), clock());
  ok(res.settled, "не устоялось"); near(res.rgb, B, 1e-12);
});
await test("неустойчивое изображение: тайм-аут, settled=false, но замер есть", async () => {
  const k = clock();
  let calls = 0;
  const res = await cal.measureUntilSettled(() => frame(++calls % 2 ? A : B), k);
  ok(!res.settled, "дрожь признана устоявшейся");
  ok(k.t >= 5000 && k.t <= 5000 + 150 + 8 * 150 + 150, `время ${k.t}`);
  ok(res.rgb, "нет замера");
});
await test("кадры с малым покрытием в среднее не идут", async () => {
  let calls = 0;
  const res = await cal.measureUntilSettled(() => {
    calls++;
    return calls > 4 && calls % 2 ? frame([1, 0, 0], 0.05) : frame(A);
  }, clock());
  near(res.rgb, A, 1e-12); eq(res.used, 4);
});
await test("ни одного годного кадра - rgb null", async () => {
  const res = await cal.measureUntilSettled(() => frame([1, 1, 1], 0), clock());
  eq([res.rgb, res.settled, res.coverage], [null, false, 0]);
});
await test("changedFrom: прошлый цвет на стене не принимается за новый", async () => {
  const run = (opts) => { let n = 0; return cal.measureUntilSettled(() => frame(++n <= 6 ? A : B), { ...clock(), ...opts }); };
  const guarded = await run({ changedFrom: cal.chromaticity(A) });
  near(guarded.rgb, B, 1e-12, "с защитой:"); eq(guarded.changed, true);
  const naive = await run({});
  ok(Math.abs(naive.rgb[0] - B[0]) > 0.01, "без защиты результат тот же - проверка ничего не доказывает");
  eq(naive.changed, null);
});
await test("changedFrom: цвет так и не сменился - ждём changeTimeout и меряем", async () => {
  const k = clock();
  let settledAt = null;
  const res = await cal.measureUntilSettled(() => { const f = frame(A); return f; },
    { ...k, changedFrom: cal.chromaticity(A) });
  settledAt = k.t;
  eq(res.changed, false); ok(res.settled, "не устоялось"); near(res.rgb, A, 1e-12);
  ok(settledAt >= 2500 && settledAt < 5000 + 2000, `время ${settledAt}`);
});

console.log("exposureState");
await test("хорошо / пересвет / темно / нет кадра", () => {
  eq(cal.exposureState({ rgb: A, coverage: 0.8, clipped: 0.1, dark: 0.1 }), "ok");
  eq(cal.exposureState({ rgb: A, coverage: 0.5, clipped: 0.5, dark: 0 }), "ok", "граница 0.5:");
  eq(cal.exposureState({ rgb: A, coverage: 0.2, clipped: 0.7, dark: 0.1 }), "bright");
  eq(cal.exposureState({ rgb: A, coverage: 0.2, clipped: 0.1, dark: 0.7 }), "dark");
  eq(cal.exposureState({ rgb: null, coverage: 0, clipped: 0, dark: 0 }), "none");
  eq(cal.exposureState(null), "none");
});

console.log("баланс белого");
await test("snapToRange: в пределах и по шагу", () => {
  eq(cal.snapToRange(6500, { min: 2850, max: 7000, step: 50 }), 6500);
  eq(cal.snapToRange(6500, { min: 2000, max: 6000, step: 100 }), 6000);
  const v = cal.snapToRange(6500, { min: 2850, max: 7500, step: 100 });
  ok(Math.abs(v - 6500) <= 50 && (v - 2850) % 100 === 0, `шаг не соблюдён: ${v}`);
  eq(cal.snapToRange(6500, { min: 6000, max: 6520, step: 300 }), 6300, "шаг за максимум:");
  eq(cal.snapToRange(6500, { min: 7000, max: 9000 }), 7000);
  eq([cal.snapToRange(6500, null), cal.snapToRange(6500, { min: 5, max: 1 })], [null, null]);
});
await test("Android (Chrome): manual + colorTemperature -> 6500 K", () => {
  eq(cal.whiteBalancePlan({ whiteBalanceMode: ["continuous", "manual"], colorTemperature: { min: 2850, max: 7000, step: 50 } }),
    { method: "6500k", colorTemperature: 6500 });
});
await test("iPhone (Safari): только manual -> по белому экрану", () => {
  eq(cal.whiteBalancePlan({ whiteBalanceMode: ["manual", "continuous"] }), { method: "reference" });
  eq(cal.whiteBalancePlan({ whiteBalanceMode: ["manual"], colorTemperature: { min: 0, max: 0, step: 0 } }),
    { method: "reference" }, "пустой диапазон температур:");
});
await test("без manual баланс не фиксируется вовсе", () => {
  eq(cal.whiteBalancePlan({ whiteBalanceMode: ["continuous"], colorTemperature: { min: 2850, max: 7000 } }), { method: null });
  eq(cal.whiteBalancePlan({}), { method: null }); eq(cal.whiteBalancePlan(null), { method: null });
});
await test("whiteBalanceHeld: 6500 K проверяется по getSettings", () => {
  const plan = { method: "6500k", colorTemperature: 6500 };
  ok(cal.whiteBalanceHeld({ whiteBalanceMode: "manual", colorTemperature: 6500 }, plan), "6500 manual");
  ok(!cal.whiteBalanceHeld({ whiteBalanceMode: "manual", colorTemperature: 5000 }, plan), "5000 принято");
  ok(!cal.whiteBalanceHeld({ whiteBalanceMode: "continuous", colorTemperature: 6500 }, plan), "авто принято");
  ok(!cal.whiteBalanceHeld({ whiteBalanceMode: "manual" }, plan), "без температуры принято");
  ok(cal.whiteBalanceHeld({ whiteBalanceMode: "manual", colorTemperature: 6650 }, plan, 200), "шаг 200");
  ok(!cal.whiteBalanceHeld({ whiteBalanceMode: "manual", colorTemperature: 6650 }, plan, 50), "шаг 50");
});
await test("whiteBalanceHeld: заморозка по белому экрану", () => {
  const plan = { method: "reference" };
  ok(cal.whiteBalanceHeld({ whiteBalanceMode: "manual" }, plan), "manual");
  ok(!cal.whiteBalanceHeld({ whiteBalanceMode: "continuous" }, plan), "авто принято");
  ok(cal.whiteBalanceHeld({}, plan), "браузер молчит о режиме - проверить нечем");
  ok(!cal.whiteBalanceHeld(null, plan) && !cal.whiteBalanceHeld({}, { method: null }), "нет данных");
});

console.log("подписи и образцы цвета");
await test("hsToCss: hs Home Assistant (0..360, 0..100)", () => {
  eq([cal.hsToCss([0, 100]), cal.hsToCss([120, 100]), cal.hsToCss([240, 100])],
    ["rgb(255,0,0)", "rgb(0,255,0)", "rgb(0,0,255)"]);
  eq([cal.hsToCss([0, 0]), cal.hsToCss(null), cal.hsToCss([60, 50]), cal.hsToCss([360, 100])],
    ["rgb(255,255,255)", "rgb(255,255,255)", "rgb(255,255,128)", "rgb(255,0,0)"]);
});
await test("colourName: по ключу, по оттенку, белый режим лампы", () => {
  eq([cal.colourName("blue", [240, 100]), cal.colourName("red_soft", [0, 50]), cal.colourName("white_mode", null)],
    ["синий", "светло-красный", "белый режим лампы"]);
  eq([cal.colourName("x", [240, 100]), cal.colourName("x", [30, 85]), cal.colourName("x", [330, 50]), cal.colourName("x", [0, 5])],
    ["синий", "оранжевый", "светло-розовый", "белый"]);
});

console.log("что показать на ленте");
await test("itemTarget: kelvin, hs и белый режим лампы", () => {
  eq(cal.itemTarget({ key: "warm_3000", hs: null, kelvin: 3000 }), { kelvin: 3000 });
  eq(cal.itemTarget({ key: "orange", hs: [30, 85], kelvin: null }), { hs: [30, 85] });
  eq(cal.itemTarget({ key: "white_mode", hs: null, kelvin: null }), { hs: null }, "белый режим:");
  eq(cal.itemTarget({ key: "old", hs: [0, 0] }), { hs: [0, 0] }, "старый сервер без kelvin:");
  eq(cal.itemTarget({ hs: [10, 20], kelvin: 0 }), { hs: [10, 20] }, "kelvin 0 - не температура:");
});
await test("showPayload: ровно одно из hs / kelvin, поправка только если есть", () => {
  const C = { version: 1 };
  eq(cal.showPayload("light.l", { hs: [0, 0] }), { entity_id: "light.l", hs: [0, 0] });
  eq(cal.showPayload("light.l", { hs: null }), { entity_id: "light.l", hs: null }, "белый режим:");
  eq(cal.showPayload("light.l", { kelvin: 3000 }, C), { entity_id: "light.l", kelvin: 3000, calibration: C });
  eq(cal.showPayload("light.l", { kelvin: 3000 }, null), { entity_id: "light.l", kelvin: 3000 }, "сырые 3000 K:");
  for (const t of [{ hs: [1, 2] }, { kelvin: 2700 }, { hs: null }, { hs: [1, 2], kelvin: 2700 }]) {
    const p = cal.showPayload("light.l", t);
    ok(("hs" in p) !== ("kelvin" in p), `оба или ни одного: ${JSON.stringify(p)}`);
  }
});
await test("previewPayload: «как было» / «с поправкой» × белый / 3000 K", () => {
  const C = { version: 1, white: { kelvin: 6500, tint: 0 } };
  const pv = (corrected, warm, whiteMode) => cal.previewPayload("light.l", { corrected, warm }, { calibration: C, whiteMode });
  eq(pv(false, false, true), { entity_id: "light.l", hs: null }, "как было, белый, есть белый режим:");
  eq(pv(false, false, false), { entity_id: "light.l", hs: [0, 0] }, "как было, белый, без белого режима:");
  eq(pv(false, true, true), { entity_id: "light.l", kelvin: 3000 }, "как было, 3000 K:");
  eq(pv(true, false, true), { entity_id: "light.l", hs: [0, 0], calibration: C }, "с поправкой, белый:");
  eq(pv(true, true, false), { entity_id: "light.l", kelvin: 3000, calibration: C }, "с поправкой, 3000 K:");
  eq(cal.WARM_KELVIN, 3000);
});
await test("sameTarget: температура и цвет не путаются", () => {
  ok(cal.sameTarget({ kelvin: 3000 }, { kelvin: 3000 }) && !cal.sameTarget({ kelvin: 3000 }, { kelvin: 4000 }), "kelvin");
  ok(!cal.sameTarget({ kelvin: 3000 }, { hs: [30, 60] }) && !cal.sameTarget({ hs: null }, { kelvin: 3000 }), "kelvin против hs");
  ok(cal.sameTarget({ hs: null }, { hs: null }) && !cal.sameTarget({ hs: null }, { hs: [0, 0] }), "белый режим");
  ok(cal.sameTarget({ hs: [0, 0] }, { hs: [0.2, 0.2] }) && !cal.sameTarget(null, { hs: [0, 0] }), "hs");
});
await test("swatchOf: образец от сервера только как #rrggbb", () => {
  eq(cal.swatchOf({ hs: null, kelvin: 3000, swatch: "#FFB46B" }), "#FFB46B");
  eq(cal.swatchOf({ hs: [240, 100], swatch: "red;background:url(x)" }), "rgb(0,0,255)", "не #rrggbb:");
  eq(cal.swatchOf({ hs: [0, 0] }), "rgb(255,255,255)"); eq(cal.swatchOf(undefined), "rgb(255,255,255)");
});

console.log("подстройка белого");
// Те же числа, что шлёт сервер (core/color_calibration.py WHITE_RANGE / WHITE_DEFAULT):
// оттенок по умолчанию 0,0032 нарочно не на сетке ползунка с шагом 0,001.
const RANGE = { kelvin: { min: 2700, max: 9000, step: 50 }, tint: { min: -0.012, max: 0.018, step: 0.001 } };
const DEF = { kelvin: 6500, tint: 0.0032 };
const INFO = { white_default: DEF, white_range: RANGE };
await test("whiteRange: без диапазонов от сервера подстройки нет", () => {
  eq(cal.whiteRange(INFO), RANGE);
  eq(cal.whiteRange({}), null, "старый сервер:");
  eq(cal.whiteRange({ white_range: { kelvin: RANGE.kelvin } }), null, "только kelvin:");
  eq(cal.whiteRange({ white_range: { kelvin: { min: 7000, max: 5000 }, tint: RANGE.tint } }), null, "max < min:");
  eq(cal.whiteRange({ white_range: { kelvin: { min: 5000, max: 8000 }, tint: RANGE.tint } }).kelvin.step, 0, "без шага:");
});
await test("fitRange (кнопки «‹ ›»): края и шаг, мелкий шаг не округляется до тысячных", () => {
  eq(cal.fitRange(6512, RANGE.kelvin), 6500); eq(cal.fitRange(9500, RANGE.kelvin), 9000); eq(cal.fitRange(10, RANGE.kelvin), 2700);
  eq(cal.fitRange(0.00126, RANGE.tint), 0.001, "сетка от min:");
  eq(cal.fitRange(0.00126, { min: -0.02, max: 0.02, step: 0.0005 }), 0.0015, "шаг 0,0005:");
  eq(cal.fitRange(6530, { min: 6000, max: 6520, step: 300 }), 6300, "шаг за максимум:");
  eq([cal.fitRange(NaN, RANGE.kelvin), cal.fitRange(1, null)], [null, null]);
});
await test("clampRange: только в пределы, без привязки к шагу", () => {
  eq([cal.clampRange(0.0032, RANGE.tint), cal.clampRange(-1, RANGE.tint), cal.clampRange(12000, RANGE.kelvin)],
    [0.0032, -0.012, 9000]);
  eq([cal.clampRange("x", RANGE.tint), cal.clampRange(1, { min: 2, max: 1 })], [null, null]);
});
await test("whiteDefault / whiteOf: от сервера как есть, иначе 6500 K без оттенка", () => {
  eq(cal.whiteDefault(INFO), DEF, "оттенок по умолчанию привязан к сетке ползунка:");
  eq(cal.whiteDefault({ white_range: RANGE }), { kelvin: 6500, tint: 0 }, "без white_default:");
  eq(cal.whiteDefault({ white_default: { kelvin: 9500, tint: 0 }, white_range: RANGE }), { kelvin: 9000, tint: 0 }, "за краем:");
  eq(cal.whiteOf({ white: { kelvin: 6100, tint: 0.004 } }, DEF), { kelvin: 6100, tint: 0.004 });
  eq(cal.whiteOf({ matrix: [] }, DEF), DEF, "без white:");
  eq(cal.whiteOf(null, DEF), DEF);
});
await test("withWhite: копия; ответ сервера не тронут; меняется только white", () => {
  const server = { version: 2, matrix: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], curves: [[0, 0], [1, 1]], white_via_colour: true,
                   white: { kelvin: 6500, tint: 0.0032 } };
  const frozen = JSON.stringify(server);
  const adj = cal.withWhite(server, { kelvin: 6123, tint: 0.0042 }, RANGE);
  eq(JSON.stringify(server), frozen, "исходник изменён:");
  eq(adj.white, { kelvin: 6123, tint: 0.0042 }, "значение изменено сверх пределов:");
  eq(cal.withWhite(server, { kelvin: 12000, tint: -1 }, RANGE).white, { kelvin: 9000, tint: -0.012 }, "за пределы - сервер откажет:");
  const { white: _a, ...restAdj } = adj, { white: _b, ...restServer } = server;
  eq(restAdj, restServer, "поменялось не только white:");
  adj.matrix[0][0] = 9;
  eq(server.matrix[0][0], 1, "копия неглубокая:");
  eq(cal.withWhite({ version: 1 }, { kelvin: 6000, tint: 0 }, null).white, { kelvin: 6000, tint: 0 }, "без диапазона:");
});
await test("nudge: около двадцати касаний на ход, по сетке и не за край", () => {
  eq(cal.nudge(6500, -1, RANGE.kelvin), 6200); eq(cal.nudge(6500, 1, RANGE.kelvin), 6800);
  eq(cal.nudge(8950, 1, RANGE.kelvin), 9000); eq(cal.nudge(2700, -1, RANGE.kelvin), 2700);
  eq(cal.nudge(0.0032, 1, RANGE.tint), 0.005, "с оттенка по умолчанию - на сетку:");
  let v = RANGE.kelvin.min, taps = 0;
  while (v < RANGE.kelvin.max && taps < 100) { v = cal.nudge(v, 1, RANGE.kelvin); taps++; }
  ok(taps >= 15 && taps <= 25, `касаний на весь ход: ${taps}`);
});
await test("whiteWords: словами от белого по умолчанию, теплее - меньше кельвинов", () => {
  const w = (kelvin, tint) => cal.whiteWords({ kelvin, tint }, DEF, RANGE);
  eq(w(6500, 0.0032), { kelvin: "Нейтральный", tint: "Без оттенка" });
  eq(w(6500, 0.003).tint, "Без оттенка", "ближайшая к умолчанию точка ползунка:");
  eq(w(6450, 0.0042), { kelvin: "Чуть теплее", tint: "Чуть зеленее" }, "один шаг:");
  eq([w(4600, 0).kelvin, w(2700, 0).kelvin, w(7000, 0).kelvin, w(9000, 0).kelvin],
    ["Теплее", "Намного теплее", "Чуть холоднее", "Намного холоднее"]);
  eq([w(6500, -0.004).tint, w(6500, -0.012).tint], ["Розовее", "Намного розовее"]);
});

console.log("latestWins (таймеры подставные)");
const tick = () => new Promise((r) => setImmediate(r));
function timers() {
  let t = 0, seq = 0;
  const q = [];
  return {
    setTimer: (fn, ms) => { const h = ++seq; q.push({ h, at: t + ms, fn }); return h; },
    clearTimer: (h) => { const i = q.findIndex((x) => x.h === h); if (i >= 0) q.splice(i, 1); },
    async advance(ms) {
      const end = t + ms;
      for (;;) {
        q.sort((a, b) => a.at - b.at);
        if (!q.length || q[0].at > end) break;
        const x = q.shift();
        t = x.at; x.fn(); await tick();
      }
      t = end; await tick();
    },
  };
}
// Лента «отвечает», когда тест скажет: так видно, что уходит, пока команда в пути.
function strip() {
  const calls = [];
  let active = 0, max = 0;
  const send = (value, live) => new Promise((resolve, reject) => {
    active++; max = Math.max(max, active);
    calls.push({ value, live, resolve: () => { active--; resolve(); }, reject: (e) => { active--; reject(e); } });
  });
  return { send, calls, sent: () => calls.map((c) => c.value), get max() { return max; } };
}
function wins(extra = {}) {
  const k = timers(), s = strip(), log = { sent: [], errors: [], states: [] };
  const lw = cal.latestWins(s.send, { delay: 250, setTimer: k.setTimer, clearTimer: k.clearTimer,
    onSent: (v) => log.sent.push(v), onError: (e, v) => log.errors.push([e.message, v]),
    onState: (st) => log.states.push(st), ...extra });
  return { k, s, lw, log };
}

await test("дребезг: из серии значений уходит одно, последнее", async () => {
  const { k, s, lw } = wins();
  lw.schedule(1); await k.advance(100); lw.schedule(2); await k.advance(100); lw.schedule(3);
  await k.advance(249);
  eq(s.sent(), [], "ушло раньше дребезга:");
  await k.advance(1);
  eq(s.sent(), [3]);
});
await test("в пути одна команда; ждёт только самое новое, промежуточные выброшены", async () => {
  const { k, s, lw, log } = wins();
  lw.now("A"); await tick();
  lw.schedule("B"); await k.advance(300);
  lw.schedule("C"); await k.advance(300);
  lw.now("D"); await tick();
  eq(s.sent(), ["A"], "вторая команда, пока первая в пути:");
  s.calls[0].resolve(); await tick();
  eq(s.sent(), ["A", "D"]);
  s.calls[1].resolve(); await k.advance(1000);
  eq(s.sent(), ["A", "D"], "старое значение догнало новое:");
  eq(s.max, 1, "две команды в пути одновременно:");
  eq(log.sent, ["A", "D"]);
});
await test("ответ пришёл, пока новое значение в дребезге, - ждём дребезг до конца", async () => {
  const { k, s, lw } = wins();
  lw.now("A"); await tick();
  lw.schedule("B"); await k.advance(100);
  s.calls[0].resolve(); await tick();
  eq(s.sent(), ["A"], "дребезг сорван ответом:");
  await k.advance(150);
  eq(s.sent(), ["A", "B"]);
  // Готовое к отправке значение, которое перебили новым, не уходит вовсе.
  lw.now("C"); lw.schedule("D"); await tick();
  s.calls[1].resolve(); await tick();
  eq(s.sent(), ["A", "B"], "перебитое значение ушло или новое - без дребезга:");
  await k.advance(250);
  eq(s.sent(), ["A", "B", "D"]);
});
await test("now - сразу, без дребезга, и отменяет ждущий дребезг", async () => {
  const { k, s, lw } = wins();
  lw.schedule("slow"); lw.now("fast"); await tick();
  eq(s.sent(), ["fast"]);
  s.calls[0].resolve(); await k.advance(1000);
  eq(s.sent(), ["fast"], "выброшенное значение ушло позже:");
});
await test("то же, что уже на ленте или в пути, повторно не шлётся", async () => {
  const { k, s, lw } = wins({ equals: cal.samePayload });
  lw.now({ hs: [0, 0] }); await tick(); s.calls[0].resolve(); await tick();
  lw.schedule({ hs: [0, 0] }); await k.advance(300);
  eq(s.sent().length, 1, "повтор показанного:");
  lw.now({ kelvin: 3000 }); await tick();
  lw.schedule({ hs: [5, 5] }); lw.now({ kelvin: 3000 }); await tick();
  s.calls[1].resolve(); await k.advance(300);
  eq(s.sent(), [{ hs: [0, 0] }, { kelvin: 3000 }], "вернулись к тому, что в пути, - а ушло ещё:");
});
await test("ошибка: onError со значением; ждущее уходит; лента после ошибки «неизвестна»", async () => {
  const { s, lw, log } = wins();
  lw.now("A"); await tick(); s.calls[0].resolve(); await tick();
  lw.now("B"); await tick();
  lw.now("C"); s.calls[1].reject(new Error("нет связи")); await tick();
  eq(log.errors, [["нет связи", "B"]]);
  eq(s.sent(), ["A", "B", "C"], "после ошибки ждущее не ушло:");
  s.calls[2].reject(new Error("снова")); await tick();
  // На ленте может быть что угодно - «A» надо послать, хоть он и был показан.
  lw.now("A"); await tick();
  eq(s.sent(), ["A", "B", "C", "A"], "после ошибки показанное раньше считается всё ещё на ленте:");
  s.calls[3].resolve(); await tick();
  eq(log.sent, ["A", "A"]);
  // Та же команда, что в пути и не дошла, повторяется, когда её снова выбрали.
  lw.now("B"); await tick(); lw.now("A"); lw.now("B"); s.calls[4].reject(new Error("сбой")); await tick();
  eq(s.sent().slice(4), ["B", "B"], "повтор не дошедшей команды выброшен:");
});
await test("cancel: ждущее выброшено, ответ отменённой не засчитан, промис ждёт её", async () => {
  const { k, s, lw, log } = wins();
  lw.now("A"); await tick();
  lw.schedule("B");
  ok(s.calls[0].live(), "живая отправка помечена отменённой");
  let waited = false;
  lw.cancel().then(() => { waited = true; });
  ok(!s.calls[0].live(), "отменённая отправка не знает, что её отменили");
  await tick();
  eq(waited, false, "cancel не дождался команды в пути:");
  s.calls[0].resolve(); await tick();
  eq(waited, true, "cancel так и не дождался:");
  await k.advance(1000);
  eq(s.sent(), ["A"], "выброшенное ушло:"); eq(log.sent, [], "отменённая засчитана как показанная:");
  lw.now("A"); await tick(); s.calls[1].resolve(); await tick();
  eq(log.sent, ["A"]);
  // cancel перед «end»: лента вернётся как была, показанное уже не на ней.
  lw.cancel();
  lw.now("A"); await tick();
  eq(s.sent(), ["A", "A", "A"], "после cancel лента считается всё ещё показывающей прежнее:");
});
await test("после cancel новое ждёт, пока вернётся отменённая: двух в пути не бывает", async () => {
  const { s, lw } = wins();
  lw.now("A"); await tick();
  lw.cancel(); lw.now("B"); await tick();
  eq(s.sent(), ["A"]);
  s.calls[0].resolve(); await tick();
  eq(s.sent(), ["A", "B"]); eq(s.max, 1);
});
await test("onState: «отправляю» и «ждёт» для подписи на экране", async () => {
  const { k, s, lw, log } = wins();
  eq(lw.state, { sending: false, waiting: false });
  lw.schedule(1);
  eq(lw.state, { sending: false, waiting: true });
  await k.advance(250);
  eq(lw.state, { sending: true, waiting: false });
  s.calls[0].resolve(); await tick();
  eq(lw.state, { sending: false, waiting: false });
  eq(log.states[log.states.length - 1], { sending: false, waiting: false }, "последнее состояние не сообщено:");
});

// ------------------------------------------------ крючок в panel.js --
console.log("panel.js: кнопка «Калибровка цвета»");
class FakeElement {
  constructor(tag = "div") { Object.assign(this, { tagName: tag.toUpperCase(), dataset: {}, style: {}, children: [], innerHTML: "" }); }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener() {} removeEventListener() {} querySelector() { return null; }
  getElementById() { return null; } attachShadow() { this.shadowRoot = this; return this; } focus() {}
}
const registry = {};
const sandbox = {
  HTMLElement: FakeElement, URL, Date, Math, JSON, Set, Map, Intl, Promise, console,
  customElements: { define: (n, c) => (registry[n] = c), get: (n) => registry[n] },
  document: { createElement: (t) => new FakeElement(t), body: new FakeElement("body"),
              addEventListener() {}, removeEventListener() {}, visibilityState: "visible" },
  window: { history: { pushState() {} }, dispatchEvent() {} },
  setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
  navigator: {}, CustomEvent: function () {},
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(readFileSync(join(PANEL_DIR, "panel.js"), "utf8"), sandbox, { filename: "panel.js" });
const Panel = registry["bms-devices-panel"];
const panelScope = (expr) => vm.runInContext(expr, sandbox);

await test("адрес panel.js берётся из стека Chrome, Safari и Firefox вместе с ?v=", () => {
  const url = "https://ha.example:8123/bms_integration_panel/panel.js?v=2026.10.7.3-1759830000";
  const moduleUrlFrom = panelScope("moduleUrlFrom");
  eq(moduleUrlFrom(`Error\n    at ${url}:24:9`), url, "Chrome:");
  eq(moduleUrlFrom(`module code@${url}:24:9`), url, "Safari:");
  eq(moduleUrlFrom(`@${url}:24:9`), url, "Firefox:");
  eq(moduleUrlFrom("Error\n    at panel.js:24:9"), "", "без адреса:");
});
await test("calibration.js грузится с тем же ?v=, что и panel.js", () => {
  const calibrationUrl = panelScope("calibrationUrl");
  eq(calibrationUrl("https://ha.example:8123/bms_integration_panel/panel.js?v=2026.10.7.3-1759830000"),
    "https://ha.example:8123/bms_integration_panel/calibration.js?v=2026.10.7.3-1759830000");
  ok(/^\/bms_integration_panel\/calibration\.js\?t=\d+$/.test(calibrationUrl("")), "без адреса модуля кэш не сбит");
  const src = readFileSync(join(PANEL_DIR, "panel.js"), "utf8");
  ok(/import\(calibrationUrl\(\)\)/.test(src), "openCalibration грузит не calibrationUrl()");
});

const deviceFixture = () => ({
  device: { device_id: "d1", name: "Лента", entity_count: 3, entities: [
    { entity_id: "switch.rele", name: "Реле", domain: "switch", dp_id: "1", state: "on", disabled: false },
    { entity_id: "light.lenta", name: "Лента кухня", domain: "light", dp_id: "20", state: "on", disabled: false },
    { entity_id: "light.belaya", name: "Белая", domain: "light", dp_id: "21", state: "on", disabled: false },
    { entity_id: "light.off", name: "Отключённая", domain: "light", dp_id: "22", state: null, disabled: true },
  ] },
  children: [], gateway: null,
});

await test("кнопка только у света с цветом, без undefined в разметке", () => {
  const p = new Panel();
  const d = deviceFixture();
  ok(!p.dvStatus(d).includes("calib-open"), "кнопка до ответа сервера");
  p._calibInfo = { "light.lenta": { info: { supported: true, calibrated: false } },
                   "light.belaya": { info: { supported: false, reason: "нет цвета" } } };
  const html = p.dvStatus(d);
  ok(html.includes('data-act="calib-open"') && html.includes('data-eid="light.lenta"'), "нет кнопки у цветной ленты");
  ok(!html.includes('data-eid="light.belaya"'), "кнопка у света без цвета");
  ok(html.includes("Калибровка цвета") && html.includes("Не откалибрована"), "нет подписи");
  ok(!html.includes("undefined"), "undefined в разметке");
  p._calibInfo["light.lenta"].info = { supported: true, calibrated: true, calibrated_at: "2026-10-07T10:00:00+00:00" };
  const done = p.dvStatus(d);
  ok(done.includes("Откалибрована") && !done.includes("undefined"), "статус калибровки");
});
await test("info спрашивается по разу и только у включённых light", async () => {
  const p = new Panel();
  const asked = [];
  p.ws = (type, payload) => { asked.push([type, payload.entity_id]); return Promise.resolve({ supported: true }); };
  p.paint = () => {};
  p.loadCalibInfo(deviceFixture());
  p.loadCalibInfo(deviceFixture());
  await new Promise((r) => setImmediate(r));
  eq(asked, [["color_calibration/info", "light.lenta"], ["color_calibration/info", "light.belaya"]]);
  eq(p._calibInfo["light.lenta"], { info: { supported: true } });
});
await test("сбой info не повторяется каждые 6 с, но повторяется через минуту", async () => {
  const p = new Panel();
  let asked = 0;
  p.ws = () => { asked++; return Promise.reject(new Error("unknown_command")); };
  p.paint = () => {};
  const one = { device: { entities: [deviceFixture().device.entities[1]] } };
  p.loadCalibInfo(one);
  await new Promise((r) => setImmediate(r));
  ok(p._calibInfo["light.lenta"].failed, "сбой не запомнен");
  p.loadCalibInfo(one);
  eq(asked, 1, "повтор сразу:");
  p._calibInfo["light.lenta"].failed = Date.now() - 61000;
  p.loadCalibInfo(one);
  eq(asked, 2, "повтор через минуту:");
});
await test("нажатие открывает калибровку нужной сущности", async () => {
  const p = new Panel();
  let opened = null;
  p.openCalibration = (id) => { opened = id; };
  await p.onClick({ composedPath: () => [{ dataset: { act: "calib-open", eid: "light.lenta" } }] });
  eq(opened, "light.lenta");
});

console.log(`\n===== ИТОГ: PASS ${passed} / FAIL ${failed} =====`);
process.exit(failed ? 1 : 0);
