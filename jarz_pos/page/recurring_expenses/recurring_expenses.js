/* jshint esversion: 9 */
/* globals frappe, __ */

frappe.pages['recurring-expenses'].on_page_load = function (wrapper) {
	let page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Recurring Expenses'),
		single_column: true,
	});
	let dash = new RecurringExpensesDashboard(page);
	$(wrapper).data('re_dash', dash);
};

frappe.pages['recurring-expenses'].on_page_show = function (wrapper) {
	let dash = $(wrapper).data('re_dash');
	if (dash) dash.refresh();
};

// ─────────────────────────────────────────────────────────────────────────────

class RecurringExpensesDashboard {
	constructor(page) {
		this.page = page;
		this.month = frappe.datetime.nowdate().slice(0, 7);
		this._setup_controls();
		this._inject_html();
		this.refresh();
	}

	// ── Controls ─────────────────────────────────────────────────────────────

	_setup_controls() {
		let me = this;

		this.page.add_field({
			fieldtype: 'Data',
			fieldname: 'month',
			label: __('Month (YYYY-MM)'),
			change() {
				let v = me.page.fields_dict.month.get_value();
				if (v && /^\d{4}-\d{2}$/.test(v)) {
					me.month = v;
					me.refresh();
				}
			},
		});
		this.page.fields_dict.month.set_value(this.month);

		this.page.add_inner_button(__('Previous Month'), () => this._shift_month(-1));
		this.page.add_inner_button(__('This Month'), () => {
			this.month = frappe.datetime.nowdate().slice(0, 7);
			this.page.fields_dict.month.set_value(this.month);
			this.refresh();
		});
		this.page.add_inner_button(__('Next Month'), () => this._shift_month(1));

		this.page.set_primary_action(__('New Recurring Expense'), () => {
			frappe.new_doc('Jarz Recurring Expense');
		});

		this.page.add_menu_item(__('Manage All Recurring Expenses'), () => {
			frappe.set_route('List', 'Jarz Recurring Expense');
		});
		this.page.add_menu_item(__('Set Up Payroll (Salary Structure)'), () => {
			frappe.set_route('List', 'Salary Structure');
		});
	}

	_shift_month(delta) {
		let [y, m] = this.month.split('-').map(Number);
		m += delta;
		while (m < 1) { m += 12; y -= 1; }
		while (m > 12) { m -= 12; y += 1; }
		this.month = `${y}-${String(m).padStart(2, '0')}`;
		this.page.fields_dict.month.set_value(this.month);
		this.refresh();
	}

	// ── HTML skeleton ────────────────────────────────────────────────────────

	_inject_html() {
		this.page.main.html(`
<style>
  .re { padding: 18px 20px 40px; background: var(--bg-color); }
  .re-section { margin-bottom: 30px; }
  .re-title { font-size: 13px; font-weight: 700; color: var(--text-muted);
              text-transform: uppercase; letter-spacing: .6px;
              margin-bottom: 14px; padding-bottom: 8px;
              border-bottom: 1px solid var(--border-color); }

  .re-kpis { display: flex; flex-wrap: wrap; gap: 12px; }
  .re-kpi  { flex: 1; min-width: 160px; background: var(--card-bg);
             border: 1px solid var(--border-color); border-radius: 8px;
             padding: 16px 18px; }
  .re-kpi .v { font-size: 23px; font-weight: 700; color: var(--text-color); }
  .re-kpi .l { font-size: 11px; color: var(--text-muted); margin-top: 3px;
               text-transform: uppercase; letter-spacing: .4px; }
  .re-kpi .s { font-size: 11px; color: var(--text-muted); margin-top: 6px; }
  .re-kpi.pos  .v { color: #27ae60; }
  .re-kpi.neg  .v { color: #e74c3c; }
  .re-kpi.info .v { color: #2980b9; }
  .re-kpi.warn .v { color: #e67e22; }

  .re-box  { background: var(--card-bg); border: 1px solid var(--border-color);
             border-radius: 8px; padding: 18px; overflow-x: auto; }

  .re-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .re-table th { text-align: left; padding: 7px 10px; font-size: 11px; font-weight: 700;
                 text-transform: uppercase; letter-spacing: .4px; white-space: nowrap;
                 color: var(--text-muted); background: var(--subtle-fg);
                 border-bottom: 1px solid var(--border-color); }
  .re-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-color); }
  .re-table tr:last-child td { border-bottom: none; }
  .re-table tr:hover td { background: var(--subtle-fg); }
  .re-table td.num, .re-table th.num { text-align: right; }
  .muted { color: var(--text-muted); }
  .strong { font-weight: 700; }

  .badge-re { display:inline-block; padding:2px 8px; border-radius:10px;
              font-size:11px; font-weight:600; white-space:nowrap; }
  .b-posted  { background:#e8f8f0; color:#1e8449; }
  .b-missing { background:#fdf2f2; color:#c0392b; }
  .b-partial { background:#fef9ec; color:#b9770e; }
  .b-over    { background:#eef3fd; color:#2471a3; }
  .b-notdue  { background:var(--subtle-fg); color:var(--text-muted); }
  .b-none    { background:var(--subtle-fg); color:var(--text-muted); }
  .b-unexpected { background:#f4ecf7; color:#6c3483; }

  .re-alert { display:flex; gap:10px; align-items:flex-start; padding:10px 14px;
              border-radius:6px; margin-bottom:8px; font-size:13px; line-height:1.5; }
  .re-alert.critical { background:#fdf2f2; border-left:3px solid #e74c3c; color:#922b21; }
  .re-alert.warning  { background:#fef9ec; border-left:3px solid #f39c12; color:#935116; }
  .re-alert.info     { background:#eaf4fb; border-left:3px solid #2e86c1; color:#1a5276; }
  .re-alert .ico     { font-size:15px; flex-shrink:0; margin-top:1px; }

  .re-bar-wrap { background: var(--subtle-fg); border-radius: 3px; height: 8px;
                 overflow: hidden; min-width: 80px; }
  .re-bar { height: 100%; background: #2980b9; border-radius: 3px; }

  .re-empty { text-align:center; color:var(--text-muted); padding:26px; font-size:13px; }
  .re-loading { text-align:center; color:var(--text-muted); padding:32px; font-size:13px; }
</style>

<div class="re">
  <div id="re-gaps"></div>

  <div class="re-section">
    <div class="re-title">${__('Monthly Run-Rate')}</div>
    <div class="re-kpis" id="re-kpis"><div class="re-loading">${__('Loading…')}</div></div>
  </div>

  <div class="re-section">
    <div class="re-title">${__('By Category')}</div>
    <div class="re-box" id="re-categories"></div>
  </div>

  <div class="re-section">
    <div class="re-title">${__('Salaries — live from HRMS payroll')}</div>
    <div class="re-box" id="re-payroll"></div>
  </div>

  <div class="re-section">
    <div class="re-title">${__('Registered Recurring Expenses')}</div>
    <div class="re-box" id="re-registry"></div>
  </div>

  <div class="re-section">
    <div class="re-title">${__('Detected — recurring spend not yet registered')}</div>
    <div class="re-box" id="re-detected"></div>
  </div>
</div>
		`);
	}

	// ── Data ─────────────────────────────────────────────────────────────────

	refresh() {
		let me = this;
		frappe.call({
			method: 'jarz_pos.api.recurring_expenses.get_recurring_expenses_overview',
			args: { month: this.month },
			callback(r) {
				if (!r || !r.message) return;
				me.data = r.message;
				me._render();
			},
			error() {
				me.page.main.find('#re-kpis').html(
					`<div class="re-empty">${__('Could not load recurring expenses.')}</div>`
				);
			},
		});
	}

	// ── Formatting helpers ───────────────────────────────────────────────────

	_fmt(v) {
		return format_currency(v || 0, this.data.currency);
	}

	_badge(status) {
		const map = {
			'Posted': ['b-posted', __('Posted')],
			'Missing': ['b-missing', __('Missing')],
			'Partial': ['b-partial', __('Partial')],
			'Over': ['b-over', __('Over')],
			'Not Due': ['b-notdue', __('Not due')],
			'None': ['b-none', __('—')],
			'Unexpected': ['b-unexpected', __('Unexpected')],
		};
		const [cls, label] = map[status] || ['b-none', frappe.utils.escape_html(status || '—')];
		return `<span class="badge-re ${cls}">${label}</span>`;
	}

	_esc(v) {
		return frappe.utils.escape_html(v == null ? '' : String(v));
	}

	// ── Render ───────────────────────────────────────────────────────────────

	_render() {
		this._render_gaps();
		this._render_kpis();
		this._render_categories();
		this._render_payroll();
		this._render_registry();
		this._render_detected();
	}

	_render_gaps() {
		const gaps = this.data.gaps || [];
		const icons = { critical: '⛔', warning: '⚠️', info: 'ℹ️' };
		this.page.main.find('#re-gaps').html(
			gaps.map(g => `
				<div class="re-alert ${this._esc(g.severity)}">
					<span class="ico">${icons[g.severity] || 'ℹ️'}</span>
					<span>${this._esc(g.message)}</span>
				</div>`).join('')
		);
	}

	_render_kpis() {
		const s = this.data.summary || {};
		const variance = s.variance || 0;
		const varianceClass = Math.abs(variance) < 0.005 ? '' : (variance < 0 ? 'neg' : 'warn');

		this.page.main.find('#re-kpis').html(`
			<div class="re-kpi info">
				<div class="v">${this._fmt(s.total_monthly_runrate)}</div>
				<div class="l">${__('Total Monthly Run-Rate')}</div>
				<div class="s">${__('Salaries')} ${this._fmt(s.payroll_monthly)} · ${__('Other')} ${this._fmt(s.registry_monthly)}</div>
			</div>
			<div class="re-kpi">
				<div class="v">${this._fmt(s.expected_this_month)}</div>
				<div class="l">${__('Expected in')} ${this._esc(this.data.month)}</div>
				<div class="s">${s.active_count || 0} ${__('active registered item(s)')}</div>
			</div>
			<div class="re-kpi pos">
				<div class="v">${this._fmt(s.posted_this_month)}</div>
				<div class="l">${__('Actually Posted to GL')}</div>
				<div class="s">${s.items_posted || 0} ${__('matched')} · ${s.items_missing || 0} ${__('missing')}</div>
			</div>
			<div class="re-kpi ${varianceClass}">
				<div class="v">${this._fmt(variance)}</div>
				<div class="l">${__('Variance (Posted − Expected)')}</div>
				<div class="s">${variance < 0 ? __('Under-posted') : (variance > 0 ? __('Over-posted') : __('Balanced'))}</div>
			</div>
		`);
	}

	_render_categories() {
		const cats = this.data.by_category || [];
		if (!cats.length) {
			this.page.main.find('#re-categories').html(
				`<div class="re-empty">${__('Nothing registered yet.')}</div>`);
			return;
		}
		const max = Math.max(...cats.map(c => c.monthly || 0), 1);
		const total = cats.reduce((a, c) => a + (c.monthly || 0), 0) || 1;

		this.page.main.find('#re-categories').html(`
			<table class="re-table">
				<thead><tr>
					<th>${__('Category')}</th><th class="num">${__('Items')}</th>
					<th class="num">${__('Monthly')}</th><th class="num">${__('Share')}</th>
					<th style="width:22%">&nbsp;</th>
				</tr></thead>
				<tbody>
					${cats.map(c => `
						<tr>
							<td>${this._esc(c.category)}${c.source === 'HRMS'
								? ` <span class="badge-re b-over">${__('HRMS')}</span>` : ''}</td>
							<td class="num">${c.count || 0}</td>
							<td class="num strong">${this._fmt(c.monthly)}</td>
							<td class="num muted">${((c.monthly || 0) / total * 100).toFixed(1)}%</td>
							<td><div class="re-bar-wrap">
								<div class="re-bar" style="width:${((c.monthly || 0) / max * 100).toFixed(1)}%"></div>
							</div></td>
						</tr>`).join('')}
				</tbody>
			</table>
		`);
	}

	_render_payroll() {
		const p = this.data.payroll || {};
		const rows = p.rows || [];
		const missing = p.missing || [];

		let head = `
			<div style="margin-bottom:14px; font-size:13px;">
				<span class="strong">${this._fmt(p.monthly_total)}</span>
				<span class="muted"> ${__('monthly across')} ${p.employees_with_structure || 0} ${__('of')} ${p.employees_total || 0} ${__('active employees')}</span>
				&nbsp;${this._badge(p.status)}
				<span class="muted"> · ${__('posted to GL')} ${this._fmt(p.posted_this_month)}</span>
			</div>`;

		let body;
		if (!rows.length) {
			body = `<div class="re-empty">
				${__('No Salary Structure Assignments exist, so no salary is counted in the run-rate above.')}<br>
				<a href="/app/salary-structure">${__('Set up Salary Structures')}</a>
			</div>`;
		} else {
			body = `
			<table class="re-table">
				<thead><tr>
					<th>${__('Employee')}</th><th>${__('Designation')}</th>
					<th>${__('Salary Structure')}</th><th>${__('Effective')}</th>
					<th class="num">${__('Base')}</th><th class="num">${__('Variable')}</th>
					<th class="num">${__('Monthly')}</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `
						<tr>
							<td><a href="/app/employee/${encodeURIComponent(r.employee)}">${this._esc(r.employee_name || r.employee)}</a></td>
							<td class="muted">${this._esc(r.designation || '—')}</td>
							<td class="muted">${this._esc(r.salary_structure || '—')}</td>
							<td class="muted">${this._esc(r.from_date || '—')}</td>
							<td class="num">${this._fmt(r.base)}</td>
							<td class="num">${this._fmt(r.variable)}</td>
							<td class="num strong">${this._fmt(r.monthly)}</td>
						</tr>`).join('')}
				</tbody>
			</table>`;
		}

		let missingBlock = '';
		if (missing.length) {
			missingBlock = `
			<div style="margin-top:16px;">
				<div class="re-alert warning">
					<span class="ico">⚠️</span>
					<span>${missing.length} ${__('active employee(s) have no Salary Structure Assignment — their pay is missing from every figure on this page.')}</span>
				</div>
				<table class="re-table">
					<thead><tr>
						<th>${__('Employee')}</th><th>${__('Designation')}</th>
						<th>${__('Department')}</th><th>${__('Joined')}</th>
					</tr></thead>
					<tbody>
						${missing.map(m => `
							<tr>
								<td><a href="/app/employee/${encodeURIComponent(m.employee)}">${this._esc(m.employee_name || m.employee)}</a></td>
								<td class="muted">${this._esc(m.designation || '—')}</td>
								<td class="muted">${this._esc(m.department || '—')}</td>
								<td class="muted">${this._esc(m.date_of_joining || '—')}</td>
							</tr>`).join('')}
					</tbody>
				</table>
			</div>`;
		}

		this.page.main.find('#re-payroll').html(head + body + missingBlock);
	}

	_render_registry() {
		const rows = this.data.registry || [];
		if (!rows.length) {
			this.page.main.find('#re-registry').html(`
				<div class="re-empty">
					${__('No recurring expenses registered yet.')}<br>
					${__('Add rent, utilities, internet, subscriptions and service retainers so they appear in the run-rate.')}<br><br>
					<button class="btn btn-primary btn-sm" onclick="frappe.new_doc('Jarz Recurring Expense')">
						${__('Add the first one')}
					</button>
				</div>`);
			return;
		}

		this.page.main.find('#re-registry').html(`
			<table class="re-table">
				<thead><tr>
					<th>${__('Expense')}</th><th>${__('Category')}</th><th>${__('Supplier')}</th>
					<th>${__('Account')}</th><th>${__('Frequency')}</th><th class="num">${__('Amount')}</th>
					<th class="num">${__('Monthly Equiv.')}</th><th class="num">${__('Posted')}</th>
					<th>${__('Status')}</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `
						<tr${r.status !== 'Active' ? ' style="opacity:.55"' : ''}>
							<td>
								<a href="/app/jarz-recurring-expense/${encodeURIComponent(r.name)}">${this._esc(r.expense_name)}</a>
								${r.status !== 'Active' ? ` <span class="badge-re b-none">${this._esc(r.status)}</span>` : ''}
								${r.shared_account ? ` <span class="badge-re b-partial" title="${__('Several items post to this same account, so the posted figure cannot be split per item.')}">${__('shared acct')}</span>` : ''}
							</td>
							<td class="muted">${this._esc(r.category)}</td>
							<td class="muted">${this._esc(r.supplier || '—')}</td>
							<td class="muted">${this._esc(r.expense_account)}</td>
							<td class="muted">${this._esc(r.frequency)}</td>
							<td class="num">${this._fmt(r.amount)}</td>
							<td class="num strong">${this._fmt(r.monthly_equivalent)}</td>
							<td class="num">${r.due_this_month ? this._fmt(r.posted_amount) : '<span class="muted">—</span>'}</td>
							<td>${this._badge(r.gl_status)}</td>
						</tr>`).join('')}
				</tbody>
			</table>
			<div class="muted" style="margin-top:10px; font-size:12px;">
				${__('“Posted” is the net amount booked to that expense account during the month. Where several items share one account it is the account total, not a per-item match.')}
			</div>
		`);
	}

	_render_detected() {
		const rows = this.data.detected || [];
		if (!rows.length) {
			this.page.main.find('#re-detected').html(
				`<div class="re-empty">${__('No unregistered recurring spend detected in the last 12 months.')}</div>`);
			return;
		}

		this.page.main.find('#re-detected').html(`
			<div class="muted" style="margin-bottom:12px; font-size:12px;">
				${__('These expense accounts were posted to in 3 or more separate months but are not linked to any registered recurring expense. They are likely recurring costs worth registering.')}
			</div>
			<table class="re-table">
				<thead><tr>
					<th>${__('Account')}</th><th class="num">${__('Months Active')}</th>
					<th class="num">${__('Entries')}</th><th class="num">${__('Avg / Month')}</th>
					<th class="num">${__('Total (12m)')}</th><th>${__('Last Posted')}</th>
				</tr></thead>
				<tbody>
					${rows.map(r => `
						<tr>
							<td>${this._esc(r.account)}</td>
							<td class="num">${r.months_active}</td>
							<td class="num muted">${r.entries}</td>
							<td class="num strong">${this._fmt(r.average_monthly)}</td>
							<td class="num">${this._fmt(r.total)}</td>
							<td class="muted">${this._esc(r.last_month)}</td>
						</tr>`).join('')}
				</tbody>
			</table>
		`);
	}
}
