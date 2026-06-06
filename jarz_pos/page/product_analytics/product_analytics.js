/* jshint esversion: 9 */
/* globals frappe */

frappe.pages['product-analytics'].on_page_load = function (wrapper) {
	let page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Product Analytics'),
		single_column: true,
	});
	let dash = new ProductAnalyticsDashboard(page);
	$(wrapper).data('pa_dash', dash);
};

frappe.pages['product-analytics'].on_page_show = function (wrapper) {
	let dash = $(wrapper).data('pa_dash');
	if (dash) dash.refresh();
};

// ─────────────────────────────────────────────────────────────────────────────

class ProductAnalyticsDashboard {
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
  .pa { padding: 18px 20px 40px; background: var(--bg-color); }
  .pa-section { margin-bottom: 32px; }
  .pa-title { font-size: 13px; font-weight: 700; color: var(--text-muted);
              text-transform: uppercase; letter-spacing: .6px;
              margin-bottom: 14px; padding-bottom: 8px;
              border-bottom: 1px solid var(--border-color); }

  /* KPI cards */
  .pa-kpis { display: flex; flex-wrap: wrap; gap: 12px; }
  .pa-kpi  { flex: 1; min-width: 140px; background: var(--card-bg);
             border: 1px solid var(--border-color); border-radius: 8px;
             padding: 16px 18px; }
  .pa-kpi .v { font-size: 24px; font-weight: 700; color: var(--text-color); }
  .pa-kpi .l { font-size: 11px; color: var(--text-muted); margin-top: 3px;
               text-transform: uppercase; letter-spacing: .4px; }
  .pa-kpi.pos  .v { color: #27ae60; }
  .pa-kpi.neg  .v { color: #e74c3c; }
  .pa-kpi.info .v { color: #2980b9; }
  .pa-kpi.warn .v { color: #e67e22; }

  /* Grid */
  .pa-grid { display: grid; gap: 18px; grid-template-columns: 1fr 1fr; }
  .pa-grid.one { grid-template-columns: 1fr; }
  .pa-box  { background: var(--card-bg); border: 1px solid var(--border-color);
             border-radius: 8px; padding: 18px; overflow: hidden; }
  .pa-box h6 { font-size: 11px; font-weight: 700; color: var(--text-muted);
               text-transform: uppercase; letter-spacing: .5px; margin-bottom: 14px; }
  .pa-box.span2 { grid-column: 1 / -1; }

  /* Tables */
  .pa-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .pa-table th { text-align: left; padding: 7px 10px; font-size: 11px; font-weight: 700;
                 text-transform: uppercase; letter-spacing: .4px;
                 color: var(--text-muted); background: var(--subtle-fg);
                 border-bottom: 1px solid var(--border-color); }
  .pa-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-color); }
  .pa-table tr:last-child td { border-bottom: none; }
  .pa-table tr:hover td { background: var(--subtle-fg); }
  .red   { color: #e74c3c; font-weight: 600; }
  .green { color: #27ae60; font-weight: 600; }
  .muted { color: var(--text-muted); }

  .badge { border-radius: 4px; padding: 2px 7px; font-size: 11px; font-weight: 600; }
  .badge-bundle { background: #ede9fe; color: #5b21b6; }
  .badge-medium { background: #dcfce7; color: #166534; }
  .badge-large  { background: #ffedd5; color: #9a3412; }

  .pa-loading { text-align: center; color: var(--text-muted); padding: 32px; font-size: 13px; }

  .margin-bar  { display: inline-block; background: #e0e0e0; border-radius: 3px;
                 height: 8px; width: 80px; vertical-align: middle; }
  .margin-fill { height: 8px; border-radius: 3px; display: block; }
  .margin-good { background: #27ae60; }
  .margin-ok   { background: #f39c12; }
  .margin-poor { background: #e74c3c; }
</style>

<div class="pa">

  <!-- ① KPIs -->
  <div class="pa-section">
    <div class="pa-title">Key Metrics</div>
    <div class="pa-kpis" id="pa-kpis"><div class="pa-loading">Loading…</div></div>
  </div>

  <!-- ② Product Type Breakdown -->
  <div class="pa-section">
    <div class="pa-title">Product Type Breakdown</div>
    <div class="pa-grid">
      <div class="pa-box">
        <h6>Revenue by Product Type</h6>
        <div id="pa-chart-type-rev"></div>
      </div>
      <div class="pa-box">
        <h6>Units Sold by Product Type</h6>
        <div id="pa-chart-type-units"></div>
      </div>
      <div class="pa-box span2">
        <h6>Gross Margin by Product Type</h6>
        <div id="pa-table-type"></div>
      </div>
    </div>
  </div>

  <!-- ③ Top Products -->
  <div class="pa-section">
    <div class="pa-title">Top Products (by Revenue)</div>
    <div class="pa-grid one">
      <div class="pa-box">
        <h6>Product Profitability</h6>
        <div id="pa-table-products"></div>
      </div>
    </div>
  </div>

  <!-- ④ Territory -->
  <div class="pa-section">
    <div class="pa-title">Sales by Territory</div>
    <div class="pa-grid">
      <div class="pa-box">
        <h6>Revenue by Territory</h6>
        <div id="pa-chart-territory-rev"></div>
      </div>
      <div class="pa-box">
        <h6>Gross Profit by Territory</h6>
        <div id="pa-chart-territory-profit"></div>
      </div>
      <div class="pa-box span2">
        <h6>Territory Summary Table</h6>
        <div id="pa-table-territory"></div>
      </div>
    </div>
  </div>

  <!-- ⑤ Sales Trend -->
  <div class="pa-section">
    <div class="pa-title">Sales Trend</div>
    <div class="pa-grid one">
      <div class="pa-box">
        <h6>Daily Revenue &amp; Orders</h6>
        <div id="pa-chart-trend"></div>
      </div>
    </div>
  </div>

  <!-- ⑥ Bundle Composition (hidden when no bundles) -->
  <div class="pa-section" id="pa-section-bundles" style="display:none">
    <div class="pa-title">Bundle Composition (Flavor Mix)</div>
    <div class="pa-grid">
      <div class="pa-box">
        <h6>Most-Chosen Bundle Components</h6>
        <div id="pa-chart-bundle"></div>
      </div>
      <div class="pa-box">
        <h6>Bundle Flavor Revenue Contribution</h6>
        <div id="pa-table-bundle"></div>
      </div>
    </div>
  </div>

</div>`);
	}

	// ── Data loading ─────────────────────────────────────────────────────────

	refresh() {
		$('#pa-kpis').html('<div class="pa-loading">Loading…</div>');

		frappe.call({
			method: 'jarz_pos.api.product_analytics.get_product_analytics',
			args: { date_from: this.from_date, date_to: this.to_date },
		}).then(r => {
			if (!r.message) return;
			let d = r.message;
			this._render_kpis(d.summary);
			this._render_type_breakdown(d.by_product_type || []);
			this._render_top_products(d.top_products || []);
			this._render_territory(d.by_territory || []);
			this._render_trend(d.trend || []);
			this._render_bundle_composition(d.bundle_composition || []);
		});
	}

	// ── Renderers ────────────────────────────────────────────────────────────

	_render_kpis(s) {
		let margin_pct = s.total_revenue > 0
			? ((s.total_gross_profit / s.total_revenue) * 100).toFixed(1)
			: 0;
		let margin_cls = margin_pct >= 40 ? 'pos' : margin_pct >= 20 ? 'info' : 'warn';
		let best = s.best_selling_product && s.best_selling_product.item_name
			? _short(s.best_selling_product.item_name, 20)
			: '—';
		let top_terr = s.top_territory && s.top_territory.territory
			? _short(s.top_territory.territory, 20)
			: '—';

		$('#pa-kpis').html(`
			${_kpi('Total Revenue',    _egp(s.total_revenue),        '')}
			${_kpi('Total Orders',     s.total_orders,               '')}
			${_kpi('Gross Profit',     _egp(s.total_gross_profit),   'pos')}
			${_kpi('Gross Margin',     margin_pct + '%',             margin_cls)}
			${_kpi('Avg Order Value',  _egp(s.avg_order_value),      'info')}
			${_kpi('Best Seller',      best,                         '')}
			${_kpi('Top Territory',    top_terr,                     '')}
		`);
	}

	_render_type_breakdown(rows) {
		if (!rows.length) return;

		_chart('pa-chart-type-rev', {
			type: 'donut',
			height: 260,
			data: {
				labels: rows.map(r => r.type),
				datasets: [{ values: rows.map(r => r.revenue) }],
			},
			colors: ['#7B61FF', '#22C55E', '#F97316'],
			tooltipOptions: { formatTooltipY: v => _egp(v) },
		});

		_chart('pa-chart-type-units', {
			type: 'bar',
			height: 260,
			data: {
				labels: rows.map(r => r.type),
				datasets: [{ name: 'Units Sold', values: rows.map(r => r.units) }],
			},
			colors: ['#7B61FF', '#22C55E', '#F97316'],
		});

		$('#pa-table-type').html(`
			<table class="pa-table">
				<thead><tr>
					<th>Type</th>
					<th>Units</th>
					<th>Revenue</th>
					<th>COGS (BOM)</th>
					<th>Gross Profit</th>
					<th>Margin %</th>
					<th></th>
				</tr></thead>
				<tbody>
					${rows.map(r => {
						let m = parseFloat(r.margin_pct || 0);
						let fill_cls = m >= 40 ? 'margin-good' : m >= 20 ? 'margin-ok' : 'margin-poor';
						let fill_w = Math.min(100, Math.max(0, m));
						return `<tr>
							<td><span class="badge ${_type_badge(r.type)}">${r.type}</span></td>
							<td>${r.units.toLocaleString()}</td>
							<td>${_egp(r.revenue)}</td>
							<td class="muted">${r.cost > 0 ? _egp(r.cost) : '—'}</td>
							<td class="green">${r.profit > 0 ? _egp(r.profit) : '—'}</td>
							<td>${m > 0 ? m.toFixed(1) + '%' : '—'}</td>
							<td>
								<span class="margin-bar">
									<span class="margin-fill ${fill_cls}" style="width:${fill_w}%"></span>
								</span>
							</td>
						</tr>`;
					}).join('')}
				</tbody>
			</table>`);
	}

	_render_top_products(rows) {
		if (!rows.length) {
			$('#pa-table-products').html('<div class="pa-loading">No product data in range.</div>');
			return;
		}

		$('#pa-table-products').html(`
			<table class="pa-table">
				<thead><tr>
					<th>#</th>
					<th>Product</th>
					<th>Type</th>
					<th>Units Sold</th>
					<th>Revenue</th>
					<th>BOM Cost / Unit</th>
					<th>Gross Profit</th>
					<th>Margin %</th>
				</tr></thead>
				<tbody>
					${rows.slice(0, 15).map((r, i) => {
						let m = parseFloat(r.margin_pct || 0);
						let m_cls = m >= 40 ? 'green' : m >= 20 ? '' : m > 0 ? 'red' : 'muted';
						return `<tr>
							<td class="muted">${i + 1}</td>
							<td>${r.item_name || r.item_code}</td>
							<td><span class="badge ${_type_badge(r.type)}">${r.type}</span></td>
							<td>${parseFloat(r.total_qty || 0).toLocaleString()}</td>
							<td>${_egp(r.total_revenue)}</td>
							<td class="muted">${r.bom_cost_per_unit > 0 ? _egp(r.bom_cost_per_unit) : '—'}</td>
							<td class="green">${r.gross_profit > 0 ? _egp(r.gross_profit) : '—'}</td>
							<td class="${m_cls}">${m > 0 ? m.toFixed(1) + '%' : '—'}</td>
						</tr>`;
					}).join('')}
				</tbody>
			</table>`);
	}

	_render_territory(rows) {
		if (!rows.length) return;

		let top7 = rows.slice(0, 7);

		_chart('pa-chart-territory-rev', {
			type: 'bar',
			height: 280,
			data: {
				labels: top7.map(r => _short(r.territory)),
				datasets: [{ name: 'Revenue', values: top7.map(r => r.revenue) }],
			},
			colors: ['#2980b9'],
			tooltipOptions: { formatTooltipY: v => _egp(v) },
		});

		_chart('pa-chart-territory-profit', {
			type: 'bar',
			height: 280,
			data: {
				labels: top7.map(r => _short(r.territory)),
				datasets: [{ name: 'Gross Profit', values: top7.map(r => r.profit) }],
			},
			colors: ['#27ae60'],
			tooltipOptions: { formatTooltipY: v => _egp(v) },
		});

		$('#pa-table-territory').html(`
			<table class="pa-table">
				<thead><tr>
					<th>Territory</th>
					<th>Orders</th>
					<th>Revenue</th>
					<th>Gross Profit</th>
					<th>Margin %</th>
				</tr></thead>
				<tbody>
					${rows.map(r => {
						let m = r.revenue > 0 ? ((r.profit / r.revenue) * 100).toFixed(1) : 0;
						return `<tr>
							<td>${r.territory}</td>
							<td>${r.orders}</td>
							<td>${_egp(r.revenue)}</td>
							<td class="green">${r.profit > 0 ? _egp(r.profit) : '—'}</td>
							<td>${m > 0 ? m + '%' : '—'}</td>
						</tr>`;
					}).join('')}
				</tbody>
			</table>`);
	}

	_render_trend(rows) {
		if (!rows.length) {
			$('#pa-chart-trend').html('<div class="pa-loading">No data in range.</div>');
			return;
		}

		_chart('pa-chart-trend', {
			type: 'axis-mixed',
			height: 300,
			data: {
				labels: rows.map(r => r.date),
				datasets: [
					{ name: 'Orders',       type: 'bar',  values: rows.map(r => r.orders) },
					{ name: 'Revenue (EGP)', type: 'line', values: rows.map(r => r.revenue) },
				],
			},
			colors: ['#95a5a6', '#7B61FF'],
		});
	}

	_render_bundle_composition(rows) {
		if (!rows.length) {
			$('#pa-section-bundles').hide();
			return;
		}
		$('#pa-section-bundles').show();

		let top10 = rows.slice(0, 10);

		_chart('pa-chart-bundle', {
			type: 'bar',
			height: 280,
			data: {
				labels: top10.map(r => _short(r.item_name || r.item_code)),
				datasets: [{ name: 'Times Picked', values: top10.map(r => r.times_in_bundle) }],
			},
			colors: ['#7B61FF'],
		});

		$('#pa-table-bundle').html(`
			<table class="pa-table">
				<thead><tr>
					<th>Component</th>
					<th>Group</th>
					<th>Times Picked</th>
					<th>Revenue</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `<tr>
						<td>${r.item_name || r.item_code}</td>
						<td class="muted">${r.item_group || '—'}</td>
						<td>${r.times_in_bundle.toLocaleString()}</td>
						<td>${_egp(r.revenue)}</td>
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

function _short(label, max = 18) {
	if (!label) return '—';
	return label.length > max ? label.slice(0, max - 1) + '…' : label;
}

function _kpi(label, value, cls) {
	return `<div class="pa-kpi ${cls}">
		<div class="v">${value}</div>
		<div class="l">${label}</div>
	</div>`;
}

function _type_badge(type) {
	if (type === 'Bundle') return 'badge-bundle';
	if (type === 'Medium') return 'badge-medium';
	return 'badge-large';
}

function _chart(id, opts) {
	let el = document.getElementById(id);
	if (!el) return;
	el.innerHTML = '';
	try {
		new frappe.Chart(el, opts);
	} catch (e) {
		el.innerHTML = `<div class="pa-loading" style="color:#e74c3c">Chart error: ${e.message}</div>`;
	}
}
