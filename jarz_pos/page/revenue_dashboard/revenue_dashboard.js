/* jshint esversion: 9 */

// Desk twin of the mobile Revenue Dashboard, "daily-first" layout (contract v2).
// Every figure comes from jarz_pos.api.revenue_dashboard.get_revenue_dashboard, whose
// definitions match the Profit & Loss report; this page only lays them out:
//   1. period buttons (Today / Yesterday / Last 7 days / Month to date / Custom) + branch
//   2. hero "at a glance" for the selected range
//   3. needs attention
//   4. where it came from (source / payment / branch)
//   5. by hour
//   6. footer links, "Updated HH:MM" and refresh

frappe.pages["revenue-dashboard"].on_page_load = function (wrapper) {
	let page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Revenue Dashboard"),
		single_column: true,
	});
	let dash = new RevenueDashboard(page);
	$(wrapper).data("rd_dash", dash);
};

frappe.pages["revenue-dashboard"].on_page_show = function (wrapper) {
	let dash = $(wrapper).data("rd_dash");
	if (dash) dash.refresh();
};

const RD_SOURCE_LABELS = {
	pos: "POS",
	woo: "Website",
	talabat: "Talabat",
	partner: "Delivery Partner",
	b2b: "B2B",
	staff: "Staff",
	samples: "Samples",
};

const RD_PERIODS = [
	["today", "Today"],
	["yesterday", "Yesterday"],
	["last_7_days", "Last 7 days"],
	["month_to_date", "Month to date"],
];

// ─────────────────────────────────────────────────────────────────────────────

class RevenueDashboard {
	constructor(page) {
		this.page = page;
		this.from_date = frappe.datetime.get_today();
		this.to_date = frappe.datetime.get_today();
		this.branch = null;
		this.snapshot = {};
		this._custom = false;
		this._seq = 0;
		this._setup_filters();
		this._inject_html();
		this._bind_events();
		this._render_periods();
		this.refresh();
	}

	// ── Filters ──────────────────────────────────────────────────────────────

	_setup_filters() {
		let me = this;

		// The date fields are the "Custom" period: hidden until Custom is picked.
		this.page.add_field({
			fieldtype: "Date",
			fieldname: "from_date",
			label: __("From"),
			default: this.from_date,
			change() {
				let v = me.page.fields_dict.from_date.get_value();
				if (v && v !== me.from_date) {
					me.from_date = v;
					me._custom = true;
					me.refresh();
				}
			},
		});

		this.page.add_field({
			fieldtype: "Date",
			fieldname: "to_date",
			label: __("To"),
			default: this.to_date,
			change() {
				let v = me.page.fields_dict.to_date.get_value();
				if (v && v !== me.to_date) {
					me.to_date = v;
					me._custom = true;
					me.refresh();
				}
			},
		});

		this.page.add_field({
			fieldtype: "Link",
			fieldname: "branch",
			label: __("Branch"),
			options: "POS Profile",
			change() {
				let v = me.page.fields_dict.branch.get_value() || null;
				if (v !== me.branch) {
					me.branch = v;
					me.refresh();
				}
			},
		});

		this._toggle_dates(false);
	}

	_toggle_dates(show) {
		["from_date", "to_date"].forEach((name) => {
			let f = this.page.fields_dict[name];
			if (f && f.$wrapper) f.$wrapper.toggle(!!show);
		});
	}

	// Where each preset points when the server has not answered yet.
	_fallback_range(key) {
		let today = frappe.datetime.get_today();
		let yday = frappe.datetime.add_days(today, -1);
		return {
			today: [today, today],
			yesterday: [yday, yday],
			last_7_days: [frappe.datetime.add_days(today, -6), today],
			month_to_date: [frappe.datetime.month_start(), today],
		}[key];
	}

	_period_range(key) {
		let w = this.snapshot[key];
		if (w && w.label_from && w.label_to) return [w.label_from, w.label_to];
		return this._fallback_range(key);
	}

	_active_period() {
		if (this._custom) return "custom";
		for (let [key] of RD_PERIODS) {
			let r = this._period_range(key);
			if (r && r[0] === this.from_date && r[1] === this.to_date) return key;
		}
		return "custom";
	}

	_set_range(from_date, to_date) {
		this.from_date = from_date;
		this.to_date = to_date;
		// set_value fires change(); the equality checks there stop a double load.
		this.page.fields_dict.from_date.set_value(from_date);
		this.page.fields_dict.to_date.set_value(to_date);
		this.refresh();
	}

	// ── HTML skeleton ─────────────────────────────────────────────────────────

	_inject_html() {
		this.page.main.html(`
<style>
  .rd { padding: 16px 20px 40px; background: var(--bg-color); color: var(--text-color); }
  .rd-section { margin-bottom: 22px; }
  .rd-title { font-size: 12px; font-weight: 700; color: var(--text-muted);
              text-transform: uppercase; letter-spacing: .6px; margin-bottom: 10px; }
  .rd-muted { color: var(--text-muted); }
  .rd-up   { color: var(--green-600, #27ae60); font-weight: 600; }
  .rd-down { color: var(--red-500, #e74c3c); font-weight: 600; }
  .rd-flat { color: var(--text-muted); }
  .rd-loading { text-align: center; color: var(--text-muted); padding: 24px; font-size: 13px; }

  .rd-periods { display: grid; gap: 10px; grid-template-columns: repeat(5, minmax(0, 1fr)); }
  .rd-period { text-align: left; background: var(--card-bg); color: var(--text-color);
               border: 1px solid var(--border-color); border-radius: 8px; padding: 10px 14px;
               cursor: pointer; line-height: 1.35; }
  .rd-period:hover { border-color: var(--text-muted); }
  .rd-period.rd-on { border-color: var(--primary, #2490ef); box-shadow: inset 0 0 0 1px var(--primary, #2490ef); }
  .rd-period .l { display: block; font-size: 11px; font-weight: 700; color: var(--text-muted);
                  text-transform: uppercase; letter-spacing: .4px; }
  .rd-period .v { display: block; font-size: 16px; font-weight: 700; }
  .rd-period .c { display: block; font-size: 12px; min-height: 16px; }
  @media (max-width: 900px) { .rd-periods { grid-template-columns: repeat(2, minmax(0, 1fr)); } }

  .rd-hero { background: var(--card-bg); border: 1px solid var(--border-color);
             border-radius: 10px; padding: 18px 22px; }
  .rd-hero-grid { display: grid; gap: 18px; grid-template-columns: 1.6fr 1fr 1fr; align-items: end; }
  .rd-stat .l { font-size: 11px; font-weight: 700; color: var(--text-muted);
                text-transform: uppercase; letter-spacing: .4px; }
  .rd-stat .v { font-size: 22px; font-weight: 700; line-height: 1.2; }
  .rd-stat.big .v { font-size: 36px; }
  .rd-stat .c { font-size: 13px; }
  .rd-stat.big .c { font-size: 15px; }
  .rd-cmp { margin-top: 10px; font-size: 12px; color: var(--text-muted); }
  .rd-cap { margin-top: 12px; font-size: 14px; }
  .rd-cap2 { margin-top: 4px; font-size: 12px; color: var(--text-muted); }
  @media (max-width: 700px) { .rd-hero-grid { grid-template-columns: 1fr 1fr; }
                              .rd-stat.big { grid-column: 1 / -1; } }

  .rd-att { background: var(--card-bg); border: 1px solid var(--border-color);
            border-radius: 8px; padding: 4px 14px; }
  .rd-att-row { display: flex; align-items: center; gap: 10px; padding: 7px 0; font-size: 13px;
                border-bottom: 1px solid var(--border-color); }
  .rd-att-row:last-child { border-bottom: none; }
  .rd-att-dot { flex: none; width: 9px; height: 9px; border-radius: 50%; }
  .rd-att-dot.high { background: var(--red-500, #e74c3c); }
  .rd-att-dot.medium { background: var(--orange-500, #f5a623); }
  .rd-none { font-size: 12px; color: var(--text-muted); }

  .rd-grid3 { display: grid; gap: 14px; grid-template-columns: repeat(3, minmax(0, 1fr)); }
  @media (max-width: 1100px) { .rd-grid3 { grid-template-columns: 1fr; } }
  .rd-box { background: var(--card-bg); border: 1px solid var(--border-color);
            border-radius: 8px; padding: 12px 14px; overflow: hidden; }
  .rd-box h6 { font-size: 11px; font-weight: 700; color: var(--text-muted);
               text-transform: uppercase; letter-spacing: .5px; margin: 0 0 8px; }
  .rd-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .rd-table td { padding: 6px 4px; border-bottom: 1px solid var(--border-color); vertical-align: top; }
  .rd-table tr:last-child td { border-bottom: none; }
  .rd-table td.n { text-align: right; white-space: nowrap; }
  .rd-table .sub { display: block; font-size: 11px; color: var(--text-muted); }

  .rd-foot { display: flex; flex-wrap: wrap; align-items: center; gap: 16px; font-size: 12px; }
  .rd-foot a { font-weight: 600; cursor: pointer; }
  .rd-foot .sp { flex: 1; }
  .rd-foot button { background: var(--card-bg); color: var(--text-color); cursor: pointer;
                    border: 1px solid var(--border-color); border-radius: 6px; padding: 3px 10px; }
</style>

<div class="rd">
  <div class="rd-section">
    <div class="rd-periods" id="rd-periods"></div>
  </div>

  <div class="rd-section">
    <div id="rd-hero"><div class="rd-loading">${__("Loading…")}</div></div>
  </div>

  <div class="rd-section">
    <div class="rd-title">${__("Needs attention")}</div>
    <div id="rd-attention"></div>
  </div>

  <div class="rd-section">
    <div class="rd-title">${__("Where it came from")}</div>
    <div class="rd-grid3">
      <div class="rd-box"><h6>${__("By source")}</h6><div id="rd-table-source"></div></div>
      <div class="rd-box"><h6>${__("By payment")}</h6><div id="rd-table-payment"></div></div>
      <div class="rd-box"><h6>${__("By branch")}</h6><div id="rd-table-branch"></div></div>
    </div>
  </div>

  <div class="rd-section">
    <div class="rd-box">
      <h6>${__("By hour")}</h6>
      <div id="rd-chart-hour"></div>
    </div>
  </div>

  <div class="rd-foot">
    <a href="/app/product-analytics" data-rd="route" data-page="product-analytics">${__("Products")} →</a>
    <a href="/app/b2b-analytics" data-rd="route" data-page="b2b-analytics">${__("B2B clients")} →</a>
    <span class="sp"></span>
    <span class="rd-muted" id="rd-updated"></span>
    <button type="button" data-rd="refresh">${__("Refresh")}</button>
  </div>
</div>`);
	}

	_bind_events() {
		let me = this;
		this.page.main.on("click", "[data-rd]", function (e) {
			let t = e.currentTarget;
			let kind = t.dataset.rd;
			if (kind === "period") {
				if (e.preventDefault) e.preventDefault();
				me._pick_period(t.dataset.key);
			} else if (kind === "route") {
				if (e.preventDefault) e.preventDefault();
				frappe.set_route(t.dataset.page);
			} else if (kind === "refresh") {
				me.refresh();
			}
		});
	}

	_pick_period(key) {
		if (key === "custom") {
			this._custom = true;
			this._render_periods();
			return;
		}
		let range = this._period_range(key);
		if (!range) return;
		this._custom = false;
		this._set_range(range[0], range[1]);
	}

	// ── Data loading ─────────────────────────────────────────────────────────

	refresh() {
		// A slow response for an old filter must not overwrite a newer one.
		let seq = ++this._seq;
		$("#rd-hero").html(`<div class="rd-loading">${__("Loading…")}</div>`);
		this._render_periods();

		frappe
			.call({
				method: "jarz_pos.api.revenue_dashboard.get_revenue_dashboard",
				args: { date_from: this.from_date, date_to: this.to_date, branch: this.branch },
				// Frappe already shows the server's message; this only clears "Loading…".
				error: () => {
					if (seq === this._seq) this._render_failed();
				},
			})
			.then((r) => {
				if (seq !== this._seq) return;
				if (!r || !r.message) return this._render_failed();
				let d = r.message;
				this.snapshot = d.snapshot || {};
				this._render_periods();
				this._render_hero(d.summary || {}, d.fulfilment || {});
				this._render_attention(d.attention || []);
				this._render_source(d.by_source || []);
				this._render_payment(d.by_payment || []);
				this._render_branch(d.by_branch || []);
				this._render_hour(d.by_hour || []);
				$("#rd-updated").text(__("Updated {0}", [_clock(d.generated_at)]));
			});
	}

	_render_failed() {
		$("#rd-hero").html(
			`<div class="rd-loading">${__("Could not load the dashboard. Check the filters and try again.")}</div>`
		);
		["#rd-attention", "#rd-table-source", "#rd-table-payment", "#rd-table-branch", "#rd-chart-hour"].forEach(
			(sel) => $(sel).html("")
		);
		$("#rd-updated").text("");
	}

	// ── Renderers ────────────────────────────────────────────────────────────

	_render_periods() {
		let active = this._active_period();
		let html = RD_PERIODS.map(([key, label]) => {
			let w = this.snapshot[key];
			return `<button type="button" class="rd-period${active === key ? " rd-on" : ""}" data-rd="period" data-key="${key}">
				<span class="l">${__(label)}</span>
				<span class="v">${w ? _egp(w.net_sales) : "&nbsp;"}</span>
				<span class="c">${w ? _change(w.change_pct) : ""}</span>
			</button>`;
		}).join("");
		let range =
			active === "custom"
				? this.from_date === this.to_date
					? _dstr(this.from_date)
					: _drange(this.from_date, this.to_date)
				: "";
		html += `<button type="button" class="rd-period${active === "custom" ? " rd-on" : ""}" data-rd="period" data-key="custom">
			<span class="l">${__("Custom")}</span>
			<span class="v">${range || "&nbsp;"}</span>
			<span class="c"></span>
		</button>`;
		$("#rd-periods").html(html);
		this._toggle_dates(active === "custom");
	}

	_render_hero(s, ful) {
		let p = s.previous || {};
		let stat = (cls, label, value, chg) =>
			`<div class="rd-stat ${cls}"><div class="l">${label}</div><div class="v">${value}</div><div class="c">${_change(
				chg
			)}</div></div>`;

		let cmp = "";
		if (p.date_from) {
			let span = p.date_from === p.date_to ? _dstr(p.date_from) : _drange(p.date_from, p.date_to);
			cmp = p.cut_at ? __("vs {0} up to {1}", [span, _esc(p.cut_at)]) : __("vs {0}", [span]);
		}

		let deliv = (ful && ful.delivery_orders) || 0;
		let pick = (ful && ful.pickup_orders) || 0;
		let caption = [
			__("B2C {0}", [_egp(s.b2c)]),
			__("B2B {0}", [_egp(s.b2b)]),
			__("{0} new customers", [_int(s.new_customers)]),
			deliv + pick ? __("{0} delivery / {1} pickup", [_int(deliv), _int(pick)]) : "",
		]
			.filter(Boolean)
			.join(" · ");

		let small = [
			__("Discounts {0}", [_egp(s.discount_total)]),
			__("Returns {0}", [_egp(s.returns_value)]),
			__("Cancelled {0}", [_int(s.cancelled)]),
			__("Total incl. shipping {0}", [_egp(s.total_revenue)]),
		].join(" · ");

		let multi = "";
		if ((s.days || 0) > 1) {
			multi = [
				__("Daily average {0}", [_egp(s.daily_average)]),
				s.best_day ? __("Best {0} {1}", [_dstr(s.best_day.date), _egp(s.best_day.net_sales)]) : "",
				s.worst_day ? __("Worst {0} {1}", [_dstr(s.worst_day.date), _egp(s.worst_day.net_sales)]) : "",
			]
				.filter(Boolean)
				.join(" · ");
		}

		$("#rd-hero").html(`<div class="rd-hero">
			<div class="rd-hero-grid">
				${stat("big", __("Net sales"), _egp(s.net_sales), p.change_pct)}
				${stat("", __("Orders"), _int(s.orders), p.orders_change_pct)}
				${stat("", __("Average order"), _egp_or_dash(s.aov), p.aov_change_pct)}
			</div>
			${cmp ? `<div class="rd-cmp">${cmp}</div>` : ""}
			<div class="rd-cap">${caption}</div>
			<div class="rd-cap2">${small}</div>
			${multi ? `<div class="rd-cap2">${multi}</div>` : ""}
		</div>`);
	}

	_render_attention(items) {
		let rows = [];
		(items || []).forEach((a) => {
			let text = _attention_text(a || {});
			if (!text) return; // unknown kind: ignored (forward compatible)
			let sev = a.severity === "high" ? "high" : "medium";
			rows.push(`<div class="rd-att-row"><span class="rd-att-dot ${sev}"></span><span>${text}</span></div>`);
		});
		$("#rd-attention").html(
			rows.length
				? `<div class="rd-att">${rows.join("")}</div>`
				: `<div class="rd-none">${__("Nothing needs attention")}</div>`
		);
	}

	_render_source(rows) {
		_table(
			"rd-table-source",
			rows.map((r) => [
				_esc(RD_SOURCE_LABELS[r.source] ? __(RD_SOURCE_LABELS[r.source]) : r.source),
				_egp(r.net_sales),
				_pct(r.share_pct),
				_change(r.change_pct),
			])
		);
	}

	_render_payment(rows) {
		_table(
			"rd-table-payment",
			rows.map((r) => [
				_esc(r.method === "Unspecified" ? __("Not specified") : r.method),
				_egp(r.net_sales),
				_pct(r.share_pct),
			])
		);
	}

	_render_branch(rows) {
		_table(
			"rd-table-branch",
			rows.map((r) => [
				_esc(r.branch),
				_egp(r.net_sales),
				_pct(r.share_pct),
				_change(r.change_pct) +
					(r.b2c_change_pct != null ? `<span class="sub">${__("B2C")} ${_change(r.b2c_change_pct)}</span>` : ""),
			])
		);
	}

	_render_hour(rows) {
		// Trim quiet hours at both edges; keep the quiet ones in between.
		let live = (r) => (r.net_sales || 0) > 0 || (r.orders || 0) > 0;
		let first = rows.findIndex(live);
		if (first < 0) {
			$("#rd-chart-hour").html(`<div class="rd-loading">${__("No orders in range.")}</div>`);
			return;
		}
		let last = rows.length - 1;
		while (last > first && !live(rows[last])) last--;
		let shown = rows.slice(first, last + 1);
		_chart("rd-chart-hour", {
			type: "bar",
			height: 180,
			data: {
				labels: shown.map((r) => String(r.hour).padStart(2, "0")),
				datasets: [{ name: __("Net sales"), values: shown.map((r) => r.net_sales || 0) }],
			},
			colors: ["#2980b9"],
			tooltipOptions: { formatTooltipY: (v) => _egp(v) },
		});
	}
}

// ─── Attention wording ──────────────────────────────────────────────────────

// Returns safe HTML, or "" for a kind this build does not know.
function _attention_text(a) {
	switch (a.kind) {
		case "cancelled":
			return (
				(a.count === 1
					? __("{0} cancelled order", [_int(a.count)])
					: __("{0} cancelled orders", [_int(a.count)])) +
				" · " +
				_egp(a.value)
			);
		case "returns":
			return (
				(a.count === 1 ? __("{0} return", [_int(a.count)]) : __("{0} returns", [_int(a.count)])) +
				" · " +
				_egp(a.value)
			);
		case "branch_drop":
			return __("{0} B2C down {1}% ({2} vs {3})", [
				_esc(a.name),
				_abs1(a.change_pct),
				_egp(a.net_sales),
				_n0(a.compare_net_sales),
			]);
		case "source_drop":
			return __("{0} down {1}% ({2} vs {3})", [
				_esc(RD_SOURCE_LABELS[a.name] ? __(RD_SOURCE_LABELS[a.name]) : a.name),
				_abs1(a.change_pct),
				_egp(a.net_sales),
				_n0(a.compare_net_sales),
			]);
		case "discount_high":
			return a.compare_discount_pct != null
				? __("Discounts {0} = {1}% of sales (vs {2}%)", [
						_egp(a.value),
						_abs1(a.discount_pct),
						_abs1(a.compare_discount_pct),
				  ])
				: __("Discounts {0} = {1}% of sales", [_egp(a.value), _abs1(a.discount_pct)]);
		default:
			return "";
	}
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function _esc(v) {
	return frappe.utils.escape_html(v == null ? "" : String(v));
}

function _n0(v) {
	let n = parseFloat(v || 0);
	return n.toLocaleString("en", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

function _egp(v) {
	return "EGP " + _n0(v);
}

function _egp_or_dash(v) {
	return v == null ? "—" : _egp(v);
}

function _int(v) {
	return (parseInt(v || 0, 10) || 0).toLocaleString("en");
}

function _pct(v) {
	return v == null ? "—" : parseFloat(v).toFixed(1) + "%";
}

function _abs1(v) {
	return Math.abs(parseFloat(v || 0)).toFixed(1);
}

// "2026-10-18 14:05:11" -> "14:05"
function _clock(v) {
	let m = /(\d{2}:\d{2})(:\d{2})?\s*$/.exec(v || "");
	return _esc(m ? m[1] : v || "");
}

// "2026-10-11" -> {d, m, w} (UTC arithmetic, no timezone drift) or null.
function _parts(iso) {
	let m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
	if (!m) return null;
	let t = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
	return { d: t.getUTCDate(), m: t.getUTCMonth(), w: t.getUTCDay() };
}

const RD_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const RD_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// "Sun 11 Oct"
function _dstr(iso) {
	let p = _parts(iso);
	return p ? `${__(RD_DAYS[p.w])} ${p.d} ${__(RD_MONTHS[p.m])}` : _esc(iso);
}

// "5–11 Oct" within a month, "28 Sep – 4 Oct" across months.
function _drange(a, b) {
	let pa = _parts(a);
	let pb = _parts(b);
	if (!pa || !pb) return `${_esc(a)} – ${_esc(b)}`;
	if (pa.m === pb.m) return `${pa.d}–${pb.d} ${__(RD_MONTHS[pb.m])}`;
	return `${pa.d} ${__(RD_MONTHS[pa.m])} – ${pb.d} ${__(RD_MONTHS[pb.m])}`;
}

// null means "no comparable period" (previous was 0), never 0%.
function _change(v) {
	if (v == null) return '<span class="rd-flat">—</span>';
	let n = parseFloat(v);
	if (n > 0) return `<span class="rd-up">▲ ${n.toFixed(1)}%</span>`;
	if (n < 0) return `<span class="rd-down">▼ ${Math.abs(n).toFixed(1)}%</span>`;
	return '<span class="rd-flat">0.0%</span>';
}

// Cells are pre-escaped HTML; every column after the first is right-aligned.
function _table(id, rows) {
	if (!rows.length) {
		$("#" + id).html(`<div class="rd-loading">${__("No data in range.")}</div>`);
		return;
	}
	$("#" + id).html(
		`<table class="rd-table"><tbody>${rows
			.map((r) => `<tr>${r.map((c, i) => `<td class="${i > 0 ? "n" : ""}">${c}</td>`).join("")}</tr>`)
			.join("")}</tbody></table>`
	);
}

function _chart(id, opts) {
	let el = document.getElementById(id);
	if (!el) return;
	el.innerHTML = "";
	try {
		new frappe.Chart(el, opts);
	} catch (e) {
		el.innerHTML = `<div class="rd-loading" style="color:var(--red-500, #e74c3c)">Chart error: ${_esc(e.message)}</div>`;
	}
}
