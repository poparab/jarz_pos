(function () {
	const getExistingField = (frm, candidates) => {
		for (const fieldname of candidates) {
			if (frm.fields_dict && frm.fields_dict[fieldname]) {
				return fieldname;
			}
		}
		return null;
	};

	const getSelectOptions = (frm, fieldname) => {
		if (!fieldname || !frm.fields_dict || !frm.fields_dict[fieldname]) {
			return "";
		}

		const options = frm.fields_dict[fieldname].df.options || "";
		return String(options)
			.split("\n")
			.map((value) => value.trim())
			.filter(Boolean)
			.join("\n");
	};

	const openCancelledInvoiceDialog = (frm) => {
		const stateField = getExistingField(frm, ["custom_sales_invoice_state", "sales_invoice_state"]);
		const acceptanceField = getExistingField(frm, ["custom_acceptance_status"]);

		const fields = [];
		if (stateField) {
			fields.push({
				fieldname: "sales_invoice_state",
				fieldtype: "Select",
				label: __(frm.fields_dict[stateField].df.label || "Sales Invoice State"),
				options: getSelectOptions(frm, stateField),
				default: frm.doc[stateField] || "",
			});
		}

		if (acceptanceField) {
			fields.push({
				fieldname: "acceptance_status",
				fieldtype: "Select",
				label: __(frm.fields_dict[acceptanceField].df.label || "Acceptance Status"),
				options: getSelectOptions(frm, acceptanceField),
				default: frm.doc[acceptanceField] || "",
			});
		}

		if (!fields.length) {
			frappe.msgprint(__("No editable cancelled-invoice fields were found on this form."));
			return;
		}

		const dialog = new frappe.ui.Dialog({
			title: __("Update Cancelled Invoice Fields"),
			fields,
			primary_action_label: __("Update"),
			primary_action(values) {
				frappe.call({
					method: "jarz_pos.api.manager.update_cancelled_invoice_status_fields",
					args: {
						invoice_id: frm.doc.name,
						sales_invoice_state: values.sales_invoice_state,
						acceptance_status: values.acceptance_status,
					},
					freeze: true,
					freeze_message: __("Updating cancelled invoice..."),
					callback(response) {
						if (!response || response.exc) {
							return;
						}

						const message = response.message || {};
						if (!message.success) {
							frappe.msgprint(message.error || __("Unable to update the cancelled invoice."));
							return;
						}

						dialog.hide();
						frappe.show_alert({
							message: message.no_change
								? __("No changes were needed.")
								: __("Cancelled invoice fields updated."),
							indicator: message.no_change ? "orange" : "green",
						});
						frm.reload_doc();
					},
				});
			},
		});

		dialog.show();
	};

	frappe.ui.form.on("Sales Invoice", {
		refresh(frm) {
			if (!frm.doc || Number(frm.doc.docstatus || 0) !== 2) {
				return;
			}

			const stateField = getExistingField(frm, ["custom_sales_invoice_state", "sales_invoice_state"]);
			const acceptanceField = getExistingField(frm, ["custom_acceptance_status"]);
			if (!stateField && !acceptanceField) {
				return;
			}

			frm.add_custom_button(__("Update Cancelled Fields"), () => {
				openCancelledInvoiceDialog(frm);
			}, __("Actions"));
		},
	});
})();