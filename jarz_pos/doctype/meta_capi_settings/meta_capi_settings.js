const META_CAPI = "jarz_pos.services.meta_capi.";

function meta_capi_call(frm, method, args, busy) {
	return frappe
		.call({ method: META_CAPI + method, args: args || {}, freeze: true, freeze_message: busy })
		.then((r) => {
			const res = r.message || {};
			if (res.message) {
				frappe.msgprint({
					title: res.ok ? __("Done") : __("Not done"),
					message: res.message,
					indicator: res.ok ? "green" : "red",
				});
			} else {
				frappe.msgprint({
					title: __("Result"),
					message:
						"<pre>" +
						frappe.utils.escape_html(JSON.stringify(res, null, 2)) +
						"</pre>",
				});
			}
			frm.reload_doc();
		});
}

frappe.ui.form.on("Meta CAPI Settings", {
	refresh(frm) {
		frm.add_custom_button(
			__("Check Token"),
			() => meta_capi_call(frm, "check_token", {}, __("Asking Meta...")),
			__("Meta")
		);
		frm.add_custom_button(
			__("Send Test Event"),
			() => meta_capi_call(frm, "send_test_event", {}, __("Sending...")),
			__("Meta")
		);
		frm.add_custom_button(
			__("Queue Orders Now"),
			() => meta_capi_call(frm, "run_queue_now", {}, __("Queueing...")),
			__("Orders")
		);
		frm.add_custom_button(
			__("Queue Last 7 Days"),
			() => {
				frappe.confirm(
					__(
						"Queue delivered chat orders from the last 7 days, ignoring the start date?"
					),
					() =>
						meta_capi_call(
							frm,
							"run_queue_now",
							{ backfill_days: 7 },
							__("Queueing...")
						)
				);
			},
			__("Orders")
		);
		frm.add_custom_button(
			__("Send Approved Now"),
			() => meta_capi_call(frm, "run_send_now", {}, __("Sending...")),
			__("Orders")
		);
		frm.add_custom_button(
			__("Open Order Log"),
			() => frappe.set_route("List", "Meta CAPI Event"),
			__("Orders")
		);
		frm.set_intro(
			frm.doc.enabled
				? frm.doc.test_mode
					? __("On, in test mode: approved orders go to Test Events only.")
					: __("Live: approved orders are sent to Meta every hour.")
				: __("Off. Nothing is queued or sent."),
			frm.doc.enabled ? (frm.doc.test_mode ? "orange" : "green") : "red"
		);
	},
});
