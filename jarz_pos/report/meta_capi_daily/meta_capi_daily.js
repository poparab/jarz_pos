frappe.query_reports["Meta CAPI Daily"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -13),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: ["By day", "Skipped by reason"],
			default: "By day",
		},
		{
			fieldname: "source",
			label: __("Source"),
			fieldtype: "Select",
			options: ["", "Chat", "Website"],
		},
	],
};
