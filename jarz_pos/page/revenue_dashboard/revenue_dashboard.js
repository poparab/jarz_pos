/* jshint esversion: 9 */

// Desk twin of the mobile Revenue Dashboard. Every figure comes from
// jarz_pos.api.revenue_dashboard.get_revenue_dashboard, whose definitions match
// the Profit & Loss report; this page only lays them out.

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

const RD_COLORS = { b2c: "#2980b9", b2b: "#e67e22", other: "#95a5a6" };

const RD_SOURCE_LABELS = {
	pos: "POS",
	woo: "Website",
	talabat: "Talabat",
	partner: "Delivery Partner",
	b2b: "B2B",
	staff: "Staff",
	samples: "Samples",
};

// ─────────────────────────────────────────────────────────────────────────────

class RevenueDashboard {
	constructor(page) {
		this.page = page;
		this.from_date = frappe.datetime.get_today();
		this.to_date = frappe.datetime.get_today();
		this.branch = null;
		this._seq = 0;
		this._setup_filters();
		this._inject_html();
		this.refresh();
	}

	// ── Filters ──────────────────────────────────────────────────────────────

	_setup_filters() {
		let me = this;

		this.page.add_field({
			fieldtype: "Date",
			fieldname: "from_date",
			label: __("From"),
			default: this.from_date,
			change() {
				let v = me.page.fields_dict.from_date.get_value();
				if (v && v !== me.from_date) {
					me.from_date = v;
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

		let today = () => frappe.datetime.get_today();
		let presets = [
			[__("Today"), () => [today(), today()]],
			[__("Yesterday"), () => [frappe.datetime.add_days(today(), -1), frappe.datetime.add_days(today(), -1)]],
			[__("Last 7 Days"), () => [frappe.datetime.add_days(today(), -6), today()]],
			[__("This Month"), () => [frappe.datetime.month_start(), today()]],
			[
				__("Last Month"),
				() => {
					let start = frappe.datetime.add_months(frappe.datetime.month_start(), -1);
					return [start, frappe.datetime.add_days(frappe.datetime.month_start(), -1)];
				},
			],
		];
		presets.forEach(([label, range]) => {
			this.page.add_inner_button(label, () => this._set_range(...range()));
		});
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
  .rd { padding: 18px 20px 40px; background: var(--bg-color); }
  .rd-section { margin-bottom: 30px; }
  .rd-title { font-size: 13px; font-weight: 700; color: var(--text-muted);
              text-transform: uppercase; letter-spacing: .6px;
              margin-bottom: 14px; padding-bottom: 8px;
              border-bottom: 1px solid var(--border-color); }
  .rd-note { font-size: 11px; color: var(--text-muted); font-weight: 500;
             text-transform: none; letter-spacing: 0; margin-left: 8px; }

  .rd-cards { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
  .rd-card { background: var(--card-bg); border: 1px solid var(--border-color);
             border-radius: 8px; padding: 14px 16px; }
  .rd-card .h { font-size: 11px; color: var(--text-muted); text-transform: uppercase;
                letter-spacing: .4px; font-weight: 700; }
  .rd-card .v { font-size: 24px; font-weight: 700; color: var(--text-color); margin: 4px 0 2px; }
  .rd-card .s { font-size: 12px; color: var(--text-muted); }
  .rd-card .c { font-size: 12px; margin-top: 6px; }
  .rd-split { display: flex; height: 6px; border-radius: 3px; overflow: hidden;
              margin: 8px 0 4px; background: var(--subtle-fg); }
  .rd-split span { display: block; height: 100%; }

  .rd-kpis { display: flex; flex-wrap: wrap; gap: 12px; }
  .rd-kpi  { flex: 1; min-width: 150px; background: var(--card-bg);
             border: 1px solid var(--border-color); border-radius: 8px; padding: 14px 16px; }
  .rd-kpi .v { font-size: 20px; font-weight: 700; color: var(--text-color); }
  .rd-kpi .l { font-size: 11px; color: var(--text-muted); margin-top: 3px;
               text-transform: uppercase; letter-spacing: .4px; }
  .rd-kpi .x { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

  .rd-grid { display: grid; gap: 18px; grid-template-columns: 1fr 1fr; }
  .rd-grid.one { grid-template-columns: 1fr; }
  .rd-box  { background: var(--card-bg); border: 1px solid var(--border-color);
             border-radius: 8px; padding: 18px; overflow: hidden; }
  .rd-box h6 { font-size: 11px; font-weight: 700; color: var(--text-muted);
               text-transform: uppercase; letter-spacing: .5px; margin-bottom: 14px; }
  @media (max-width: 900px) { .rd-grid { grid-template-columns: 1fr; } }

  .rd-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .rd-table th { text-align: left; padding: 7px 10px; font-size: 11px; font-weight: 700;
                 text-transform: uppercase; letter-spacing: .4px; color: var(--text-muted);
                 background: var(--subtle-fg); border-bottom: 1px solid var(--border-color); }
  .rd-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-color); }
  .rd-table tr:last-child td { border-bottom: none; }
  .rd-table td.n, .rd-table th.n { text-align: right; white-space: nowrap; }
  .rd-up   { color: #27ae60; font-weight: 600; }
  .rd-down { color: #e74c3c; font-weight: 600; }
  .rd-flat { color: var(--text-muted); }
  .rd-muted { color: var(--text-muted); }
  .rd-loading { text-align: center; color: var(--text-muted); padding: 28px; font-size: 13px; }
  .rd-legend { display: flex; gap: 14px; font-size: 12px; color: var(--text-muted); margin-top: 6px; }
  .rd-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; }
</style>

<div class="rd">
  <div class="rd-section">
    <div class="rd-title">${__("At a Glance")}<span class="rd-note">${__(
			"Always today-relative; follows the branch filter, not the dates"
		)}</span></div>
    <div class="rd-cards" id="rd-snapshot"><div class="rd-loading">${__("Loading…")}</div></div>
  </div>

  <div class="rd-section">
    <div class="rd-title"><span id="rd-period-title">${__("Selected Period")}</span><span class="rd-note" id="rd-period-note"></span></div>
    <div class="rd-kpis" id="rd-kpis"><div class="rd-loading">${__("Loading…")}</div></div>
  </div>

  <div class="rd-section">
    <div class="rd-grid">
      <div class="rd-box" style="grid-column: 1 / -1">
        <h6 id="rd-trend-title">${__("B2C vs B2B Trend")}</h6>
        <div id="rd-chart-trend"></div>
      </div>
      <div class="rd-box">
        <h6>${__("B2C / B2B / Other Split")}</h6>
        <div id="rd-chart-split"></div>
      </div>
      <div class="rd-box">
        <h6>${__("By Source")}</h6>
        <div id="rd-table-source"></div>
      </div>
      <div class="rd-box">
        <h6>${__("By Payment Method")}</h6>
        <div id="rd-table-payment"></div>
      </div>
      <div class="rd-box">
        <h6>${__("By Branch")}</h6>
        <div id="rd-table-branch"></div>
      </div>
      <div class="rd-box" style="grid-column: 1 / -1">
        <h6>${__("Orders by Hour")}<span class="rd-note" id="rd-fulfilment"></span></h6>
        <div id="rd-chart-hour"></div>
      </div>
      <div class="rd-box">
        <h6>${__("Top Products")}</h6>
        <div id="rd-table-products"></div>
      </div>
      <div class="rd-box">
        <h6>${__("Top B2B Customers")}</h6>
        <div id="rd-table-b2b"></div>
      </div>
    </div>
  </div>
  <div class="rd-muted" style="font-size:11px" id="rd-updated"></div>
</div>`);
	}

	// ── Data loading ─────────────────────────────────────────────────────────

	refresh() {
		// A slow response for an old filter must not overwrite a newer one.
		let seq = ++this._seq;
		$("#rd-kpis").html(`<div class="rd-loading">${__("Loading…")}</div>`);

		frappe
			.call({
				method: "jarz_pos.api.revenue_dashboard.get_revenue_dashboard",
				args: { date_from: this.from_date, date_to: this.to_date, branch: this.branch },
			})
			.then((r) => {
				if (seq !== this._seq || !r.message) return;
				let d = r.message;
				this._render_snapshot(d.snapshot || {});
				this._render_summary(d.summary || {}, d.filters || {});
				this._render_trend(d.trend || [], d.granularity);
				this._render_split(d.summary || {});
				this._render_source(d.by_source || []);
				this._render_payment(d.by_payment || []);
				this._render_branch(d.by_branch || []);
				this._render_hour(d.by_hour || [], d.fulfilment || {});
				this._render_products(d.top_products || []);
				this._render_b2b(d.top_b2b_customers || []);
				$("#rd-updated").text(__("Updated {0}", [d.generated_at || ""]));
			});
	}

	// ── Renderers ────────────────────────────────────────────────────────────

	_render_snapshot(snap) {
		let cards = [
			["today", __("Today"), __("vs same day last week")],
			["yesterday", __("Yesterday"), __("vs same day last week")],
			["last_7_days", __("Last 7 Days"), __("vs previous 7 days")],
			["month_to_date", __("Month to Date"), __("vs same days last month")],
		];
		$("#rd-snapshot").html(
			cards
				.map(([key, label, cmp]) => {
					let w = snap[key] || {};
					return `<div class="rd-card">
					<div class="h">${label}</div>
					<div class="v">${_egp(w.net_sales)}</div>
					${_split_bar(w.b2c, w.b2b, w.other)}
					<div class="s">${__("B2C")} ${_egp(w.b2c)} · ${__("B2B")} ${_egp(w.b2b)}</div>
					<div class="s">${_int(w.orders)} ${__("orders")} · ${__("AOV")} ${_egp_or_dash(w.aov)}</div>
					<div class="c">${_change(w.change_pct)} <span class="rd-muted">${cmp} (${_egp(
						w.compare_net_sales
					)})</span></div>
				</div>`;
				})
				.join("")
		);
	}

	_render_summary(s, f) {
		let p = s.previous || {};
		let range = f.date_from === f.date_to ? _date(f.date_from) : `${_date(f.date_from)} → ${_date(f.date_to)}`;
		$("#rd-period-title").text(__("Selected Period") + ": " + range);
		$("#rd-period-note").text(
			p.date_from ? __("compared with {0} → {1}", [_date(p.date_from), _date(p.date_to)]) : ""
		);
		let best = s.best_day ? _egp(s.best_day.net_sales) : "—";
		let best_on = s.best_day ? _date(s.best_day.date) : "";
		let worst = s.worst_day ? `${_egp(s.worst_day.net_sales)} (${_date(s.worst_day.date)})` : "—";

		$("#rd-kpis").html(`
			${_kpi(__("Net Sales"), _egp(s.net_sales), `${_change(p.change_pct)} <span class="rd-muted">${__("prev")} ${_egp(p.net_sales)}</span>`)}
			${_kpi(__("B2C"), _egp(s.b2c), `${_change(p.b2c_change_pct)} · ${_int(s.b2c_orders)} ${__("orders")}`)}
			${_kpi(__("B2B"), _egp(s.b2b), `${_change(p.b2b_change_pct)} · ${_int(s.b2b_orders)} ${__("orders")} · ${_pct(s.b2b_share_pct)} ${__("share")}`)}
			${_kpi(__("Staff + Samples"), _egp((s.staff || 0) + (s.samples || 0)), `${_int(s.other_orders)} ${__("orders")}`)}
			${_kpi(__("Delivery Fees"), _egp(s.shipping_income), `${__("Total incl. fees")} ${_egp(s.total_revenue)}`)}
			${_kpi(__("Orders"), _int(s.orders), `${_change(p.orders_change_pct)} <span class="rd-muted">${__("prev")} ${_int(p.orders)}</span>`)}
			${_kpi(__("Avg Order Value"), _egp_or_dash(s.aov), `${__("B2C")} ${_egp_or_dash(s.b2c_aov)} · ${__("B2B")} ${_egp_or_dash(s.b2b_aov)}`)}
			${_kpi(__("Daily Average"), _egp(s.daily_average), `${_int(s.days)} ${__("days")}`)}
			${_kpi(__("Best Day"), best, `${best_on} · ${__("Worst")}: ${worst}`)}
			${_kpi(__("Customers"), _int(s.customers), `${_int(s.new_customers)} ${__("new")}`)}
			${_kpi(__("Returns"), _egp(s.returns_value), `${_int(s.returns)} ${__("credit notes")}`)}
			${_kpi(__("Cancelled"), _egp(s.cancelled_value), `${_int(s.cancelled)} ${__("orders")}`)}
			${_kpi(__("Discounts"), _egp(s.discount_total), __("promos + bundle pricing"))}
		`);
	}

	_render_trend(rows, granularity) {
		let per = { day: __("per day"), week: __("per week"), month: __("per month") }[granularity] || "";
		$("#rd-trend-title").text(__("B2C vs B2B Trend") + (per ? ` (${per})` : ""));
		if (!rows.length) {
			$("#rd-chart-trend").html(`<div class="rd-loading">${__("No data in range.")}</div>`);
			return;
		}
		_chart("rd-chart-trend", {
			type: "bar",
			height: 280,
			data: {
				labels: rows.map((r) => r.date),
				datasets: [
					{ name: __("B2C"), values: rows.map((r) => r.b2c || 0) },
					{ name: __("B2B"), values: rows.map((r) => r.b2b || 0) },
					{ name: __("Other"), values: rows.map((r) => r.other || 0) },
				],
			},
			barOptions: { stacked: 1, spaceRatio: 0.4 },
			colors: [RD_COLORS.b2c, RD_COLORS.b2b, RD_COLORS.other],
			tooltipOptions: { formatTooltipY: (v) => _egp(v) },
		});
	}

	_render_split(s) {
		let other = (s.staff || 0) + (s.samples || 0);
		let values = [s.b2c || 0, s.b2b || 0, other];
		if (!values.some((v) => v > 0)) {
			$("#rd-chart-split").html(`<div class="rd-loading">${__("No sales in range.")}</div>`);
			return;
		}
		_chart("rd-chart-split", {
			type: "donut",
			height: 240,
			data: { labels: [__("B2C"), __("B2B"), __("Staff + Samples")], datasets: [{ values }] },
			colors: [RD_COLORS.b2c, RD_COLORS.b2b, RD_COLORS.other],
			tooltipOptions: { formatTooltipY: (v) => _egp(v) },
		});
	}

	_render_source(rows) {
		_table(
			"rd-table-source",
			[__("Source"), __("Net Sales"), __("Orders"), __("Share")],
			rows.map((r) => [_esc(RD_SOURCE_LABELS[r.source] || r.source), _egp(r.net_sales), _int(r.orders), _pct(r.share_pct)])
		);
	}

	_render_payment(rows) {
		_table(
			"rd-table-payment",
			[__("Method"), __("Net Sales"), __("Orders"), __("Share")],
			rows.map((r) => [_esc(r.method === "Unspecified" ? __("Not specified") : r.method), _egp(r.net_sales), _int(r.orders), _pct(r.share_pct)])
		);
	}

	_render_branch(rows) {
		_table(
			"rd-table-branch",
			[__("Branch"), __("Net Sales"), __("B2C"), __("B2B"), __("Orders"), __("Share")],
			rows.map((r) => [
				_esc(r.branch),
				_egp(r.net_sales),
				_egp(r.b2c),
				_egp(r.b2b),
				_int(r.orders),
				_pct(r.share_pct),
			])
		);
	}

	_render_hour(rows, ful) {
		let d = ful.delivery_orders || 0;
		let p = ful.pickup_orders || 0;
		$("#rd-fulfilment").text(
			d + p ? __("Delivery {0} · Pickup {1}", [_int(d), _int(p)]) : ""
		);
		if (!rows.some((r) => r.orders)) {
			$("#rd-chart-hour").html(`<div class="rd-loading">${__("No orders in range.")}</div>`);
			return;
		}
		_chart("rd-chart-hour", {
			type: "bar",
			height: 220,
			data: {
				labels: rows.map((r) => String(r.hour).padStart(2, "0")),
				datasets: [{ name: __("Orders"), values: rows.map((r) => r.orders || 0) }],
			},
			colors: [RD_COLORS.b2c],
		});
	}

	_render_products(rows) {
		_table(
			"rd-table-products",
			["#", __("Product"), __("Qty"), __("Net Sales")],
			rows.map((r, i) => [
				`<span class="rd-muted">${i + 1}</span>`,
				_esc(r.item_name || r.item_code),
				_num(r.qty),
				_egp(r.net_sales),
			])
		);
	}

	_render_b2b(rows) {
		_table(
			"rd-table-b2b",
			["#", __("Customer"), __("Orders"), __("Net Sales")],
			rows.map((r, i) => [
				`<span class="rd-muted">${i + 1}</span>`,
				`<a href="/app/customer/${encodeURIComponent(r.customer)}" target="_blank">${_esc(
					r.customer_name || r.customer
				)}</a>`,
				_int(r.orders),
				_egp(r.net_sales),
			])
		);
	}
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function _esc(v) {
	return frappe.utils.escape_html(v == null ? "" : String(v));
}

function _egp(v) {
	let n = parseFloat(v || 0);
	return "EGP " + n.toLocaleString("en", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

function _egp_or_dash(v) {
	return v == null ? "—" : _egp(v);
}

function _int(v) {
	return (parseInt(v || 0, 10) || 0).toLocaleString("en");
}

function _num(v) {
	return parseFloat(v || 0).toLocaleString("en", { maximumFractionDigits: 2 });
}

function _pct(v) {
	return v == null ? "—" : parseFloat(v).toFixed(1) + "%";
}

function _date(v) {
	return v ? frappe.datetime.str_to_user(v) : "—";
}

// null means "no comparable period" (previous was 0), never 0%.
function _change(v) {
	if (v == null) return '<span class="rd-flat">—</span>';
	let n = parseFloat(v);
	if (n > 0) return `<span class="rd-up">▲ ${n.toFixed(1)}%</span>`;
	if (n < 0) return `<span class="rd-down">▼ ${Math.abs(n).toFixed(1)}%</span>`;
	return '<span class="rd-flat">0.0%</span>';
}

function _split_bar(b2c, b2b, other) {
	let vals = [b2c || 0, b2b || 0, other || 0].map((v) => Math.max(v, 0));
	let total = vals.reduce((a, b) => a + b, 0);
	if (!total) return '<div class="rd-split"></div>';
	let colors = [RD_COLORS.b2c, RD_COLORS.b2b, RD_COLORS.other];
	return `<div class="rd-split">${vals
		.map((v, i) => (v ? `<span style="width:${(v / total) * 100}%;background:${colors[i]}"></span>` : ""))
		.join("")}</div>`;
}

function _kpi(label, value, extra) {
	return `<div class="rd-kpi">
		<div class="v">${value}</div>
		<div class="l">${label}</div>
		${extra ? `<div class="x">${extra}</div>` : ""}
	</div>`;
}

// Cells are pre-escaped HTML; every column after the first is numeric.
function _table(id, headers, rows) {
	if (!rows.length) {
		$("#" + id).html(`<div class="rd-loading">${__("No data in range.")}</div>`);
		return;
	}
	let num = (i) => (i > 0 && headers[0] !== "#") || (headers[0] === "#" && i > 1);
	$("#" + id).html(`
		<table class="rd-table">
			<thead><tr>${headers.map((h, i) => `<th class="${num(i) ? "n" : ""}">${h}</th>`).join("")}</tr></thead>
			<tbody>${rows
				.map((r) => `<tr>${r.map((c, i) => `<td class="${num(i) ? "n" : ""}">${c}</td>`).join("")}</tr>`)
				.join("")}</tbody>
		</table>`);
}

function _chart(id, opts) {
	let el = document.getElementById(id);
	if (!el) return;
	el.innerHTML = "";
	try {
		new frappe.Chart(el, opts);
	} catch (e) {
		el.innerHTML = `<div class="rd-loading" style="color:#e74c3c">Chart error: ${_esc(e.message)}</div>`;
	}
}
