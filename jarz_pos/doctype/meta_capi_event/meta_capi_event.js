const META_CAPI_EVENT = "jarz_pos.services.meta_capi.";

frappe.ui.form.on("Meta CAPI Event", {
	refresh(frm) {
		if (!frappe.user.has_role(["Meta CAPI Manager", "System Manager"])) return;
		if (frm.doc.status === "Failed") {
			frm.add_custom_button(__("Retry"), () => {
				frappe
					.call({
						method: META_CAPI_EVENT + "retry_events",
						args: { names: [frm.doc.name] },
					})
					.then(() => frm.reload_doc());
			}).addClass("btn-primary");
			return;
		}
		if (!["Pending Approval", "Held"].includes(frm.doc.status)) return;
		frm.add_custom_button(__("Approve"), () => {
			frappe
				.call({
					method: META_CAPI_EVENT + "approve_events",
					args: { names: [frm.doc.name] },
				})
				.then(() => frm.reload_doc());
		}).addClass("btn-primary");
		frm.add_custom_button(__("Reject"), () => {
			frappe.prompt(
				{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
				(v) => {
					frappe
						.call({
							method: META_CAPI_EVENT + "reject_events",
							args: { names: [frm.doc.name], reason: v.reason },
						})
						.then(() => frm.reload_doc());
				},
				__("Reject this order"),
				__("Reject")
			);
		});
	},
});
