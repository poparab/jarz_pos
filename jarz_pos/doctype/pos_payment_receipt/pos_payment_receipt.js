// Copyright (c) 2025, Jarz and contributors
// For license information, please see license.txt

frappe.ui.form.on("POS Payment Receipt", {
	refresh(frm) {
		// Add custom buttons or actions if needed
		if (frm.doc.status === "Unconfirmed" && frm.doc.receipt_image) {
			frm.add_custom_button(__("Confirm Receipt"), function() {
				frm.set_value("status", "Confirmed");
				frm.save();
			});
		}
	},
});
