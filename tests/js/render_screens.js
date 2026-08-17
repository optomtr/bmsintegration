/**
 * Отрисовка каждого экрана панели с подставными данными.
 *
 * Панель нельзя открыть без входа в Home Assistant, а `node --check` ловит
 * только синтаксис. Здесь класс инстанцируется по-настоящему и у него
 * вызывается каждый экран: опечатка в шаблоне, обращение к полю, которого
 * бэкенд не присылает, или неверная форма данных дают исключение здесь, а не
 * у оператора на объекте.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SOURCE = path.join(__dirname, "..", "..", "custom_components",
                         "bms_integration", "panel", "panel.js");

// --- минимальный DOM ------------------------------------------------------
class FakeElement {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.dataset = {};
    this.style = {};
    this.children = [];
    this.innerHTML = "";
    this.textContent = "";
    this.scrollTop = 0;
    this.parentElement = null;
  }
  appendChild(c) { this.children.push(c); c.parentElement = this; return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); }
  remove() {}
  addEventListener() {}
  removeEventListener() {}
  querySelector() { return null; }
  getElementById() { return null; }
  attachShadow() { return this; }
  focus() {}
  click() {}
}

class FakeHTMLElement extends FakeElement {}

const registry = {};
const sandbox = {
  HTMLElement: FakeHTMLElement,
  // Повторяем настоящий реестр: у него есть и get - панель на него опирается,
  // чтобы не регистрировать имя дважды.
  customElements: {
    define: (name, cls) => (registry[name] = cls),
    get: (name) => registry[name],
  },
  document: {
    createElement: (tag) => new FakeElement(tag),
    body: new FakeElement("body"),
    addEventListener() {}, removeEventListener() {},
    visibilityState: "visible",
  },
  window: { history: { pushState() {} }, dispatchEvent() {} },
  setInterval: () => 0, clearInterval: () => {},
  setTimeout: (fn) => 0, clearTimeout: () => {},
  Date, Math, JSON, Set, Map, Intl, URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
  Blob: function () {}, console,
  navigator: { clipboard: { writeText: async () => {} } },
  CustomEvent: function () {},
  confirm: () => true, prompt: () => "1", alert: () => {},
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(SOURCE, "utf8"), sandbox, { filename: "panel.js" });

// --- подставные данные, повторяющие форму ответов бэкенда -----------------
const device = (id, name, extra = {}) => ({
  entry_id: "e1", device_id: id, name, host: "10.0.0.7", protocol: "3.5",
  model: "", node_id: null, gateway_id: null, is_subdevice: false,
  is_gateway: false, sub_device_count: 0, state: "online", connected: true,
  available: true, retries: 0, last_error: "", config_issue: "",
  enable_debug: false, last_update_age: 3.2, quiet: false,
  registry_id: "r1", area_id: "a1", area_name: "Гостиная",
  entity_count: 2, entities: [], dps_count: 2, ...extra,
});

const fixtures = {
  overview: {
    devices: [device("d1", "Реле"), device("d2", "Шлюз", { is_gateway: true, sub_device_count: 3 })],
    // Форма должна повторять _summary() из websocket.py: пропущенное поле
    // здесь означает "undefined" на плитке у оператора.
    summary: { total: 2, online: 2, connecting: 0, reconnecting: 0, grace: 0,
               unavailable: 0, config: 0, bad_gateways: 0, bad_gateway_children: 0,
               gateways: 1, subdevices: 0, quiet: 0, entities: 4 },
    entries: [{ entry_id: "e1", title: "Дом", devices: 2, entities: 4, problems: 0, no_cloud: true }],
  },
  topology: { tree: [{ id: "n1", kind: "entry", name: "Дом", state: "online", children: [
    { id: "n2", kind: "device", name: "Реле", state: "online", device_id: "d1", children: [] }] }] },
  device: { device: device("d1", "Реле", { entities: [
      { entity_id: "switch.rele", name: "Реле", domain: "switch", dp_id: "1", state: "on", disabled: false }] }),
    gateway: null, children: [], config: { host: "10.0.0.7", entities: [], dps_strings: ["1 (value: True)"] },
    entity_configs: [{ id: "1", platform: "switch", name: "Реле", device_class: "", mapping: { id: "1", platform: "switch" } }],
    dps: [{ id: "1", value: true, name: "Реле", entity: "switch.rele" }],
    events: [], values: { "1": true }, diagnostics: { history: [] } },
  logs: { entries: [{ time: Date.now() / 1000, level: "INFO", logger: "x", message: "привет" }] },
  report: { entries: [{ ts: new Date().toISOString(), event: "disconnect_detected", name: "Реле",
                        device_id: "d1", reason: "socket closed" }] },
  discovered: { devices: [
    { device_id: "new1", host: "10.0.0.9", protocol: "3.5",
      product_key: "", configured: false, serves: 0, is_known_gateway: false },
    // Шлюз, за которым уже настроены узлы: не «новое устройство».
    { device_id: "gw1", host: "10.0.0.5", protocol: "3.5", product_key: "",
      configured: false, serves: 26, is_known_gateway: true,
      serves_example: "1 этаж кухня" },
  ], listening: true },
  areas: { areas: [{ area_id: "a1", name: "Гостиная", icon: null,
                     devices: [{ device_id: "d1", name: "Реле", state: "online",
                                 entity_count: 2, registry_id: "r1", is_gateway: false }] }],
           unassigned: [{ device_id: "d2", name: "Шлюз", state: "online", entity_count: 0,
                          registry_id: null, is_gateway: true }],
           total_devices: 2 },
  settings: { entries: [{ entry_id: "e1", title: "Дом", home_name: "", grace_period: 120,
                          startup_grace: 300, watchdog_interval: 30, debug: false }],
              defaults: { grace_period: 120, startup_grace: 300, watchdog_interval: 30 } },
  replace: {
    targets: [{ entry_id: "e1", device_id: "d1", name: "Реле", host: "10.0.0.7",
                protocol: "3.5", model: "", node_id: null, gateway_id: null,
                is_subdevice: false, area_name: "Гостиная", registry_id: "r1",
                entities: [{ entity_id: "switch.rele", name: "Реле", domain: "switch",
                             state: "on", area_id: "a1" }],
                entity_count: 1, datapoints: ["1"], children: [] }],
    candidates: [{ device_id: "new1", host: "10.0.0.9", protocol: "3.5",
                   product_key: "", configured: false }],
    credentials_suspects: ["d1"],
  },
};

const Panel = registry["bms-devices-panel"];
if (!Panel) { console.error("панель не зарегистрировала свой веб-компонент"); process.exit(1); }

const screens = ["overview", "map", "device", "add", "rooms", "events", "settings"];
let failed = 0;
for (const screen of screens) {
  const panel = new Panel();
  panel.d = JSON.parse(JSON.stringify(fixtures));
  panel.s.screen = screen;
  panel.s.deviceId = "d1";
  if (screen === "add") {
    panel.s.replaceTarget = "d1";   // карточка замены раскрыта
    panel.s.addDevice = "new1";     // и форма добавления тоже
  }
  const view = {
    overview: () => panel.vOverview(), map: () => panel.vMap(), device: () => panel.vDevice(),
    add: () => panel.vAdd(), rooms: () => panel.vRooms(), events: () => panel.vEvents(),
    settings: () => panel.vSettings(),
  }[screen];
  try {
    const html = view();
    if (typeof html !== "string" || html.length < 40) {
      console.error(`  ✗ ${screen}: пустой результат`); failed++; continue;
    }
    const at = html.indexOf("undefined");
    if (at >= 0) {
      console.error(`  ✗ ${screen}: undefined в разметке -> ...${
        html.slice(Math.max(0, at - 120), at + 40).replace(/\s+/g, " ")}...`);
      failed++; continue;
    }
    console.log(`  ✓ ${screen} (${html.length} символов)`);
  } catch (err) {
    console.error(`  ✗ ${screen}: ${err && err.stack ? err.stack.split("\n").slice(0, 3).join(" | ") : err}`);
    failed++;
  }
}

// Устройство с пустыми данными: панель не должна падать до первого ответа.
try {
  const empty = new Panel();
  empty.d = { overview: null, topology: null, device: null, logs: null, report: null,
              discovered: null, areas: null, settings: null, replace: null };
  for (const s of screens) { empty.s.screen = s; empty.paint(); }
  console.log("  ✓ экраны переживают отсутствие данных");
} catch (err) {
  console.error(`  ✗ пустые данные: ${err}`); failed++;
}

// Экран «Добавить» обязан отделять шлюз от нового устройства и объяснять,
// почему узлов за шлюзом в списке нет: без этого установщик считает панель
// сломанной, а на шлюзе жмёт «Добавить».
try {
  // Тесты выше зовут отрисовку экрана напрямую - без построения DOM,
  // так что и здесь берём разметку из самого представления.
  const p2 = new Panel();
  p2.d = JSON.parse(JSON.stringify(fixtures));
  p2.s.screen = "add";
  p2.s.addDevice = null;
  const html = p2.vAdd();
  const cases = [
    ["шлюз не попал в «Найденные устройства»", /Найденные устройства · 1/.test(html)],
    ["шлюз показан отдельным блоком", html.includes("уже обслуживающие устройства")],
    ["сказано, сколько за ним настроено", html.includes("26")],
    ["объяснено, что узлы за шлюзом не вещают",
     html.includes("не вещают") || html.includes("не появятся")],
    ["на шлюзе нет кнопки «Добавить»", !html.includes('data-id="gw1"')],
  ];
  for (const [name, ok] of cases) {
    if (ok) console.log(`  ✓ ${name}`);
    else { console.error(`  ✗ ${name}`); failed++; }
  }
} catch (err) {
  console.error(`  ✗ экран «Добавить»: ${err}`); failed++;
}

process.exit(failed ? 1 : 0);
