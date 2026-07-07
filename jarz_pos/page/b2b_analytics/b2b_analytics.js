/* jshint esversion: 9 */
/* globals frappe */

frappe.pages['b2b-analytics'].on_page_load = function (wrapper) {
	let page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('B2B Sales & Clients'),
		single_column: true,
	});
	let dash = new B2BAnalyticsDashboard(page);
	$(wrapper).data('ba_dash', dash);
};

frappe.pages['b2b-analytics'].on_page_show = function (wrapper) {
	let dash = $(wrapper).data('ba_dash');
	if (dash) dash.refresh();
};

// ─────────────────────────────────────────────────────────────────────────────

class B2BAnalyticsDashboard {
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
  .ba { padding: 18px 20px 40px; background: var(--bg-color); }
  .ba-section { margin-bottom: 32px; }
  .ba-title { font-size: 13px; font-weight: 700; color: var(--text-muted);
              text-transform: uppercase; letter-spacing: .6px;
              margin-bottom: 14px; padding-bottom: 8px;
              border-bottom: 1px solid var(--border-color); }
  .ba-note { font-size: 11px; color: var(--text-muted); font-weight: 500;
             text-transform: none; letter-spacing: 0; margin-left: 8px; }

  /* KPI cards */
  .ba-kpis { display: flex; flex-wrap: wrap; gap: 12px; }
  .ba-kpi  { flex: 1; min-width: 140px; background: var(--card-bg);
             border: 1px solid var(--border-color); border-radius: 8px;
             padding: 16px 18px; }
  .ba-kpi .v { font-size: 24px; font-weight: 700; color: var(--text-color); }
  .ba-kpi .l { font-size: 11px; color: var(--text-muted); margin-top: 3px;
               text-transform: uppercase; letter-spacing: .4px; }
  .ba-kpi.pos  .v { color: #27ae60; }
  .ba-kpi.neg  .v { color: #e74c3c; }
  .ba-kpi.info .v { color: #2980b9; }
  .ba-kpi.warn .v { color: #e67e22; }

  /* Grid */
  .ba-grid { display: grid; gap: 18px; grid-template-columns: 1fr 1fr; }
  .ba-grid.one { grid-template-columns: 1fr; }
  .ba-box  { background: var(--card-bg); border: 1px solid var(--border-color);
             border-radius: 8px; padding: 18px; overflow: hidden; }
  .ba-box h6 { font-size: 11px; font-weight: 700; color: var(--text-muted);
               text-transform: uppercase; letter-spacing: .5px; margin-bottom: 14px; }
  .ba-box.span2 { grid-column: 1 / -1; }

  /* Tables */
  .ba-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .ba-table th { text-align: left; padding: 7px 10px; font-size: 11px; font-weight: 700;
                 text-transform: uppercase; letter-spacing: .4px;
                 color: var(--text-muted); background: var(--subtle-fg);
                 border-bottom: 1px solid var(--border-color); }
  .ba-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-color); }
  .ba-table tr:last-child td { border-bottom: none; }
  .ba-table tr:hover td { background: var(--subtle-fg); }
  .red   { color: #e74c3c; font-weight: 600; }
  .green { color: #27ae60; font-weight: 600; }
  .muted { color: var(--text-muted); }

  .ba-badge { border-radius: 4px; padding: 2px 7px; font-size: 11px; font-weight: 600;
              white-space: nowrap; }

  .ba-loading { text-align: center; color: var(--text-muted); padding: 32px; font-size: 13px; }

  /* Alerts feed */
  .ba-alerts { display: flex; flex-direction: column; gap: 8px; }
  .ba-alert  { border-radius: 6px; padding: 10px 14px; font-size: 13px;
               border: 1px solid var(--border-color); }
  .ba-alert.danger  { background: rgba(231,76,60,0.10);  border-color: rgba(231,76,60,0.30);  color: #c0392b; }
  .ba-alert.warning { background: rgba(230,126,34,0.10); border-color: rgba(230,126,34,0.30); color: #b9770e; }
  .ba-alert.info    { background: rgba(41,128,185,0.10); border-color: rgba(41,128,185,0.30); color: #2471a3; }
</style>

<div class="ba">

  <!-- ① KPIs -->
  <div class="ba-section">
    <div class="ba-title">Key Metrics</div>
    <div class="ba-kpis" id="ba-kpis"><div class="ba-loading">Loading…</div></div>
  </div>

  <!-- ② Pipeline -->
  <div class="ba-section">
    <div class="ba-title">B2B Pipeline by Stage</div>
    <div class="ba-grid">
      <div class="ba-box">
        <h6>Clients per Stage</h6>
        <div id="ba-chart-pipeline"></div>
      </div>
      <div class="ba-box">
        <h6>Open Value per Stage</h6>
        <div id="ba-table-pipeline"></div>
      </div>
    </div>
  </div>

  <!-- ③ Revenue Trend -->
  <div class="ba-section">
    <div class="ba-title">B2B Revenue Trend</div>
    <div class="ba-grid one">
      <div class="ba-box">
        <h6>Daily Revenue &amp; Orders</h6>
        <div id="ba-chart-trend"></div>
      </div>
    </div>
  </div>

  <!-- ④ Top Clients -->
  <div class="ba-section">
    <div class="ba-title">Top B2B Clients (by Revenue in Range)</div>
    <div class="ba-grid one">
      <div class="ba-box">
        <h6>Highest-Value Clients</h6>
        <div id="ba-table-clients"></div>
      </div>
    </div>
  </div>

  <!-- ⑤ Revenue by Policy / Territory -->
  <div class="ba-section">
    <div class="ba-title">Revenue Breakdown</div>
    <div class="ba-grid">
      <div class="ba-box">
        <h6>Revenue by Commercial Policy</h6>
        <div id="ba-chart-policy"></div>
      </div>
      <div class="ba-box">
        <h6>Revenue by Territory</h6>
        <div id="ba-chart-territory"></div>
      </div>
      <div class="ba-box span2">
        <h6>Clients by Group</h6>
        <div id="ba-table-group"></div>
      </div>
    </div>
  </div>

  <!-- ⑥ Reorder Due (hidden when empty) -->
  <div class="ba-section" id="ba-section-reorder" style="display:none">
    <div class="ba-title">Reorder Due</div>
    <div class="ba-grid one">
      <div class="ba-box">
        <h6>Clients Overdue for a Reorder</h6>
        <div id="ba-table-reorder"></div>
      </div>
    </div>
  </div>

  <!-- ⑦ At-Risk Clients (hidden when empty) -->
  <div class="ba-section" id="ba-section-risk" style="display:none">
    <div class="ba-title">At-Risk Clients</div>
    <div class="ba-grid one">
      <div class="ba-box">
        <h6>Win-Back List (Current Snapshot)</h6>
        <div id="ba-table-risk"></div>
      </div>
    </div>
  </div>

  <!-- ⑧ Conversion & Alerts -->
  <div class="ba-section">
    <div class="ba-title">Conversion &amp; Alerts</div>
    <div class="ba-grid">
      <div class="ba-box">
        <h6>Opportunity Conversion</h6>
        <div id="ba-conversion"></div>
      </div>
      <div class="ba-box">
        <h6>Alerts</h6>
        <div id="ba-alerts"><div class="ba-loading">Loading…</div></div>
      </div>
    </div>
  </div>

</div>`);
	}

	// ── Data loading ─────────────────────────────────────────────────────────

	refresh() {
		$('#ba-kpis').html('<div class="ba-loading">Loading…</div>');

		frappe.call({
			method: 'jarz_pos.api.b2b_analytics.get_b2b_analytics',
			args: { date_from: this.from_date, date_to: this.to_date },
		}).then(r => {
			if (!r.message) return;
			let d = r.message;
			this._render_kpis(d.summary || {});
			this._render_pipeline(d.pipeline_by_stage || []);
			this._render_trend(d.revenue_trend || []);
			this._render_top_clients(d.top_clients || []);
			this._render_policy(d.revenue_by_policy || []);
			this._render_territory(d.revenue_by_territory || []);
			this._render_group(d.clients_by_group || []);
			this._render_reorder(d.reorder_due || []);
			this._render_risk(d.at_risk_clients || []);
			this._render_conversion(d.conversion || {});
			this._render_alerts(d.alerts || []);
		});
	}

	// ── Renderers ────────────────────────────────────────────────────────────

	_render_kpis(s) {
		let margin_pct = parseFloat(s.gross_margin_pct || 0);
		let margin_cls = margin_pct >= 40 ? 'pos' : margin_pct >= 20 ? 'info' : 'warn';
		let reorder_cls = (s.reorder_due_count || 0) > 0 ? 'warn' : 'pos';
		let risk_cls = (s.at_risk_count || 0) > 0 ? 'warn' : 'pos';

		$('#ba-kpis').html(`
			${_kpi('B2B Revenue',    _egp(s.b2b_revenue),                          '')}
			${_kpi('B2B Orders',     (s.b2b_orders || 0).toLocaleString(),          '')}
			${_kpi('Active Clients', (s.active_clients || 0).toLocaleString(),      'info')}
			${_kpi('New Clients',    (s.new_clients || 0).toLocaleString(),         'pos')}
			${_kpi('Avg Order Value', _egp(s.avg_order_value),                      'info')}
			${_kpi('Gross Margin',   margin_pct.toFixed(1) + '%',                   margin_cls)}
			${_kpi('Reorder Due',    (s.reorder_due_count || 0).toLocaleString(),   reorder_cls)}
			${_kpi('At Risk',        (s.at_risk_count || 0).toLocaleString(),       risk_cls)}
		`);
	}

	_render_pipeline(rows) {
		if (!rows.length) {
			$('#ba-chart-pipeline').html('<div class="ba-loading">No pipeline data.</div>');
			$('#ba-table-pipeline').html('<div class="ba-loading">No pipeline data.</div>');
			return;
		}

		_chart('ba-chart-pipeline', {
			type: 'bar',
			height: 280,
			data: {
				labels: rows.map(r => _short(r.stage)),
				datasets: [{ name: 'Clients', values: rows.map(r => r.count) }],
			},
			colors: ['#2980b9'],
		});

		$('#ba-table-pipeline').html(`
			<table class="ba-table">
				<thead><tr>
					<th>Stage</th>
					<th>Clients</th>
					<th>Open Value</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `<tr>
						<td>${r.stage}</td>
						<td>${(r.count || 0).toLocaleString()}</td>
						<td>${r.value > 0 ? _egp(r.value) : '—'}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_trend(rows) {
		if (!rows.length) {
			$('#ba-chart-trend').html('<div class="ba-loading">No data in range.</div>');
			return;
		}

		_chart('ba-chart-trend', {
			type: 'axis-mixed',
			height: 300,
			data: {
				labels: rows.map(r => r.posting_date),
				datasets: [
					{ name: 'Orders',        type: 'bar',  values: rows.map(r => r.orders) },
					{ name: 'Revenue (EGP)', type: 'line', values: rows.map(r => r.revenue) },
				],
			},
			colors: ['#95a5a6', '#2980b9'],
		});
	}

	_render_top_clients(rows) {
		if (!rows.length) {
			$('#ba-table-clients').html('<div class="ba-loading">No B2B client orders in range.</div>');
			return;
		}
		$('#ba-table-clients').html(`
			<table class="ba-table">
				<thead><tr>
					<th>#</th>
					<th>Client</th>
					<th>Segment</th>
					<th>Orders</th>
					<th>Revenue</th>
					<th>Last Order</th>
				</tr></thead>
				<tbody>
					${rows.map((r, i) => `<tr>
						<td class="muted">${i + 1}</td>
						<td><a href="/app/customer/${encodeURIComponent(r.customer)}" target="_blank">${r.customer_name || r.customer}</a></td>
						<td>${r.segment ? `<span class="ba-badge" style="background:rgba(41,128,185,0.14);color:#2471a3">${r.segment}</span>` : '<span class="muted">—</span>'}</td>
						<td>${(r.orders || 0).toLocaleString()}</td>
						<td class="green">${_egp(r.revenue)}</td>
						<td class="muted">${r.last_order_date || '—'}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_policy(rows) {
		if (!rows.length) {
			$('#ba-chart-policy').html('<div class="ba-loading">No policy data.</div>');
			return;
		}
		_chart('ba-chart-policy', {
			type: 'donut',
			height: 280,
			data: {
				labels: rows.map(r => _short(r.policy)),
				datasets: [{ values: rows.map(r => r.revenue) }],
			},
			colors: ['#2980b9', '#27ae60', '#e67e22', '#7B61FF', '#16a085', '#e74c3c'],
			tooltipOptions: { formatTooltipY: v => _egp(v) },
		});
	}

	_render_territory(rows) {
		if (!rows.length) {
			$('#ba-chart-territory').html('<div class="ba-loading">No territory data.</div>');
			return;
		}
		let top7 = rows.slice(0, 7);
		_chart('ba-chart-territory', {
			type: 'bar',
			height: 280,
			data: {
				labels: top7.map(r => _short(r.territory)),
				datasets: [{ name: 'Revenue', values: top7.map(r => r.revenue) }],
			},
			colors: ['#27ae60'],
			tooltipOptions: { formatTooltipY: v => _egp(v) },
		});
	}

	_render_group(rows) {
		if (!rows.length) {
			$('#ba-table-group').html('<div class="ba-loading">No client group data.</div>');
			return;
		}
		$('#ba-table-group').html(`
			<table class="ba-table">
				<thead><tr>
					<th>Customer Group</th>
					<th>Clients</th>
					<th>Revenue</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `<tr>
						<td>${r.customer_group}</td>
						<td>${(r.client_count || 0).toLocaleString()}</td>
						<td>${r.revenue > 0 ? _egp(r.revenue) : '—'}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_reorder(rows) {
		if (!rows.length) {
			$('#ba-section-reorder').hide();
			return;
		}
		$('#ba-section-reorder').show();
		$('#ba-table-reorder').html(`
			<table class="ba-table">
				<thead><tr>
					<th>Client</th>
					<th>Last Order</th>
					<th>Days Since</th>
					<th>Expected Reorder</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `<tr>
						<td><a href="/app/customer/${encodeURIComponent(r.customer)}" target="_blank">${r.customer_name || r.customer}</a></td>
						<td class="muted">${r.last_order_date || '—'}</td>
						<td class="red">${(r.days_since || 0).toLocaleString()}d</td>
						<td>${r.expected_reorder_date || '—'}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_risk(rows) {
		if (!rows.length) {
			$('#ba-section-risk').hide();
			return;
		}
		$('#ba-section-risk').show();
		$('#ba-table-risk').html(`
			<table class="ba-table">
				<thead><tr>
					<th>Client</th>
					<th>Segment</th>
					<th>Days Since Order</th>
					<th>Revenue</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `<tr>
						<td><a href="/app/customer/${encodeURIComponent(r.customer)}" target="_blank">${r.customer_name || r.customer}</a></td>
						<td>${r.segment ? `<span class="ba-badge" style="background:rgba(230,126,34,0.14);color:#b9770e">${r.segment}</span>` : '<span class="muted">—</span>'}</td>
						<td class="red">${(r.recency_days || 0).toLocaleString()}d</td>
						<td>${r.revenue > 0 ? _egp(r.revenue) : '—'}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_conversion(c) {
		let rate = parseFloat(c.conversion_rate || 0);
		$('#ba-conversion').html(`
			<div class="ba-kpis">
				${_kpi('Opportunities', (c.opportunities || 0).toLocaleString(), '')}
				${_kpi('Won',           (c.won || 0).toLocaleString(),           'pos')}
				${_kpi('Conversion',    rate.toFixed(1) + '%',                   'info')}
			</div>`);
	}

	_render_alerts(rows) {
		if (!rows.length) {
			$('#ba-alerts').html('<div class="ba-loading">✅ No alerts right now.</div>');
			return;
		}
		$('#ba-alerts').html(`
			<div class="ba-alerts">
				${rows.map(a => `<div class="ba-alert ${a.type || 'info'}">${a.message}</div>`).join('')}
			</div>`);
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
	return `<div class="ba-kpi ${cls}">
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
		el.innerHTML = `<div class="ba-loading" style="color:#e74c3c">Chart error: ${e.message}</div>`;
	}
}
