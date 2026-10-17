/**
 * Калибровка шторы в панели: чистые функции и окно
 * (panel/cover_calibration.js), крючок в карточке устройства (panel/panel.js).
 *
 * Штора и браузер здесь не нужны: окно гоняется на поддельном DOM, сервер -
 * поддельный ws. Ошибки, которые на объекте выглядят как «отметил края, а
 * сохранилось не то» или «калибровка кончилась, а окно показывает прошлый
 * итог», живут именно здесь.
 *
 *   node tests/test_cover_calibration_js.mjs
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const PANEL_DIR = join(HERE, "..", "custom_components", "bms_integration", "panel");

// ------------------------------------------------- поддельный DOM окна --
const registry = {};
// Узел окна: считает, сколько раз его разметку переписали целиком.
function node(id) {
  let html = "";
  const n = { id, textContent: "", hidden: false, className: "", writes: 0, querySelector: () => null };
  Object.defineProperty(n, "innerHTML", { get: () => html, set: (v) => { html = v; n.writes++; } });
  return n;
}
class FakeElement {
  constructor(tag = "div") { Object.assign(this, { tagName: String(tag).toUpperCase(), dataset: {}, style: {}, children: [], innerHTML: "", isConnected: false }); }
  appendChild(c) { this.children.push(c); c.isConnected = true; c.connectedCallback?.(); return c; }
  addEventListener() {} removeEventListener() {} querySelector() { return null; } querySelectorAll() { return []; }
  getElementById() { return null; } focus() {}
  attachShadow() {
    const ids = {};
    this.shadowRoot = { innerHTML: "", activeElement: null, addEventListener() {}, querySelector: () => null, querySelectorAll: () => [],
      getElementById: (id) => (ids[id] ||= node(id)) };
    return this.shadowRoot;
  }
  remove() { this.isConnected = false; }
}
Object.assign(globalThis, {
  HTMLElement: FakeElement,
  customElements: { define: (n, c) => (registry[n] = c), get: (n) => registry[n] },
});
globalThis.document = { visibilityState: "visible", body: new FakeElement("body"),
  createElement: (t) => (registry[t] ? new registry[t]() : new FakeElement(t)) };

// ES-модуль в файле .js без package.json рядом: грузим копию как .mjs.
const tmp = mkdtempSync(join(tmpdir(), "bms-cover-calibration-"));
let cc;
try {
  const copy = join(tmp, "cover_calibration.mjs");
  writeFileSync(copy, readFileSync(join(PANEL_DIR, "cover_calibration.js")));
  cc = await import(pathToFileURL(copy).href);
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

console.log("чистые функции");
await test("показание мотора: целое 0..100, остальное - нет показания", () => {
  eq([cc.rawValue(7), cc.rawValue(99.6), cc.rawValue(-3), cc.rawValue(140)], [7, 100, 0, 100]);
  for (const bad of [null, undefined, "7", NaN, Infinity, true]) eq(cc.rawValue(bad), null, `${String(bad)}:`);
});
await test("normInfo: мусор от сервера не ломает окно", () => {
  const i = cc.normInfo({ supported: true, open_raw: 7, closed_raw: 100, raw: 55, position: 48, state: "open",
    calibrating: false, last_run: { ok: true, text: "Готово", at: 1760000000.5 } });
  eq([i.open, i.closed, i.raw, i.position, i.lastRun.at], [7, 100, 55, 48, 1760000000.5]);
  const z = cc.normInfo(null);
  eq([z.supported, z.open, z.raw, z.lastRun, z.calibrating], [false, null, null, null, false]);
  eq(cc.normInfo({ last_run: { ok: true } }).lastRun, null, "итог без текста:");
  const junk = cc.normInfo({ open_raw: "7", closed_raw: { x: 1 }, raw: "55", position: NaN });
  eq([junk.open, junk.closed, junk.raw, junk.position], [null, null, null, null], "мусор вместо чисел:");
});
await test("строка краёв", () => {
  eq(cc.endsText(null, null), "Не откалибрована");
  eq(cc.endsText(7, 100), "Открыто 7 · закрыто 100");
  eq(cc.endsText(7, null), "Открыто 7 · закрыто по умолчанию");
});
await test("сохранить можно только два разнесённых края", () => {
  ok(cc.saveProblem(null, 100), "без открытого края");
  ok(cc.saveProblem(7, null), "без закрытого края");
  ok(cc.saveProblem(50, 55).includes("почти совпадают"), "близкие края");
  eq(cc.saveProblem(50, 50 + cc.MIN_SPREAD), "");
  eq(cc.saveProblem(7, 100), "");
  eq(cc.saveProblem(100, 7), "", "мотор, считающий наоборот:");
});
await test("итог калибровки - только новый, не прошлый", () => {
  const info = (at, calibrating = false) => cc.normInfo({ calibrating, last_run: at === null ? null : { ok: true, text: "x", at } });
  eq(cc.freshRun(info(100), 100), null, "прошлый итог:");
  eq(cc.freshRun(info(200, true), 100), null, "ещё едет:");
  eq(cc.freshRun(info(null), 100), null, "итога нет:");
  eq(cc.freshRun(info(200), 100)?.at, 200);
  eq(cc.freshRun(info(5), 0)?.at, 5, "первая калибровка:");
});

await test("вписанное руками: пусто и мусор - нет края, за шкалой - край шкалы", () => {
  eq([cc.parseRaw("93"), cc.parseRaw(" 7 "), cc.parseRaw("150"), cc.parseRaw("-4"), cc.parseRaw("7,6")], [93, 7, 100, 0, 8]);
  for (const bad of ["", "  ", "abc", null, undefined, "9x"]) eq(cc.parseRaw(bad), null, `${JSON.stringify(bad)}:`);
});
await test("− и +: шаг 1 в пределах 0..100, пустой край - с запасного значения", () => {
  eq([cc.stepEnd(40, 1, 0), cc.stepEnd(40, -1, 0), cc.stepEnd(100, 1, 0), cc.stepEnd(0, -1, 0)], [41, 39, 100, 0]);
  eq(cc.stepEnd(null, 1, 40), 40, "пустой с показания мотора:");
  eq([cc.startFor("open", 40), cc.startFor("open", null), cc.startFor("closed", null)], [40, 100, 0]);
});

// ------------------------------------------------------------ окно --
console.log("окно калибровки");
function server(over = {}) {
  const s = {
    calls: [], fail: {},
    info: { supported: true, can_auto: true, can_goto: true, has_reading: true, open_raw: null, closed_raw: null,
      raw: 100, position: 100, state: "open", calibrating: false, last_run: null, ...over },
  };
  s.ws = async (type, payload) => {
    s.calls.push([type, payload]);
    if (s.fail[type]) throw s.fail[type];
    if (type === "cover_calibration/info") return JSON.parse(JSON.stringify(s.info));
    if (type === "cover_calibration/set") Object.assign(s.info, { open_raw: payload.open_raw, closed_raw: payload.closed_raw });
    return { ok: true };
  };
  return s;
}
async function open(srv) {
  let closedWith;
  const el = cc.openCoverCalibration({ entityId: "cover.tiul", name: "Тюль", ws: srv.ws, toast() {},
    onClose: (changed) => { closedWith = changed; } });
  await tick(); await tick();
  return { el, html: () => el.$("c").innerHTML, click: async (act) => { await el.onClick({ composedPath: () => [{ dataset: { act } }] }); await tick(); },
    type: async (k, value) => { el.onInput({ composedPath: () => [{ dataset: { k }, value }] }); await tick(); },
    writes: () => el.$("c").writes,
    closedWith: () => closedWith, sent: (type) => srv.calls.filter((c) => c[0] === type).map((c) => c[1]) };
}

await test("показывает, что сообщает мотор, и края", async () => {
  const srv = server({ raw: 100, position: 0, state: "closed", open_raw: 7, closed_raw: 100 });
  const w = await open(srv);
  ok(w.html().includes(">100<") && w.html().includes("0%"), "нет показания мотора или процента");
  ok(w.html().includes("Открыто 7 · закрыто 100"), "нет краёв");
  ok(w.html().includes("Закрыта"), "нет состояния");
  ok(w.html().includes("Motor Direction"), "нет подсказки о направлении мотора");
  ok(!w.html().includes("undefined") && !w.html().includes("null"), "undefined/null в разметке");
  w.el.close();
});
await test("вручную: взять края с мотора и сохранить", async () => {
  const srv = server();
  const w = await open(srv);
  srv.info.raw = 7; await w.el.poll();
  await w.click("take-open");
  srv.info.raw = 100; await w.el.poll();
  await w.click("take-closed");
  ok(w.html().includes("можно сохранить"), "нет подтверждения краёв");
  await w.click("save");
  eq(w.sent("cover_calibration/set"), [{ entity_id: "cover.tiul", open_raw: 7, closed_raw: 100 }]);
  ok(w.html().includes("Сохранено.") && /data-act="save" disabled/.test(w.html()), "после сохранения не «Сохранено.»");
  w.el.close();
  eq(w.closedWith(), true, "панель не узнала о перемене:");
});
await test("близкие края не уходят на сервер", async () => {
  const srv = server({ raw: 50 });
  const w = await open(srv);
  await w.click("take-open");
  srv.info.raw = 55; await w.el.poll();
  await w.click("take-closed");
  ok(w.html().includes("почти совпадают"), "нет объяснения");
  ok(/data-act="save" disabled/.test(w.html()), "кнопка «Сохранить» не выключена");
  await w.click("save");
  eq(w.sent("cover_calibration/set"), []);
  w.el.close();
  eq(w.closedWith(), false, "без перемен панель не перезапрашивает:");
});
await test("кнопки движения шлют команду своей шторе", async () => {
  const srv = server();
  const w = await open(srv);
  for (const a of ["move-open", "move-stop", "move-close"]) await w.click(a);
  eq(w.sent("cover_calibration/move"), [{ entity_id: "cover.tiul", action: "open" }, { entity_id: "cover.tiul", action: "stop" },
    { entity_id: "cover.tiul", action: "close" }]);
  w.el.close();
});
await test("автоматически: идёт, затем новый итог, а не прошлый", async () => {
  const srv = server({ last_run: { ok: false, text: "Прошлая ошибка", at: 100 } });
  const w = await open(srv);
  srv.info = { ...srv.info, calibrating: true, last_run: null };
  await w.click("run");
  eq(w.sent("cover_calibration/run"), [{ entity_id: "cover.tiul" }]);
  ok(w.html().includes("Идёт калибровка"), "нет «идёт калибровка»");
  ok(/data-act="run" class|data-act="run"[^>]*disabled/.test(w.html()), "повторный запуск не выключен");
  ok(!w.html().includes("Прошлая ошибка"), "показан прошлый итог");
  srv.info = { ...srv.info, calibrating: false, open_raw: 7, closed_raw: 100,
    last_run: { ok: true, text: "Готово: мотор показывает 7 и 100", at: 200 } };
  await w.el.poll();
  ok(w.html().includes("Готово: мотор показывает 7 и 100") && w.html().includes('class="ok"'), "нет нового итога");
  ok(!w.html().includes("Идёт калибровка"), "всё ещё «идёт»");
  w.el.close();
  eq(w.closedWith(), true);
});
await test("сервер ещё не отметил запуск - прошлый итог не выдаётся за новый", async () => {
  const srv = server({ last_run: { ok: true, text: "Старый итог", at: 100 } });
  const w = await open(srv);
  await w.click("run");
  ok(!w.html().includes("Старый итог"), "старый итог как новый");
  ok(w.html().includes("Идёт калибровка"), "окно не ждёт итога");
  w.el.close();
});
await test("полностью вручную: мотор молчит - края числами, без автоматики", async () => {
  const srv = server({ raw: null, position: null, has_reading: false, can_auto: false });
  const w = await open(srv);
  ok(w.html().includes("не сообщает положение"), "нет объяснения про молчащий мотор");
  ok(!w.html().includes('data-act="run"'), "автоматика у мотора без показаний");
  ok(!w.html().includes("take-open"), "«Взять с мотора» без показаний");
  ok(/data-k="open"/.test(w.html()) && /data-k="closed"/.test(w.html()), "нет полей для чисел");
  await w.type("open", "93");
  await w.type("closed", "0");
  await w.click("save");
  eq(w.sent("cover_calibration/set"), [{ entity_id: "cover.tiul", open_raw: 93, closed_raw: 0 }]);
  w.el.close();
});
await test("− и + подгоняют край; пустой начинается с мотора или с конца шкалы", async () => {
  const srv = server({ raw: 40 });
  const w = await open(srv);
  await w.click("inc-open");
  eq(w.el.open, 40, "пустой «+» не с показания мотора:");
  await w.click("inc-open"); await w.click("inc-open");
  eq(w.el.open, 42);
  await w.click("dec-open");
  eq(w.el.open, 41);
  const quiet = server({ raw: null, has_reading: false });
  const q = await open(quiet);
  await q.click("inc-open"); await q.click("dec-closed");
  eq([q.el.open, q.el.closed], [100, 0], "без показаний - концы шкалы:");
  await q.click("dec-closed");
  eq(q.el.closed, 0, "ниже нуля:");
  w.el.close(); q.el.close();
});
await test("сохранённые края - в полях для подгонки; без перемен «Сохранено.»", async () => {
  const srv = server({ open_raw: 7, closed_raw: 100 });
  const w = await open(srv);
  ok(/data-k="open" value="7"/.test(w.html()) && /data-k="closed" value="100"/.test(w.html()), "сохранённые края не в полях");
  ok(w.html().includes("Сохранено.") && /data-act="save" disabled/.test(w.html()), "сохранять нечего, а кнопка горит");
  await w.click("inc-open");
  ok(!/data-act="save" disabled/.test(w.html()), "после подгонки сохранить нельзя");
  await w.click("save");
  eq(w.sent("cover_calibration/set"), [{ entity_id: "cover.tiul", open_raw: 8, closed_raw: 100 }]);
  w.el.close();
});
await test("«Поехать сюда» шлёт мотору число как есть", async () => {
  const srv = server();
  const w = await open(srv);
  await w.type("open", "95");
  await w.click("goto-open");
  eq(w.sent("cover_calibration/goto"), [{ entity_id: "cover.tiul", raw: 95 }]);
  await w.click("goto-closed");
  eq(w.sent("cover_calibration/goto").length, 1, "пустой край ушёл мотору:");
  w.el.close();
  const nocmd = await open(server({ can_goto: false }));
  ok(!nocmd.html().includes("goto-open"), "«Поехать сюда» у шторы без команды положения");
  nocmd.el.close();
});
await test("пока печатают, поле не пересоздаётся; числа сверху обновляются", async () => {
  const srv = server({ raw: 40 });
  const w = await open(srv);
  const before = w.writes();
  await w.type("open", "9");
  await w.el.poll();
  srv.info.raw = 55; srv.info.position = 55;
  await w.el.poll();
  eq(w.writes(), before, "разметка переписана под пальцами:");
  eq(w.el.$("raw").textContent, "55", "показание мотора не обновилось:");
  eq(w.el.open, 9);
  w.el.close();
});
await test("сброс - только после подтверждения", async () => {
  const srv = server({ open_raw: 7, closed_raw: 100 });
  const w = await open(srv);
  await w.click("reset-ask");
  eq(w.sent("cover_calibration/set"), [], "сброшено без подтверждения:");
  ok(w.html().includes("Сбросить калибровку?"), "нет вопроса");
  await w.click("reset");
  eq(w.sent("cover_calibration/set"), [{ entity_id: "cover.tiul", open_raw: null, closed_raw: null }]);
  w.el.close();
});
await test("ошибка сервера - словами; старый сервер - «обновите интеграцию»", async () => {
  const srv = server();
  const w = await open(srv);
  srv.fail["cover_calibration/move"] = new Error("Штора сейчас недоступна.");
  await w.click("move-open");
  ok(w.html().includes("Штора сейчас недоступна."), "нет текста ошибки");
  w.el.close();
  const old = server();
  old.fail["cover_calibration/info"] = { code: "unknown_command", message: "Unknown command." };
  const w2 = await open(old);
  ok(w2.html().includes("обновите интеграцию"), "нет совета обновиться");
  w2.el.close();
});
await test("штора без положения - объяснение вместо кнопок", async () => {
  const w = await open(server({ supported: false, reason: "Калибровка нужна шторам, которые сами сообщают положение." }));
  ok(w.html().includes("сами сообщают положение"), "нет причины");
  ok(!w.html().includes('data-act="run"') && !w.html().includes('data-act="save"'), "кнопки у неподдерживаемой");
  w.el.close();
});
await test("закрытое окно больше не опрашивает сервер", async () => {
  const srv = server();
  const w = await open(srv);
  w.el.close();
  const n = srv.calls.length;
  await w.el.poll();
  eq(srv.calls.length, n);
});

// ------------------------------------------------ крючок в panel.js --
console.log("panel.js: карточка «Калибровка штор»");
const sandbox = {
  HTMLElement: FakeElement, URL, Date, Math, JSON, Set, Map, Intl, Promise, console, Number,
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

await test("cover_calibration.js грузится с тем же ?v=, что и panel.js", () => {
  const url = panelScope("coverCalibrationUrl");
  eq(url("https://ha.example:8123/bms_integration_panel/panel.js?v=2026.10.17.5-1760000000"),
    "https://ha.example:8123/bms_integration_panel/cover_calibration.js?v=2026.10.17.5-1760000000");
  ok(/import\(coverCalibrationUrl\(\)\)/.test(panelSrc), "openCoverCalibration грузит не coverCalibrationUrl()");
  const panelPy = readFileSync(join(PANEL_DIR, "..", "panel.py"), "utf8");
  ok(panelPy.includes('"cover_calibration.js"'), "panel.py не сбивает кэш cover_calibration.js при выпуске");
});

const cover = { entity_id: "cover.tiul", name: "Тюль <b>зал</b>", domain: "cover", dp_id: "1", state: "open", disabled: false };
const fixture = () => ({ device: { device_id: "d1", name: "Тюль", entity_count: 2, entities: [
  { entity_id: "switch.rele", name: "Реле", domain: "switch", dp_id: "9", state: "on", disabled: false }, cover,
  { entity_id: "cover.off", name: "Откл", domain: "cover", dp_id: "2", state: null, disabled: true },
] }, children: [], gateway: null });

await test("карточка - только у поддерживаемой шторы, края в статусе", () => {
  const p = new Panel();
  ok(!p.dvStatus(fixture()).includes("cover-calib-open"), "кнопка до ответа сервера");
  p._coverInfo = { "cover.tiul": { info: { supported: true, open_raw: null, closed_raw: null } } };
  let html = p.dvStatus(fixture());
  ok(html.includes('data-act="cover-calib-open"') && html.includes("Калибровка шторы"), "нет кнопки");
  ok(html.includes("Не откалибрована"), "нет статуса без калибровки");
  p._coverInfo["cover.tiul"].info = { supported: true, open_raw: 7, closed_raw: 100 };
  html = p.dvStatus(fixture());
  ok(html.includes("Открыто 7 · закрыто 100"), "нет краёв в статусе");
  ok(!html.includes("<b>зал</b>") && html.includes("&lt;b&gt;"), "имя шторы не экранировано");
  ok(!html.includes("undefined"), "undefined в разметке");
  p._coverInfo["cover.tiul"].info = { supported: false, reason: "нет" };
  ok(!p.dvStatus(fixture()).includes("cover-calib-open"), "кнопка у неподдерживаемой");
});
await test("cover_calibration/info по разу на включённую штору; сбой - не чаще раза в минуту", async () => {
  const p = new Panel();
  const asked = [];
  let fail = true;
  p.ws = (type, payload) => { asked.push([type, payload.entity_id]); return fail ? Promise.reject(new Error("x")) : Promise.resolve({ supported: true }); };
  p.paint = () => {};
  p.loadCoverInfo(fixture()); p.loadCoverInfo(fixture());
  await tick();
  eq(asked, [["cover_calibration/info", "cover.tiul"]]);
  p.loadCoverInfo(fixture());
  eq(asked.length, 1, "повтор сразу:");
  p._coverInfo["cover.tiul"].failed = Date.now() - 61000;
  fail = false;
  p.loadCoverInfo(fixture());
  await tick();
  eq(asked.length, 2, "повтор через минуту:");
  eq(p._coverInfo["cover.tiul"], { info: { supported: true } });
});
await test("нажатие открывает калибровку нужной шторы", async () => {
  const p = new Panel();
  let opened = null;
  p.openCoverCalibration = (id) => { opened = id; };
  await p.onClick({ composedPath: () => [{ dataset: { act: "cover-calib-open", eid: "cover.tiul" } }] });
  eq(opened, "cover.tiul");
});

console.log(`\n===== ИТОГ: PASS ${passed} / FAIL ${failed} =====`);
process.exit(failed ? 1 : 0);
