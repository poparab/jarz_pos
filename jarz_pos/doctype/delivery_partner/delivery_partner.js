// Copyright (c) 2025, Jarz Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Delivery Partner", {
	refresh(frm) {
		if (!frm.is_new()) {
			// Show unsettled balance summary
			frm.dashboard.clear_headline();
			frappe.call({
				method: "jarz_pos.api.delivery_partners.get_delivery_partner_balances",
				args: { delivery_partner: frm.doc.partner_name },
				callback(r) {
					if (r.message && r.message.length) {
						const data = r.message[0];
						const total = parseFloat(data.total_shipping_fee || 0).toFixed(2);
						const count = data.unsettled_count || 0;
						frm.dashboard.set_headline(
							__("Unsettled: {0} orders, Total Fee: {1}", [count, total])
						);
					} else {
						frm.dashboard.set_headline(__("No unsettled orders"));
					}
				},
			});

			// Add Settle Partner button
			frm.add_custom_button(
				__("Settle Partner"),
				function () {
					_show_settlement_dialog(frm);
				},
				__("Actions")
			);

			// Add View Unsettled Details button
			frm.add_custom_button(
				__("View Unsettled Orders"),
				function () {
					_show_unsettled_details(frm);
				},
				__("Actions")
			);
		}
	},
});

function _show_settlement_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Settle Delivery Partner: {0}", [frm.doc.partner_name]),
		fields: [
			{
				fieldtype: "Link",
				fieldname: "bank_account",
				label: __("Bank Account"),
				options: "Bank Account",
				default: frm.doc.bank_account || "",
				description: __(
					"Bank account to credit. Leave blank to use partner default."
				),
			},
			{
				fieldtype: "Section Break",
			},
			{
				fieldtype: "HTML",
				fieldname: "preview_html",
			},
		],
		primary_action_label: __("Confirm Settlement"),
		primary_action(values) {
			d.disable_primary_action();
			frappe.call({
				method: "jarz_pos.api.delivery_partners.settle_delivery_partner",
				args: {
					delivery_partner: frm.doc.partner_name,
					bank_account: values.bank_account || null,
				},
				callback(r) {
					d.enable_primary_action();
					if (r.message && r.message.journal_entry) {
						d.hide();
						frappe.show_alert({
							message: __(
								"Settlement complete. Journal Entry: {0}",
								[r.message.journal_entry]
							),
							indicator: "green",
						});
						frm.reload_doc();
					} else if (r.message && r.message.message) {
						frappe.msgprint(r.message.message);
					}
				},
				error() {
					d.enable_primary_action();
				},
			});
		},
	});

	// Load preview into dialog
	frappe.call({
		method: "jarz_pos.api.delivery_partners.get_delivery_partner_unsettled_details",
		args: { delivery_partner: frm.doc.partner_name },
		callback(r) {
			if (r.message && r.message.length) {
				let html =
					'<table class="table table-bordered table-sm"><thead><tr>' +
					"<th>" + __("Invoice") + "</th>" +
					"<th>" + __("Order Amount") + "</th>" +
					"<th>" + __("Partner Fee") + "</th>" +
					"<th>" + __("Date") + "</th>" +
					"</tr></thead><tbody>";
				let totalFee = 0;
				r.message.forEach(function (row) {
					const fee = parseFloat(row.shipping_amount || 0);
					totalFee += fee;
					html +=
						"<tr>" +
						"<td>" + frappe.utils.escape_html(row.invoice || "") + "</td>" +
						"<td>" + parseFloat(row.order_amount || 0).toFixed(2) + "</td>" +
						"<td>" + fee.toFixed(2) + "</td>" +
						"<td>" + frappe.utils.escape_html(row.creation || "").split(" ")[0] + "</td>" +
						"</tr>";
				});
				html +=
					"</tbody><tfoot><tr>" +
					'<td colspan="2"><strong>' + __("Total") + "</strong></td>" +
					"<td><strong>" + totalFee.toFixed(2) + "</strong></td>" +
					"<td></td>" +
					"</tr></tfoot></table>";
				d.fields_dict.preview_html.$wrapper.html(html);
			} else {
				d.fields_dict.preview_html.$wrapper.html(
					'<p class="text-muted">' +
						__("No unsettled orders for this partner.") +
						"</p>"
				);
				d.disable_primary_action();
			}
		},
	});

	d.show();
}

function _show_unsettled_details(frm) {
	frappe.call({
		method: "jarz_pos.api.delivery_partners.get_delivery_partner_unsettled_details",
		args: { delivery_partner: frm.doc.partner_name },
		callback(r) {
			if (r.message && r.message.length) {
				let html =
					'<table class="table table-bordered table-sm"><thead><tr>' +
					"<th>" + __("Invoice") + "</th>" +
					"<th>" + __("Courier") + "</th>" +
					"<th>" + __("Order Amount") + "</th>" +
					"<th>" + __("Partner Fee") + "</th>" +
					"<th>" + __("Date") + "</th>" +
					"</tr></thead><tbody>";
				let totalFee = 0;
				let totalOrder = 0;
				r.message.forEach(function (row) {
					const fee = parseFloat(row.shipping_amount || 0);
					const order = parseFloat(row.order_amount || 0);
					totalFee += fee;
					totalOrder += order;
					html +=
						"<tr>" +
						"<td>" + frappe.utils.escape_html(row.invoice || "") + "</td>" +
						"<td>" + frappe.utils.escape_html(row.party_name || row.party || "") + "</td>" +
						"<td>" + order.toFixed(2) + "</td>" +
						"<td>" + fee.toFixed(2) + "</td>" +
						"<td>" + frappe.utils.escape_html(row.creation || "").split(" ")[0] + "</td>" +
						"</tr>";
				});
				html +=
					"</tbody><tfoot><tr>" +
					'<td colspan="2"><strong>' + __("Total") + "</strong></td>" +
					"<td><strong>" + totalOrder.toFixed(2) + "</strong></td>" +
					"<td><strong>" + totalFee.toFixed(2) + "</strong></td>" +
					"<td></td>" +
					"</tr></tfoot></table>";
				frappe.msgprint({
					title: __("Unsettled Orders — {0}", [frm.doc.partner_name]),
					message: html,
					wide: true,
				});
			} else {
				frappe.msgprint(__("No unsettled orders for this partner."));
			}
		},
	});
}
