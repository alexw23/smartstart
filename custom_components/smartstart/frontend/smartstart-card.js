/**
 * SmartStart Card
 * Uses ha-dialog (HA's native Material dialog) for the scheduling popup.
 *
 * Card config:
 *   type: custom:smartstart-card
 *   devices:
 *     - dishwasher
 *     - washing_machine
 */

const STATUS_COLOUR = {
  idle:      "var(--secondary-text-color)",
  scheduled: "var(--warning-color, #ff9800)",
  running:   "var(--success-color, #4caf50)",
  error:     "var(--error-color, #f44336)",
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function _fmt(iso, withDate = false) {
  if (!iso || iso === "unavailable" || iso === "unknown") return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (!withDate) return t;
  return d.toDateString() === new Date().toDateString() ? t : `${t} tomorrow`;
}

function _f(v) {
  const n = parseFloat(v);
  return isNaN(n) ? null : n;
}

function _pct(v) {
  const n = _f(v);
  return n == null ? null : Math.round(n);
}

// ── Main card ─────────────────────────────────────────────────────────────────

class SmartStartCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  static getConfigElement() { return document.createElement("smartstart-card-editor"); }
  static getStubConfig()    { return { devices: ["my_appliance"] }; }

  setConfig(config) {
    if (!Array.isArray(config.devices) || !config.devices.length) {
      throw new Error("SmartStart: 'devices' must be a non-empty list of slugs");
    }
    this._config = config;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _st(slug, suffix = "status") {
    return this._hass?.states[`sensor.smartstart_${slug}_${suffix}`];
  }
  _attr(slug, key)   { return this._st(slug)?.attributes?.[key]; }
  _val(slug, suffix) { return this._st(slug, suffix)?.state; }

  _render() {
    if (!this._hass || !this._config) return;

    const css = `
      :host { display: block; }
      ha-card { padding: 8px 0; }
      .row {
        display: flex; align-items: center; gap: 12px;
        padding: 10px 16px; cursor: pointer; border-radius: 0;
        transition: background 0.12s;
      }
      .row:hover { background: var(--secondary-background-color); }
      .icon-wrap {
        width: 38px; height: 38px; border-radius: 10px; flex-shrink: 0;
        display: flex; align-items: center; justify-content: center;
      }
      .icon-wrap.idle      { background: rgba(128,128,128,.1); color: var(--secondary-text-color); }
      .icon-wrap.scheduled { background: rgba(255,152,0,.15); color: var(--warning-color,#ff9800); }
      .icon-wrap.running   { background: rgba(76,175,80,.15);  color: var(--success-color,#4caf50); }
      .icon-wrap.error     { background: rgba(244,67,54,.12);  color: var(--error-color,#f44336); }
      .icon-wrap.default   { background: rgba(var(--rgb-primary-color,3,169,244),.12); color: var(--primary-color); }
      .body  { flex: 1; overflow: hidden; }
      .name  { font-weight: 500; font-size: .9rem; color: var(--primary-text-color); }
      .sub   { font-size: .76rem; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 1px; }
      .chip  { font-size: .68rem; font-weight: 700; padding: 2px 8px; border-radius: 10px; color: #fff; white-space: nowrap; flex-shrink: 0; }
    `;

    const rows = this._config.devices.map(slug => {
      const ent = this._st(slug);
      if (!ent) return `<div class="row" data-slug="${slug}">
        <div class="icon-wrap idle"><ha-icon icon="mdi:lightning-bolt"></ha-icon></div>
        <div class="body"><div class="name">${slug}</div><div class="sub">Sensor not found</div></div>
      </div>`;

      const status = ent.state || "idle";
      const attr   = ent.attributes || {};
      const name   = attr.name || slug;
      const icon   = attr.icon || "mdi:lightning-bolt";
      const unit   = attr.rate_unit || "p";
      const currR  = _f(this._val(slug, "current_rate"));
      const bestR  = _f(this._val(slug, "best_window_rate"));
      const bestS  = this._val(slug, "best_window_start");
      const savPct = _pct(this._val(slug, "best_window_savings_pct"));
      const iconCls = ["idle","scheduled","running","error"].includes(status) ? status : "default";

      let sub = "";
      if (status === "scheduled" && attr.scheduled_start) {
        const cname = (attr.cycles || []).find(c => c.cycle_id === attr.cycle_id)?.name || attr.cycle_id || "";
        sub = `Scheduled ${_fmt(attr.scheduled_start, true)}${cname ? " · " + cname : ""}`;
      } else if (status === "running") {
        sub = `Running · est. finish ${_fmt(attr.estimated_finish)}`;
      } else if (currR != null && bestR != null) {
        sub = `Now: ${currR.toFixed(1)}${unit} · Best: ${bestR.toFixed(1)}${unit} @ ${_fmt(bestS, true)}${savPct > 0 ? " · save " + savPct + "%" : ""}`;
      } else {
        sub = "No rate data yet";
      }

      const chipColour = STATUS_COLOUR[status] || "#888";
      return `<div class="row" data-slug="${slug}">
        <div class="icon-wrap ${iconCls}"><ha-icon icon="${icon}"></ha-icon></div>
        <div class="body">
          <div class="name">${name}</div>
          <div class="sub">${sub}</div>
        </div>
        <span class="chip" style="background:${chipColour}">${status}</span>
      </div>`;
    }).join("");

    this.shadowRoot.innerHTML = `<style>${css}</style><ha-card>${rows}</ha-card>`;
    this.shadowRoot.querySelectorAll(".row").forEach(row =>
      row.addEventListener("click", () => this._open(row.dataset.slug))
    );
  }

  _open(slug) {
    document.querySelector("smartstart-dialog")?.remove();
    const dlg = document.createElement("smartstart-dialog");
    dlg._hass = this._hass;
    dlg._slug = slug;
    document.body.appendChild(dlg);
  }

  getCardSize() { return (this._config?.devices?.length || 1) + 1; }
}

// ── Scheduling dialog (uses ha-dialog) ───────────────────────────────────────

class SmartStartDialog extends HTMLElement {
  connectedCallback() {
    this._selectedCycle = null;
    this._deadlineTime  = null;
    this._build();
  }

  _st(suffix = "status") {
    return this._hass?.states[`sensor.smartstart_${this._slug}_${suffix}`];
  }
  _attr(key)   { return this._st()?.attributes?.[key]; }
  _val(suffix) { return this._st(suffix)?.state; }

  _build() {
    // Resolve cycle list & selection
    const cycles = this._attr("cycles") || [];
    if (!this._selectedCycle && cycles.length) this._selectedCycle = cycles[0].cycle_id;

    const status  = this._st()?.state || "idle";
    const name    = this._attr("name") || this._slug;
    const icon    = this._attr("icon") || "mdi:lightning-bolt";
    const unit    = this._attr("rate_unit") || "p";
    const symbol  = this._attr("currency_symbol") || "£";

    const currR  = _f(this._val("current_rate"));
    const bestR  = _f(this._val("best_window_rate"));
    const bestS  = this._val("best_window_start");
    const savPct = _pct(this._val("best_window_savings_pct")) || 0;

    const cycle   = cycles.find(c => c.cycle_id === this._selectedCycle) || cycles[0] || {};
    const kwh     = cycle.total_kwh || 0;
    const costNow  = currR != null ? `${symbol}${(currR * kwh).toFixed(1)}` : "?";
    const costBest = bestR != null ? `${symbol}${(bestR * kwh).toFixed(1)}` : "?";

    // ── ha-dialog ────────────────────────────────────────────────────────────
    const dialog = document.createElement("ha-dialog");
    dialog.setAttribute("open", "");

    // Heading (use slot so we can put an icon in it)
    const heading = document.createElement("span");
    heading.setAttribute("slot", "heading");
    heading.innerHTML = `<ha-icon icon="${icon}" style="--mdc-icon-size:20px;vertical-align:middle;margin-right:6px"></ha-icon>${name}`;
    dialog.appendChild(heading);

    // ── Content area ─────────────────────────────────────────────────────────
    const content = document.createElement("div");

    // Styles (scoped inside the dialog content — ha-dialog uses light DOM)
    const style = document.createElement("style");
    style.textContent = `
      .ss-wrap { width: min(380px, 85vw); }

      .ss-banner {
        border-radius: 8px; padding: 10px 12px; margin-bottom: 14px;
        font-size: .82rem; line-height: 1.4;
      }
      .ss-banner.scheduled {
        background: rgba(var(--rgb-success-color, 76,175,80), .1);
        border: 1px solid var(--success-color, #4caf50);
      }
      .ss-banner.running {
        background: rgba(var(--rgb-warning-color, 255,152,0), .1);
        border: 1px solid var(--warning-color, #ff9800);
      }
      .ss-banner-label { font-weight: 600; margin-bottom: 2px; }

      .ss-tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
      .ss-tab {
        padding: 4px 14px; border-radius: 16px; font-size: .8rem; cursor: pointer;
        border: 1.5px solid var(--divider-color); background: none;
        color: var(--secondary-text-color); font-family: inherit;
      }
      .ss-tab.active {
        border-color: var(--primary-color); color: var(--primary-color);
        background: rgba(var(--rgb-primary-color, 3,169,244), .1); font-weight: 600;
      }

      .ss-option {
        display: flex; align-items: center; gap: 10px;
        padding: 12px 0; border-bottom: 1px solid var(--divider-color);
      }
      .ss-option:last-child { border-bottom: none; }
      .ss-opt-icon { font-size: 1.1rem; width: 22px; text-align: center; flex-shrink: 0; }
      .ss-opt-body { flex: 1; overflow: hidden; }
      .ss-opt-title { font-weight: 500; font-size: .85rem; color: var(--primary-text-color); }
      .ss-opt-sub {
        font-size: .75rem; color: var(--secondary-text-color);
        margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .ss-time-row { display: flex; gap: 8px; align-items: center; margin-top: 4px; }
      .ss-time {
        padding: 5px 8px; border-radius: 4px; font-size: .82rem;
        border: 1px solid var(--divider-color); font-family: inherit;
        background: var(--secondary-background-color); color: var(--primary-text-color);
      }
    `;

    // Banner (if scheduled or running)
    let bannerHtml = "";
    if (status === "scheduled" && this._attr("scheduled_start")) {
      const cname    = cycles.find(c => c.cycle_id === this._attr("cycle_id"))?.name || this._attr("cycle_id") || "";
      const schedSav = this._attr("savings_pct");
      const schedCost= this._attr("estimated_cost");
      bannerHtml = `<div class="ss-banner scheduled">
        <div class="ss-banner-label">⏱ Scheduled</div>
        ${cname ? `<div>${cname}</div>` : ""}
        <div>Starts ${_fmt(this._attr("scheduled_start"), true)} · est. finish ${_fmt(this._attr("estimated_finish"))}</div>
        <div>${schedSav || 0}% savings · ${symbol}${schedCost || "?"}</div>
      </div>`;
    } else if (status === "running") {
      bannerHtml = `<div class="ss-banner running">
        <div class="ss-banner-label">▶ Running</div>
        <div>Est. finish ${_fmt(this._attr("estimated_finish"))}</div>
      </div>`;
    }

    // Cycle tabs
    let tabsHtml = "";
    if (cycles.length > 1) {
      tabsHtml = `<div class="ss-tabs">${cycles.map(c =>
        `<button class="ss-tab${c.cycle_id === this._selectedCycle ? " active" : ""}" data-cycle="${c.cycle_id}">${c.name}</button>`
      ).join("")}</div>`;
    }

    // Option panels
    const nowSub  = currR != null
      ? `${currR.toFixed(1)}${unit}/kWh · ${kwh}kWh · est. ${costNow}`
      : "No rate data";
    const bestSub = bestR != null
      ? `${_fmt(bestS, true)} · ${bestR.toFixed(1)}${unit}/kWh · est. ${costBest}${savPct > 0 ? " · " + savPct + "% cheaper" : ""}`
      : "No rate data";

    const optionsHtml = `
      <div class="ss-option">
        <div class="ss-opt-icon">⚡</div>
        <div class="ss-opt-body">
          <div class="ss-opt-title">Start now</div>
          <div class="ss-opt-sub">${nowSub}</div>
        </div>
        <mwc-button id="ss-now" unelevated dense>Start</mwc-button>
      </div>
      <div class="ss-option">
        <div class="ss-opt-icon">⭐</div>
        <div class="ss-opt-body">
          <div class="ss-opt-title">Cheapest window</div>
          <div class="ss-opt-sub">${bestSub}</div>
        </div>
        <mwc-button id="ss-best" unelevated dense>Schedule</mwc-button>
      </div>
      <div class="ss-option">
        <div class="ss-opt-icon">⏰</div>
        <div class="ss-opt-body">
          <div class="ss-opt-title">Done by</div>
          <div class="ss-time-row">
            <input class="ss-time" id="ss-time-input" type="time" value="${this._deadlineTime || ""}">
          </div>
        </div>
        <mwc-button id="ss-deadline" unelevated dense>Schedule</mwc-button>
      </div>`;

    const wrap = document.createElement("div");
    wrap.className = "ss-wrap";
    wrap.innerHTML = bannerHtml + tabsHtml + optionsHtml;
    content.appendChild(style);
    content.appendChild(wrap);
    dialog.appendChild(content);

    // ── Action buttons (ha-dialog footer slots) ───────────────────────────────
    if (status === "scheduled") {
      const cancelBtn = document.createElement("mwc-button");
      cancelBtn.setAttribute("slot", "secondaryAction");
      cancelBtn.textContent = "Cancel schedule";
      cancelBtn.style.color = "var(--error-color, #f44336)";
      cancelBtn.addEventListener("click", () => { this._cancel(); dialog.open = false; });
      dialog.appendChild(cancelBtn);
    }

    const closeBtn = document.createElement("mwc-button");
    closeBtn.setAttribute("slot", "primaryAction");
    closeBtn.setAttribute("dialogAction", "close");
    closeBtn.textContent = "Close";
    dialog.appendChild(closeBtn);

    // ── Cleanup on close ──────────────────────────────────────────────────────
    dialog.addEventListener("closed", () => this.remove());

    this.appendChild(dialog);

    // ── Wire interactions (rAF so elements are in DOM) ────────────────────────
    requestAnimationFrame(() => {
      wrap.querySelector("#ss-now")?.addEventListener("click", () => this._schedule("now", dialog));
      wrap.querySelector("#ss-best")?.addEventListener("click", () => this._schedule("best", dialog));
      wrap.querySelector("#ss-deadline")?.addEventListener("click", () => this._scheduleDeadline(dialog));
      wrap.querySelector("#ss-time-input")?.addEventListener("change", e => { this._deadlineTime = e.target.value; });

      // Cycle tab selection — rebuild dialog in place
      wrap.querySelectorAll(".ss-tab").forEach(tab => {
        tab.addEventListener("click", () => {
          this._selectedCycle = tab.dataset.cycle;
          dialog.open = false; // triggers 'closed' → this.remove()
          // Re-open with new cycle
          setTimeout(() => {
            const next = document.createElement("smartstart-dialog");
            next._hass = this._hass;
            next._slug = this._slug;
            next._selectedCycle = this._selectedCycle;
            next._deadlineTime  = this._deadlineTime;
            document.body.appendChild(next);
          }, 80);
        });
      });
    });
  }

  async _schedule(intention, dialog) {
    if (!this._selectedCycle) return;
    try {
      await this._hass.callService("smartstart", "schedule", {
        device_slug: this._slug,
        cycle_id:    this._selectedCycle,
        intention,
      });
      dialog.open = false;
    } catch (e) {
      console.error("SmartStart: schedule failed", e);
    }
  }

  async _scheduleDeadline(dialog) {
    const timeVal = this.querySelector("#ss-time-input")?.value || this._deadlineTime;
    if (!timeVal || !this._selectedCycle) return;
    const [h, m] = timeVal.split(":").map(Number);
    const deadline = new Date();
    deadline.setHours(h, m, 0, 0);
    if (deadline <= new Date()) deadline.setDate(deadline.getDate() + 1);
    try {
      await this._hass.callService("smartstart", "schedule", {
        device_slug: this._slug,
        cycle_id:    this._selectedCycle,
        intention:   "deadline",
        deadline:    deadline.toISOString(),
      });
      dialog.open = false;
    } catch (e) {
      console.error("SmartStart: deadline schedule failed", e);
    }
  }

  async _cancel() {
    try {
      await this._hass.callService("smartstart", "cancel", { device_slug: this._slug });
    } catch (e) {
      console.error("SmartStart: cancel failed", e);
    }
  }
}

// ── Card editor ───────────────────────────────────────────────────────────────

class SmartStartCardEditor extends HTMLElement {
  setConfig(config) { this._config = config; this._render(); }
  set hass(h)       { this._hass = h; }

  _render() {
    this.innerHTML = `<div style="padding:16px">
      <label style="display:block;margin-bottom:6px;font-weight:600;font-size:.875rem">Device slugs (one per line)</label>
      <ha-textarea id="slugs" rows="4" style="width:100%" .value="${(this._config?.devices || []).join("\n")}"></ha-textarea>
    </div>`;
    this.querySelector("#slugs")?.addEventListener("change", e => {
      const devices = e.target.value.split("\n").map(s => s.trim()).filter(Boolean);
      this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: { ...this._config, devices } } }));
    });
  }
}

// ── Registration ──────────────────────────────────────────────────────────────

customElements.define("smartstart-card",        SmartStartCard);
customElements.define("smartstart-dialog",      SmartStartDialog);
customElements.define("smartstart-card-editor", SmartStartCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type:        "smartstart-card",
  name:        "SmartStart",
  description: "Energy-aware appliance scheduling — Octopus Agile and more",
  preview:     false,
});

console.info(
  "%c SMARTSTART %c loaded",
  "background:#1565c0;color:#fff;padding:2px 6px;border-radius:3px 0 0 3px",
  "background:#333;color:#fff;padding:2px 6px;border-radius:0 3px 3px 0"
);
