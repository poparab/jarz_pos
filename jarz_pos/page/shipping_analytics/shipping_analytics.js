/* jshint esversion: 9 */
/* globals frappe */

frappe.pages['shipping-analytics'].on_page_load = function (wrapper) {
	let page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Shipping Analytics'),
		single_column: true,
	});
	let dash = new ShippingAnalyticsDashboard(page);
	$(wrapper).data('sa_dash', dash);
};

frappe.pages['shipping-analytics'].on_page_show = function (wrapper) {
	let dash = $(wrapper).data('sa_dash');
	if (dash) dash.refresh();
};

// ─────────────────────────────────────────────────────────────────────────────

class ShippingAnalyticsDashboard {
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
  .sa { padding: 18px 20px 40px; background: var(--bg-color); }
  .sa-section { margin-bottom: 32px; }
  .sa-title  { font-size: 13px; font-weight: 700; color: var(--text-muted);
               text-transform: uppercase; letter-spacing: .6px;
               margin-bottom: 14px; padding-bottom: 8px;
               border-bottom: 1px solid var(--border-color); }

  /* KPI cards */
  .sa-kpis  { display: flex; flex-wrap: wrap; gap: 12px; }
  .sa-kpi   { flex: 1; min-width: 140px; background: var(--card-bg);
              border: 1px solid var(--border-color); border-radius: 8px;
              padding: 16px 18px; }
  .sa-kpi .v { font-size: 24px; font-weight: 700; color: var(--text-color); }
  .sa-kpi .l { font-size: 11px; color: var(--text-muted); margin-top: 3px;
               text-transform: uppercase; letter-spacing: .4px; }
  .sa-kpi.pos .v { color: #27ae60; }
  .sa-kpi.neg .v { color: #e74c3c; }
  .sa-kpi.warn .v { color: #e67e22; }

  /* Chart grid */
  .sa-grid  { display: grid; gap: 18px; grid-template-columns: 1fr 1fr; }
  .sa-grid.one { grid-template-columns: 1fr; }
  .sa-box   { background: var(--card-bg); border: 1px solid var(--border-color);
              border-radius: 8px; padding: 18px; overflow: hidden; }
  .sa-box h6 { font-size: 11px; font-weight: 700; color: var(--text-muted);
               text-transform: uppercase; letter-spacing: .5px; margin-bottom: 14px; }
  .sa-box.span2 { grid-column: 1 / -1; }

  /* Tables */
  .sa-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .sa-table th { text-align: left; padding: 7px 10px; font-size: 11px; font-weight: 700;
                 text-transform: uppercase; letter-spacing: .4px;
                 color: var(--text-muted); background: var(--subtle-fg);
                 border-bottom: 1px solid var(--border-color); }
  .sa-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-color); }
  .sa-table tr:last-child td { border-bottom: none; }
  .sa-table tr:hover td { background: var(--subtle-fg); }
  .red  { color: #e74c3c; font-weight: 600; }
  .green { color: #27ae60; font-weight: 600; }
  .badge-warn  { background:#fef3cd; color:#856404; border-radius:4px;
                 padding:2px 7px; font-size:11px; font-weight:600; }
  .badge-ok    { background:#d1f5e0; color:#155724; border-radius:4px;
                 padding:2px 7px; font-size:11px; font-weight:600; }
  .badge-pend  { background:#cce5ff; color:#004085; border-radius:4px;
                 padding:2px 7px; font-size:11px; font-weight:600; }

  /* Alerts */
  .sa-alert { display:flex; gap:10px; align-items:flex-start; padding:10px 14px;
              border-radius:6px; margin-bottom:8px; font-size:13px; line-height:1.5; }
  .sa-alert.danger  { background:#fdf2f2; border-left:3px solid #e74c3c; color:#922b21; }
  .sa-alert.warning { background:#fef9ec; border-left:3px solid #f39c12; color:#935116; }
  .sa-alert.info    { background:#eaf4fb; border-left:3px solid #2e86c1; color:#1a5276; }
  .sa-alert .ico    { font-size:15px; flex-shrink:0; margin-top:1px; }
  .sa-no-alerts     { text-align:center; color:var(--text-muted); padding:24px; font-size:13px; }
  .sa-loading       { text-align:center; color:var(--text-muted); padding:32px; font-size:13px; }
</style>

<div class="sa">

  <!-- ① KPIs -->
  <div class="sa-section">
    <div class="sa-title">Key Metrics</div>
    <div class="sa-kpis" id="sa-kpis"><div class="sa-loading">Loading…</div></div>
  </div>

  <!-- ② Alerts -->
  <div class="sa-section">
    <div class="sa-title">⚠ Discrepancies &amp; Alerts</div>
    <div id="sa-alerts"><div class="sa-loading">Scanning for issues…</div></div>
  </div>

  <!-- ③ Territory -->
  <div class="sa-section">
    <div class="sa-title">Territory Analysis</div>
    <div class="sa-grid">
      <div class="sa-box">
        <h6>Expense vs Income by Territory</h6>
        <div id="sa-chart-terr-bar"></div>
      </div>
      <div class="sa-box">
        <h6>Average Cost per Order by Territory</h6>
        <div id="sa-chart-terr-avg"></div>
      </div>
      <div class="sa-box span2">
        <h6>Territory P&amp;L Table (Income − Expense)</h6>
        <div id="sa-table-terr"></div>
      </div>
    </div>
  </div>

  <!-- ④ Branch -->
  <div class="sa-section">
    <div class="sa-title">Branch / POS Profile Analysis</div>
    <div class="sa-grid">
      <div class="sa-box">
        <h6>Total Shipping Cost per Branch</h6>
        <div id="sa-chart-branch-cost"></div>
      </div>
      <div class="sa-box">
        <h6>Average Cost per Order by Branch</h6>
        <div id="sa-chart-branch-avg"></div>
      </div>
    </div>
  </div>

  <!-- ⑤ Courier -->
  <div class="sa-section">
    <div class="sa-title">Courier Performance</div>
    <div class="sa-grid">
      <div class="sa-box">
        <h6>Amount by Courier — Settled vs Unsettled</h6>
        <div id="sa-chart-courier"></div>
      </div>
      <div class="sa-box">
        <h6>Unsettled Balances</h6>
        <div id="sa-table-unsettled"></div>
      </div>
    </div>
  </div>

  <!-- ⑥ Custom Shipping Overrides -->
  <div class="sa-section">
    <div class="sa-title">Custom Shipping Overrides</div>
    <div class="sa-grid">
      <div class="sa-box">
        <h6>Request Status Breakdown</h6>
        <div id="sa-chart-csr-donut"></div>
      </div>
      <div class="sa-box">
        <h6>Override Requests — Original vs Requested Amount</h6>
        <div id="sa-table-csr"></div>
      </div>
    </div>
  </div>

  <!-- ⑦ Trends -->
  <div class="sa-section">
    <div class="sa-title">Daily Trends</div>
    <div class="sa-grid one">
      <div class="sa-box">
        <h6>Daily Order Volume &amp; Shipping Cost</h6>
        <div id="sa-chart-trend"></div>
      </div>
    </div>
  </div>

  <!-- ⑧ Operational mix -->
  <div class="sa-section">
    <div class="sa-title">Operational Mix</div>
    <div class="sa-grid">
      <div class="sa-box">
        <h6>Pickup vs Delivery Orders</h6>
        <div id="sa-chart-pickup"></div>
      </div>
      <div class="sa-box">
        <h6>Sub-Territory Shipping Cost (Top 20)</h6>
        <div id="sa-chart-sub-terr"></div>
      </div>
    </div>
  </div>

  <!-- ⑨ Double Shipping -->
  <div class="sa-section">
    <div class="sa-title">Double Shipping Impact</div>
    <div class="sa-grid one">
      <div class="sa-box">
        <h6>Trips where 2× cost was applied</h6>
        <div id="sa-table-double"></div>
      </div>
    </div>
  </div>

</div>`);
	}

	// ── Data loading ─────────────────────────────────────────────────────────

	refresh() {
		let args = { from_date: this.from_date, to_date: this.to_date };

		frappe.call('jarz_pos.api.shipping_analytics.get_summary_kpis', args)
			.then(r => r.message && this._render_kpis(r.message));

		frappe.call({
			method: 'jarz_pos.api.shipping_analytics.get_alerts_data',
			args: args,
			error: () => {
				$('#sa-alerts').html(
					'<div class="sa-alert warning"><span class="ico">⚠</span>' +
					'<span>Could not load alerts — check the error log for details.</span></div>'
				);
			},
		}).then(r => r.message !== undefined && this._render_alerts(r.message));

		frappe.call('jarz_pos.api.shipping_analytics.get_cost_by_territory', args)
			.then(r => r.message && this._render_territory(r.message));

		frappe.call('jarz_pos.api.shipping_analytics.get_cost_by_pos_profile', args)
			.then(r => r.message && this._render_branch(r.message));

		frappe.call('jarz_pos.api.shipping_analytics.get_cost_by_courier', args)
			.then(r => r.message && this._render_courier(r.message));

		frappe.call('jarz_pos.api.shipping_analytics.get_custom_shipping_breakdown', args)
			.then(r => r.message && this._render_csr(r.message));

		frappe.call('jarz_pos.api.shipping_analytics.get_daily_trend', args)
			.then(r => r.message && this._render_trend(r.message));

		frappe.call('jarz_pos.api.shipping_analytics.get_pickup_vs_delivery_split', args)
			.then(r => r.message && this._render_pickup(r.message));

		frappe.call('jarz_pos.api.shipping_analytics.get_cost_by_sub_territory', args)
			.then(r => r.message && this._render_sub_territory(r.message));

		frappe.call('jarz_pos.api.shipping_analytics.get_unsettled_courier_balances')
			.then(r => r.message && this._render_unsettled(r.message));

		frappe.call('jarz_pos.api.shipping_analytics.get_double_shipping_impact', args)
			.then(r => r.message && this._render_double(r.message));
	}

	// ── Renderers ────────────────────────────────────────────────────────────

	_render_kpis(d) {
		let pl_cls = d.net_pl >= 0 ? 'pos' : 'neg';
		let csr_cls = d.pending_csr_count > 0 ? 'warn' : 'pos';
		let uns_cls = d.unsettled_courier_total > 0 ? 'warn' : 'pos';

		$('#sa-kpis').html(`
			${_kpi('Total Orders', d.total_orders, '')}
			${_kpi('Delivery Orders', d.delivery_orders, '')}
			${_kpi('Pickup Orders', d.pickup_orders, '')}
			${_kpi('Shipping Expense', _egp(d.total_expense), 'neg')}
			${_kpi('Delivery Income', _egp(d.total_income), 'pos')}
			${_kpi('Net P&L', _egp(d.net_pl), pl_cls)}
			${_kpi('Avg Cost / Delivery', _egp(d.avg_cost_per_order), '')}
			${_kpi('Pending Overrides', d.pending_csr_count, csr_cls)}
			${_kpi('Unsettled (All-Time)', _egp(d.unsettled_courier_total), uns_cls)}
		`);
	}

	_render_alerts(alerts) {
		let icons = { danger: '🔴', warning: '🟡', info: '🔵' };
		if (!alerts.length) {
			$('#sa-alerts').html('<div class="sa-no-alerts">✅ No active discrepancies detected for the selected period.</div>');
			return;
		}
		$('#sa-alerts').html(alerts.map(a =>
			`<div class="sa-alert ${a.type}">
				<span class="ico">${icons[a.type] || 'ℹ'}</span>
				<span>${a.message}</span>
			</div>`
		).join(''));
	}

	_render_territory(rows) {
		if (!rows.length) return;

		// Stacked bar: expense + income
		_chart('sa-chart-terr-bar', {
			type: 'bar',
			height: 280,
			data: {
				labels: rows.map(r => _short(r.territory)),
				datasets: [
					{ name: 'Expense', values: rows.map(r => r.total_expense) },
					{ name: 'Income',  values: rows.map(r => r.total_income) },
				],
			},
			colors: ['#e74c3c', '#27ae60'],
			barOptions: { stacked: false },
			tooltipOptions: { formatTooltipY: v => _egp(v) },
		});

		// Avg cost bar — sorted high to low so the most expensive territories appear first
		let by_avg = [...rows].sort((a, b) => b.avg_cost - a.avg_cost);
		_chart('sa-chart-terr-avg', {
			type: 'bar',
			height: 280,
			data: {
				labels: by_avg.map(r => _short(r.territory)),
				datasets: [{ name: 'Avg Cost/Order', values: by_avg.map(r => r.avg_cost) }],
			},
			colors: ['#FF6B35'],
			tooltipOptions: { formatTooltipY: v => _egp(v) },
		});

		// P&L table
		let pos_neg = v => `<span class="${v >= 0 ? 'green' : 'red'}">${_egp(v)}</span>`;
		$('#sa-table-terr').html(`
			<table class="sa-table">
				<thead><tr>
					<th>Territory</th><th>Orders</th>
					<th>Expense (EGP)</th><th>Income (EGP)</th>
					<th>Net P&L</th><th>Avg Cost</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `<tr>
						<td>${r.territory}</td>
						<td>${r.order_count}</td>
						<td class="red">${_egp(r.total_expense)}</td>
						<td class="green">${_egp(r.total_income)}</td>
						<td>${pos_neg(r.net_pl)}</td>
						<td>${_egp(r.avg_cost)}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_branch(rows) {
		if (!rows.length) return;

		_chart('sa-chart-branch-cost', {
			type: 'bar',
			height: 260,
			data: {
				labels: rows.map(r => _short(r.branch)),
				datasets: [
					{ name: 'Expense', values: rows.map(r => r.total_expense) },
					{ name: 'Income',  values: rows.map(r => r.total_income) },
				],
			},
			colors: ['#e74c3c', '#27ae60'],
			tooltipOptions: { formatTooltipY: v => _egp(v) },
		});

		_chart('sa-chart-branch-avg', {
			type: 'bar',
			height: 260,
			data: {
				labels: rows.map(r => _short(r.branch)),
				datasets: [{ name: 'Avg Cost/Order', values: rows.map(r => r.avg_cost) }],
			},
			colors: ['#FF6B35'],
			tooltipOptions: { formatTooltipY: v => _egp(v) },
		});
	}

	_render_courier(rows) {
		if (!rows.length) {
			$('#sa-chart-courier').html('<div class="sa-loading">No courier transactions in range.</div>');
			return;
		}

		_chart('sa-chart-courier', {
			type: 'bar',
			height: 280,
			data: {
				labels: rows.map(r => _short(r.party)),
				datasets: [
					{ name: 'Settled',   values: rows.map(r => r.settled) },
					{ name: 'Unsettled', values: rows.map(r => r.unsettled) },
				],
			},
			colors: ['#27ae60', '#e74c3c'],
			barOptions: { stacked: true },
			tooltipOptions: { formatTooltipY: v => _egp(v) },
		});
	}

	_render_unsettled(rows) {
		if (!rows.length) {
			$('#sa-table-unsettled').html('<div class="sa-loading">No unsettled balances.</div>');
			return;
		}
		$('#sa-table-unsettled').html(`
			<table class="sa-table">
				<thead><tr>
					<th>Courier</th><th>Type</th><th>Orders</th>
					<th>Amount</th><th>Oldest</th><th>Age</th>
				</tr></thead>
				<tbody>
					${rows.map(r => {
						let age_cls = r.days_aged > 7 ? 'red' : '';
						return `<tr>
							<td>${r.party}</td>
							<td>${r.party_type}</td>
							<td>${r.order_count}</td>
							<td class="red">${_egp(r.total_owed)}</td>
							<td>${r.oldest_date || '—'}</td>
							<td class="${age_cls}">${r.days_aged}d</td>
						</tr>`;
					}).join('')}
				</tbody>
			</table>`);
	}

	_render_csr(data) {
		let s = data.summary;

		// Donut
		if (s.total > 0) {
			_chart('sa-chart-csr-donut', {
				type: 'donut',
				height: 240,
				data: {
					labels: ['Approved', 'Rejected', 'Pending'],
					datasets: [{ values: [s.approved, s.rejected, s.pending] }],
				},
				colors: ['#27ae60', '#e74c3c', '#f39c12'],
			});
		} else {
			$('#sa-chart-csr-donut').html('<div class="sa-loading">No override requests in range.</div>');
		}

		// Table
		if (!data.rows.length) {
			$('#sa-table-csr').html('<div class="sa-loading">No requests found.</div>');
			return;
		}

		let status_badge = s => {
			if (s === 'Approved') return '<span class="badge-ok">Approved</span>';
			if (s === 'Rejected') return '<span class="badge-warn">Rejected</span>';
			return '<span class="badge-pend">Pending</span>';
		};

		$('#sa-table-csr').html(`
			<div style="font-size:12px;color:var(--text-muted);margin-bottom:10px;">
				${s.total} requests — approval rate: <b>${s.approval_rate}%</b>
			</div>
			<table class="sa-table">
				<thead><tr>
					<th>Invoice</th><th>Territory</th><th>Original</th>
					<th>Requested</th><th>Delta</th><th>Status</th>
				</tr></thead>
				<tbody>
					${data.rows.map(r => {
						let delta_cls = r.is_large_override ? 'red' : '';
						return `<tr>
							<td><a href="/app/sales-invoice/${r.invoice}" target="_blank">${r.invoice}</a></td>
							<td>${r.territory || '—'}</td>
							<td>${_egp(r.original_amount)}</td>
							<td>${_egp(r.requested_amount)}</td>
							<td class="${delta_cls}">${r.delta >= 0 ? '+' : ''}${_egp(r.delta)}</td>
							<td>${status_badge(r.status)}</td>
						</tr>`;
					}).join('')}
				</tbody>
			</table>`);
	}

	_render_trend(rows) {
		if (!rows.length) {
			$('#sa-chart-trend').html('<div class="sa-loading">No data in range.</div>');
			return;
		}

		_chart('sa-chart-trend', {
			type: 'axis-mixed',
			height: 300,
			data: {
				labels: rows.map(r => r.posting_date),
				datasets: [
					{ name: 'Orders', type: 'bar', values: rows.map(r => r.order_count) },
					{ name: 'Expense (EGP)', type: 'line', values: rows.map(r => r.total_expense) },
					{ name: 'Income (EGP)',  type: 'line', values: rows.map(r => r.total_income) },
				],
			},
			colors: ['#95a5a6', '#e74c3c', '#27ae60'],
		});
	}

	_render_pickup(data) {
		let total = data.pickup + data.delivery;
		if (!total) {
			$('#sa-chart-pickup').html('<div class="sa-loading">No data.</div>');
			return;
		}
		_chart('sa-chart-pickup', {
			type: 'donut',
			height: 240,
			data: {
				labels: ['Delivery', 'Pickup'],
				datasets: [{ values: [data.delivery, data.pickup] }],
			},
			colors: ['#3498db', '#FF6B35'],
		});
	}

	_render_sub_territory(rows) {
		let non_empty = rows.filter(r => r.sub_territory !== '(None)');
		if (!non_empty.length) {
			$('#sa-chart-sub-terr').html('<div class="sa-loading">No sub-territories assigned in range.</div>');
			return;
		}
		_chart('sa-chart-sub-terr', {
			type: 'bar',
			height: 260,
			data: {
				labels: non_empty.map(r => _short(r.sub_territory)),
				datasets: [{ name: 'Expense', values: non_empty.map(r => r.total_expense) }],
			},
			colors: ['#9b59b6'],
			tooltipOptions: { formatTooltipY: v => _egp(v) },
		});
	}

	_render_double(data) {
		if (!data.trips.length) {
			$('#sa-table-double').html('<div class="sa-loading">No double-shipping trips in range.</div>');
			return;
		}

		$('#sa-table-double').html(`
			<div style="font-size:12px;color:var(--text-muted);margin-bottom:10px;">
				<b>${data.total_double_trips}</b> trips — total extra cost:
				<span class="red"><b>${_egp(data.total_extra_cost)}</b></span>
			</div>
			<table class="sa-table">
				<thead><tr>
					<th>Trip</th><th>Date</th><th>Courier</th>
					<th>Territory Trigger</th><th>Orders</th>
					<th>Total Paid</th><th>Extra Cost (2×)</th>
				</tr></thead>
				<tbody>
					${data.trips.map(r => `<tr>
						<td><a href="/app/delivery-trip/${r.name}" target="_blank">${r.name}</a></td>
						<td>${r.trip_date}</td>
						<td>${r.courier_party || '—'}</td>
						<td>${r.double_shipping_territory || '—'}</td>
						<td>${r.total_orders}</td>
						<td>${_egp(r.total_shipping_expense)}</td>
						<td class="red">${_egp(r.extra_cost)}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function _egp(v) {
	let n = parseFloat(v || 0);
	return 'EGP ' + n.toLocaleString('en', {
		minimumFractionDigits: 0,
		maximumFractionDigits: 0,
	});
}

function _short(label, max = 18) {
	if (!label) return '—';
	return label.length > max ? label.slice(0, max - 1) + '…' : label;
}

function _kpi(label, value, cls) {
	return `<div class="sa-kpi ${cls}">
		<div class="v">${value}</div>
		<div class="l">${label}</div>
	</div>`;
}

function _chart(id, opts) {
	let el = document.getElementById(id);
	if (!el) return;
	el.innerHTML = '';
	try {
		new frappe.Chart(el, opts);
	} catch (e) {
		el.innerHTML = `<div class="sa-loading" style="color:#e74c3c">Chart error: ${e.message}</div>`;
	}
}
