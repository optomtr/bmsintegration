/**
 * Жизненный цикл панели: как до неё доходят данные.
 *
 * render_screens.js рисует экраны с готовыми данными и НИКОГДА не вызывает
 * refresh() - поэтому он не заметил, как панель навсегда зависла на
 * «Загрузка…» у оператора: Home Assistant присваивает el.hass, и если
 * присвоение произошло до апгрейда элемента до нашего класса, оно оседает
 * обычным полем поверх сеттера. Сеттер не срабатывает никогда, refresh()
 * выходит на первой строке, данных нет.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SOURCE = path.join(__dirname, "..", "..", "custom_components",
                         "bms_integration", "panel", "panel.js");

class FakeElement {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.dataset = {}; this.style = {}; this.children = [];
    this.innerHTML = ""; this.textContent = ""; this.scrollTop = 0;
    this.parentElement = null; this._byId = {};
  }
  appendChild(c) { this.children.push(c); c.parentElement = this; return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); }
  remove() {} addEventListener() {} removeEventListener() {}
  querySelector() { return null; }
  // Браузер возвращает null, пока элемента нет в разметке. Прежняя заглушка
  // услужливо создавала его на лету - и именно поэтому не заметила, что
  // панель рисует шапку до построения DOM и падает у оператора.
  getElementById(id) {
    if (!String(this.innerHTML).includes(`id="${id}"`)) return null;
    return (this._byId[id] ||= new FakeElement());
  }
  // В браузере attachShadow сам выставляет свойство shadowRoot - шим обязан
  // повторять это, иначе тест ловит собственную неточность, а не дефект.
  attachShadow() { this.shadowRoot = this; return this; }
  focus() {} click() {}
}
class FakeHTMLElement extends FakeElement {}

const registry = {};
const timers = [];
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
  setInterval: (fn) => (timers.push(fn), timers.length),
  clearInterval: () => {}, setTimeout: (fn) => 0, clearTimeout: () => {},
  Date, Math, JSON, Set, Map, Intl, Promise,
  URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
  Blob: function () {}, console,
  navigator: { clipboard: { writeText: async () => {} } },
  CustomEvent: function () {},
  confirm: () => true, prompt: () => "1", alert: () => {},
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(SOURCE, "utf8"), sandbox, { filename: "panel.js" });

const Panel = registry["bms-devices-panel"];
let pass = 0, fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`  ✓ ${name}`); }
  else { fail++; console.log(`  ✗ ПРОВАЛ ${name}${detail ? " — " + detail : ""}`); }
};

// Ответы бэкенда не важны: важно, дошёл ли вызов вообще.
const REPLIES = {
  "bms_integration/overview": {
    devices: [], entries: [],
    summary: { total: 0, online: 0, connecting: 0, reconnecting: 0, grace: 0,
               unavailable: 0, config: 0, bad_gateways: 0, bad_gateway_children: 0,
               gateways: 0, subdevices: 0, quiet: 0, entities: 0 },
  },
  "bms_integration/settings": { entries: [], defaults: {} },
  "bms_integration/report": { entries: [], total: 0 },
};
const makeHass = (calls) => ({
  callWS: (msg) => {
    calls.push(msg.type);
    return Promise.resolve(REPLIES[msg.type] ?? {});
  },
});

async function settle() { for (let i = 0; i < 10; i++) await Promise.resolve(); }

(async () => {
  console.log("\n== Панель получает hass ==");

  // 1. Обычный порядок: элемент уже апгрейднут, hass приходит через сеттер.
  {
    const calls = [];
    const el = new Panel();
    el.hass = makeHass(calls);
    el.connectedCallback();
    await settle();
    check("штатный порядок: запросы ушли", calls.length > 0, `вызовов ${calls.length}`);
  }

  // 2. Гонка апгрейда: Home Assistant присвоил hass ДО того, как браузер
  //    обновил элемент до нашего класса. Ровно то, что случилось у оператора.
  {
    const calls = [];
    // Браузер до апгрейда держит обычный HTMLElement: HA присваивает ему
    // hass, и присвоение оседает СОБСТВЕННЫМ полем экземпляра.
    const raw = new FakeHTMLElement("bms-devices-panel");
    raw.hass = makeHass(calls);
    // Апгрейд: прототип меняется на наш класс, конструктор отрабатывает -
    // но собственное поле hass остаётся и перекрывает сеттер прототипа.
    Object.setPrototypeOf(raw, Panel.prototype);
    const fresh = new Panel();
    for (const key of Object.keys(fresh)) {
      if (key !== "hass") raw[key] = fresh[key];
    }
    raw.attachShadow({ mode: "open" });
    raw.connectedCallback();
    await settle();
    check("гонка апгрейда: панель всё равно получает hass",
          calls.length > 0,
          "hass осел обычным полем поверх сеттера — панель навсегда в «Загрузка…»");
  }

  // 3. hass приходит уже после подключения к DOM (тоже штатный путь HA).
  {
    const calls = [];
    const el = new Panel();
    el.connectedCallback();
    el.hass = makeHass(calls);
    await settle();
    check("hass после подключения: запросы ушли", calls.length > 0);
  }

  // 4. Бэкенд ответил не той формой: панель обязана показать хоть что-то,
  //    а не замереть на прежнем кадре.
  {
    const calls = [];
    const el = new Panel();
    el.hass = { callWS: (m) => (calls.push(m.type), Promise.resolve({})) };
    el.connectedCallback();
    await settle();
    const main = el.shadowRoot.getElementById("main");
    check("неполный ответ не замораживает панель",
          calls.length > 0 && typeof main.innerHTML === "string" && main.innerHTML.length > 0,
          `в главной области ${main.innerHTML.length} символов`);
  }

  // 5. hass приходит ДО подключения к DOM - штатный порядок Home Assistant.
  //    Ни исключения наружу, ни залипшего флага занятости быть не должно.
  {
    const calls = [];
    const el = new Panel();
    let threw = null;
    try { el.hass = makeHass(calls); } catch (err) { threw = err; }
    check("присвоение hass до построения DOM не бросает наружу", !threw,
          threw && String(threw));
    await settle();
    el.connectedCallback();
    await settle();
    check("после подключения запросы всё-таки ушли", calls.length > 0,
          `вызовов ${calls.length}, занято=${el._inflight}`);
    check("флаг занятости не залип", el._inflight === false);
  }

  console.log(`\n===== ИТОГ: PASS ${pass} / FAIL ${fail} =====`);
  if (fail) process.exit(1);
})();
