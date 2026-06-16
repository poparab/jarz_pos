/* jshint esversion: 9 */
/* globals frappe */

frappe.pages['customer-analytics'].on_page_load = function (wrapper) {
	let page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Customer Analytics'),
		single_column: true,
	});
	let dash = new CustomerAnalyticsDashboard(page);
	$(wrapper).data('ca_dash', dash);
};

frappe.pages['customer-analytics'].on_page_show = function (wrapper) {
	let dash = $(wrapper).data('ca_dash');
	if (dash) dash.refresh();
};

// ─────────────────────────────────────────────────────────────────────────────

class CustomerAnalyticsDashboard {
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
  .ca { padding: 18px 20px 40px; background: var(--bg-color); }
  .ca-section { margin-bottom: 32px; }
  .ca-title { font-size: 13px; font-weight: 700; color: var(--text-muted);
              text-transform: uppercase; letter-spacing: .6px;
              margin-bottom: 14px; padding-bottom: 8px;
              border-bottom: 1px solid var(--border-color); }
  .ca-note { font-size: 11px; color: var(--text-muted); font-weight: 500;
             text-transform: none; letter-spacing: 0; margin-left: 8px; }

  /* KPI cards */
  .ca-kpis { display: flex; flex-wrap: wrap; gap: 12px; }
  .ca-kpi  { flex: 1; min-width: 140px; background: var(--card-bg);
             border: 1px solid var(--border-color); border-radius: 8px;
             padding: 16px 18px; }
  .ca-kpi .v { font-size: 24px; font-weight: 700; color: var(--text-color); }
  .ca-kpi .l { font-size: 11px; color: var(--text-muted); margin-top: 3px;
               text-transform: uppercase; letter-spacing: .4px; }
  .ca-kpi.pos  .v { color: #27ae60; }
  .ca-kpi.neg  .v { color: #e74c3c; }
  .ca-kpi.info .v { color: #2980b9; }
  .ca-kpi.warn .v { color: #e67e22; }

  /* Grid */
  .ca-grid { display: grid; gap: 18px; grid-template-columns: 1fr 1fr; }
  .ca-grid.one { grid-template-columns: 1fr; }
  .ca-box  { background: var(--card-bg); border: 1px solid var(--border-color);
             border-radius: 8px; padding: 18px; overflow: hidden; }
  .ca-box h6 { font-size: 11px; font-weight: 700; color: var(--text-muted);
               text-transform: uppercase; letter-spacing: .5px; margin-bottom: 14px; }
  .ca-box.span2 { grid-column: 1 / -1; }

  /* Tables */
  .ca-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .ca-table th { text-align: left; padding: 7px 10px; font-size: 11px; font-weight: 700;
                 text-transform: uppercase; letter-spacing: .4px;
                 color: var(--text-muted); background: var(--subtle-fg);
                 border-bottom: 1px solid var(--border-color); }
  .ca-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-color); }
  .ca-table tr:last-child td { border-bottom: none; }
  .ca-table tr:hover td { background: var(--subtle-fg); }
  .red   { color: #e74c3c; font-weight: 600; }
  .green { color: #27ae60; font-weight: 600; }
  .muted { color: var(--text-muted); }

  .ca-badge { border-radius: 4px; padding: 2px 7px; font-size: 11px; font-weight: 600;
              white-space: nowrap; }

  .ca-loading { text-align: center; color: var(--text-muted); padding: 32px; font-size: 13px; }
</style>

<div class="ca">

  <!-- ① KPIs -->
  <div class="ca-section">
    <div class="ca-title">Key Metrics</div>
    <div class="ca-kpis" id="ca-kpis"><div class="ca-loading">Loading…</div></div>
  </div>

  <!-- ② Segments -->
  <div class="ca-section">
    <div class="ca-title">Customer Segments
      <span class="ca-note">— segment counts are current; revenue is for the selected range</span>
    </div>
    <div class="ca-grid">
      <div class="ca-box">
        <h6>Segment Distribution (Current)</h6>
        <div id="ca-chart-seg-dist"></div>
      </div>
      <div class="ca-box">
        <h6>Revenue by Segment (Selected Range)</h6>
        <div id="ca-chart-seg-rev"></div>
      </div>
      <div class="ca-box span2">
        <h6>Segment Detail</h6>
        <div id="ca-table-seg"></div>
      </div>
    </div>
  </div>

  <!-- ③ Top Customers -->
  <div class="ca-section">
    <div class="ca-title">Top Customers (by Revenue in Range)</div>
    <div class="ca-grid one">
      <div class="ca-box">
        <h6>Highest-Value Customers</h6>
        <div id="ca-table-top"></div>
      </div>
    </div>
  </div>

  <!-- ④ Needs Attention -->
  <div class="ca-section">
    <div class="ca-title">Needs Attention — At Risk &amp; Can't-Lose Customers</div>
    <div class="ca-grid one">
      <div class="ca-box">
        <h6>Win-Back List (Current Snapshot)</h6>
        <div id="ca-table-risk"></div>
      </div>
    </div>
  </div>

  <!-- ⑤ Acquisition -->
  <div class="ca-section">
    <div class="ca-title">New Customer Acquisition</div>
    <div class="ca-grid one">
      <div class="ca-box">
        <h6>First-Time Customers per Day</h6>
        <div id="ca-chart-acq"></div>
      </div>
    </div>
  </div>

</div>`);
	}

	// ── Data loading ─────────────────────────────────────────────────────────

	refresh() {
		$('#ca-kpis').html('<div class="ca-loading">Loading…</div>');

		frappe.call({
			method: 'jarz_pos.api.customer_analytics.get_customer_analytics',
			args: { date_from: this.from_date, date_to: this.to_date },
		}).then(r => {
			if (!r.message) return;
			let d = r.message;
			this._render_kpis(d.summary);
			this._render_segments(d.segment_distribution || [], d.segment_table || []);
			this._render_top(d.top_customers || []);
			this._render_risk(d.at_risk_customers || []);
			this._render_acquisition(d.acquisition_trend || []);
		});
	}

	// ── Renderers ────────────────────────────────────────────────────────────

	_render_kpis(s) {
		let repeat_cls = s.repeat_rate >= 40 ? 'pos' : s.repeat_rate >= 20 ? 'info' : 'warn';
		let risk_cls = s.at_risk > 0 ? 'warn' : 'pos';

		$('#ca-kpis').html(`
			${_kpi('Total Customers',   (s.total_customers || 0).toLocaleString(), '')}
			${_kpi('Active (Range)',    (s.active_in_period || 0).toLocaleString(), 'info')}
			${_kpi('New (Range)',       (s.new_customers || 0).toLocaleString(), 'pos')}
			${_kpi('Repeat Rate',       (s.repeat_rate || 0) + '%', repeat_cls)}
			${_kpi('Champions',         (s.champions || 0).toLocaleString(), 'pos')}
			${_kpi('At Risk / Can\'t Lose', (s.at_risk || 0).toLocaleString(), risk_cls)}
			${_kpi('Lost',              (s.lost || 0).toLocaleString(), 'neg')}
			${_kpi('Avg Order Value',   _egp(s.avg_order_value), 'info')}
		`);
	}

	_render_segments(dist, table) {
		if (dist.length) {
			_chart('ca-chart-seg-dist', {
				type: 'donut',
				height: 280,
				data: {
					labels: dist.map(r => r.segment),
					datasets: [{ values: dist.map(r => r.count) }],
				},
				colors: dist.map(r => _seg_color(r.segment)),
			});
		} else {
			$('#ca-chart-seg-dist').html('<div class="ca-loading">No segmented customers yet.</div>');
		}

		let with_rev = table.filter(r => r.revenue > 0);
		if (with_rev.length) {
			_chart('ca-chart-seg-rev', {
				type: 'bar',
				height: 280,
				data: {
					labels: with_rev.map(r => r.segment),
					datasets: [{ name: 'Revenue', values: with_rev.map(r => r.revenue) }],
				},
				colors: with_rev.map(r => _seg_color(r.segment)),
				tooltipOptions: { formatTooltipY: v => _egp(v) },
			});
		} else {
			$('#ca-chart-seg-rev').html('<div class="ca-loading">No revenue in range.</div>');
		}

		if (!table.length) {
			$('#ca-table-seg').html('<div class="ca-loading">No customer data.</div>');
			return;
		}

		$('#ca-table-seg').html(`
			<table class="ca-table">
				<thead><tr>
					<th>Segment</th>
					<th>Customers</th>
					<th>Active (Range)</th>
					<th>Orders (Range)</th>
					<th>Revenue (Range)</th>
					<th>Avg Recency</th>
					<th>Avg Freq (90d)</th>
					<th>Avg Order Value</th>
				</tr></thead>
				<tbody>
					${table.map(r => `<tr>
						<td><span class="ca-badge" style="background:${_seg_bg(r.segment)};color:${_seg_color(r.segment)}">${r.segment}</span></td>
						<td>${(r.customers || 0).toLocaleString()}</td>
						<td>${(r.active_customers || 0).toLocaleString()}</td>
						<td>${(r.orders || 0).toLocaleString()}</td>
						<td>${r.revenue > 0 ? _egp(r.revenue) : '—'}</td>
						<td class="muted">${r.avg_recency || 0}d</td>
						<td class="muted">${r.avg_frequency || 0}</td>
						<td>${r.avg_aov > 0 ? _egp(r.avg_aov) : '—'}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_top(rows) {
		if (!rows.length) {
			$('#ca-table-top').html('<div class="ca-loading">No customer orders in range.</div>');
			return;
		}
		$('#ca-table-top').html(`
			<table class="ca-table">
				<thead><tr>
					<th>#</th>
					<th>Customer</th>
					<th>Segment</th>
					<th>Orders</th>
					<th>Revenue</th>
					<th>Last Order</th>
				</tr></thead>
				<tbody>
					${rows.map((r, i) => `<tr>
						<td class="muted">${i + 1}</td>
						<td><a href="/app/customer/${encodeURIComponent(r.customer)}" target="_blank">${r.customer_name || r.customer}</a></td>
						<td><span class="ca-badge" style="background:${_seg_bg(r.segment)};color:${_seg_color(r.segment)}">${r.segment}</span></td>
						<td>${(r.orders || 0).toLocaleString()}</td>
						<td class="green">${_egp(r.revenue)}</td>
						<td class="muted">${r.recency_days}d ago</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_risk(rows) {
		if (!rows.length) {
			$('#ca-table-risk').html('<div class="ca-loading">✅ No at-risk customers right now.</div>');
			return;
		}
		$('#ca-table-risk').html(`
			<table class="ca-table">
				<thead><tr>
					<th>Customer</th>
					<th>Segment</th>
					<th>Territory</th>
					<th>Days Since Order</th>
					<th>Freq (90d)</th>
					<th>Avg Order Value</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `<tr>
						<td><a href="/app/customer/${encodeURIComponent(r.customer)}" target="_blank">${r.customer_name || r.customer}</a></td>
						<td><span class="ca-badge" style="background:${_seg_bg(r.segment)};color:${_seg_color(r.segment)}">${r.segment}</span></td>
						<td class="muted">${r.territory || '—'}</td>
						<td class="red">${r.recency_days}d</td>
						<td>${r.frequency}</td>
						<td>${r.avg_aov > 0 ? _egp(r.avg_aov) : '—'}</td>
					</tr>`).join('')}
				</tbody>
			</table>`);
	}

	_render_acquisition(rows) {
		if (!rows.length) {
			$('#ca-chart-acq').html('<div class="ca-loading">No new customers in range.</div>');
			return;
		}
		_chart('ca-chart-acq', {
			type: 'bar',
			height: 280,
			data: {
				labels: rows.map(r => r.date),
				datasets: [{ name: 'New Customers', values: rows.map(r => r.new_customers) }],
			},
			colors: ['#27ae60'],
		});
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
	return `<div class="ca-kpi ${cls}">
		<div class="v">${value}</div>
		<div class="l">${label}</div>
	</div>`;
}

// Segment colour palette — text/accent colour.
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

// Soft background tint for badges (derived from the accent colour at low alpha).
function _seg_bg(segment) {
	let c = _seg_color(segment);
	// Expand #rrggbb to an rgba() with ~14% alpha.
	let r = parseInt(c.slice(1, 3), 16);
	let g = parseInt(c.slice(3, 5), 16);
	let b = parseInt(c.slice(5, 7), 16);
	return `rgba(${r},${g},${b},0.14)`;
}

function _chart(id, opts) {
	let el = document.getElementById(id);
	if (!el) return;
	el.innerHTML = '';
	try {
		new frappe.Chart(el, opts);
	} catch (e) {
		el.innerHTML = `<div class="ca-loading" style="color:#e74c3c">Chart error: ${e.message}</div>`;
	}
}
