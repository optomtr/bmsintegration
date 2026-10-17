/**
 * Ручная калибровка цвета: чистые функции (panel/manual_colors.js) и крючок
 * в карточке устройства (panel/panel.js).
 *
 * Лента и браузер здесь не нужны: шаг оттенка по кругу, черновик точек и
 * предпросмотр «последнее значение побеждает» - обычные функции, и именно в
 * них живут ошибки, которые на объекте выглядят как «ползунок не туда» или
 * «сохранил, а цвет пропал».
 *
 *   node tests/test_manual_colors_js.mjs
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const PANEL_DIR = join(HERE, "..", "custom_components", "bms_integration", "panel");

// ES-модуль в файле .js без package.json рядом: разные версии node
// по-разному угадывают его тип, поэтому грузим копию как .mjs.
const tmp = mkdtempSync(join(tmpdir(), "bms-manual-colors-"));
let mc;
try {
  const copy = join(tmp, "manual_colors.mjs");
  writeFileSync(copy, readFileSync(join(PANEL_DIR, "manual_colors.js")));
  mc = await import(pathToFileURL(copy).href);
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
const tick = () => new Promise((r) => setImmediate(r));
const pt = (target, device = target, label = "") => ({ target, device, label });

console.log("оттенок и насыщенность");
await test("оттенок идёт по кругу: 359° -> 0°, 0° -> 359°", () => {
  eq(mc.stepHue(359, 1), 0);
  eq(mc.stepHue(0, -1), 359);
  eq(mc.stepHue(180, 1), 181);
  eq(mc.stepHue(12.4, 1), 13.4, "дробный оттенок шагает на 1°, а не округляется:");
  eq(mc.stepHue(359.6, 1), 0.6);
});
await test("wrapHue: 360 - это 0, отрицательные и мусор", () => {
  eq([mc.wrapHue(360), mc.wrapHue(720), mc.wrapHue(-90), mc.wrapHue(359.96), mc.wrapHue(-0.04)], [0, 0, 270, 0, 0]);
  eq([mc.wrapHue(NaN), mc.wrapHue("x"), mc.wrapHue(undefined)], [0, 0, 0]);
});
await test("насыщенность упирается в 0 и 100", () => {
  eq([mc.stepSat(100, 1), mc.stepSat(0, -1), mc.stepSat(50, 1), mc.stepSat(50, -1)], [100, 0, 51, 49]);
  eq([mc.clampSat(140), mc.clampSat(-3), mc.clampSat(NaN)], [100, 0, 0]);
});
await test("normHs принимает только два конечных числа", () => {
  eq(mc.normHs([370, 120]), [10, 100]);
  eq(mc.normHs([12.345, 50.06]), [12.3, 50.1]);
  for (const bad of [null, undefined, [1], ["1", 2], [NaN, 1], [1, Infinity], [null, 0], "0,0", {}]) {
    eq(mc.normHs(bad), null, `${JSON.stringify(bad)}:`);
  }
});

console.log("hs -> css");
await test("основные цвета и белый как на круге Home Assistant", () => {
  eq(mc.hsToCss([0, 100]), "rgb(255,0,0)");
  eq(mc.hsToCss([120, 100]), "rgb(0,255,0)");
  eq(mc.hsToCss([240, 100]), "rgb(0,0,255)");
  eq(mc.hsToCss([60, 100]), "rgb(255,255,0)");
  eq(mc.hsToCss([0, 0]), "rgb(255,255,255)");
  eq(mc.hsToCss([60, 50]), "rgb(255,255,128)");
});
await test("360° и -120° - те же цвета, мусор - белый", () => {
  eq(mc.hsToCss([360, 100]), mc.hsToCss([0, 100]));
  eq(mc.hsToCss([-120, 100]), mc.hsToCss([240, 100]));
  eq(mc.hsToCss(null), "rgb(255,255,255)");
  eq(mc.hsToCss([30, 250]), mc.hsToCss([30, 100]), "насыщенность больше 100:");
});
await test("кружок цели: образец пресета, иначе цвет круга", () => {
  const presets = mc.normPresets([{ key: "warm", label: "Тёплый", target: [30, 50], swatch: "#FFB061" }]);
  eq(mc.targetSwatch([30.2, 50.1], presets), "#FFB061");
  eq(mc.targetSwatch([200, 80], presets), mc.hsToCss([200, 80]));
});
await test("готовые цвета по разделам, в порядке сервера", () => {
  const ps = mc.normPresets([
    { label: "2700 K", target: [30, 65], group: "Цвета Home Assistant" },
    { label: "Голубой (HA)", target: [219, 50], group: "Цвета Home Assistant" },
    { label: "3000 K", target: [30, 56], group: "Температура белого" },
    { label: "Белый", target: [0, 0] },
  ]);
  eq(ps[3].group, "Цвета", "без раздела - «Цвета»:");
  const groups = mc.groupPresets(ps);
  eq(groups.map((g) => g.group), ["Цвета Home Assistant", "Температура белого", "Цвета"]);
  eq(groups[0].items.map((p) => p.label), ["2700 K", "Голубой (HA)"]);
});
await test("название своего цвета", () => {
  eq([mc.colourLabel([0, 0]), mc.colourLabel([30, 100]), mc.colourLabel([240, 40]), mc.colourLabel([350, 90])],
    ["Белый", "Оранжевый", "Светло-синий", "Красный"]);
});

console.log("данные сервера");
await test("точки: мусор отбрасывается, подпись по умолчанию и обрезка", () => {
  const list = mc.normPoints([
    pt([0, 0], [20, 6], "  Белый  "), pt([0, 100], [3, 100]), { target: [1, 2] }, null, pt([1], [1, 1]),
    pt([240, 100], [235, 100], { evil: 1 }), pt([10, 100], [10, 100], "x".repeat(200)),
  ]);
  eq(list.length, 4);
  eq(list[0], { target: [0, 0], device: [20, 6], label: "Белый" });
  eq(list[1].label, "Красный");
  eq(list[2].label, "Синий", "подпись не строкой:");
  eq(list[3].label.length, 60);
  eq(mc.normPoints(undefined), []);
});
await test("пресеты: образец только строгим #rrggbb, без цели - пропуск", () => {
  const ps = mc.normPresets([
    { key: "white", label: "Белый", target: [0, 0], kelvin: null, swatch: "#ffffff" },
    { key: "x", label: "Злой", target: [0, 100], swatch: "red;background:url(x)" },
    { key: "bad", label: "Без цели", target: null },
  ]);
  eq(ps.map((p) => p.swatch), ["#ffffff", "rgb(255,0,0)"]);
  eq(ps.map((p) => p.key), ["white", "x"]);
});
await test("число цветов по-русски", () => {
  eq([1, 2, 5, 11, 21, 22, 24, 25, 111].map(mc.colours),
    ["1 цвет", "2 цвета", "5 цветов", "11 цветов", "21 цвет", "22 цвета", "24 цвета", "25 цветов", "111 цветов"]);
});
await test("предел точек: от сервера, иначе 24", () => {
  eq([mc.maxPoints({ max_points: 10 }), mc.maxPoints({ max_points: 0 }), mc.maxPoints({ max_points: "7" }),
      mc.maxPoints(null), mc.maxPoints({ max_points: 2.5 })], [10, 24, 7, 24, 24]);
});

console.log("черновик точек");
await test("одна цель: у белого нет оттенка, 359.8° и 0.1° - рядом", () => {
  ok(mc.sameHs([0, 0], [200, 0.3]), "белые разные");
  ok(mc.sameHs([359.8, 80], [0.1, 80]), "через 0° не совпало");
  ok(!mc.sameHs([10, 80], [11, 80]), "1° - уже другая цель");
  ok(!mc.sameHs([10, 80], [10, 81]), "1% - уже другая цель");
  ok(!mc.sameHs(null, [0, 0]));
});
await test("добавление в конец и предел", () => {
  const a = [pt([0, 0])];
  const r = mc.upsertPoint(a, pt([0, 100]), null, 2);
  eq(r.index, 1); eq(r.points.length, 2);
  eq(a.length, 1, "исходный список изменён:");
  eq(mc.upsertPoint(r.points, pt([120, 100]), null, 2), null, "сверх предела:");
  ok(!mc.canAdd(r.points, 2) && mc.canAdd(a, 2), "canAdd");
});
await test("та же цель заменяется, а не дублируется - и при полном списке", () => {
  const a = [pt([0, 0], [20, 5], "Белый"), pt([0, 100], [2, 100], "Красный")];
  const r = mc.upsertPoint(a, pt([0, 0.2], [25, 7], "Белый"), null, 2);
  eq(r.index, 0);
  eq(r.points.map((p) => p.device), [[25, 7], [2, 100]]);
});
await test("правка по индексу и вычищение дубля цели", () => {
  const a = [pt([0, 100], [1, 100]), pt([120, 100]), pt([0, 100], [9, 100])];
  const r = mc.upsertPoint(a, pt([0, 100], [5, 100]), 2, 24);
  eq(r.points.length, 2);
  eq(r.points.map((p) => p.device), [[120, 100], [5, 100]]);
  eq(r.index, 1, "индекс правленой точки после вычищения:");
});
await test("удаление; чужой индекс ничего не трогает", () => {
  const a = [pt([0, 0]), pt([0, 100]), pt([240, 100])];
  eq(mc.removePoint(a, 1).map((p) => p.target), [[0, 0], [240, 100]]);
  eq(mc.removePoint(a, 7).length, 3);
  eq(mc.removePoint(a, -1).length, 3);
  eq(mc.removePoint(a, "1").length, 3);
  eq(a.length, 3, "исходный список изменён:");
});
await test("готовые цвета, что уже в списке, не предлагаются", () => {
  const presets = mc.normPresets([{ label: "Белый", target: [0, 0] }, { label: "Красный", target: [0, 100] }]);
  eq(mc.freePresets(presets, [pt([0, 0], [20, 5])]).map((p) => p.label), ["Красный"]);
  eq(mc.freePresets(presets, []).length, 2);
});
await test("изменился ли черновик", () => {
  const a = [pt([0, 0], [20, 5], "Белый")];
  ok(mc.samePoints(a, [pt([0, 0], [20, 5], "Белый")]), "одинаковые - разные");
  ok(!mc.samePoints(a, [pt([0, 0], [21, 5], "Белый")]), "другая команда");
  ok(!mc.samePoints(a, []), "удалили");
  ok(!mc.samePoints([pt([0, 0]), pt([0, 100])], [pt([0, 100]), pt([0, 0])]), "порядок");
});
await test("запросы серверу - копии в форме договора", () => {
  const a = [pt([0, 0], [20, 5], "Белый")];
  const payload = mc.commandPayload("light.l", [30, 50], a);
  eq(payload, { entity_id: "light.l", target: [30, 50], points: [{ target: [0, 0], device: [20, 5], label: "Белый" }] });
  a[0].device[0] = 99;
  eq(payload.points[0].device, [20, 5], "черновик правится, пока запрос в пути:");
  eq(mc.showPayload("light.l", [12, 34]), { entity_id: "light.l", hs: [12, 34] });
});

console.log("предпросмотр: последнее значение побеждает");
function clock() {
  let now = 0, id = 0;
  const timers = new Map();
  return {
    setTimer: (fn, ms) => { timers.set(++id, { fn, at: now + ms }); return id; },
    clearTimer: (h) => timers.delete(h),
    async advance(ms) {
      now += ms;
      for (const [h, t] of [...timers]) if (t.at <= now) { timers.delete(h); t.fn(); }
      await tick();
    },
  };
}
function sender() {
  const calls = [];
  return { calls, send: (v) => new Promise((resolve, reject) => calls.push({ v, resolve, reject })) };
}
await test("ползунок: только после паузы и только последнее", async () => {
  const k = clock(), s = sender();
  const lw = mc.latestWins(s.send, { delay: 200, ...k });
  lw.schedule(1); await k.advance(100);
  lw.schedule(2); await k.advance(100);
  lw.schedule(3); await k.advance(199);
  eq(s.calls.length, 0, "ушло до паузы:");
  await k.advance(1);
  eq(s.calls.map((c) => c.v), [3]);
});
await test("в пути одна команда; ждёт только самое новое", async () => {
  const k = clock(), s = sender();
  const lw = mc.latestWins(s.send, { delay: 200, ...k });
  lw.now(1); await tick();
  lw.now(2); lw.now(3); lw.now(4); await tick();
  eq(s.calls.map((c) => c.v), [1], "очередь вместо одной:");
  s.calls[0].resolve(); await tick(); await tick();
  eq(s.calls.map((c) => c.v), [1, 4]);
});
await test("то же, что на ленте, повторно не шлётся; после ошибки - шлётся", async () => {
  const k = clock(), s = sender(), errs = [];
  const lw = mc.latestWins(s.send, { delay: 0, ...k, onError: (e) => errs.push(e) });
  lw.now(5); await tick(); s.calls[0].resolve(); await tick(); await tick();
  lw.now(5); await tick();
  eq(s.calls.length, 1, "повтор того же:");
  lw.now(6); await tick(); s.calls[1].reject(new Error("нет связи")); await tick(); await tick();
  eq(errs.length, 1);
  lw.now(6); await tick();
  eq(s.calls.length, 3, "после ошибки повтор не ушёл:");
});
await test("cancel: ждущее выброшено, промис дожидается команды в пути", async () => {
  const k = clock(), s = sender(), sent = [];
  const lw = mc.latestWins((v, live) => s.send(v).then(() => live()), { delay: 200, ...k, onSent: (v) => sent.push(v) });
  lw.now(1); await tick();
  lw.schedule(2);
  let done = false;
  const p = lw.cancel().then(() => { done = true; });
  await k.advance(500);
  eq(s.calls.length, 1, "ждущее ушло после cancel:");
  ok(!done, "cancel не дождался команды в пути");
  s.calls[0].resolve(); await p;
  ok(done, "cancel не завершился");
  eq(sent, [], "отменённая команда засчитана как показанная:");
});

// ------------------------------------------------ крючок в panel.js --
console.log("panel.js: кнопка «Ручная калибровка»");
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
const panelSrc = readFileSync(join(PANEL_DIR, "panel.js"), "utf8");
vm.runInContext(panelSrc, sandbox, { filename: "panel.js" });
const Panel = registry["bms-devices-panel"];
const panelScope = (expr) => vm.runInContext(expr, sandbox);

await test("manual_colors.js грузится с тем же ?v=, что и panel.js", () => {
  const url = panelScope("manualColorsUrl");
  eq(url("https://ha.example:8123/bms_integration_panel/panel.js?v=2026.10.17.1-1760000000"),
    "https://ha.example:8123/bms_integration_panel/manual_colors.js?v=2026.10.17.1-1760000000");
  ok(/^\/bms_integration_panel\/manual_colors\.js\?t=\d+$/.test(url("")), "без адреса модуля кэш не сбит");
  ok(/import\(manualColorsUrl\(\)\)/.test(panelSrc), "openManualColors грузит не manualColorsUrl()");
});

const light = { entity_id: "light.lenta", name: "Лента <b>кухня</b>", domain: "light", dp_id: "20", state: "on", disabled: false };
const fixture = () => ({ device: { device_id: "d1", name: "Лента", entity_count: 2, entities: [
  { entity_id: "switch.rele", name: "Реле", domain: "switch", dp_id: "1", state: "on", disabled: false }, light,
  { entity_id: "light.off", name: "Откл", domain: "light", dp_id: "22", state: null, disabled: true },
] }, children: [], gateway: null });

await test("кнопка рядом с «Калибровка цвета», статус «Ручная: N цветов»", () => {
  const p = new Panel();
  p._calibInfo = { "light.lenta": { info: { supported: true, calibrated: true, calibrated_at: "2026-10-07T10:00:00+00:00" } } };
  ok(!p.dvStatus(fixture()).includes("manual-open"), "кнопка до ответа color_points/get");
  p._pointsInfo = { "light.lenta": { info: { supported: true, points: [] } } };
  let html = p.dvStatus(fixture());
  ok(html.includes('data-act="manual-open"') && html.includes("Ручная калибровка"), "нет кнопки");
  ok(html.includes("Откалибрована") && !html.includes("Ручная:"), "статус без ручных точек");
  for (const [n, words] of [[1, "1 цвет"], [3, "3 цвета"], [5, "5 цветов"], [21, "21 цвет"]]) {
    p._pointsInfo["light.lenta"].info.points = Array.from({ length: n }, () => ({ target: [0, 0], device: [0, 0] }));
    html = p.dvStatus(fixture());
    ok(html.includes(`Ручная: ${words}`), `нет «Ручная: ${words}»`);
  }
  ok(!html.includes("<b>кухня</b>") && html.includes("&lt;b&gt;"), "имя света не экранировано");
  ok(!html.includes("undefined"), "undefined в разметке");
  p._pointsInfo["light.lenta"].info = { supported: false, reason: "нет" };
  ok(!p.dvStatus(fixture()).includes("manual-open"), "кнопка у неподдерживаемого");
});
await test("color_points/get по разу на включённый свет; сбой - не чаще раза в минуту", async () => {
  const p = new Panel();
  const asked = [];
  let fail = true;
  p.ws = (type, payload) => { asked.push([type, payload.entity_id]); return fail ? Promise.reject(new Error("x")) : Promise.resolve({ supported: true, points: [] }); };
  p.paint = () => {};
  p.loadPointsInfo(fixture()); p.loadPointsInfo(fixture());
  await tick();
  eq(asked, [["color_points/get", "light.lenta"]]);
  ok(p._pointsInfo["light.lenta"].failed, "сбой не запомнен");
  p.loadPointsInfo(fixture());
  eq(asked.length, 1, "повтор сразу:");
  p._pointsInfo["light.lenta"].failed = Date.now() - 61000;
  fail = false;
  p.loadPointsInfo(fixture());
  await tick();
  eq(asked.length, 2, "повтор через минуту:");
  eq(p._pointsInfo["light.lenta"], { info: { supported: true, points: [] } });
});
await test("нажатие открывает ручную калибровку нужной сущности", async () => {
  const p = new Panel();
  let opened = null;
  p.openManualColors = (id) => { opened = id; };
  await p.onClick({ composedPath: () => [{ dataset: { act: "manual-open", eid: "light.lenta" } }] });
  eq(opened, "light.lenta");
});

console.log(`\n===== ИТОГ: PASS ${passed} / FAIL ${failed} =====`);
process.exit(failed ? 1 : 0);
