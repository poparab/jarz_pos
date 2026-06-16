/* jshint esversion: 9 */
/* globals frappe */

frappe.pages['inventory-analytics'].on_page_load = function (wrapper) {
	let page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Inventory Intelligence'),
		single_column: true,
	});
	let dash = new InventoryAnalyticsDashboard(page);
	$(wrapper).data('inv_dash', dash);
};

frappe.pages['inventory-analytics'].on_page_show = function (wrapper) {
	let dash = $(wrapper).data('inv_dash');
	if (dash) dash.refresh();
};

// ─────────────────────────────────────────────────────────────────────────────

class InventoryAnalyticsDashboard {
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
  .inv { padding: 18px 20px 40px; background: var(--bg-color); }
  .inv-section { margin-bottom: 32px; }
  .inv-title { font-size: 13px; font-weight: 700; color: var(--text-muted);
               text-transform: uppercase; letter-spacing: .6px;
               margin-bottom: 14px; padding-bottom: 8px;
               border-bottom: 1px solid var(--border-color); }
  .inv-note { font-size: 11px; color: var(--text-muted); font-weight: 500;
              text-transform: none; letter-spacing: 0; margin-left: 8px; }

  /* KPI cards */
  .inv-kpis { display: flex; flex-wrap: wrap; gap: 12px; }
  .inv-kpi  { flex: 1; min-width: 140px; background: var(--card-bg);
              border: 1px solid var(--border-color); border-radius: 8px;
              padding: 16px 18px; }
  .inv-kpi .v { font-size: 24px; font-weight: 700; color: var(--text-color); }
  .inv-kpi .l { font-size: 11px; color: var(--text-muted); margin-top: 3px;
                text-transform: uppercase; letter-spacing: .4px; }
  .inv-kpi.pos  .v { color: #27ae60; }
  .inv-kpi.neg  .v { color: #e74c3c; }
  .inv-kpi.info .v { color: #2980b9; }
  .inv-kpi.warn .v { color: #e67e22; }

  /* Grid */
  .inv-grid { display: grid; gap: 18px; grid-template-columns: 1fr 1fr; }
  .inv-grid.one { grid-template-columns: 1fr; }
  .inv-box  { background: var(--card-bg); border: 1px solid var(--border-color);
              border-radius: 8px; padding: 18px; overflow: hidden; }
  .inv-box h6 { font-size: 11px; font-weight: 700; color: var(--text-muted);
                text-transform: uppercase; letter-spacing: .5px; margin-bottom: 14px; }
  .inv-box.span2 { grid-column: 1 / -1; }

  /* Tables */
  .inv-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .inv-table th { text-align: left; padding: 7px 10px; font-size: 11px; font-weight: 700;
                  text-transform: uppercase; letter-spacing: .4px;
                  color: var(--text-muted); background: var(--subtle-fg);
                  border-bottom: 1px solid var(--border-color); }
  .inv-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-color); }
  .inv-table tr:last-child td { border-bottom: none; }
  .inv-table tr:hover td { background: var(--subtle-fg); }
  .red   { color: #e74c3c; font-weight: 600; }
  .green { color: #27ae60; font-weight: 600; }
  .amber { color: #e67e22; font-weight: 600; }
  .muted { color: var(--text-muted); }

  .inv-badge { border-radius: 4px; padding: 2px 7px; font-size: 11px; font-weight: 600;
               white-space: nowrap; }
  .badge-make { background: #ede9fe; color: #5b21b6; }
  .badge-buy  { background: #e0f2fe; color: #075985; }

  .trend-up   { color: #16a34a; font-weight: 600; }
  .trend-flat { color: #6b7280; font-weight: 600; }
  .trend-down { color: #dc2626; font-weight: 600; }

  .inv-loading { text-align: center; color: var(--text-muted); padding: 32px; font-size: 13px; }
</style>

<div class="inv">

  <!-- ① KPIs -->
  <div class="inv-section">
    <div class="inv-title">Stock Health
      <span class="inv-note">— velocity &amp; days-of-stock are a current snapshot (recomputed weekly)</span>
    </div>
    <div class="inv-kpis" id="inv-kpis"><div class="inv-loading">Loading…</div></div>
  </div>

  <!-- ② Velocity overview -->
  <div class="inv-section">
    <div class="inv-title">Velocity Overview</div>
    <div class="inv-grid">
      <div class="inv-box">
        <h6>Items by Velocity Trend</h6>
        <div id="inv-chart-trend"></div>
      </div>
      <div class="inv-box">
        <h6>Fastest Movers (units/day, 60d)</h6>
        <div id="inv-chart-movers"></div>
      </div>
      <div class="inv-box span2">
        <h6>Top Movers Detail</h6>
        <div id="inv-table-movers"></div>
      </div>
    </div>
  </div>

  <!-- ③ Restock alerts -->
  <div class="inv-section">
    <div class="inv-title">🔴 Restock Alerts</div>
    <div class="inv-grid">
      <div class="inv-box">
        <h6>Critical — Running Out Soon</h6>
        <div id="inv-table-critical"></div>
      </div>
      <div class="inv-box">
        <h6>Watch List — Low Stock</h6>
        <div id="inv-table-watch"></div>
      </div>
    </div>
  </div>

  <!-- ④ Dead stock -->
  <div class="inv-section">
    <div class="inv-title">Dead &amp; Excess Stock</div>
    <div class="inv-grid">
      <div class="inv-box">
        <h6>Slow Movers — No Recent Sales</h6>
        <div id="inv-table-slow"></div>
      </div>
      <div class="inv-box">
        <h6>Overstocked — Excess Inventory</h6>
        <div id="inv-table-over"></div>
      </div>
    </div>
  </div>

  <!-- ⑤ Top sellers in range -->
  <div class="inv-section">
    <div class="inv-title">Top Sellers (Selected Range)</div>
    <div class="inv-grid one">
      <div class="inv-box">
        <h6>Most Units Sold in Range</h6>
        <div id="inv-table-sold"></div>
      </div>
    </div>
  </div>

</div>`);
	}

	// ── Data loading ─────────────────────────────────────────────────────────

	refresh() {
		$('#inv-kpis').html('<div class="inv-loading">Loading…</div>');

		frappe.call({
			method: 'jarz_pos.api.inventory_analytics.get_inventory_analytics',
			args: { date_from: this.from_date, date_to: this.to_date },
		}).then(r => {
			if (!r.message) return;
			let d = r.message;
			let a = d.alerts || {};
			this._render_kpis(d.summary || {});
			this._render_trend(d.velocity_distribution || []);
			this._render_movers(d.top_movers || []);
			this._render_critical(a.critical || []);
			this._render_watch(a.watch_list || []);
			this._render_slow(a.slow_movers || []);
			this._render_over(a.overstocked || []);
			this._render_sold(d.top_sold_in_range || []);
		});
	}

	// ── Renderers ────────────────────────────────────────────────────────────

	_render_kpis(s) {
		$('#inv-kpis').html(`
			${_kpi('Stock Items',     (s.total_stock_items || 0).toLocaleString(), '')}
			${_kpi('Critical',        (s.critical_count || 0).toLocaleString(), s.critical_count > 0 ? 'neg' : 'pos')}
			${_kpi('Watch List',      (s.watch_count || 0).toLocaleString(), s.watch_count > 0 ? 'warn' : 'pos')}
			${_kpi('Slow Movers',     (s.slow_count || 0).toLocaleString(), s.slow_count > 0 ? 'warn' : 'pos')}
			${_kpi('Overstocked',     (s.overstock_count || 0).toLocaleString(), 'info')}
			${_kpi('Total Stock Value', _egp(s.total_stock_value), '')}
		`);
	}

	_render_trend(rows) {
		if (!rows.length) {
			$('#inv-chart-trend').html('<div class="inv-loading">No velocity data yet — run a velocity update.</div>');
			return;
		}
		_chart('inv-chart-trend', {
			type: 'donut',
			height: 280,
			data: {
				labels: rows.map(r => r.trend),
				datasets: [{ values: rows.map(r => r.count) }],
			},
			colors: rows.map(r => _trend_color(r.trend)),
		});
	}

	_render_movers(rows) {
		if (!rows.length) {
			$('#inv-chart-movers').html('<div class="inv-loading">No movers yet.</div>');
			return;
		}
		let top = rows.slice(0, 10);
		_chart('inv-chart-movers', {
			type: 'bar',
			height: 280,
			data: {
				labels: top.map(r => _short(r.item_name || r.item_code)),
				datasets: [{ name: 'Units/day', values: top.map(r => r.velocity_60d) }],
			},
			colors: ['#16a085'],
		});

		$('#inv-table-movers').html(`
			<table class="inv-table">
				<thead><tr>
					<th>Item</th><th>Group</th><th>Type</th>
					<th>30d /day</th><th>60d /day</th><th>Trend</th>
					<th>Stock</th><th>Days Left</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `<tr>
						<td>${r.item_name || r.item_code}</td>
						<td class="muted">${r.item_group || '—'}</td>
						<td>${_replenish_badge(r.replenishment_type)}</td>
						<td>${(r.velocity_30d || 0).toFixed(2)}</td>
						<td>${(r.velocity_60d || 0).toFixed(2)}</td>
						<td class="${_trend_cls(r.trend)}">${r.trend || '—'}</td>
						<td>${(r.stock_on_hand || 0).toLocaleString()}</td>
						<td>${_days_cell(r.days_of_stock)}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_critical(rows) {
		this._alert_table('#inv-table-critical', rows, true,
			'✅ Nothing critical right now.');
	}

	_render_watch(rows) {
		this._alert_table('#inv-table-watch', rows, true,
			'✅ Nothing on the watch list.');
	}

	_alert_table(sel, rows, show_days, empty_msg) {
		if (!rows.length) {
			$(sel).html(`<div class="inv-loading">${empty_msg}</div>`);
			return;
		}
		$(sel).html(`
			<table class="inv-table">
				<thead><tr>
					<th>Item</th><th>Type</th><th>Stock</th>
					<th>Velocity</th>${show_days ? '<th>Days Left</th>' : ''}
				</tr></thead>
				<tbody>
					${rows.map(r => `<tr>
						<td>${r.item_name || r.item_code}</td>
						<td>${_replenish_badge(r.replenishment_type)}</td>
						<td>${(r.stock_on_hand || 0).toLocaleString()}</td>
						<td class="muted">${parseFloat(r.daily_velocity || 0).toFixed(2)}/day</td>
						${show_days ? `<td>${_days_cell(r.days_remaining)}</td>` : ''}
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_slow(rows) {
		if (!rows.length) {
			$('#inv-table-slow').html('<div class="inv-loading">✅ No slow-moving stock.</div>');
			return;
		}
		$('#inv-table-slow').html(`
			<table class="inv-table">
				<thead><tr>
					<th>Item</th><th>Group</th><th>Type</th><th>Stock</th><th>Trend</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `<tr>
						<td>${r.item_name || r.item_code}</td>
						<td class="muted">${r.item_group || '—'}</td>
						<td>${_replenish_badge(r.replenishment_type)}</td>
						<td>${(r.stock_on_hand || 0).toLocaleString()}</td>
						<td class="${_trend_cls(r.trend)}">${r.trend || '—'}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_over(rows) {
		if (!rows.length) {
			$('#inv-table-over').html('<div class="inv-loading">✅ No overstocked items.</div>');
			return;
		}
		$('#inv-table-over').html(`
			<table class="inv-table">
				<thead><tr>
					<th>Item</th><th>Type</th><th>Stock</th><th>Days Supply</th><th>Est. Value</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `<tr>
						<td>${r.item_name || r.item_code}</td>
						<td>${_replenish_badge(r.replenishment_type)}</td>
						<td>${(r.stock_on_hand || 0).toLocaleString()}</td>
						<td class="amber">${r.days_remaining}</td>
						<td>${_egp(r.stock_value)}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_sold(rows) {
		if (!rows.length) {
			$('#inv-table-sold').html('<div class="inv-loading">No sales in range.</div>');
			return;
		}
		$('#inv-table-sold').html(`
			<table class="inv-table">
				<thead><tr>
					<th>#</th><th>Item</th><th>Group</th><th>Units Sold</th><th>Revenue</th>
				</tr></thead>
				<tbody>
					${rows.map((r, i) => `<tr>
						<td class="muted">${i + 1}</td>
						<td>${r.item_name || r.item_code}</td>
						<td class="muted">${r.item_group || '—'}</td>
						<td>${(r.qty || 0).toLocaleString()}</td>
						<td class="green">${_egp(r.revenue)}</td>
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
	return `<div class="inv-kpi ${cls}">
		<div class="v">${value}</div>
		<div class="l">${label}</div>
	</div>`;
}

function _replenish_badge(type) {
	if (type === 'Manufacture') return '<span class="inv-badge badge-make">🏭 Make</span>';
	return '<span class="inv-badge badge-buy">🛒 Buy</span>';
}

function _days_cell(days) {
	let d = parseInt(days || 0, 10);
	if (d >= 999) return '<span class="muted">∞</span>';
	let cls = d <= 5 ? 'red' : d <= 14 ? 'amber' : '';
	return `<span class="${cls}">${d}d</span>`;
}

const _TREND_COLORS = {
	'Accelerating': '#16a34a',
	'Stable':       '#2563eb',
	'Declining':    '#dc2626',
	'New Item':     '#7c3aed',
	'No Sales':     '#6b7280',
	'Unrated':      '#94a3b8',
};

function _trend_color(trend) {
	return _TREND_COLORS[trend] || '#94a3b8';
}

function _trend_cls(trend) {
	if (trend === 'Accelerating') return 'trend-up';
	if (trend === 'Declining' || trend === 'No Sales') return 'trend-down';
	return 'trend-flat';
}

function _chart(id, opts) {
	let el = document.getElementById(id);
	if (!el) return;
	el.innerHTML = '';
	try {
		new frappe.Chart(el, opts);
	} catch (e) {
		el.innerHTML = `<div class="inv-loading" style="color:#e74c3c">Chart error: ${e.message}</div>`;
	}
}
