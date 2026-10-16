frappe.listview_settings["Meta CAPI Event"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = {
			"Pending Approval": "orange", Held: "red", Approved: "blue", Rejected: "gray",
			Sent: "green", Failed: "red", Expired: "gray", Skipped: "gray",
		};
		return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
	},
	onload(listview) {
		if (!frappe.user.has_role(["Meta CAPI Manager", "System Manager"])) return;
		const method = "jarz_pos.services.meta_capi.";
		const picked = () => listview.get_checked_items().map((d) => d.name);
		const done = (r) => {
			const m = r.message || {};
			frappe.show_alert({ message: __("{0} done, {1} skipped (not waiting for a decision)",
				[m.approved ?? m.rejected ?? 0, m.skipped || 0]), indicator: "green" });
			listview.refresh();
		};
		listview.page.add_actions_menu_item(__("Approve"), () => {
			const names = picked();
			if (!names.length) return;
			frappe.call({ method: method + "approve_events", args: { names } }).then(done);
		});
		listview.page.add_actions_menu_item(__("Reject"), () => {
			const names = picked();
			if (!names.length) return;
			frappe.prompt({ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 }, (v) => {
				frappe.call({ method: method + "reject_events", args: { names, reason: v.reason } }).then(done);
			}, __("Reject {0} orders", [names.length]), __("Reject"));
		});
	},
};
