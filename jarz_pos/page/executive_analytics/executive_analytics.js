/* jshint esversion: 9 */
/* globals frappe */

frappe.pages['executive-analytics'].on_page_load = function (wrapper) {
	let page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Executive Overview'),
		single_column: true,
	});
	let dash = new ExecutiveAnalyticsDashboard(page);
	$(wrapper).data('ex_dash', dash);
};

frappe.pages['executive-analytics'].on_page_show = function (wrapper) {
	let dash = $(wrapper).data('ex_dash');
	if (dash) dash.refresh();
};

// ─────────────────────────────────────────────────────────────────────────────

class ExecutiveAnalyticsDashboard {
	constructor(page) {
		this.page = page;
		this.from_date = frappe.datetime.month_start();
		this.to_date = frappe.datetime.month_end();
		this._setup_filters();
		this._inject_html();
		this.refresh();
	}

	// ── Filters ──────────────────────────────────────────────────────────────

	_setup_filters() {
		let me = this;

		this.page.add_field({
			fieldtype: 'Date',
			fieldname: 'from_date',
			label: __('From'),
			change() {
				me.from_date = me.page.fields_dict.from_date.get_value() || me.from_date;
				me.refresh();
			},
		});
		this.page.fields_dict.from_date.set_value(this.from_date);

		this.page.add_field({
			fieldtype: 'Date',
			fieldname: 'to_date',
			label: __('To'),
			change() {
				me.to_date = me.page.fields_dict.to_date.get_value() || me.to_date;
				me.refresh();
			},
		});
		this.page.fields_dict.to_date.set_value(this.to_date);

		this.page.add_inner_button(__('This Month'), () => {
			this.from_date = frappe.datetime.month_start();
			this.to_date = frappe.datetime.month_end();
			this.page.fields_dict.from_date.set_value(this.from_date);
			this.page.fields_dict.to_date.set_value(this.to_date);
			this.refresh();
		});

		this.page.add_inner_button(__('Last 30 Days'), () => {
			this.to_date = frappe.datetime.nowdate();
			this.from_date = frappe.datetime.add_days(this.to_date, -30);
			this.page.fields_dict.from_date.set_value(this.from_date);
			this.page.fields_dict.to_date.set_value(this.to_date);
			this.refresh();
		});

		this.page.add_inner_button(__('Last 90 Days'), () => {
			this.to_date = frappe.datetime.nowdate();
			this.from_date = frappe.datetime.add_days(this.to_date, -90);
			this.page.fields_dict.from_date.set_value(this.from_date);
			this.page.fields_dict.to_date.set_value(this.to_date);
			this.refresh();
		});
	}

	// ── HTML skeleton ─────────────────────────────────────────────────────────

	_inject_html() {
		this.page.main.html(`
<style>
  .ex { padding: 18px 20px 40px; background: var(--bg-color); }
  .ex-section { margin-bottom: 32px; }
  .ex-title { font-size: 13px; font-weight: 700; color: var(--text-muted);
              text-transform: uppercase; letter-spacing: .6px;
              margin-bottom: 14px; padding-bottom: 8px;
              border-bottom: 1px solid var(--border-color); }

  /* KPI cards */
  .ex-kpis { display: flex; flex-wrap: wrap; gap: 12px; }
  .ex-kpi  { flex: 1; min-width: 150px; background: var(--card-bg);
             border: 1px solid var(--border-color); border-radius: 8px;
             padding: 16px 18px; }
  .ex-kpi .v { font-size: 24px; font-weight: 700; color: var(--text-color); }
  .ex-kpi .l { font-size: 11px; color: var(--text-muted); margin-top: 3px;
               text-transform: uppercase; letter-spacing: .4px; }
  .ex-kpi.pos  .v { color: #27ae60; }
  .ex-kpi.neg  .v { color: #e74c3c; }
  .ex-kpi.info .v { color: #2980b9; }
  .ex-kpi.warn .v { color: #e67e22; }

  /* Grid */
  .ex-grid { display: grid; gap: 18px; grid-template-columns: 1fr 1fr; }
  .ex-grid.one { grid-template-columns: 1fr; }
  .ex-box  { background: var(--card-bg); border: 1px solid var(--border-color);
             border-radius: 8px; padding: 18px; overflow: hidden; }
  .ex-box h6 { font-size: 11px; font-weight: 700; color: var(--text-muted);
               text-transform: uppercase; letter-spacing: .5px; margin-bottom: 14px; }
  .ex-box.span2 { grid-column: 1 / -1; }

  /* Tables */
  .ex-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .ex-table th { text-align: left; padding: 7px 10px; font-size: 11px; font-weight: 700;
                 text-transform: uppercase; letter-spacing: .4px;
                 color: var(--text-muted); background: var(--subtle-fg);
                 border-bottom: 1px solid var(--border-color); }
  .ex-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-color); }
  .ex-table tr:last-child td { border-bottom: none; }
  .ex-table tr:hover td { background: var(--subtle-fg); }
  .green { color: #27ae60; font-weight: 600; }
  .muted { color: var(--text-muted); }

  /* Alerts */
  .ex-alert { display:flex; gap:10px; align-items:flex-start; padding:10px 14px;
              border-radius:6px; margin-bottom:8px; font-size:13px; line-height:1.5; }
  .ex-alert.danger  { background:#fdf2f2; border-left:3px solid #e74c3c; color:#922b21; }
  .ex-alert.warning { background:#fef9ec; border-left:3px solid #f39c12; color:#935116; }
  .ex-alert.info    { background:#eaf4fb; border-left:3px solid #2e86c1; color:#1a5276; }
  .ex-alert .ico    { font-size:15px; flex-shrink:0; margin-top:1px; }
  .ex-no-alerts     { text-align:center; color:var(--text-muted); padding:24px; font-size:13px; }

  .ex-loading { text-align: center; color: var(--text-muted); padding: 32px; font-size: 13px; }

  /* Quick links to detail dashboards */
  .ex-links { display:flex; flex-wrap:wrap; gap:10px; margin-top:4px; }
  .ex-link  { font-size:12px; font-weight:600; padding:7px 12px; border-radius:6px;
              border:1px solid var(--border-color); background:var(--card-bg);
              color:var(--text-color); text-decoration:none; }
  .ex-link:hover { background:var(--subtle-fg); }
</style>

<div class="ex">

  <!-- Quick nav -->
  <div class="ex-section">
    <div class="ex-links">
      <a class="ex-link" href="/app/product-analytics">📦 Product Analytics</a>
      <a class="ex-link" href="/app/shipping-analytics">🚚 Shipping Analytics</a>
      <a class="ex-link" href="/app/customer-analytics">👥 Customer Analytics</a>
      <a class="ex-link" href="/app/inventory-analytics">📊 Inventory Intelligence</a>
    </div>
  </div>

  <!-- ① KPIs -->
  <div class="ex-section">
    <div class="ex-title">Business Snapshot</div>
    <div class="ex-kpis" id="ex-kpis"><div class="ex-loading">Loading…</div></div>
  </div>

  <!-- ② Alerts -->
  <div class="ex-section">
    <div class="ex-title">⚠ Priority Alerts</div>
    <div id="ex-alerts"><div class="ex-loading">Scanning…</div></div>
  </div>

  <!-- ③ Revenue trend -->
  <div class="ex-section">
    <div class="ex-title">Revenue Trend</div>
    <div class="ex-grid one">
      <div class="ex-box">
        <h6>Daily Revenue &amp; Orders</h6>
        <div id="ex-chart-trend"></div>
      </div>
    </div>
  </div>

  <!-- ④ Mix -->
  <div class="ex-section">
    <div class="ex-title">Sales &amp; Customer Mix</div>
    <div class="ex-grid">
      <div class="ex-box">
        <h6>Revenue by Product Type</h6>
        <div id="ex-chart-product"></div>
      </div>
      <div class="ex-box">
        <h6>Customers by Segment</h6>
        <div id="ex-chart-segment"></div>
      </div>
    </div>
  </div>

  <!-- ⑤ Territories -->
  <div class="ex-section">
    <div class="ex-title">Top Territories</div>
    <div class="ex-grid one">
      <div class="ex-box">
        <h6>Revenue &amp; Profit by Territory</h6>
        <div id="ex-table-terr"></div>
      </div>
    </div>
  </div>

</div>`);
	}

	// ── Data loading ─────────────────────────────────────────────────────────

	refresh() {
		$('#ex-kpis').html('<div class="ex-loading">Loading…</div>');

		frappe.call({
			method: 'jarz_pos.api.executive_analytics.get_executive_overview',
			args: { date_from: this.from_date, date_to: this.to_date },
		}).then(r => {
			if (!r.message) return;
			let d = r.message;
			this._render_kpis(d.kpis || {});
			this._render_alerts(d.alerts || []);
			this._render_trend(d.revenue_trend || []);
			this._render_product(d.product_mix || []);
			this._render_segment(d.segment_mix || []);
			this._render_territories(d.top_territories || []);
		});
	}

	// ── Renderers ────────────────────────────────────────────────────────────

	_render_kpis(k) {
		let pl_cls = k.net_shipping_pl >= 0 ? 'pos' : 'neg';
		let margin_cls = k.gross_margin >= 40 ? 'pos' : k.gross_margin >= 20 ? 'info' : 'warn';
		let crit_cls = k.critical_stock > 0 ? 'neg' : 'pos';

		$('#ex-kpis').html(`
			${_kpi('Revenue',          _egp(k.total_revenue), '')}
			${_kpi('Orders',           (k.total_orders || 0).toLocaleString(), '')}
			${_kpi('Gross Profit',     _egp(k.gross_profit), 'pos')}
			${_kpi('Gross Margin',     (k.gross_margin || 0) + '%', margin_cls)}
			${_kpi('Avg Order Value',  _egp(k.avg_order_value), 'info')}
			${_kpi('Net Shipping P&L', _egp(k.net_shipping_pl), pl_cls)}
			${_kpi('Customers',        (k.total_customers || 0).toLocaleString(), '')}
			${_kpi('Critical Stock',   (k.critical_stock || 0).toLocaleString(), crit_cls)}
		`);
	}

	_render_alerts(alerts) {
		let icons = { danger: '🔴', warning: '🟡', info: '🔵' };
		if (!alerts.length) {
			$('#ex-alerts').html('<div class="ex-no-alerts">✅ No priority alerts for the selected period.</div>');
			return;
		}
		$('#ex-alerts').html(alerts.map(a =>
			`<div class="ex-alert ${a.type}">
				<span class="ico">${icons[a.type] || 'ℹ'}</span>
				<span>${a.message}</span>
			</div>`
		).join(''));
	}

	_render_trend(rows) {
		if (!rows.length) {
			$('#ex-chart-trend').html('<div class="ex-loading">No data in range.</div>');
			return;
		}
		_chart('ex-chart-trend', {
			type: 'axis-mixed',
			height: 300,
			data: {
				labels: rows.map(r => r.date),
				datasets: [
					{ name: 'Orders', type: 'bar', values: rows.map(r => r.orders) },
					{ name: 'Revenue (EGP)', type: 'line', values: rows.map(r => r.revenue) },
				],
			},
			colors: ['#95a5a6', '#7B61FF'],
		});
	}

	_render_product(rows) {
		if (!rows.length) {
			$('#ex-chart-product').html('<div class="ex-loading">No product data.</div>');
			return;
		}
		_chart('ex-chart-product', {
			type: 'donut',
			height: 260,
			data: {
				labels: rows.map(r => r.type),
				datasets: [{ values: rows.map(r => r.revenue) }],
			},
			colors: ['#7B61FF', '#22C55E', '#F97316'],
			tooltipOptions: { formatTooltipY: v => _egp(v) },
		});
	}

	_render_segment(rows) {
		let real = rows.filter(r => r.segment && r.segment !== 'Unclassified');
		let use = real.length ? real : rows;
		if (!use.length) {
			$('#ex-chart-segment').html('<div class="ex-loading">No segment data.</div>');
			return;
		}
		_chart('ex-chart-segment', {
			type: 'donut',
			height: 260,
			data: {
				labels: use.map(r => r.segment),
				datasets: [{ values: use.map(r => r.count) }],
			},
			colors: use.map(r => _seg_color(r.segment)),
		});
	}

	_render_territories(rows) {
		if (!rows.length) {
			$('#ex-table-terr').html('<div class="ex-loading">No territory data in range.</div>');
			return;
		}
		$('#ex-table-terr').html(`
			<table class="ex-table">
				<thead><tr>
					<th>Territory</th><th>Orders</th><th>Revenue</th><th>Gross Profit</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `<tr>
						<td>${r.territory}</td>
						<td>${r.orders}</td>
						<td>${_egp(r.revenue)}</td>
						<td class="green">${r.profit > 0 ? _egp(r.profit) : '—'}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function _egp(v) {
	let n = parseFloat(v || 0);
	return 'EGP ' + n.toLocaleString('en', {
		minimumFractionDigits: 0,
		maximumFractionDigits: 0,
	});
}

function _kpi(label, value, cls) {
	return `<div class="ex-kpi ${cls}">
		<div class="v">${value}</div>
		<div class="l">${label}</div>
	</div>`;
}

const _SEG_COLORS = {
	'Champion':           '#16a34a',
	'Loyal':              '#2563eb',
	'Potential Loyalist': '#0891b2',
	'New Customer':       '#7c3aed',
	'At Risk':            '#ea580c',
	"Can't Lose Them":    '#dc2626',
	'Lost':               '#6b7280',
	'One-Time':           '#a16207',
	'Unclassified':       '#94a3b8',
};

function _seg_color(segment) {
	return _SEG_COLORS[segment] || '#7B61FF';
}

function _chart(id, opts) {
	let el = document.getElementById(id);
	if (!el) return;
	el.innerHTML = '';
	try {
		new frappe.Chart(el, opts);
	} catch (e) {
		el.innerHTML = `<div class="ex-loading" style="color:#e74c3c">Chart error: ${e.message}</div>`;
	}
}
