(function () {
	const trimFractionalSeconds = (value) => {
		if (typeof value !== "string") {
			return value;
		}
		return value.split(".")[0];
	};

	const patchClosePos = () => {
		const Controller = erpnext && erpnext.PointOfSale && erpnext.PointOfSale.Controller;
		if (!Controller || Controller.prototype.__jarzClosePosPatched) {
			return Boolean(Controller);
		}

		Controller.prototype.close_pos = function () {
			if (!this.$components_wrapper.is(":visible")) return;

			let voucher = frappe.model.get_new_doc("POS Closing Entry");
			voucher.pos_profile = this.frm.doc.pos_profile;
			voucher.user = frappe.session.user;
			voucher.company = this.frm.doc.company;
			voucher.pos_opening_entry = this.pos_opening;
			voucher.period_end_date = trimFractionalSeconds(frappe.datetime.now_datetime());
			voucher.posting_date = frappe.datetime.now_date();
			voucher.posting_time = trimFractionalSeconds(frappe.datetime.now_time());
			frappe.set_route("Form", "POS Closing Entry", voucher.name);
		};

		Controller.prototype.__jarzClosePosPatched = true;
		return true;
	};

	const ensureClosePosPatch = (attempt = 0) => {
		if (patchClosePos() || attempt >= 30) {
			return;
		}
		window.setTimeout(() => ensureClosePosPatch(attempt + 1), 100);
	};

	frappe.require("point-of-sale.bundle.js", () => ensureClosePosPatch());
	ensureClosePosPatch();
})();