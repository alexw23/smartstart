/**
 * SmartStart sidebar panel.
 *
 * Read-mostly view over the smartstart/* websocket API (panel.py): per-device
 * run history with power curves, per-cycle stats + rhythm, recommended
 * windows on a rate chart, and tagging of Unknown (externally started) runs.
 */

const POLL_MS = 30000;
const PAGE = 30;
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const STATUS_COLOUR = {
  idle:      "var(--secondary-text-color)",
  scheduled: "var(--warning-color, #ff9800)",
  running:   "var(--success-color, #4caf50)",
  paused:    "var(--info-color, #039be5)",
};
const LAUNDRY_COLOUR = {
  idle:                "var(--secondary-text-color)",
  scheduled:           "var(--warning-color, #ff9800)",
  running:             "var(--success-color, #4caf50)",
  awaiting_changeover: "var(--error-color, #f44336)",
};
const LAUNDRY_LABEL = {
  idle: "idle", scheduled: "scheduled", running: "running",
  awaiting_changeover: "changeover", complete: "complete",
};
const STEP_LABEL = {
  planned: "planned", pending: "waiting", fired: "starting", running: "running", done: "done",
};
const MODE_LABEL = { now: "Now", balanced: "Balanced", max_savings: "Max savings" };
// One colour per load on the laundry timeline (readable on light and dark).
const LOAD_COLOURS = ["#039be5", "#8e24aa", "#43a047", "#f4511e", "#00897b", "#c0a000"];
const RHYTHM_LABEL = {
  learning:  "Learning",
  irregular: "Irregular",
  upcoming:  "Upcoming",
  due:       "Due",
  overdue:   "Overdue",
};

// ── Formatting ────────────────────────────────────────────────────────────────

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function fmtDay(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((day - today) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  if (diff === -1) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}

const fmtWhen = (iso) => (iso ? `${fmtDay(iso)} ${fmtTime(iso)}` : "—");

function fmtDur(min) {
  if (min == null) return "—";
  const m = Math.round(min);
  return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `${m}m`;
}

function fmtHour(h) {
  if (h == null) return "—";
  const d = new Date(); d.setHours(Math.floor(h), Math.round((h % 1) * 60), 0, 0);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function fmtInterval(days) {
  if (days == null) return "—";
  if (days < 1.5) return `every ${Math.round(days * 24)}h`;
  if (days >= 13) return `every ${(days / 7).toFixed(1)} wk`;
  return `every ${days.toFixed(1)} d`;
}

// ── Panel ─────────────────────────────────────────────────────────────────────

class SmartStartPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = null;
    this._device = null;          // selected device slug
    this._winCycle = {};          // slug → cycle_key for the windows section
    this._windows = null;         // smartstart/windows result for that pair
    this._runs = [];              // loaded page(s) of runs for this._device
    this._runsTotal = 0;
    this._runFilter = null;       // cycle_key filter for the runs list
    this._detail = null;          // smartstart/run result for the open dialog
    this._picked = null;          // start ISO of a Recommended window the user tapped
    this._error = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (this._menu) this._menu.hass = hass;
    if (first) this._boot();
  }
  set narrow(n) { this._narrow = n; if (this._menu) this._menu.narrow = n; }
  set panel(p) { this._panel = p; }

  connectedCallback() {
    if (this._hass && !this._timer) this._start();
  }

  disconnectedCallback() {
    clearInterval(this._timer); this._timer = null;
    (this._unsubs || []).forEach((u) => u && u.then((f) => f()).catch(() => {}));
    this._unsubs = [];
  }

  _boot() {
    // Deep link: /smartstart?device=<slug> (e.g. from a notification tap).
    // Path links: /smartstart/laundry → the laundry pair; /smartstart/laundry/<slug>
    // → that pair; /smartstart/<device_slug> → that device. The query form wins.
    // Unknown slugs fall back to the first device in _refresh().
    try {
      const parts = window.location.pathname.split("/").filter(Boolean);
      if (parts[0] === "smartstart" && parts[1]) {
        this._device = parts[1] === "laundry"
          ? `laundry:${parts[2] || "laundry"}`
          : decodeURIComponent(parts[1]);
      }
      const want = new URLSearchParams(window.location.search).get("device");
      if (want) this._device = want;
      this._explicitDevice = !!this._device;
    } catch (_) { /* no location — embedded */ }
    this._renderShell();
    this._start();
  }

  _start() {
    this._refresh(true);
    this._timer = setInterval(() => this._refresh(false), POLL_MS);
    const ev = ["smartstart_cycle_started", "smartstart_cycle_ended",
                "smartstart_cycle_rescheduled", "smartstart_cycle_paused", "smartstart_cycle_resumed",
                "smartstart_offer_found", "smartstart_laundry_scheduled", "smartstart_laundry_step_done",
                "smartstart_laundry_completed", "smartstart_laundry_cancelled", "smartstart_laundry_offer_found"];
    this._unsubs = ev.map((t) => this._hass.connection
      .subscribeEvents(() => this._refresh(true), t).catch(() => null));
  }

  _ws(msg) { return this._hass.callWS(msg); }

  async _refresh(withRuns) {
    try {
      this._data = await this._ws({ type: "smartstart/overview" });
      this._error = null;
      const slugs = this._data.devices.map((d) => d.device_slug)
        .concat((this._data.laundry || []).map((l) => `laundry:${l.slug}`));
      // No deep link: open on a laundry pair that's waiting for a changeover
      // (e.g. the kiosk switched to this view when you walked in).
      if (!this._explicitDevice && !this._pickedPending) {
        this._pickedPending = true;
        const due = (this._data.laundry || []).find((l) => l.snapshot?.status === "awaiting_changeover");
        if (due) { this._device = `laundry:${due.slug}`; withRuns = true; }
      }
      if (!slugs.includes(this._device)) {
        this._device = slugs[0] || null;
        withRuns = true;
      }
      if (this._dev()) {
        const dev = this._dev();
        if (!this._winCycle[this._device]) {
          this._winCycle[this._device] = dev.selected_cycle
            || dev.cycles.find((c) => c.cycle_key !== "_external")?.cycle_key;
        }
        await Promise.all([
          this._loadWindows(),
          withRuns ? this._loadRuns(false) : null,
        ]);
      }
    } catch (e) {
      this._error = e.message || String(e);
    }
    this._render();
  }

  _dev() { return this._data?.devices.find((d) => d.device_slug === this._device); }
  _pair() {
    return (this._data?.laundry || []).find((l) => `laundry:${l.slug}` === this._device);
  }

  async _loadWindows() {
    const ck = this._winCycle[this._device];
    if (!ck) { this._windows = null; return; }
    try {
      this._windows = await this._ws({
        type: "smartstart/windows", device_slug: this._device, cycle_key: ck,
      });
    } catch (_) { this._windows = null; }
  }

  async _loadRuns(append) {
    const msg = {
      type: "smartstart/runs", device_slug: this._device,
      limit: PAGE, offset: append ? this._runs.length : 0,
    };
    if (this._runFilter) msg.cycle_key = this._runFilter;
    const res = await this._ws(msg);
    this._runs = append ? this._runs.concat(res.runs) : res.runs;
    this._runsTotal = res.total;
  }

  // ── Units ──────────────────────────────────────────────────────────────────

  _money(v, dp = 2) {
    if (v == null) return "—";
    const sym = this._data?.currency_symbol || "£";
    return `${v < 0 ? "−" : ""}${sym}${Math.abs(v).toFixed(dp)}`;
  }

  // Rates are stored in major units/kWh; show the subunit when one is set.
  _rate(v) {
    if (v == null) return "—";
    const u = this._data?.rate_unit;
    return u ? `${(v * 100).toFixed(1)}${u}` : this._money(v, 3);
  }

  // ── Shell ──────────────────────────────────────────────────────────────────

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <div class="toolbar">
        <span class="menu"></span>
        <div class="title">SmartStart</div>
      </div>
      <div class="tabs"></div>
      <main></main>
      <div class="dialog-host"></div>
      <div class="tip" hidden></div>`;
    this._menu = document.createElement("ha-menu-button");
    this._menu.hass = this._hass;
    this._menu.narrow = this._narrow;
    this.shadowRoot.querySelector(".menu").appendChild(this._menu);
    this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
    this.shadowRoot.addEventListener("change", (e) => this._onChange(e));
    this.shadowRoot.addEventListener("mousemove", (e) => this._onHover(e));
    this.shadowRoot.addEventListener("mouseleave", () => this._hideTip(), true);
    this.shadowRoot.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this._detail) { this._detail = null; this._renderDialog(); }
    });
  }

  _render() {
    this._hideTip();
    const root = this.shadowRoot;
    const main = root.querySelector("main");
    const tabs = root.querySelector(".tabs");
    if (this._error && !this._data) {
      main.innerHTML = `<div class="card error">Couldn't load SmartStart: ${esc(this._error)}</div>`;
      return;
    }
    if (!this._data) { main.innerHTML = `<div class="empty">Loading…</div>`; return; }
    tabs.innerHTML = this._data.devices.map((d) => `
      <button class="tab ${d.device_slug === this._device ? "on" : ""}"
              data-act="device" data-slug="${esc(d.device_slug)}">
        <span class="dot" style="background:${STATUS_COLOUR[d.schedule.status] || STATUS_COLOUR.idle}"></span>
        ${esc(d.name)}
        ${d.unknown_runs ? `<span class="badge" title="Untagged runs">${d.unknown_runs}</span>` : ""}
      </button>`).join("") + (this._data.laundry || []).map((l) => `
      <button class="tab ${`laundry:${l.slug}` === this._device ? "on" : ""}"
              data-act="device" data-slug="laundry:${esc(l.slug)}">
        <span class="dot" style="background:${LAUNDRY_COLOUR[l.snapshot.status] || STATUS_COLOUR.idle}"></span>
        ${esc(l.name)}
        ${l.snapshot.continue_available ? `<span class="badge" title="Changeover waiting">!</span>` : ""}
      </button>`).join("");
    const pair = this._pair();
    if (pair) {
      main.innerHTML = [
        this._laundryStatus(pair),
        this._laundryAway(pair),
        this._laundryKpis(pair),
        this._laundryTimeline(pair),
        this._laundryWindows(pair),
        this._flowsCard(pair),
        this._loadsCard(pair),
      ].join("");
      return;
    }
    const dev = this._dev();
    if (!dev) { main.innerHTML = `<div class="empty">No SmartStart devices configured.</div>`; return; }
    main.innerHTML = [
      this._statusCard(dev),
      this._kpis(dev),
      this._windowsCard(dev),
      this._cyclesCard(dev),
      this._rhythmCard(dev),
      this._runsCard(dev),
    ].join("");
  }

  // ── Sections ───────────────────────────────────────────────────────────────

  _statusCard(dev) {
    const s = dev.schedule || {};
    const status = s.status || "idle";
    let body = "";
    if (status === "scheduled") {
      body = `<div class="big">${esc(s.cycle_name || "")} starts ${fmtWhen(s.scheduled_start)}</div>
        <div class="sub">Finishes ~${fmtWhen(s.estimated_finish)} · ${this._rate(s.rate_cost_per_kwh)}/kWh
        · est. ${this._money(s.estimated_cost)}${s.reschedule_count ? ` · moved ${s.reschedule_count}×` : ""}
        ${s.hold_until ? ` · holding for tomorrow's rates` : ""}</div>
        ${this._reachLine(dev)}
        ${this._offerBlock(dev)}`;
    } else if (status === "running" || status === "paused") {
      const l = dev.live || {};
      body = `<div class="big">${esc(s.cycle_name || "Running")} — ${fmtDur(l.elapsed_minutes)} in</div>
        <div class="sub">${l.current_kwh != null ? `${l.current_kwh.toFixed(2)} kWh` : ""}
        ${l.current_cost != null ? ` · ${this._money(l.current_cost)} so far` : ""}
        ${s.estimated_finish ? ` · ends ~${fmtTime(s.estimated_finish)}` : ""}
        ${l.source === "estimated" ? " · estimated (no power sensor)" : ""}</div>
        ${l.samples?.length > 1 ? this._powerChart(l.samples, null, { height: 120 }) : ""}`;
    } else {
      const last = dev.totals?.total_run_count ? "" : "No runs recorded yet.";
      body = `<div class="big">Idle</div><div class="sub">${last}</div>`;
    }
    return `<section class="card status">
      <div class="pill" style="--c:${STATUS_COLOUR[status] || STATUS_COLOUR.idle}">${esc(status)}</div>
      ${body}
      ${this._controls(dev, status)}</section>`;
  }

  // Drives the device's own entities (same as the dashboard card), so the
  // panel, dashboards and automations never disagree.
  _entSelect(eid, label) {
    const st = eid && this._hass.states[eid];
    if (!st) return "";
    const off = st.state === "unavailable";
    return `<label class="ctl">${label}
      <select data-act="ent-select" data-eid="${esc(eid)}" ${off ? "disabled" : ""}>
        ${(st.attributes.options || []).map((o) => `<option ${o === st.state ? "selected" : ""}>${esc(o)}</option>`).join("")}
      </select></label>`;
  }

  _entButton(eid, text, cls = "primary", confirmText = "") {
    const st = eid && this._hass.states[eid];
    if (!st) return "";
    return `<button class="${cls}" data-act="press" data-eid="${esc(eid)}"
      ${confirmText ? `data-confirm="${esc(confirmText)}"` : ""} ${st.state === "unavailable" ? "disabled" : ""}>${text}</button>`;
  }

  _controls(dev, status) {
    const e = dev.entities || {};
    const idle = status === "idle";
    return `<div class="controls">
      ${this._entSelect(e.cycle, "Cycle")}
      ${this._entSelect(e.mode, "Mode")}
      ${this._entSelect(e.power_down, "Power-down")}
      ${idle ? this._entButton(e.start, "Start")
             : this._entButton(e.stop, status === "scheduled" ? "Cancel schedule" : "Stop", "danger",
                 status === "scheduled" ? "" : `Stop ${dev.name}? The running cycle is stopped and not resumed.`)}
    </div>`;
  }

  _reachLine(dev) {
    const r = dev.reach;
    const bits = [];
    if (r) {
      bits.push(dev.auto_reschedule
        ? `Can move until ${fmtWhen(r.latest_start)}${r.bounds_finish ? " (start)" : ""}`
        : `Auto-reschedule off — stays put unless you accept an offer`);
    }
    if (dev.auto_reschedule && dev.rates_release) {
      bits.push(Date.parse(dev.rates_release) > Date.now()
        ? `re-checks when tomorrow's rates land ~${fmtTime(dev.rates_release)}`
        : `re-checks as soon as tomorrow's rates land (late)`);
    }
    return bits.length ? `<div class="sub">${bits.join(" · ")}</div>` : "";
  }

  _offerBlock(dev) {
    const o = dev.offer;
    if (!o) return "";
    const band = o.predicted && o.rate_low != null
      ? ` · forecast ${this._rate(o.rate_low)}–${this._rate(o.rate_high)}` : "";
    return `<div class="offer">
      <div><b>Cheaper: ${fmtWhen(o.start)}</b> → ${fmtTime(o.end)} · ${this._rate(o.rate)}/kWh
        · save ~${this._money(o.saving)}${o.saving_pct != null ? ` (${o.saving_pct}%)` : ""}${band}
        ${o.predicted ? `<span class="tag">forecast</span>` : ""}</div>
      <div class="cta"><button class="primary" data-act="accept-offer" data-slug="${esc(dev.device_slug)}">Wait for ${fmtTime(o.start)}</button>
        <span class="sub">${o.predicted ? "Re-checked against real rates when they land." : "Confirmed rates."}</span></div>
    </div>`;
  }

  _kpis(dev) {
    const m = dev.last_30d, t = dev.totals || {};
    const tile = (label, value, hint = "") => `
      <div class="kpi"><div class="kv">${value}</div><div class="kl">${label}</div>
      ${hint ? `<div class="kh">${hint}</div>` : ""}</div>`;
    return `<section class="kpis">
      ${tile("Runs · 30 days", m.runs)}
      ${dev.has_power ? tile("Cost · 30 days", this._money(m.cost), `${m.kwh.toFixed(1)} kWh`) : ""}
      ${dev.has_power ? tile("Saved · 30 days", this._money(m.saved), "vs starting when scheduled") : ""}
      ${tile("All-time runs", t.total_run_count ?? 0,
             t.total_cost != null ? `${this._money(t.total_cost)} · ${(t.total_kwh || 0).toFixed(1)} kWh` : "")}
      ${dev.unknown_runs ? tile("Untagged runs", dev.unknown_runs, "tag them below to teach SmartStart") : ""}
    </section>`;
  }

  _windowsCard(dev) {
    const cycles = dev.cycles.filter((c) => c.cycle_key !== "_external");
    const ck = this._winCycle[dev.device_slug];
    const w = this._windows;
    const opts = cycles.map((c) => `<option value="${esc(c.cycle_key)}" ${c.cycle_key === ck ? "selected" : ""}>${esc(c.name)}</option>`).join("");
    const sched = dev.schedule || {};
    const ctx = sched.status === "scheduled" && sched.cycle_id === ck
      ? { reach: dev.reach, offer: dev.offer } : {};
    if (["scheduled", "running", "paused"].includes(sched.status)) ctx.sched = sched;
    ctx.picked = this._picked;
    let list = this._windowList(w?.windows, w?.now?.cost, ctx);
    if (w?.windows?.length) {
      list += `
      ${w.now ? `<div class="sub">Starting now: ${this._rate(w.now.rate)}/kWh · ${this._money(w.now.cost)}
        · ${fmtDur(w.duration_minutes)}, ~${w.kwh.toFixed(2)} kWh${w.profile ? " · scored on learned curve" : ""}</div>` : ""}`;
    }
    return `<section class="card">
      <div class="head"><h2>Recommended windows</h2>
        <select data-act="wincycle">${opts}</select></div>
      ${list}
      ${this._pickBar(dev, w?.windows || [], ck)}
      ${this._rateChart(this._data.rates, w?.windows || [], ctx)}
    </section>`;
  }

  _pickBar(dev, windows, ck) {
    const win = windows.find((x) => x.start === this._picked);
    if (!win) return "";
    const s = dev.schedule || {};
    const cname = dev.cycles.find((c) => c.cycle_key === ck)?.name || ck;
    let verb = "Schedule", note = "";
    if (s.status === "scheduled") {
      verb = s.cycle_id === ck ? "Move here" : `Replace ${esc(s.cycle_name || "current run")}`;
      note = s.cycle_id === ck ? "" : ` (instead of ${esc(s.cycle_name || "")} at ${fmtWhen(s.scheduled_start)})`;
    } else if (s.status === "running" || s.status === "paused") {
      return `<div class="pick"><span class="sub">${esc(dev.name)} is running — pick a window once it's finished.</span></div>`;
    }
    return `<div class="pick">
      <div><b>${esc(cname)}</b> ${fmtWhen(win.start)} → ${fmtTime(win.end)} · ${this._rate(win.rate)}/kWh${note}
        ${win.confirmed ? "" : `<span class="sub"> · forecast — re-checked when real rates land, and only moved if cheaper</span>`}</div>
      <div class="cta"><button class="primary" data-act="pick-go" data-ck="${esc(ck)}" data-start="${esc(win.start)}">${verb}</button>
        <button class="link" data-act="pick" data-start="">Cancel</button></div>
    </div>`;
  }

  _windowList(windows, nowCost, ctx = {}) {
    if (!windows?.length) return `<div class="empty small">No windows — rates not loaded yet.</div>`;
    const near = (a, b) => Math.abs(Date.parse(a) - Date.parse(b)) < 45 * 60000;
    return `<ol class="windows">${windows.map((win, i) => {
      const save = nowCost != null ? nowCost - win.cost : null;
      const offered = ctx.offer && near(win.start, ctx.offer.start);
      const out = !offered && ctx.reach && Date.parse(win.start) > Date.parse(ctx.reach.latest_start);
      return `<li class="${i === 0 ? "best" : ""} ${ctx.picked === win.start ? "picked" : ""}"
        data-act="pick" data-start="${esc(win.start)}" title="Tap to schedule this window">
        <div class="wtime">${fmtDay(win.start)} <b>${fmtTime(win.start)}</b> → ${fmtTime(win.end)}</div>
        <div class="wmeta">${this._rate(win.rate)}/kWh · ${this._money(win.cost)}
          ${save != null && save > 0.005 ? `<span class="good">save ${this._money(save)} vs now</span>` : ""}
          ${win.confirmed ? "" : `<span class="tag" title="Forecast band ${this._rate(win.rate_low)}–${this._rate(win.rate_high)}">forecast</span>`}
          ${offered ? `<span class="tag on">offered</span>` : ""}
          ${out ? `<span class="tag" title="Past what the current schedule can move to (${fmtWhen(ctx.reach.latest_start)})">out of reach</span>` : ""}
        </div></li>`;
    }).join("")}</ol>`;
  }

  // ── Laundry ────────────────────────────────────────────────────────────────

  _laundryStatus(p) {
    const s = p.snapshot;
    const label = LAUNDRY_LABEL[s.status] || s.status;
    const hasPlan = !!s.planned_start;
    const idle = s.status === "idle" || s.status === "complete";
    const facts = hasPlan ? `<div class="sub">
        ${s.preview ? "If started now: " : ""}${fmtWhen(s.planned_start)} → ${fmtWhen(s.planned_finish)}
        · ${this._rate(s.plan_rate)}/kWh · ${this._money(s.plan_cost)}
        ${s.estimated_savings > 0.005 ? ` · <span class="good">save ${this._money(s.estimated_savings)} vs now</span>` : ""}
        ${s.current_step && !s.preview ? ` · step ${esc(s.current_step)}` : ""}</div>` : "";
    const setup = [
      !idle && p.rows.length ? `Loads: ${p.rows.map(esc).join(", ")}` : "",
      `Mode: ${esc(MODE_LABEL[p.mode] || p.mode)}`,
      p.due_at ? `Due by ${fmtWhen(p.due_at)}` : "",
      s.sleep_window ? `No changeovers ${esc(s.sleep_window)}` : "",
      s.away?.length ? s.away.map((a) => (a.kind === "until" ? `Not around until ${fmtWhen(a.until)}`
        : `Away ${fmtWhen(a.start)}–${fmtTime(a.until)}`)).join(" · ") : "",
    ].filter(Boolean).join(" · ");
    const machines = (s.awaiting_machines || []).map((m) => (m === "wash" ? "washer" : "dryer")).join(" and ");
    const e = p.entities || {};
    return `<section class="card status">
      <div class="pill" style="--c:${LAUNDRY_COLOUR[s.status] || STATUS_COLOUR.idle}">${esc(s.preview ? "preview" : label)}</div>
      <div class="big">${esc(s.next_action)}</div>
      ${facts}
      <div class="sub">${setup}</div>
      ${idle ? this._loadPicker(p) : ""}
      ${idle ? this._orderLine(p) : ""}
      ${this._suggestions(p)}
      ${this._laundryOffer(p)}
      ${this._stalledBlock(s)}
      <div class="controls">
        ${this._entSelect(e.mode, "Mode")}
        ${this._entSelect(e.power_down, "Power-down")}
        ${idle ? this._duePicker(p) : ""}
        ${idle ? this._entSelect(e.order, "Order") : ""}
        ${idle || s.preview
          ? this._entButton(e.start, "Start")
          : this._entButton(e.cancel, "Cancel", "danger", "Cancel this laundry run? A running cycle just finishes on its own.")}
      </div>
      ${s.continue_available ? this._continueBlock(p, machines) : ""}
      ${s.continue_delay ? `<div class="offer">
        <div>${esc(s.continue_delay.message)}</div>
        <div class="cta"><button class="primary" data-act="start-now" data-pair="${esc(p.slug)}">Start now instead</button>
          <span class="sub">Done ~${fmtTime(s.continue_delay.finish)} (vs ${fmtTime(s.continue_delay.finish_now)} starting now).</span></div>
      </div>` : ""}
    </section>`;
  }

  _continueBlock(p, machines) {
    const s = p.snapshot, o = s.continue_option;
    const load = machines ? ` — make sure the ${machines} ${s.awaiting_machines.length > 1 ? "are" : "is"} loaded` : "";
    if (!o) {
      return `<div class="cta">
        <button class="primary" data-act="continue" data-pair="${esc(p.slug)}">Continue</button>
        <span class="sub">Starts the next step${load}.</span></div>`;
    }
    return `<div class="cta">
      <button class="primary" data-act="continue" data-pair="${esc(p.slug)}">Continue — start ${fmtTime(o.start_at)}, save ${this._money(o.saving)}</button>
      <button class="link" data-act="continue" data-now="1" data-pair="${esc(p.slug)}">Continue &amp; start now</button>
      <span class="sub">${esc(o.message)}${load ? ` Make sure the ${machines} ${s.awaiting_machines.length > 1 ? "are" : "is"} loaded.` : ""}</span></div>`;
  }

  _stalledBlock(s) {
    return (s.stalled || []).map((x) => `<div class="offer warn-block">
      <b>${esc(x.load || x.name)}</b>: the ${x.machine === "wash" ? "washer" : "dryer"} should have started ${esc(x.name)} at ${fmtTime(x.due)} but isn't running —
      check it's on, the door's shut and remote start is enabled.</div>`).join("");
  }

  _duePicker(p) {
    const pad = (n) => String(n).padStart(2, "0");
    const d = p.due_at ? new Date(p.due_at) : null;
    const val = d ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}` : "";
    return `<label class="ctl">Done by
      <input type="datetime-local" data-act="due" data-pair="${esc(p.slug)}" value="${val}">
      ${d ? `<button class="x" title="Clear" data-act="due-clear" data-pair="${esc(p.slug)}">×</button>` : ""}</label>`;
  }

  /** Load N selects as chips: each filled row (change / remove) plus one "Add load". */
  _loadPicker(p) {
    const ids = (p.load_entities || []).filter((eid) => eid && this._hass.states[eid]);
    if (!ids.length) return "";
    const filled = ids.filter((eid) => this._hass.states[eid].state !== "None");
    const next = ids.find((eid) => this._hass.states[eid].state === "None");
    const sel = (eid, placeholder) => {
      const st = this._hass.states[eid];
      return `<select data-act="ent-select" data-eid="${esc(eid)}">
        ${(st.attributes.options || []).map((o) => `<option value="${esc(o)}" ${o === st.state ? "selected" : ""}>${
          o === "None" ? esc(placeholder) : esc(o)}</option>`).join("")}
      </select>`;
    };
    return `<div class="loads">
      ${filled.map((eid) => `<span class="load-chip" style="--lc:${this._loadColour(p, this._hass.states[eid].state)}">
        <i></i>${sel(eid, "Remove")}
        <button class="x" title="Remove" data-act="load-clear" data-eids="${esc(eid)}">×</button></span>`).join("")}
      ${next ? `<span class="load-chip add">${sel(next, filled.length ? "+ Add load" : "+ Pick a load")}</span>` : ""}
    </div>`;
  }

  /** One colour per load, by its place in the listed order — shared by the
   *  chips (row labels: "Towels", "Wash: Cotton 40") and the timeline (load
   *  labels: "Towels", "Cotton 40 → Cupboard Eco"). */
  _loadColour(p, label) {
    const units = p.snapshot.order?.listed
      || [...new Set((p.snapshot.steps || []).map((s) => s.load).filter(Boolean))];
    const bare = String(label).replace(/^(Wash|Dry): /, "");
    const i = units.findIndex((u) => u === label || u.split(" → ").includes(bare));
    return i < 0 ? "var(--divider-color)" : LOAD_COLOURS[i % LOAD_COLOURS.length];
  }

  _orderLine(p) {
    const o = p.snapshot.order;
    if (!o || o.listed.length < 2) return "";
    const arrow = (xs) => xs.map(esc).join(" → ");
    let gain = "";
    if (o.improves && o.minutes_sooner != null) {
      const t = o.minutes_sooner >= 5 ? `done ${fmtDur(o.minutes_sooner)} sooner`
        : o.minutes_sooner <= -5 ? `done ${fmtDur(-o.minutes_sooner)} later` : "";
      const c = o.cost_delta <= -0.005 ? `${this._money(-o.cost_delta)} cheaper`
        : o.cost_delta >= 0.005 ? `${this._money(o.cost_delta)} more` : "";
      gain = [t, c].filter(Boolean).join(", ");
    }
    if (o.reordered) {
      return `<div class="order"><b>Running as:</b> ${arrow(o.order)}${gain ? ` <span class="good">— ${gain} than as listed</span>` : ""}</div>`;
    }
    if (o.improves) {
      return `<div class="order">Listed order kept. Optimised would be ${arrow(o.optimised)}${gain ? ` (${gain})` : ""}.</div>`;
    }
    return `<div class="order muted">Order ${arrow(o.order)} is already the best.</div>`;
  }

  _suggestions(p) {
    const list = p.snapshot.suggestions || [];
    if (!list.length) return "";
    const ids = p.load_entities || [];
    return list.map((x) => `<div class="offer">
      <div>${esc(x.message)}</div>
      <div class="cta"><button class="primary" data-act="load-clear"
        data-eids="${esc((x.rows || []).map((r) => ids[r]).filter(Boolean).join(","))}">Remove ${esc(x.load)}</button></div>
    </div>`).join("");
  }

  _laundryOffer(p) {
    const o = p.snapshot.offer;
    if (!o) return "";
    const band = o.predicted && o.rate_low != null
      ? ` · forecast ${this._rate(o.rate_low)}–${this._rate(o.rate_high)}` : "";
    return `<div class="offer">
      <div><b>Cheaper: ${fmtWhen(o.start)}</b> → ${fmtWhen(o.end)} · ${this._rate(o.rate)}/kWh
        · save ~${this._money(o.saving)}${o.saving_pct != null ? ` (${o.saving_pct}%)` : ""}${band}
        ${o.predicted ? `<span class="tag">forecast</span>` : ""}</div>
      <div class="cta"><button class="primary" data-act="laundry-offer" data-pair="${esc(p.slug)}">Wait for ${fmtTime(o.start)}</button>
        <span class="sub">${o.predicted ? "Re-checked against real rates when they land." : "Confirmed rates."}</span></div>
    </div>`;
  }

  /** Away periods: no changeovers planned inside them (wet laundry may wait up to the pair's limit). */
  _laundryAway(p) {
    const s = p.snapshot;
    if (!(s.status === "idle" || s.status === "complete" || s.status === "scheduled") && !s.away?.length) return "";
    const pad = (n) => String(n).padStart(2, "0");
    const local = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    const soon = new Date(Date.now() + 3600000); soon.setMinutes(0, 0, 0);
    const rows = (s.away || []).map((a, i) => `<li>
      ${a.kind === "until" ? `Not around until <b>${fmtWhen(a.end)}</b>`
        : `<b>${fmtWhen(a.start)}</b> → ${a.end ? fmtWhen(a.end) : `not back today <span class="sub">(until ${fmtWhen(a.until)})</span>`}`}
      <button class="x" title="Remove" data-act="away-remove" data-pair="${esc(p.slug)}" data-index="${i}">×</button></li>`).join("");
    return `<section class="card">
      <div class="head"><h2>Away</h2><span class="kh">no changeovers planned while you're out — washed loads may wait up to the pair's wet-wait limit</span></div>
      ${rows ? `<ul class="away">${rows}</ul>` : `<div class="empty small">No away periods. Add one to keep changeovers out of it — e.g. lunch 13:00 → 22:00, or a lie-in (leaving now, back 09:00).</div>`}
      ${this._hass.user?.is_admin ? `<div class="tagbox sleep-set">
        <label>Asleep from<input type="time" data-role="bed" value="${esc(p.bedtime)}"></label>
        <label>Up and around from<input type="time" data-role="wake" value="${esc(p.wake_time)}"></label>
        <button class="primary" data-act="sleep-save" data-pair="${esc(p.slug)}">Save</button>
        <span class="sub">Every night — no changeovers, and in-use-overnight loads wait for this.</span>
      </div>` : ""}
      <div class="tagbox away-add">
        <label>Leaving<input type="datetime-local" data-role="away-start" value="${local(soon)}"></label>
        <label>Back <span class="sub">(blank = not back today)</span><input type="datetime-local" data-role="away-end"></label>
        <button class="primary" data-act="away-add" data-pair="${esc(p.slug)}">Add</button>
      </div>
    </section>`;
  }

  _laundryKpis(p) {
    const m = p.last_30d;
    const tile = (label, value, hint = "") => `
      <div class="kpi"><div class="kv">${value}</div><div class="kl">${label}</div>
      ${hint ? `<div class="kh">${hint}</div>` : ""}</div>`;
    return `<section class="kpis">
      ${tile("Loads · 30 days", m.loads)}
      ${tile("Cost · 30 days", this._money(m.cost), "wash + dry")}
      ${tile("Saved · 30 days", this._money(m.saved), "vs starting when you pressed Start")}
    </section>`;
  }

  /** Two lanes (Wash / Dry) of step bars, sleep window shaded, now line. */
  _laundryTimeline(p) {
    const steps = p.snapshot.steps || [];
    if (!steps.length) return "";
    const W = 640, L = 44, R = 8, T = 14, laneH = 24, H = T + 2 * laneH + 22;
    const now = Date.now();
    const starts = steps.map((s) => Date.parse(s.planned_start));
    const ends = steps.map((s) => Date.parse(s.planned_end));
    const pad = 20 * 60000;
    const t0 = Math.min(...starts, now) - pad, t1 = Math.max(...ends) + pad;
    const x = (ms) => L + ((ms - t0) / (t1 - t0)) * (W - L - R);
    const lane = { wash: 0, dry: 1 };
    const loadNames = [...new Set(steps.map((s) => s.load).filter(Boolean))];
    const lc = (s) => (s.load ? this._loadColour(p, s.load) : "var(--primary-color)");
    // Sleep window shading for every night the plan spans.
    let sleep = "";
    const sw = p.snapshot.sleep_window?.split("–");
    if (sw?.length === 2) {
      const [bh, bm] = sw[0].split(":").map(Number), [wh, wm] = sw[1].split(":").map(Number);
      const day = new Date(t0); day.setHours(0, 0, 0, 0); day.setDate(day.getDate() - 1);
      for (let d = day.getTime(); d < t1; d += 86400000) {
        const a = new Date(d); a.setHours(bh, bm, 0, 0);
        const b = new Date(d); b.setHours(wh, wm, 0, 0);
        if (b <= a) b.setDate(b.getDate() + 1);
        const xa = Math.max(L, x(a.getTime())), xb = Math.min(W - R, x(b.getTime()));
        if (xb > xa) sleep += `<rect class="sleep" x="${xa}" y="${T}" width="${xb - xa}" height="${2 * laneH}" data-tip="Sleep window — no changeovers" />`;
      }
    }
    let away = "";
    for (const a of p.snapshot.away || []) {
      const xa = Math.max(L, x(Date.parse(a.start))), xb = Math.min(W - R, x(Date.parse(a.until)));
      if (xb > xa) away += `<rect class="away-sh" x="${xa}" y="${T}" width="${xb - xa}" height="${2 * laneH}" data-tip="Away ${fmtWhen(a.start)} → ${fmtTime(a.until)} — no changeovers" />`;
    }
    let blackout = "";
    for (const bo of p.blackouts || []) {
      const xa = Math.max(L, x(Date.parse(bo.start))), xb = Math.min(W - R, x(Date.parse(bo.end)));
      if (xb > xa) blackout += `<rect class="blackout" x="${xa}" y="${T}" width="${xb - xa}" height="${2 * laneH}" data-tip="Power-down ${fmtWhen(bo.start)} → ${fmtTime(bo.end)} — machines wait it out" />`;
    }
    const bars = steps.map((s, i) => {
      const a = x(starts[i]), b = x(ends[i]);
      const y = T + lane[s.machine] * laneH + 4;
      const state = s.state || "planned";
      const w = Math.max(3, b - a - 2);
      // Load name inside the pill when it fits (~5 user units per char), else the tooltip + legend carry it.
      const name = s.load || s.name;
      const fit = Math.floor((w - 10) / 5);
      const text = fit >= 3 ? (name.length > fit ? `${name.slice(0, fit - 1)}…` : name) : "";
      return `<g style="--lc:${lc(s)}">
        <rect class="step st-${state}" x="${a}" y="${y}" width="${w}" height="${laneH - 8}" rx="4"
          data-tip="${s.load ? `${esc(s.load)}: ` : ""}${esc(s.name)} — ${fmtWhen(s.planned_start)} → ${fmtTime(s.planned_end)} · ${STEP_LABEL[state] || state}${s.confirm_before && i ? " · needs a changeover" : ""}" />
        ${s.confirm_before && i ? `<circle class="co" cx="${a}" cy="${y + (laneH - 8) / 2}" r="4" />` : ""}
        ${text ? `<text class="sl" x="${a + 7}" y="${y + (laneH - 8) / 2}">${esc(text)}</text>` : ""}
      </g>`;
    }).join("");
    const xt = [];
    const stepH = (t1 - t0) > 12 * 3600000 ? 3 : 1;
    const d = new Date(t0); d.setMinutes(0, 0, 0);
    for (let ms = d.getTime() + 3600000; ms < t1; ms += 3600000) {
      const h = new Date(ms).getHours();
      if (h % stepH) continue;
      xt.push(`<text class="ax" x="${x(ms)}" y="${H - 6}" text-anchor="middle">${h === 0 ? new Date(ms).toLocaleDateString([], { weekday: "short" }) : `${h}:00`}</text>`);
    }
    return `<section class="card">
      <div class="head"><h2>${p.snapshot.preview ? "Plan preview" : "Plan"}</h2>
        <span class="kh">${steps.length} steps · ${fmtWhen(p.snapshot.planned_start)} → ${fmtWhen(p.snapshot.planned_finish)}</span></div>
      <div class="scroll"><svg class="chart wide" viewBox="0 0 ${W} ${H}">
        ${sleep}${away}${blackout}
        <text class="ax" x="${L - 6}" y="${T + laneH / 2 + 3}" text-anchor="end">Wash</text>
        <text class="ax" x="${L - 6}" y="${T + laneH * 1.5 + 3}" text-anchor="end">Dry</text>
        <line class="grid" x1="${L}" x2="${W - R}" y1="${T + laneH}" y2="${T + laneH}" />
        ${bars}${xt.join("")}
        ${now > t0 && now < t1 ? `<line class="now" x1="${x(now)}" x2="${x(now)}" y1="${T - 10}" y2="${T + 2 * laneH}" />` : ""}
      </svg></div>
      <div class="legend">${loadNames.map((n) => `<span><i style="background:${this._loadColour(p, n)}"></i>${esc(n)}</span>`).join("")}</div>
      <div class="legend"><span><i class="lg-done"></i>Done</span><span><i class="lg-run"></i>Running</span>
        <span><i class="lg-plan"></i>Planned</span><span><i class="lg-co"></i>Changeover</span>
        ${sleep ? `<span><i class="lg-sleep"></i>Sleep window</span>` : ""}
        ${away ? `<span><i class="lg-away"></i>Away</span>` : ""}
        ${blackout ? `<span><i class="lg-blackout"></i>Power-down</span>` : ""}</div>
    </section>`;
  }

  _laundryWindows(p) {
    const s = p.snapshot;
    const nowCost = s.now_rate != null && s.total_kwh ? s.now_rate * s.total_kwh : null;
    const windows = s.best_windows || [];
    const ctx = { picked: this._picked, offer: s.offer };
    if (s.status === "scheduled" && s.planned_start) {
      ctx.sched = { scheduled_start: s.planned_start, estimated_finish: s.planned_finish, status: "scheduled", cycle_name: "Laundry plan" };
    }
    return `<section class="card">
      <div class="head"><h2>Recommended windows</h2>
        <span class="kh">${s.status === "idle" || s.preview ? "for the loads currently selected" : "for the active plan"}</span></div>
      ${windows.length ? this._windowList(windows, nowCost, ctx)
        : `<div class="empty small">Pick loads above to see windows for them.</div>`}
      ${this._laundryPickBar(p, windows)}
      ${this._rateChart(this._data.rates, windows, ctx)}
    </section>`;
  }

  _laundryPickBar(p, windows) {
    const win = windows.find((x) => x.start === this._picked);
    if (!win) return "";
    const s = p.snapshot;
    if (!(s.status === "idle" || s.status === "complete" || s.status === "scheduled")) {
      return `<div class="pick"><span class="sub">The plan is under way — pick a window once it's finished.</span></div>`;
    }
    const verb = s.status === "scheduled" ? "Move here" : "Schedule";
    return `<div class="pick">
      <div><b>${s.status === "scheduled" ? "Plan" : "These loads"}</b> ${fmtWhen(win.start)} → ${fmtWhen(win.end)} · ${this._rate(win.rate)}/kWh · ${this._money(win.cost)}
        ${win.confirmed ? "" : `<span class="sub"> · forecast — re-checked when real rates land, and only moved if cheaper</span>`}</div>
      <div class="cta"><button class="primary" data-act="laundry-pick-go" data-pair="${esc(p.slug)}" data-start="${esc(win.start)}">${verb}</button>
        <button class="link" data-act="pick" data-start="">Cancel</button></div>
    </div>`;
  }

  _flowsCard(p) {
    if (!p.flows.length) return "";
    const admin = this._hass.user?.is_admin;
    const rows = p.flows.map((f) => {
      const r = f.rhythm || {};
      const steps = f.steps.map((st) => `${st.machine === "wash" ? "Wash" : "Dry"}: ${esc(st.name)}`).join(" → ");
      const sug = f.suggest_repeat_days;
      return `<tr>
        <td><b>${esc(f.name)}</b><div class="kh">${steps}</div>
          ${admin ? `<label class="chk kh"><input type="checkbox" data-act="overnight" data-pair="${esc(p.slug)}" data-flow="${esc(f.slug)}" ${f.overnight ? "checked" : ""}>
            In use overnight — start only once I'm up</label>` : (f.overnight ? `<div class="kh">In use overnight</div>` : "")}</td>
        <td class="num">${f.loads}</td>
        <td class="num">${this._money(f.avg_cost)}</td>
        <td>${f.repeat_days ? `every ${f.repeat_days} d` : `<span class="muted">off</span>`}</td>
        <td>${r.interval_days != null ? fmtInterval(r.interval_days) : `<span class="muted">${f.loads < 3 ? "learning" : "irregular"}</span>`}
          ${sug && admin ? `<div><button class="link" data-act="repeat" data-pair="${esc(p.slug)}" data-flow="${esc(f.slug)}" data-days="${sug}">Set to every ${sug} d</button></div>` : ""}</td>
        <td>${f.last_done ? fmtDay(f.last_done) : "—"}</td>
        <td>${f.next_due ? fmtDay(f.next_due) : "—"}</td>
      </tr>`;
    }).join("");
    return `<section class="card">
      <div class="head"><h2>Flows</h2><span class="kh">Configured repeat vs how often you actually run it</span></div>
      <div class="scroll"><table class="flows">
        <thead><tr><th>Flow</th><th class="num">Loads</th><th class="num">Avg cost</th><th>Repeat</th>
          <th>Actually</th><th>Last done</th><th>Next due</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
    </section>`;
  }

  _loadsCard(p) {
    const rows = p.loads.map((l) => `
      <tr class="${l.run_ids.length ? "click" : ""}" data-act="${l.run_ids.length ? "load" : ""}" data-id="${esc(l.id)}">
        <td>${fmtWhen(l.started_at)}</td>
        <td>${l.flows.length ? l.flows.map(esc).join(", ") : `<span class="muted">custom</span>`}
          <div class="kh">${l.steps.map((s) => esc(s.name)).join(" → ")}</div></td>
        <td class="num">${l.started_at && l.finished_at ? fmtDur((Date.parse(l.finished_at) - Date.parse(l.started_at)) / 60000) : "—"}</td>
        <td class="num">${l.kwh != null ? l.kwh.toFixed(2) : "—"}</td>
        <td class="num">${this._money(l.cost)}${l.cost_source === "planned" ? `<div class="kh">planned</div>` : ""}</td>
        <td class="num">${l.saved != null ? `<span class="${l.saved > 0 ? "good" : ""}">${this._money(l.saved)}</span>` : "—"}</td>
      </tr>`).join("");
    return `<section class="card">
      <div class="head"><h2>Loads</h2><span class="kh">Completed wash → dry runs</span></div>
      ${p.loads.length ? `<div class="scroll"><table class="runs">
        <thead><tr><th>Started</th><th>Flow</th><th class="num">Total time</th><th class="num">kWh</th>
          <th class="num">Cost</th><th class="num">Saved</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
        : `<div class="empty small">No completed loads yet — they're recorded from now on.</div>`}
    </section>`;
  }

  async _openLoad(id) {
    const load = this._pair()?.loads.find((l) => l.id === id);
    if (!load) return;
    const runs = await Promise.all(load.run_ids.map((rid) =>
      this._ws({ type: "smartstart/run", run_id: rid }).catch(() => null)));
    this._detail = { load, runs: runs.filter(Boolean) };
    this._renderDialog();
  }

  _loadDialog(d) {
    const l = d.load;
    const parts = d.runs.map((r) => `
      <div class="chart-label">${esc(r.cycle_name)} · ${fmtWhen(r.started_at)} · ${fmtDur(r.duration_minutes)}
        · ${r.kwh != null ? `${r.kwh.toFixed(2)} kWh` : ""} · ${this._money(r.cost)}
        <button class="link" data-act="run" data-id="${esc(r.id)}">details</button></div>
      ${r.samples.length > 1 ? this._powerChart(r.samples, null, { height: 110 }) : `<div class="empty small">No power trace.</div>`}`).join("");
    return `<div class="scrim" data-act="close">
      <div class="dialog" role="dialog" aria-modal="true">
        <div class="head"><h2>${esc(l.flows.join(", ") || "Load")} · ${fmtWhen(l.started_at)}</h2>
          <button class="x" data-act="close" aria-label="Close">✕</button></div>
        <div class="facts">
          <div><span>Total time</span>${fmtDur((Date.parse(l.finished_at) - Date.parse(l.started_at)) / 60000)}</div>
          <div><span>Energy</span>${l.kwh != null ? `${l.kwh.toFixed(3)} kWh` : "—"}</div>
          <div><span>Cost</span>${this._money(l.cost, 3)}</div>
          <div><span>Saved</span>${this._money(l.saved, 3)}</div>
        </div>
        ${parts}
      </div></div>`;
  }

  _cyclesCard(dev) {
    const rows = dev.cycles.map((c) => {
      const r = c.rhythm || {};
      const curve = c.curve;
      const conf = curve ? Math.round(curve.confidence * 100) : null;
      const sug = c.suggestion;
      let next = "—";
      if (r.next_due) {
        next = `<span class="rs rs-${r.status}">${RHYTHM_LABEL[r.status]}</span> ${fmtDay(r.next_due)}`;
      } else if (r.status) {
        next = `<span class="rs rs-${r.status}">${RHYTHM_LABEL[r.status]}</span>`;
      }
      let sugTxt = "";
      if (sug?.beyond_horizon) sugTxt = `<div class="kh">rates not out yet</div>`;
      else if (sug?.start) sugTxt = `<div class="kh">best: ${fmtWhen(sug.start)} · ${this._money(sug.cost)}${sug.predicted ? " (forecast)" : ""}</div>`;
      return `<tr class="click ${this._runFilter === c.cycle_key ? "sel" : ""}" data-act="filter" data-ck="${esc(c.cycle_key)}">
        <td><b>${esc(c.name)}</b>
          ${curve?.profile?.length ? this._sparkline(curve.profile) : ""}</td>
        <td class="num">${c.runs}</td>
        <td class="num">${fmtDur(c.avg_minutes ?? curve?.learned_minutes ?? c.configured_minutes)}</td>
        <td class="num">${c.avg_kwh != null ? c.avg_kwh.toFixed(2) : (c.configured_kwh != null ? `<span class="muted">${c.configured_kwh}</span>` : "—")}</td>
        <td class="num">${this._money(c.avg_cost)}</td>
        <td class="num">${c.total_saved ? this._money(c.total_saved) : "—"}</td>
        <td>${conf != null ? `<div class="bar" title="${esc(curve.status)} · ${curve.n_curves} curves"><i style="width:${conf}%"></i></div><span class="kh">${conf}% · ${curve.n_curves}</span>` : "—"}</td>
        <td>${r.interval_days != null ? fmtInterval(r.interval_days) : "—"}
          <div class="kh">${r.runs_per_week ? `${r.runs_per_week}/wk` : ""}${r.typical_hour != null ? ` · ~${fmtHour(r.typical_hour)}` : ""}</div></td>
        <td>${next}${sugTxt}</td>
      </tr>`;
    }).join("");
    return `<section class="card">
      <div class="head"><h2>Cycles</h2><span class="kh">Click a cycle to filter runs</span></div>
      <div class="scroll"><table>
        <thead><tr><th>Cycle</th><th class="num">Runs</th><th class="num">Avg time</th>
          <th class="num">Avg kWh</th><th class="num">Avg cost</th><th class="num">Saved</th>
          <th>Curve</th><th>Rhythm</th><th>Next</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
    </section>`;
  }

  _rhythmCard(dev) {
    const grid = dev.heatmap || [];
    const max = Math.max(1, ...grid.flat());
    if (!grid.flat().some(Boolean)) return "";
    const cells = grid.map((row, d) => `
      <div class="hl">${DAYS[d]}</div>
      ${row.map((n, h) => `<div class="hc" data-tip="${DAYS[d]} ${String(h).padStart(2, "0")}:00 — ${n} run${n === 1 ? "" : "s"}"
          style="--a:${n ? 0.15 + 0.85 * n / max : 0}"></div>`).join("")}`).join("");
    const hours = Array.from({ length: 24 }, (_, h) => `<div class="hh">${h % 6 === 0 ? h : ""}</div>`).join("");
    return `<section class="card">
      <div class="head"><h2>When it runs</h2><span class="kh">Run starts by weekday and hour</span></div>
      <div class="scroll"><div class="heat"><div></div>${hours}${cells}</div></div>
    </section>`;
  }

  _runsCard(dev) {
    const filterName = this._runFilter ? dev.cycles.find((c) => c.cycle_key === this._runFilter)?.name : null;
    const rows = this._runs.map((r) => `
      <tr class="click" data-act="run" data-id="${esc(r.id)}">
        <td>${fmtWhen(r.started_at)}</td>
        <td>${r.cycle_key === "_external"
          ? `<span class="tag warn">Unknown — tag</span>`
          : esc(r.cycle_name)}${r.tagged_at ? ` <span class="tag">tagged</span>` : ""}</td>
        <td class="num">${fmtDur(r.duration_minutes)}</td>
        <td class="num">${r.kwh != null ? r.kwh.toFixed(2) : "—"}</td>
        <td class="num">${this._money(r.cost)}</td>
        <td class="num">${this._rate(r.avg_rate)}</td>
        <td class="num">${r.saved != null ? `<span class="${r.saved > 0 ? "good" : ""}">${this._money(r.saved)}</span>` : "—"}</td>
        <td>${r.source === "seed" ? `<span class="muted">from curves</span>`
          : r.triggered_by === "external" ? "Manual" : (r.laundry_run ? "Laundry" : "SmartStart")}
          ${r.delay_minutes > 1 ? `<div class="kh">waited ${fmtDur(r.delay_minutes)}</div>` : ""}</td>
        <td>${r.has_curve ? this._sparkline(null, true) : ""}</td>
      </tr>`).join("");
    return `<section class="card">
      <div class="head"><h2>Runs${filterName ? ` · ${esc(filterName)}` : ""}</h2>
        ${this._runFilter ? `<button class="link" data-act="filter" data-ck="">Show all</button>` : ""}
        <span class="kh">${this._runsTotal} total</span></div>
      ${this._runs.length ? `<div class="scroll"><table class="runs">
        <thead><tr><th>Started</th><th>Cycle</th><th class="num">Time</th><th class="num">kWh</th>
          <th class="num">Cost</th><th class="num">Rate</th><th class="num">Saved</th><th>Started by</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : `<div class="empty small">No runs yet — they appear here as cycles finish.</div>`}
      ${this._runs.length < this._runsTotal ? `<button class="more" data-act="more">Load more</button>` : ""}
    </section>`;
  }

  // ── Charts (hand-rolled SVG; viewBox units = px at 100% width) ─────────────

  _sparkline(profile, icon = false) {
    if (icon) {
      return `<svg class="ico" viewBox="0 0 24 12"><path d="M1 11 L5 3 L9 8 L13 2 L17 9 L23 11" /></svg>`;
    }
    const max = Math.max(...profile, 1e-9);
    const W = 80, H = 18, n = profile.length;
    const pts = profile.map((v, i) => `${(i + 0.5) * W / n},${H - 1 - (v / max) * (H - 3)}`).join(" ");
    return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><polyline points="${pts}" /></svg>`;
  }

  /** samples: [[t_s, W]]; reference: {profile, avg_minutes, kwh} → dashed overlay. */
  _powerChart(samples, reference, { height = 200 } = {}) {
    const W = 640, H = height, L = 44, R = 8, T = 8, B = 22;
    const tMax = Math.max(samples[samples.length - 1][0], (reference?.avg_minutes || 0) * 60, 60);
    let refPts = [];
    if (reference?.profile?.length && reference.avg_minutes && reference.kwh) {
      // Energy fraction per bucket → mean power over that bucket.
      const n = reference.profile.length;
      const bucketS = reference.avg_minutes * 60 / n;
      refPts = reference.profile.map((f, i) => [
        (i + 0.5) * bucketS, f * reference.kwh * 3.6e6 / bucketS,
      ]);
    }
    const pMax = Math.max(10, ...samples.map((s) => s[1]), ...refPts.map((p) => p[1])) * 1.08;
    const x = (t) => L + (t / tMax) * (W - L - R);
    const y = (p) => T + (1 - p / pMax) * (H - T - B);
    const line = samples.map((s, i) => `${i ? "L" : "M"}${x(s[0]).toFixed(1)},${y(s[1]).toFixed(1)}`).join("");
    const area = `${line}L${x(samples[samples.length - 1][0]).toFixed(1)},${y(0)}L${x(samples[0][0]).toFixed(1)},${y(0)}Z`;
    const ref = refPts.length
      ? `<path class="ref" d="${refPts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("")}" />`
      : "";
    const yTicks = niceTicks(pMax, 4).map((v) => `
      <line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" />
      <text class="ax" x="${L - 6}" y="${y(v) + 3}" text-anchor="end">${v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v}</text>`).join("");
    const stepMin = tMax > 3 * 3600 ? 60 : 30;
    const xTicks = [];
    for (let m = 0; m * 60 <= tMax; m += stepMin) {
      xTicks.push(`<text class="ax" x="${x(m * 60)}" y="${H - 6}" text-anchor="middle">${fmtDur(m)}</text>`);
    }
    const cid = String((this._chartSeq = (this._chartSeq || 0) + 1));
    (this._charts ||= {})[cid] = { samples, x0: L, x1: W - R, tMax, W };
    return `<svg class="chart power" viewBox="0 0 ${W} ${H}" data-chart="power" data-cid="${cid}">
      ${yTicks}${xTicks.join("")}
      <path class="area" d="${area}" /><path class="line" d="${line}" />${ref}
      <line class="cross" x1="0" x2="0" y1="${T}" y2="${H - B}" visibility="hidden" />
      <rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" />
    </svg>`;
  }

  /** Step chart of the tariff in effect during a run: [[t_s, rate]]. */
  _runRateChart(rates, tEnd) {
    if (!rates?.length) return "";
    const W = 640, H = 70, L = 44, R = 8, T = 6, B = 16;
    const vals = rates.map((r) => r[1]);
    const lo = Math.min(0, ...vals), hi = Math.max(...vals) * 1.1 || 1;
    const x = (t) => L + (t / tEnd) * (W - L - R);
    const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    let d = "";
    rates.forEach((r, i) => {
      const next = i + 1 < rates.length ? rates[i + 1][0] : tEnd;
      d += `${i ? "L" : "M"}${x(r[0]).toFixed(1)},${y(r[1]).toFixed(1)}L${x(next).toFixed(1)},${y(r[1]).toFixed(1)}`;
    });
    const labels = rates.map((r, i) => {
      const next = i + 1 < rates.length ? rates[i + 1][0] : tEnd;
      return (x(next) - x(r[0]) > 46)
        ? `<text class="ax" x="${(x(r[0]) + x(next)) / 2}" y="${y(r[1]) - 4}" text-anchor="middle">${this._rate(r[1])}</text>` : "";
    }).join("");
    return `<div class="chart-label">Rate during this run (/kWh)</div>
      <svg class="chart" viewBox="0 0 ${W} ${H}">
        ${lo < 0 ? `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" />` : ""}
        <path class="rate" d="${d}" />${labels}</svg>`;
  }

  /** Next ~48h of slots as bars, recommended windows shaded. */
  _rateChart(rates, windows, ctx = {}) {
    const slots = rates?.slots || [];
    if (!slots.length) return "";
    const W = 640, H = 150, L = 36, R = 8, T = 18, B = 22;
    const t0 = Date.parse(slots[0].start), t1 = Date.parse(slots[slots.length - 1].end);
    const vals = slots.map((s) => s.rate);
    const lo = Math.min(0, ...vals), hi = Math.max(...vals, ...slots.map((s) => s.high ?? 0)) * 1.08 || 1;
    const x = (ms) => L + ((ms - t0) / (t1 - t0)) * (W - L - R);
    const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    const now = Date.now();
    const shade = windows.map((w, i) => {
      const a = x(Date.parse(w.start)), b = x(Date.parse(w.end));
      return `<rect class="win ${i === 0 ? "best" : ""} ${ctx.picked === w.start ? "picked" : ""}" x="${a}" y="${T - 14}" width="${Math.max(2, b - a)}" height="${H - B - T + 14}" rx="3"
          data-act="pick" data-start="${esc(w.start)}" data-tip="Window ${i + 1}: ${fmtWhen(w.start)} — tap to schedule" />
        <text class="wl" x="${a + 4}" y="${T - 4}">${i + 1}</text>`;
    }).join("");
    let reach = "";
    if (ctx.reach) {
      const a = x(Math.max(now, t0)), b = x(Math.min(Date.parse(ctx.reach.latest_start), t1));
      if (b > a) reach = `<rect class="reach" x="${a}" y="${T - 14}" width="${b - a}" height="${H - B - T + 14}"
          data-tip="Current schedule can move to any start up to ${fmtWhen(ctx.reach.latest_start)}" />
        <line class="reach-edge" x1="${b}" x2="${b}" y1="${T - 14}" y2="${H - B}" />`;
    }
    // Power-down / saving-session windows: cycles avoid these when set to Avoid.
    let blackout = "";
    for (const bo of rates.blackouts || []) {
      const a = x(Math.max(Date.parse(bo.start), t0)), b = x(Math.min(Date.parse(bo.end), t1));
      if (b > a) blackout += `<rect class="blackout" x="${a}" y="${T - 14}" width="${b - a}" height="${H - B - T + 14}"
          data-tip="Power-down ${fmtWhen(bo.start)} → ${fmtTime(bo.end)}" />`;
    }
    let sched = "";
    if (ctx.sched?.scheduled_start && ctx.sched?.estimated_finish) {
      const a = x(Math.max(Date.parse(ctx.sched.scheduled_start), t0));
      const b = x(Math.min(Date.parse(ctx.sched.estimated_finish), t1));
      if (b > a) sched = `<rect class="sched" x="${a}" y="${T - 14}" width="${Math.max(2, b - a)}" height="${H - B - T + 14}" rx="3"
          data-tip="${esc(ctx.sched.cycle_name || "Run")} ${ctx.sched.status === "scheduled" ? "scheduled" : "running"}: ${fmtWhen(ctx.sched.scheduled_start)} → ${fmtTime(ctx.sched.estimated_finish)}" />
        <text class="wl sched-l" x="${a + 4}" y="${H - B - 4}">▶</text>`;
    }
    let offer = "";
    if (ctx.offer) {
      const a = x(Date.parse(ctx.offer.start)), b = x(Date.parse(ctx.offer.end));
      if (a < W - R) offer = `<rect class="offer-win" x="${a}" y="${T - 14}" width="${Math.max(2, Math.min(b, W - R) - a)}" height="${H - B - T + 14}" rx="3"
          data-tip="Offer: ${fmtWhen(ctx.offer.start)} — save ~${this._money(ctx.offer.saving)}" />`;
    }
    const bars = slots.map((s) => {
      const a = x(Date.parse(s.start)) + 1, b = x(Date.parse(s.end)) - 1;
      const top = Math.min(y(s.rate), y(0)), h = Math.max(1, Math.abs(y(s.rate) - y(0)));
      const past = Date.parse(s.end) <= now;
      const band = s.predicted && s.high != null
        ? `<line class="band" x1="${(a + b) / 2}" x2="${(a + b) / 2}" y1="${y(s.high)}" y2="${y(s.low ?? s.rate)}" />` : "";
      return `${band}<rect class="slot ${s.predicted ? "pred" : ""} ${past ? "past" : ""}" x="${a}" y="${top}" width="${Math.max(1, b - a)}" height="${h}" rx="1.5"
        data-tip="${fmtWhen(s.start)} — ${this._rate(s.rate)}/kWh${s.predicted ? ` (forecast${s.high != null ? ` ${this._rate(s.low)}–${this._rate(s.high)}` : ""})` : ""}" />`;
    }).join("");
    const yt = niceTicks(hi, 3).map((v) => `
      <line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" />
      <text class="ax" x="${L - 4}" y="${y(v) + 3}" text-anchor="end">${this._rate(v)}</text>`).join("");
    const xt = [];
    const d = new Date(t0); d.setMinutes(0, 0, 0);
    for (let ms = d.getTime(); ms <= t1; ms += 3600000) {
      const h = new Date(ms).getHours();
      if (ms < t0 || h % 6) continue;
      xt.push(`<text class="ax" x="${x(ms)}" y="${H - 6}" text-anchor="middle">${h === 0 ? new Date(ms).toLocaleDateString([], { weekday: "short" }) : `${h}:00`}</text>`);
    }
    return `<div class="chart-label">Rates — next 48h${rates.real_until ? ` · forecast after ${fmtWhen(rates.real_until)}` : ""}</div>
      <div class="scroll"><svg class="chart wide" viewBox="0 0 ${W} ${H}">
      ${yt}${reach}${blackout}${shade}${sched}${offer}${bars}${xt.join("")}
      ${now > t0 && now < t1 ? `<line class="now" x1="${x(now)}" x2="${x(now)}" y1="${T - 14}" y2="${H - B}" />` : ""}
      ${lo < 0 ? `<line class="zero" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" />` : ""}
      </svg></div>
      <div class="legend"><span><i class="lg-slot"></i>Confirmed rate</span>
        <span><i class="lg-pred"></i>Forecast</span><span><i class="lg-win"></i>Recommended window</span>
        ${sched ? `<span><i class="lg-sched"></i>${ctx.sched.status === "scheduled" ? "Scheduled" : "Running"}</span>` : ""}
        ${ctx.reach ? `<span><i class="lg-reach"></i>Can move until ${fmtWhen(ctx.reach.latest_start)}</span>` : ""}
        ${ctx.offer ? `<span><i class="lg-offer"></i>Offer</span>` : ""}
        ${blackout ? `<span><i class="lg-blackout"></i>Power-down</span>` : ""}</div>`;
  }

  // ── Run detail dialog ──────────────────────────────────────────────────────

  async _openRun(id) {
    try {
      this._detail = await this._ws({ type: "smartstart/run", run_id: id });
    } catch (e) {
      this._detail = { error: e.message };
    }
    this._renderDialog();
  }

  _renderDialog() {
    const host = this.shadowRoot.querySelector(".dialog-host");
    const r = this._detail;
    this._hideTip();
    if (!r) { host.innerHTML = ""; return; }
    if (r.load) { host.innerHTML = this._loadDialog(r); return; }
    if (r.error) {
      host.innerHTML = `<div class="scrim" data-act="close"><div class="dialog">${esc(r.error)}</div></div>`;
      return;
    }
    const ref = r.reference && r.kwh ? { ...r.reference, kwh: r.kwh } : null;
    const tEnd = r.samples.length ? r.samples[r.samples.length - 1][0] : (r.duration_minutes || 1) * 60;
    const isUnknown = r.cycle_key === "_external";
    // Unknown runs pre-select the best curve match when it's convincing.
    const best = r.matches?.[0];
    const pick = isUnknown && best?.score >= 0.6 ? best.cycle_key : r.cycle_key;
    const opts = r.cycle_options.map((c) =>
      `<option value="${esc(c.cycle_key)}" ${c.cycle_key === pick ? "selected" : ""}>${esc(c.name)}</option>`).join("");
    const matches = r.matches?.length ? `<div class="matches">Looks most like:
      ${r.matches.map((m) => `<span class="tag" title="shape ${Math.round(m.shape * 100)}% · duration ${Math.round(m.duration * 100)}%">${esc(m.name)} ${Math.round(m.score * 100)}%</span>`).join(" ")}</div>` : "";
    const admin = this._hass.user?.is_admin;
    host.innerHTML = `<div class="scrim" data-act="close">
      <div class="dialog" role="dialog" aria-modal="true">
        <div class="head"><h2>${esc(r.cycle_name)} · ${fmtWhen(r.started_at)}</h2>
          <button class="x" data-act="close" aria-label="Close">✕</button></div>
        <div class="facts">
          <div><span>Duration</span>${fmtDur(r.duration_minutes)}</div>
          <div><span>Energy</span>${r.kwh != null ? `${r.kwh.toFixed(3)} kWh` : "—"}</div>
          <div><span>Cost</span>${this._money(r.cost, 3)}</div>
          <div><span>Avg rate</span>${this._rate(r.avg_rate)}/kWh</div>
          <div><span>Saved</span>${this._money(r.saved, 3)}</div>
          <div><span>Started by</span>${r.triggered_by === "external" ? "Manual" : "SmartStart"}${r.delay_minutes > 1 ? ` · waited ${fmtDur(r.delay_minutes)}` : ""}</div>
        </div>
        ${r.samples.length > 1 ? `
          <div class="chart-label">Power (W) vs time into cycle</div>
          ${this._powerChart(r.samples, ref)}
          <div class="legend"><span><i class="lg-line"></i>This run</span>
            ${ref ? `<span><i class="lg-ref"></i>${esc(r.cycle_name)} learned curve (${r.reference.n_curves} runs)</span>` : ""}</div>
          ${this._runRateChart(r.rates, tEnd)}`
          : `<div class="empty small">${r.source === "seed" ? "Recorded before run history existed — no power trace kept." : "No power trace for this run."}</div>`}
        ${matches}
        <div class="tagbox">
          <label>${isUnknown ? "What cycle was this?" : "Cycle"}
            <select data-role="tag"><option value="_external" ${pick === "_external" ? "selected" : ""}>Unknown</option>${opts}</select></label>
          ${r.samples.length >= 5 ? `<label class="chk"><input type="checkbox" data-role="learn" checked> Learn its curve &amp; duration</label>` : ""}
          <button class="primary" data-act="tag">Save</button>
          ${admin ? `<button class="danger" data-act="delete">Delete run</button>` : ""}
        </div>
        ${r.learned ? `<div class="kh">This run is part of ${esc(r.cycle_name)}'s learned curve.</div>` : ""}
      </div></div>`;
  }

  // ── Events ─────────────────────────────────────────────────────────────────

  async _onClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const act = el.dataset.act;
    if (act === "close") {
      if (e.target !== el) return;  // clicks inside the dialog bubble to the scrim
      this._detail = null; this._renderDialog(); return;
    }
    if (act === "device") {
      this._device = el.dataset.slug; this._runFilter = null; this._picked = null;
      if (this._dev()) {
        if (!this._winCycle[this._device]) {
          const dev = this._dev();
          this._winCycle[this._device] = dev.selected_cycle
            || dev.cycles.find((c) => c.cycle_key !== "_external")?.cycle_key;
        }
        await Promise.all([this._loadWindows(), this._loadRuns(false)]);
      }
      this._render();
    } else if (act === "filter") {
      const ck = el.dataset.ck || null;
      this._runFilter = this._runFilter === ck ? null : ck;
      await this._loadRuns(false); this._render();
    } else if (act === "more") {
      await this._loadRuns(true); this._render();
    } else if (act === "pick") {
      const start = el.dataset.start || null;
      this._picked = this._picked === start ? null : start;
      this._render();
    } else if (act === "pick-go") {
      el.disabled = true;
      try {
        await this._hass.callService("smartstart", "start", {
          device_slug: this._device, cycle_id: el.dataset.ck, start_at: el.dataset.start,
        });
        this._picked = null;
        await this._refresh(false);
      } catch (err) { el.disabled = false; alert(`Couldn't schedule: ${err.message}`); }
    } else if (act === "press") {
      if (el.dataset.confirm && !confirm(el.dataset.confirm)) return;
      el.disabled = true;
      try {
        await this._hass.callService("button", "press", { entity_id: el.dataset.eid });
        setTimeout(() => this._refresh(true), 1200);
      } catch (err) { el.disabled = false; alert(`Couldn't do that: ${err.message}`); }
    } else if (act === "accept-offer") {
      el.disabled = true;
      try {
        await this._hass.callService("smartstart", "accept_offer", { device_slug: el.dataset.slug });
        await this._refresh(false);
      } catch (err) { el.disabled = false; alert(`Couldn't accept: ${err.message}`); }
    } else if (act === "load") {
      this._openLoad(el.dataset.id);
    } else if (act === "sleep-save") {
      const box = el.closest(".sleep-set");
      el.disabled = true;
      try {
        await this._ws({ type: "smartstart/laundry_sleep", pair: el.dataset.pair,
          bedtime: box.querySelector("[data-role=bed]").value, wake_time: box.querySelector("[data-role=wake]").value });
        setTimeout(() => this._refresh(false), 1500);
      } catch (err) { alert(`Couldn't save: ${err.message}`); }
      el.disabled = false;
    } else if (act === "start-now") {
      el.disabled = true;
      try {
        await this._ws({ type: "smartstart/laundry_start_now", pair: el.dataset.pair });
        await this._refresh(false);
      } catch (err) { el.disabled = false; alert(`Couldn't start: ${err.message}`); }
    } else if (act === "due-clear") {
      try {
        await this._ws({ type: "smartstart/laundry_due", pair: el.dataset.pair, due_at: null });
        await this._refresh(false);
      } catch (err) { alert(`Couldn't clear: ${err.message}`); }
    } else if (act === "continue") {
      el.disabled = true;
      try {
        await this._ws({ type: "smartstart/laundry_continue", pair: el.dataset.pair, start_now: !!el.dataset.now });
        await this._refresh(false);
      } catch (err) { el.disabled = false; alert(`Couldn't continue: ${err.message}`); }
    } else if (act === "laundry-pick-go") {
      el.disabled = true;
      try {
        await this._ws({ type: "smartstart/laundry_schedule", pair: el.dataset.pair, start_at: el.dataset.start });
        this._picked = null;
        await this._refresh(false);
      } catch (err) { el.disabled = false; alert(`Couldn't schedule: ${err.message}`); }
    } else if (act === "laundry-offer") {
      el.disabled = true;
      try {
        await this._ws({ type: "smartstart/laundry_accept_offer", pair: el.dataset.pair });
        await this._refresh(false);
      } catch (err) { el.disabled = false; alert(`Couldn't accept: ${err.message}`); }
    } else if (act === "load-clear") {
      const eids = (el.dataset.eids || "").split(",").filter(Boolean);
      el.disabled = true;
      try {
        for (const eid of eids) {
          await this._hass.callService("select", "select_option", { entity_id: eid, option: "None" });
        }
        setTimeout(() => this._refresh(false), 800);
      } catch (err) { el.disabled = false; alert(`Couldn't remove: ${err.message}`); }
    } else if (act === "away-add") {
      const card = el.closest(".away-add");
      const start = card.querySelector("[data-role=away-start]").value;
      const end = card.querySelector("[data-role=away-end]").value;
      if (!start) { alert("Pick when you're leaving."); return; }
      el.disabled = true;
      try {
        await this._ws({ type: "smartstart/laundry_away", pair: el.dataset.pair, action: "add",
          start: new Date(start).toISOString(), end: end ? new Date(end).toISOString() : null });
        await this._refresh(false);
      } catch (err) { el.disabled = false; alert(`Couldn't add: ${err.message}`); }
    } else if (act === "away-remove") {
      try {
        await this._ws({ type: "smartstart/laundry_away", pair: el.dataset.pair, action: "remove", index: Number(el.dataset.index) });
        await this._refresh(false);
      } catch (err) { alert(`Couldn't remove: ${err.message}`); }
    } else if (act === "repeat") {
      const days = Number(el.dataset.days);
      if (!confirm(`Set this flow to repeat every ${days} days? The laundry pair reloads to apply it.`)) return;
      try {
        await this._ws({ type: "smartstart/laundry_set_repeat", pair: el.dataset.pair, flow: el.dataset.flow, repeat_days: days });
        setTimeout(() => this._refresh(false), 1500);
      } catch (err) { alert(`Couldn't update: ${err.message}`); }
    } else if (act === "run") {
      this._openRun(el.dataset.id);
    } else if (act === "tag") {
      const dlg = this.shadowRoot.querySelector(".dialog");
      const ck = dlg.querySelector("[data-role=tag]").value;
      const learn = dlg.querySelector("[data-role=learn]")?.checked ?? false;
      el.disabled = true;
      try {
        await this._ws({ type: "smartstart/tag_run", run_id: this._detail.id, cycle_key: ck, learn });
        this._detail = null; this._renderDialog();
        await this._refresh(true);
      } catch (err) { el.disabled = false; alert(`Couldn't tag run: ${err.message}`); }
    } else if (act === "delete") {
      if (!confirm("Delete this run from history? If it was learned, its curve is removed too.")) return;
      try {
        await this._ws({ type: "smartstart/delete_run", run_id: this._detail.id });
        this._detail = null; this._renderDialog();
        await this._refresh(true);
      } catch (err) { alert(`Couldn't delete run: ${err.message}`); }
    }
  }

  async _onChange(e) {
    if (e.target.dataset.act === "overnight") {
      try {
        await this._ws({ type: "smartstart/laundry_flow_overnight", pair: e.target.dataset.pair,
          flow: e.target.dataset.flow, overnight: e.target.checked });
        setTimeout(() => this._refresh(false), 1500);
      } catch (err) { e.target.checked = !e.target.checked; alert(`Couldn't update: ${err.message}`); }
      return;
    }
    if (e.target.dataset.act === "due") {
      const v = e.target.value;
      try {
        await this._ws({ type: "smartstart/laundry_due", pair: e.target.dataset.pair,
          due_at: v ? new Date(v).toISOString() : null });
        await this._refresh(false);
      } catch (err) { alert(`Couldn't set: ${err.message}`); }
      return;
    }
    if (e.target.dataset.act === "ent-select") {
      try {
        await this._hass.callService("select", "select_option",
          { entity_id: e.target.dataset.eid, option: e.target.value });
        setTimeout(() => this._refresh(false), 800);
      } catch (err) { alert(`Couldn't change: ${err.message}`); }
      return;
    }
    if (e.target.dataset.act === "wincycle") {
      this._picked = null;
      this._winCycle[this._device] = e.target.value;
      await this._loadWindows(); this._render();
    }
  }

  _onHover(e) {
    const svg = e.target.closest?.("svg[data-chart=power]");
    const tipEl = e.target.closest?.("[data-tip]");
    const cd = svg && this._charts?.[svg.dataset.cid];
    if (cd) {
      const { samples, x0, x1, tMax, W } = cd;
      const box = svg.getBoundingClientRect();
      const px = (e.clientX - box.left) * (W / box.width);
      const t = ((px - x0) / (x1 - x0)) * tMax;
      if (t < 0 || t > tMax) { this._hideTip(); return; }
      let best = samples[0];
      for (const s of samples) if (Math.abs(s[0] - t) < Math.abs(best[0] - t)) best = s;
      const cross = svg.querySelector(".cross");
      const cx = x0 + (best[0] / tMax) * (x1 - x0);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx);
      cross.setAttribute("visibility", "visible");
      this._showTip(e, `${fmtDur(best[0] / 60)} in · <b>${Math.round(best[1])} W</b>`);
      return;
    }
    if (tipEl) { this._showTip(e, esc(tipEl.dataset.tip)); return; }
    this._hideTip();
  }

  _showTip(e, html) {
    const tip = this.shadowRoot.querySelector(".tip");
    tip.innerHTML = html; tip.hidden = false;
    const x = Math.min(e.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = `${x}px`; tip.style.top = `${e.clientY + 14}px`;
  }

  _hideTip() {
    const tip = this.shadowRoot.querySelector(".tip");
    if (tip) tip.hidden = true;
    this.shadowRoot.querySelectorAll(".cross").forEach((c) => c.setAttribute("visibility", "hidden"));
  }
}

function niceTicks(max, count) {
  if (!(max > 0)) return [0];
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const out = [];
  for (let v = 0; v <= max + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}

const STYLES = `
  :host { display:block; background:var(--primary-background-color); min-height:100vh;
    color:var(--primary-text-color); font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
  .toolbar { display:flex; align-items:center; height:56px; padding:0 12px; gap:8px;
    background:var(--app-header-background-color, var(--primary-color)); color:var(--app-header-text-color, #fff); }
  .title { font-size:20px; }
  .tabs { display:flex; gap:8px; padding:12px 16px 0; flex-wrap:wrap; }
  .tab { display:flex; align-items:center; gap:8px; border:1px solid var(--divider-color); background:var(--card-background-color);
    color:var(--primary-text-color); border-radius:18px; padding:6px 14px; font:inherit; cursor:pointer; }
  .tab.on { border-color:var(--primary-color); box-shadow:inset 0 0 0 1px var(--primary-color); }
  .dot { width:8px; height:8px; border-radius:50%; }
  .badge { background:var(--warning-color,#ff9800); color:#000; border-radius:9px; font-size:11px; padding:0 6px; }
  main { padding:16px; display:grid; gap:16px; max-width:1200px; margin:0 auto; }
  .card { background:var(--card-background-color); border-radius:var(--ha-card-border-radius,12px);
    box-shadow:var(--ha-card-box-shadow, none); border:1px solid var(--divider-color); padding:16px; min-width:0; }
  .card.error { color:var(--error-color); }
  .head { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:12px; }
  h2 { font-size:16px; font-weight:500; margin:0; flex:1; }
  .status { display:grid; gap:4px; }
  .pill { justify-self:start; font-size:12px; text-transform:uppercase; letter-spacing:.05em; padding:2px 10px; border-radius:10px;
    color:var(--c); border:1px solid var(--c); }
  .big { font-size:20px; }
  .sub, .kh { color:var(--secondary-text-color); font-size:12px; }
  .kh { margin-top:2px; }
  .muted { color:var(--secondary-text-color); }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit, minmax(160px,1fr)); gap:16px; }
  .kpi { background:var(--card-background-color); border:1px solid var(--divider-color); border-radius:var(--ha-card-border-radius,12px); padding:14px 16px; }
  .kv { font-size:26px; font-weight:500; font-variant-numeric:tabular-nums; }
  .kl { color:var(--secondary-text-color); font-size:13px; }
  select { font:inherit; padding:6px 8px; border-radius:6px; border:1px solid var(--divider-color);
    background:var(--card-background-color); color:var(--primary-text-color); }
  .windows { list-style:none; padding:0; margin:0 0 8px; display:grid; gap:8px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); }
  .windows li { border:1px solid var(--divider-color); border-radius:8px; padding:10px 12px; }
  .windows li.best { border-color:var(--primary-color); }
  .wtime { font-size:15px; } .wmeta { font-size:13px; color:var(--secondary-text-color); display:flex; gap:6px; flex-wrap:wrap; margin-top:2px; }
  .good { color:var(--success-color,#4caf50); }
  .tag { font-size:11px; border:1px solid var(--divider-color); border-radius:8px; padding:0 6px; color:var(--secondary-text-color); }
  .tag.on { border-color:var(--warning-color,#ff9800); color:var(--warning-color,#ff9800); }
  .offer { margin-top:12px; padding:10px 12px; border-radius:10px; border:1px solid var(--warning-color,#ff9800);
    background:color-mix(in srgb, var(--warning-color,#ff9800) 8%, transparent); }
  .offer .cta { margin-top:8px; }
  .tag.warn { border-color:var(--warning-color,#ff9800); color:var(--primary-text-color); }
  .scroll { overflow-x:auto; }
  table { border-collapse:collapse; width:100%; font-size:14px; }
  th { text-align:left; font-weight:500; color:var(--secondary-text-color); font-size:12px; padding:6px 8px; border-bottom:1px solid var(--divider-color); white-space:nowrap; }
  td { padding:8px; border-bottom:1px solid var(--divider-color); vertical-align:top; }
  .num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  tr.click { cursor:pointer; } tr.click:hover { background:var(--secondary-background-color); }
  tr.sel { background:var(--secondary-background-color); }
  .bar { width:64px; height:6px; border-radius:3px; background:var(--divider-color); overflow:hidden; margin-top:6px; }
  .bar i { display:block; height:100%; background:var(--primary-color); border-radius:3px; }
  .rs { font-size:12px; padding:0 6px; border-radius:8px; border:1px solid var(--divider-color); }
  .rs-due { border-color:var(--warning-color,#ff9800); }
  .rs-overdue { border-color:var(--error-color,#f44336); }
  .spark { display:block; width:80px; height:18px; margin-top:4px; }
  .spark polyline, .ico path { fill:none; stroke:var(--primary-color); stroke-width:1.5; vector-effect:non-scaling-stroke; }
  .ico { width:24px; height:12px; }
  .heat { min-width:520px; display:grid; grid-template-columns:36px repeat(24, 1fr); gap:2px; font-size:11px; }
  .hl, .hh { color:var(--secondary-text-color); } .hh { text-align:left; }
  .hc { aspect-ratio:2; border-radius:3px; background:var(--divider-color); position:relative; }
  .hc::after { content:""; position:absolute; inset:0; border-radius:3px; background:var(--primary-color); opacity:var(--a); }
  .chart { width:100%; height:auto; display:block; overflow:visible; }
  .chart.wide { min-width:560px; }
  table.runs td { white-space:nowrap; }
  table.flows { min-width:640px; }
  .chart-label { font-size:12px; color:var(--secondary-text-color); margin:12px 0 4px; }
  .grid { stroke:var(--divider-color); stroke-width:1; }
  .ax { fill:var(--secondary-text-color); font-size:10px; }
  .line { fill:none; stroke:var(--primary-color); stroke-width:2; stroke-linejoin:round; }
  .area { fill:var(--primary-color); opacity:.12; }
  .ref { fill:none; stroke:var(--secondary-text-color); stroke-width:2; stroke-dasharray:5 4; }
  .rate { fill:none; stroke:var(--primary-text-color); stroke-width:2; }
  .cross { stroke:var(--secondary-text-color); stroke-width:1; }
  .hit { fill:transparent; }
  .slot { fill:var(--primary-color); opacity:.75; } .slot.pred { opacity:.3; } .slot.past { opacity:.25; }
  .band { stroke:var(--secondary-text-color); stroke-width:1; opacity:.6; }
  .win { fill:var(--primary-color); opacity:.08; } .win.best { opacity:.16; }
  .reach { fill:var(--success-color,#4caf50); opacity:.07; }
  .blackout { fill:var(--warning-color,#ff9800); opacity:.2; }
  .sched { fill:var(--warning-color,#ff9800); opacity:.22; }
  .win, .windows li { cursor:pointer; } .win.picked { opacity:.32; stroke:var(--primary-color); stroke-width:2; }
  .windows li.picked { border-color:var(--primary-color); box-shadow:0 0 0 1px var(--primary-color) inset; }
  .reach-edge { stroke:var(--success-color,#4caf50); stroke-width:1; stroke-dasharray:4 3; }
  .offer-win { fill:none; stroke:var(--warning-color,#ff9800); stroke-width:2; }
  .wl { fill:var(--primary-text-color); font-size:11px; font-weight:500; }
  .now { stroke:var(--primary-text-color); stroke-width:1; stroke-dasharray:2 3; }
  .zero { stroke:var(--secondary-text-color); stroke-width:1; }
  .legend { display:flex; gap:16px; flex-wrap:wrap; font-size:12px; color:var(--secondary-text-color); margin-top:6px; }
  .legend i { display:inline-block; width:14px; height:8px; border-radius:2px; margin-right:6px; vertical-align:middle; }
  .lg-slot { background:var(--primary-color); opacity:.75; } .lg-pred { background:var(--primary-color); opacity:.3; }
  .lg-win { background:var(--primary-color); opacity:.16; outline:1px solid var(--primary-color); }
  .lg-blackout { background:var(--warning-color,#ff9800); opacity:.4; }
  .lg-reach { background:var(--success-color,#4caf50); opacity:.25; border-right:1px dashed var(--success-color,#4caf50); }
  .lg-offer { background:none; outline:2px solid var(--warning-color,#ff9800); }
  .lg-sched { background:var(--warning-color,#ff9800); opacity:.45; }
  .controls { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-top:12px; padding-top:12px; border-top:1px solid var(--divider-color); }
  .ctl { display:flex; gap:6px; align-items:center; font-size:13px; color:var(--secondary-text-color); }
  .pick { margin-top:10px; padding:10px 12px; border-radius:10px; border:1px solid var(--primary-color); }
  .lg-line { height:2px !important; background:var(--primary-color); }
  .lg-ref { height:0 !important; border-top:2px dashed var(--secondary-text-color); border-radius:0 !important; }
  .empty { padding:24px; text-align:center; color:var(--secondary-text-color); } .empty.small { padding:8px 0; text-align:left; }
  button.link { background:none; border:none; color:var(--primary-color); cursor:pointer; font:inherit; }
  button.more { margin-top:12px; width:100%; padding:8px; border-radius:8px; border:1px solid var(--divider-color);
    background:none; color:var(--primary-color); font:inherit; cursor:pointer; }
  .scrim { position:fixed; inset:0; background:rgba(0,0,0,.45); display:flex; align-items:center; justify-content:center; z-index:10; padding:16px; }
  .dialog { background:var(--card-background-color); border-radius:16px; padding:20px; width:min(760px,100%); max-height:90vh; overflow:auto; }
  .x { background:none; border:none; color:var(--secondary-text-color); font-size:18px; cursor:pointer; }
  .facts { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; margin-bottom:8px; }
  .facts div { font-size:15px; font-variant-numeric:tabular-nums; } .facts span { display:block; font-size:12px; color:var(--secondary-text-color); }
  .matches { margin-top:12px; font-size:13px; color:var(--secondary-text-color); display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  .cta { display:flex; gap:12px; align-items:center; margin-top:8px; flex-wrap:wrap; }
  .sleep { fill:var(--secondary-text-color); opacity:.1; }
  .step { stroke-width:1.5; }
  .st-done { fill:var(--secondary-text-color); opacity:.45; }
  .st-running, .st-fired { fill:var(--lc, var(--primary-color)); stroke:var(--lc, var(--primary-color)); }
  .st-planned, .st-pending { fill:var(--lc, var(--primary-color)); fill-opacity:.25; stroke:var(--lc, var(--primary-color)); }
  .away-sh { fill:var(--warning-color,#ff9800); opacity:.1; }
  .lg-away { background:var(--warning-color,#ff9800); opacity:.25; }
  .loads { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
  .load-chip { display:inline-flex; align-items:center; gap:4px; border:1px solid var(--lc, var(--divider-color)); border-radius:18px; padding:2px 4px 2px 10px; }
  .load-chip i { width:8px; height:8px; border-radius:50%; background:var(--lc); }
  .load-chip select { border:none; background:none; padding:4px 2px; }
  .load-chip.add { border-style:dashed; padding-right:10px; }
  .order { font-size:13px; margin-top:8px; }
  .warn-block { border-color:var(--error-color,#f44336); background:color-mix(in srgb, var(--error-color,#f44336) 8%, transparent); }
  .ctl input { font:inherit; padding:5px 8px; border-radius:6px; border:1px solid var(--divider-color);
    background:var(--card-background-color); color:var(--primary-text-color); }
  ul.away { list-style:none; padding:0; margin:0; display:grid; gap:4px; }
  ul.away li { display:flex; align-items:center; gap:8px; }
  .tagbox input { font:inherit; padding:5px 8px; border-radius:6px; border:1px solid var(--divider-color);
    background:var(--card-background-color); color:var(--primary-text-color); }
  .co { fill:var(--card-background-color); stroke:var(--warning-color,#ff9800); stroke-width:2; }
  .sl { fill:var(--primary-text-color); font-size:9px; font-weight:500; dominant-baseline:central; pointer-events:none; }
  .lg-done { background:var(--secondary-text-color); opacity:.45; } .lg-run { background:var(--primary-color); }
  .lg-plan { background:var(--primary-color); opacity:.3; outline:1px solid var(--primary-color); }
  .lg-co { width:8px !important; border-radius:50% !important; border:2px solid var(--warning-color,#ff9800); }
  .lg-sleep { background:var(--secondary-text-color); opacity:.15; }
  .tagbox { display:flex; gap:12px; align-items:end; flex-wrap:wrap; margin-top:16px; padding-top:12px; border-top:1px solid var(--divider-color); }
  .tagbox label { display:grid; gap:4px; font-size:12px; color:var(--secondary-text-color); }
  .tagbox .chk { display:flex; align-items:center; gap:6px; font-size:13px; color:var(--primary-text-color); }
  .primary, .danger { font:inherit; border-radius:8px; padding:7px 16px; cursor:pointer; border:1px solid transparent; }
  .primary { background:var(--primary-color); color:var(--text-primary-color,#fff); }
  .danger { background:none; border-color:var(--error-color,#f44336); color:var(--error-color,#f44336); margin-left:auto; }
  .tip { position:fixed; z-index:20; pointer-events:none; background:var(--card-background-color); color:var(--primary-text-color);
    border:1px solid var(--divider-color); border-radius:6px; padding:4px 8px; font-size:12px; box-shadow:0 2px 8px rgba(0,0,0,.2); }
`;

// A newer build loaded over an open tab must not throw on re-define.
if (!customElements.get("smartstart-panel")) customElements.define("smartstart-panel", SmartStartPanel);
