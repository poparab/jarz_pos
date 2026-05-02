(function () {
	const isNewDoc = (frm) => {
		if (typeof frm.is_new === "function") {
			return frm.is_new();
		}
		return Boolean(frm.doc && frm.doc.__islocal);
	};

	const trimFractionalSeconds = (value) => {
		if (typeof value !== "string") {
			return value;
		}
		return value.split(".")[0];
	};

	const normalizeNewDraftTimestamps = (frm) => {
		if (!frm.doc || frm.doc.docstatus !== 0 || !isNewDoc(frm)) {
			return;
		}

		const periodEndDate = trimFractionalSeconds(frm.doc.period_end_date);
		const postingTime = trimFractionalSeconds(frm.doc.posting_time);

		if (frm.doc.period_end_date !== periodEndDate) {
			frm.doc.period_end_date = periodEndDate;
			frm.refresh_field("period_end_date");
		}

		if (frm.doc.posting_time !== postingTime) {
			frm.doc.posting_time = postingTime;
			frm.refresh_field("posting_time");
		}
	};

	const syncExistingDraftTimestamps = async (frm) => {
		if (!frm.doc || frm.doc.docstatus !== 0 || frm.doc.amended_from || isNewDoc(frm) || !frm.doc.name) {
			return;
		}

		if (frm.__jarzClosingTimestampSyncInFlight || frm.__jarzClosingTimestampSyncModified === frm.doc.modified) {
			return;
		}

		frm.__jarzClosingTimestampSyncInFlight = true;
		try {
			const serverDoc = await frappe.db.get_doc("POS Closing Entry", frm.doc.name);
			if (!serverDoc || serverDoc.name !== frm.doc.name) {
				return;
			}

			let changed = false;
			const serverPeriodEndDate = trimFractionalSeconds(serverDoc.period_end_date || frm.doc.period_end_date);
			const serverPostingTime = trimFractionalSeconds(serverDoc.posting_time || frm.doc.posting_time);

			if (frm.doc.period_end_date !== serverPeriodEndDate) {
				frm.doc.period_end_date = serverPeriodEndDate;
				changed = true;
			}

			if (frm.doc.posting_time !== serverPostingTime) {
				frm.doc.posting_time = serverPostingTime;
				changed = true;
			}

			if (changed) {
				frm.refresh_field("period_end_date");
				frm.refresh_field("posting_time");
				frm.doc.__unsaved = 0;
				frm.refresh_header();
			}

			frm.__jarzClosingTimestampSyncModified = serverDoc.modified;
		} finally {
			frm.__jarzClosingTimestampSyncInFlight = false;
		}
	};

	frappe.ui.form.on("POS Closing Entry", {
		onload(frm) {
			normalizeNewDraftTimestamps(frm);
			syncExistingDraftTimestamps(frm);
		},
		refresh(frm) {
			normalizeNewDraftTimestamps(frm);
			syncExistingDraftTimestamps(frm);
		},
	});
})();