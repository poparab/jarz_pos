// Copyright (c) 2025, Jarz Technologies and contributors
// For license information, please see license.txt

// The weekly partner run. Two things make this screen more than a "settle all"
// button: the partner sends their own invoice and it does not always agree with
// ours, so each trip is ticked individually; and that invoice carries fixed charges
// (subscription, waiting time, a returned trip) that were never accrued per order
// and have to be added here so the payment matches what they billed.

frappe.ui.form.on("Delivery Partner", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.dashboard.clear_headline();
		frappe.call({
			method: "jarz_pos.api.delivery_partners.get_delivery_partner_balances",
			args: { delivery_partner: frm.doc.name },
			callback(r) {
				const data = (r.message || [])[0];
				if (data && data.order_count) {
					frm.dashboard.set_headline(
						__("Unbilled: {0} trips, fees {1}", [
							data.order_count,
							format_currency(data.total_fee || 0),
						])
					);
				} else {
					frm.dashboard.set_headline(__("Nothing outstanding"));
				}
			},
		});

		frm.add_custom_button(__("Settle Partner"), () => _settlement_dialog(frm), __("Actions"));
		frm.add_custom_button(__("View Unbilled Trips"), () => _unbilled_dialog(frm), __("Actions"));
	},
});

function _fetch_unbilled(frm) {
	return frappe.call({
		method: "jarz_pos.api.delivery_partners.get_delivery_partner_unsettled_details",
		args: { delivery_partner: frm.doc.name },
	});
}

function _trips_table_html(rows) {
	if (!rows.length) return `<p>${__("No unbilled trips.")}</p>`;
	let total = 0;
	let html =
		'<table class="table table-bordered table-sm"><thead><tr>' +
		`<th>${__("Order")}</th><th class="text-right">${__("Order Amount")}</th>` +
		`<th class="text-right">${__("Partner Fee")}</th><th>${__("Date")}</th>` +
		"</tr></thead><tbody>";
	rows.forEach((row) => {
		const fee = parseFloat(row.fee || 0);
		total += fee;
		html +=
			"<tr><td>" +
			frappe.utils.escape_html(row.invoice || "") +
			'</td><td class="text-right">' +
			format_currency(parseFloat(row.amount || 0)) +
			'</td><td class="text-right">' +
			format_currency(fee) +
			"</td><td>" +
			frappe.datetime.str_to_user(row.date) +
			"</td></tr>";
	});
	html +=
		`</tbody><tfoot><tr><th colspan="2">${__("Total")}</th>` +
		`<th class="text-right">${format_currency(total)}</th><th></th></tr></tfoot></table>`;
	return html;
}

function _unbilled_dialog(frm) {
	_fetch_unbilled(frm).then((r) => {
		const rows = r.message || [];
		new frappe.ui.Dialog({
			title: __("Unbilled Trips: {0}", [frm.doc.partner_name || frm.doc.name]),
			size: "large",
			fields: [{ fieldtype: "HTML", fieldname: "body", options: _trips_table_html(rows) }],
		}).show();
	});
}

function _settlement_dialog(frm) {
	_fetch_unbilled(frm).then((r) => {
		const rows = r.message || [];

		const d = new frappe.ui.Dialog({
			title: __("Settle Delivery Partner: {0}", [frm.doc.partner_name || frm.doc.name]),
			size: "large",
			fields: [
				{
					fieldtype: "Link",
					fieldname: "bank_account",
					label: __("Pay From"),
					options: "Account",
					default: "",
					get_query: () => ({ filters: { is_group: 0, account_type: "Bank" } }),
					description: __("Leave blank to use the partner's own bank account, then the company default."),
				},
				{ fieldtype: "Section Break", label: __("Trips on this invoice") },
				{
					fieldtype: "HTML",
					fieldname: "trips_help",
					options: `<p class="text-muted">${__(
						"Tick only the trips the partner actually billed. Anything you leave out stays unbilled and appears again next week."
					)}</p>`,
				},
				{
					fieldtype: "Table",
					fieldname: "trips",
					cannot_add_rows: true,
					cannot_delete_rows: true,
					in_place_edit: true,
					data: rows.map((row) => ({
						__checked: 1,
						courier_transaction: row.name,
						invoice: row.invoice,
						fee: parseFloat(row.fee || 0),
					})),
					get_data: () => d.get_value("trips"),
					fields: [
						{
							fieldtype: "Check",
							fieldname: "__checked",
							label: __("Pay"),
							in_list_view: 1,
							columns: 1,
						},
						{
							fieldtype: "Data",
							fieldname: "invoice",
							label: __("Order"),
							read_only: 1,
							in_list_view: 1,
							columns: 5,
						},
						{
							fieldtype: "Currency",
							fieldname: "fee",
							label: __("Fee"),
							read_only: 1,
							in_list_view: 1,
							columns: 3,
						},
						{ fieldtype: "Data", fieldname: "courier_transaction", label: __("CT") },
					],
				},
				{ fieldtype: "Section Break", label: __("Fixed charges") },
				{
					fieldtype: "HTML",
					fieldname: "charges_help",
					options: `<p class="text-muted">${__(
						"Anything on the partner's invoice that is not a per-order fee — subscription, waiting time, returned trips."
					)}</p>`,
				},
				{
					fieldtype: "Table",
					fieldname: "extra_charges",
					data: [],
					get_data: () => d.get_value("extra_charges"),
					fields: [
						{
							fieldtype: "Data",
							fieldname: "description",
							label: __("Description"),
							in_list_view: 1,
							columns: 6,
						},
						{
							fieldtype: "Currency",
							fieldname: "amount",
							label: __("Amount"),
							in_list_view: 1,
							columns: 3,
						},
					],
				},
				{ fieldtype: "Section Break" },
				{ fieldtype: "HTML", fieldname: "total_html" },
			],
			primary_action_label: __("Confirm Payment"),
			primary_action() {
				const picked = (d.get_value("trips") || []).filter((t) => t.__checked);
				const charges = (d.get_value("extra_charges") || [])
					.filter((c) => flt(c.amount) !== 0)
					.map((c) => ({ description: c.description || __("Fixed charge"), amount: flt(c.amount) }));

				if (!picked.length && !charges.length) {
					frappe.msgprint(__("Select at least one trip, or add a fixed charge."));
					return;
				}

				d.disable_primary_action();
				frappe.call({
					method: "jarz_pos.api.delivery_partners.settle_delivery_partner",
					args: {
						delivery_partner: frm.doc.name,
						bank_account: d.get_value("bank_account") || null,
						courier_transactions: picked.map((t) => t.courier_transaction),
						extra_charges: charges,
					},
					callback(res) {
						d.enable_primary_action();
						const m = res.message || {};
						if (m.journal_entry) {
							d.hide();
							frappe.show_alert(
								{
									message: __("Paid {0}. Journal Entry: {1}", [
										format_currency(m.total_paid || 0),
										m.journal_entry,
									]),
									indicator: "green",
								},
								10
							);
							frm.reload_doc();
						} else {
							frappe.msgprint(m.message || __("Nothing was settled."));
						}
					},
					error() {
						d.enable_primary_action();
					},
				});
			},
		});

		// Keep the running total honest as trips are ticked and charges typed —
		// this figure is what gets compared against the partner's invoice.
		const refresh_total = () => {
			const fees = (d.get_value("trips") || [])
				.filter((t) => t.__checked)
				.reduce((sum, t) => sum + flt(t.fee), 0);
			const extra = (d.get_value("extra_charges") || []).reduce((sum, c) => sum + flt(c.amount), 0);
			d.get_field("total_html").$wrapper.html(
				`<table class="table table-bordered table-sm"><tbody>
					<tr><td>${__("Trip fees")}</td><td class="text-right">${format_currency(fees)}</td></tr>
					<tr><td>${__("Fixed charges")}</td><td class="text-right">${format_currency(extra)}</td></tr>
					<tr><th>${__("Total to pay")}</th><th class="text-right">${format_currency(fees + extra)}</th></tr>
				</tbody></table>`
			);
		};
		d.fields_dict.trips.grid.wrapper.on("change", refresh_total);
		d.fields_dict.extra_charges.grid.wrapper.on("change", refresh_total);
		refresh_total();
		d.show();
	});
}
