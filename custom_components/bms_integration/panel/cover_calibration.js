/**
 * Калибровка шторы - края мотора: что он показывает, когда штора открыта и
 * когда закрыта.
 *
 * На объекте мотор считал в обратную сторону и не доходил до края шкалы:
 * закрытая штора показывала 100, открытая 7. Владелец попросил настраивать это
 * через интерфейс, как калибровку цвета. Два пути: автоматически (штора сама
 * едет в оба края, сервер записывает показания) и вручную (человек доводит
 * штору кнопками и говорит «это открыто», «это закрыто»).
 *
 * Панель грузит модуль лениво и даёт ему только ws(name, payload),
 * toast(text, bad) и onClose(changed). Сверху - чистые функции: они
 * проверяются в node (tests/test_cover_calibration_js.mjs) без браузера.
 */

// ====================================================== чистые функции ==
// Меньший разброс краёв - штору не довели (то же число в cover.py).
export const MIN_SPREAD = 10;
export const POLL_MS = 1000;

/** Показание мотора: целое 0..100, иначе null. */
export function rawValue(v) {
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  return Math.min(100, Math.max(0, Math.round(v)));
}

export function normInfo(info) {
  const i = info && typeof info === "object" ? info : {};
  const run = i.last_run && typeof i.last_run === "object" ? i.last_run : null;
  return {
    supported: i.supported === true,
    reason: typeof i.reason === "string" ? i.reason : "",
    open: rawValue(i.open_raw),
    closed: rawValue(i.closed_raw),
    raw: rawValue(i.raw),
    position: rawValue(i.position),
    state: typeof i.state === "string" ? i.state : "",
    calibrating: i.calibrating === true,
    lastRun: run && typeof run.text === "string"
      ? { ok: run.ok === true, text: run.text, at: Number(run.at) || 0 } : null,
  };
}

/** Края для строки статуса. Один заданный край - второй берёт сервер сам. */
export function endsText(open, closed) {
  if (open === null && closed === null) return "Не откалибрована";
  const part = (v) => (v === null ? "по умолчанию" : String(v));
  return `Открыто ${part(open)} · закрыто ${part(closed)}`;
}

/** Почему края нельзя сохранить; пустая строка - можно. */
export function saveProblem(open, closed) {
  if (open === null || closed === null) return "Отметьте оба края.";
  if (Math.abs(open - closed) < MIN_SPREAD) {
    return `Края почти совпадают (${open} и ${closed}) — штору не довели до края.`;
  }
  return "";
}

const STATES = { open: "Открыта", closed: "Закрыта", opening: "Открывается", closing: "Закрывается",
  unavailable: "Недоступна", unknown: "Неизвестно" };
export const stateText = (s) => STATES[s] || (s ? s : "—");

/** Итог автоматической калибровки, пришедший после запуска, а не прошлый.
 *  Сверяем с меткой прошлого итога, а не с часами телефона: часы сервера
 *  и телефона расходятся. */
export function freshRun(info, prevAt) {
  const run = info?.lastRun;
  if (!run || info.calibrating) return null;
  return run.at !== prevAt ? run : null;
}

// ============================================================== окно ==
const TAG = "bms-cover-calibration";

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const btn = (label, act, kind = "", extra = "") =>
  `<button type="button" class="b ${kind}" data-act="${act}" ${extra}>${label}</button>`;
const num = (v) => (v === null ? "—" : String(v));

function errorText(err) {
  if (err?.code === "unknown_command") {
    return "Сервер не знает калибровку штор — обновите интеграцию BMS и перезапустите Home Assistant.";
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
.body{width:100%;max-width:560px;margin:0 auto;padding:16px;display:flex;flex-direction:column;gap:14px}
h2{font-size:17px;line-height:1.3;margin:0;font-weight:700}
p{margin:0}.mut{color:#9AA7B4;font-size:14px}
.b{min-height:48px;width:100%;padding:10px 18px;border-radius:12px;border:1px solid #2B3744;background:#18212B;color:#EEF2F6;
  font:inherit;font-size:16px;font-weight:600;cursor:pointer;touch-action:manipulation}
.b.pri{background:#006FFF;border-color:#006FFF;color:#fff}
.b.dan{background:transparent;border-color:#6A2B35;color:#FF8592}
.b:disabled{opacity:.45;cursor:not-allowed}
button:focus-visible{outline:3px solid #8DBBFF;outline-offset:2px}
.card{background:#141C25;border:1px solid #222D39;border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:12px}
.now{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.now div{display:flex;flex-direction:column;gap:2px;min-width:0}
.big{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.1}
.lh{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#9AA7B4}
.btns{display:flex;flex-wrap:wrap;gap:8px}.btns .b{flex:1 1 84px;min-width:0;padding:8px 6px}
.end{display:flex;align-items:center;gap:12px}
.end .v{flex:none;min-width:84px}.end .v b{font-size:22px;font-variant-numeric:tabular-nums}
.end .b{flex:1}
.ok{border-left:3px solid #3DD68C;padding:2px 0 2px 10px;color:#BDF0D5;font-size:14px}
.note{border-left:3px solid #F0B429;padding:2px 0 2px 10px;color:#E8D6A8;font-size:14px}
.err{border-left:3px solid #FF5A6A;padding:2px 0 2px 10px;color:#FFB3BB;font-size:14px}
.spin{display:inline-block;width:16px;height:16px;border-radius:50%;border:2px solid #3A4756;border-top-color:#8DBBFF;
  vertical-align:-3px;margin-right:8px;animation:r 1s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}
.toast{position:fixed;left:50%;bottom:calc(20px + env(safe-area-inset-bottom));transform:translateX(-50%);z-index:3;max-width:calc(100% - 32px);
  background:#EEF2F6;color:#0D1319;padding:10px 16px;border-radius:10px;font-size:14px;font-weight:600}
.toast[hidden]{display:none}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
`;

const SHELL = `<style>${CSS}</style>
<div class="wrap" role="dialog" aria-modal="true" aria-labelledby="t">
  <div class="bar"><div class="ttl"><div class="h" id="t">Калибровка шторы</div><div class="sub" id="name"></div></div>
    <button type="button" class="x" data-act="close" aria-label="Закрыть калибровку шторы">
      <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>
    </button></div>
  <div class="body" id="c"></div>
</div>
<div class="toast" id="toast" role="status" hidden></div>
<div class="sr" id="sr" role="status" aria-live="polite"></div>`;

function defineOverlay() {
  if (customElements.get(TAG)) return;
  customElements.define(TAG, class extends HTMLElement {
    setup(o) {
      this.o = o;
      this.id = o.entityId;
      Object.assign(this, { info: null, loadErr: "", err: "", open: null, closed: null, busy: false,
        started: false, prevAt: 0, result: null, confirmReset: false, changed: false, closed_: false, polling: false });
    }

    connectedCallback() {
      if (this.shadowRoot) return;
      const root = this.attachShadow({ mode: "open" });
      root.innerHTML = SHELL;
      this.$ = (id) => root.getElementById(id);
      this.$("name").textContent = this.o.name || this.id;
      root.addEventListener("click", (e) => this.onClick(e));
      root.addEventListener("keydown", (e) => this.onKey(e));
      this.render();
      this.poll();
      this._timer = setInterval(() => this.poll(), POLL_MS);
    }

    disconnectedCallback() { this.close(); }

    async poll() {
      // Скрытая вкладка не опрашивает: окно может пролежать открытым часами.
      if (this.polling || this.closed_ || document.visibilityState === "hidden") return;
      this.polling = true;
      try {
        this.info = normInfo(await this.o.ws("cover_calibration/info", { entity_id: this.id }));
        this.loadErr = "";
        const run = this.started ? freshRun(this.info, this.prevAt) : null;
        if (run) {
          this.started = false;
          this.result = run;
          if (run.ok) this.changed = true;
          this.$("sr").textContent = run.text;
        }
      } catch (err) {
        this.loadErr = errorText(err);
      } finally {
        this.polling = false;
      }
      if (!this.closed_) this.render();
    }

    render() {
      if (!this.shadowRoot) return;
      const c = this.$("c");
      // Опрос раз в секунду: без перемен разметку не трогать, иначе касание,
      // пришедшееся на перерисовку, теряется вместе со старой кнопкой.
      const html = this.view();
      if (html === this._html) return;
      this._html = html;
      const focusAct = this.shadowRoot.activeElement?.dataset?.act;
      c.innerHTML = html;
      if (focusAct) c.querySelector(`[data-act="${focusAct}"]:not([disabled])`)?.focus();
    }

    view() {
      const i = this.info;
      if (!i) {
        return this.loadErr ? `<p class="err">${esc(this.loadErr)}</p>` : `<p class="mut"><span class="spin"></span>Загрузка…</p>`;
      }
      if (!i.supported) {
        return `<div class="card"><h2>Калибровка недоступна</h2><p class="mut">${esc(i.reason || "Эта штора не сообщает своё положение.")}</p></div>`;
      }
      const running = i.calibrating || this.started;
      const off = running || this.busy ? "disabled" : "";
      const problem = saveProblem(this.open, this.closed);
      const result = this.result
        ? `<p class="${this.result.ok ? "ok" : "err"}">${esc(this.result.text)}</p>` : "";
      return `
      ${this.loadErr ? `<p class="err">${esc(this.loadErr)}</p>` : ""}
      ${this.err ? `<p class="err">${esc(this.err)}</p>` : ""}
      <div class="card">
        <div class="now">
          <div><span class="lh">Мотор показывает</span><span class="big">${num(i.raw)}</span></div>
          <div><span class="lh">В Home Assistant</span><span class="big">${i.position === null ? "—" : `${i.position}%`}</span>
            <span class="mut">${esc(stateText(i.state))}</span></div>
        </div>
        <p class="mut">Сейчас: ${esc(endsText(i.open, i.closed))}</p>
      </div>

      <div class="card">
        <h2>Автоматически</h2>
        <p class="mut">Штора сама откроется до конца, потом закроется до конца, и края запишутся. Это занимает около минуты — не трогайте штору.</p>
        ${running ? `<p><span class="spin"></span>Идёт калибровка: штора едет…</p>` : ""}
        ${result}
        ${btn("Откалибровать автоматически", "run", "pri", off)}
      </div>

      <div class="card">
        <h2>Вручную</h2>
        <p class="mut">Доведите штору кнопками до края и отметьте его. Так можно поставить край там, где штора должна останавливаться.</p>
        <div class="btns">
          ${btn("Открыть", "move-open", "", off)}${btn("Стоп", "move-stop", "", running ? "disabled" : "")}${btn("Закрыть", "move-close", "", off)}
        </div>
        <div class="end"><span class="v"><span class="lh">Открыто</span><br><b>${num(this.open)}</b></span>
          ${btn("Это полностью открыто", "mark-open", "", i.raw === null || running ? "disabled" : "")}</div>
        <div class="end"><span class="v"><span class="lh">Закрыто</span><br><b>${num(this.closed)}</b></span>
          ${btn("Это полностью закрыто", "mark-closed", "", i.raw === null || running ? "disabled" : "")}</div>
        ${this.open !== null || this.closed !== null ? `<p class="${problem ? "mut" : "ok"}">${esc(problem || "Края отмечены — можно сохранить.")}</p>` : ""}
        ${btn("Сохранить края", "save", "pri", problem || off ? "disabled" : "")}
      </div>

      <p class="note">Если «Открыть» закрывает штору — у мотора перепутано направление. Смените у устройства «Motor Direction» (направление мотора) и откалибруйте заново.</p>

      ${i.open !== null || i.closed !== null ? (this.confirmReset
        ? `<div class="card"><p>Сбросить калибровку? Home Assistant снова будет читать мотор как есть.</p>
            <div class="btns">${btn("Сбросить", "reset", "dan", off)}${btn("Отмена", "reset-no")}</div></div>`
        : btn("Сбросить калибровку", "reset-ask", "dan", off)) : ""}`;
    }

    async onClick(e) {
      const el = e.composedPath().find((n) => n?.dataset?.act);
      if (!el || el.disabled) return;
      const a = el.dataset.act;
      if (a === "close") return this.close();
      if (a.startsWith("move-")) return this.call("cover_calibration/move", { action: a.slice(5) });
      if (a === "mark-open" || a === "mark-closed") {
        const raw = this.info?.raw ?? null;
        if (raw === null) return;
        if (a === "mark-open") this.open = raw; else this.closed = raw;
        this.err = "";
        return this.render();
      }
      if (a === "save") {
        if (saveProblem(this.open, this.closed)) return;
        const ok = await this.call("cover_calibration/set", { open_raw: this.open, closed_raw: this.closed });
        if (ok) { this.changed = true; this.open = this.closed = null; this.notify("Края сохранены"); }
        return this.render();
      }
      if (a === "run") {
        this.result = null;
        this.prevAt = this.info?.lastRun?.at ?? 0;
        this.started = true;
        const ok = await this.call("cover_calibration/run", {});
        if (!ok) this.started = false;
        return this.render();
      }
      if (a === "reset-ask") { this.confirmReset = true; return this.render(); }
      if (a === "reset-no") { this.confirmReset = false; return this.render(); }
      if (a === "reset") {
        const ok = await this.call("cover_calibration/set", { open_raw: null, closed_raw: null });
        this.confirmReset = false;
        if (ok) { this.changed = true; this.notify("Калибровка сброшена"); }
        return this.render();
      }
    }

    async call(type, payload) {
      if (this.busy) return false;
      this.busy = true;
      this.err = "";
      this.render();
      try {
        await this.o.ws(type, { entity_id: this.id, ...payload });
        return true;
      } catch (err) {
        this.err = errorText(err);
        this.$("sr").textContent = this.err;
        return false;
      } finally {
        this.busy = false;
        await this.poll();
      }
    }

    notify(text) {
      const t = this.$("toast");
      t.textContent = text;
      t.hidden = false;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { t.hidden = true; }, 2500);
    }

    onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); return this.close(); }
      if (e.key !== "Tab") return;
      // Фокус не уходит за окно на страницу под ним.
      const items = [...this.shadowRoot.querySelectorAll(".wrap button:not([disabled])")]
        .filter((b) => b.getClientRects().length);
      if (!items.length) return;
      const active = this.shadowRoot.activeElement, first = items[0], last = items[items.length - 1];
      if (e.shiftKey && (active === first || !items.includes(active))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (active === last || !items.includes(active))) { e.preventDefault(); first.focus(); }
    }

    close() {
      if (this.closed_) return;
      this.closed_ = true;
      clearInterval(this._timer);
      clearTimeout(this._toastTimer);
      // Автоматическая калибровка идёт на сервере и без окна - итог будет в карточке.
      if (this.isConnected) this.remove();
      try { this.o.onClose?.(this.changed || this.started); } catch (err) {
        console.error("BMS калибровка шторы: onClose", err);
      }
    }
  });
}

/**
 * Открыть калибровку шторы поверх страницы.
 * opts: { entityId, name, ws(name, payload), toast(text, bad), onClose(changed), mount? }
 */
export function openCoverCalibration(opts) {
  defineOverlay();
  const el = document.createElement(TAG);
  el.setup(opts);
  (opts.mount || document.body).appendChild(el);
  return el;
}
