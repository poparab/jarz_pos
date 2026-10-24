"""Seed the Single settings added with the follow-up cycle and the RFM fix.

Singles do not receive the defaults of fields added after the site was
created, so ``Jarz Follow-up Settings`` would read empty and
``Jarz Segmentation Settings`` would lack the new Regular / scope fields.

Rules (re-runnable, never overwrites a value someone set):

* a field is written only when no value is stored for it yet;
* ``cant_lose_frequency_min`` moves 5 -> 4 only when it is still exactly 5
  (the old shipped default); any other stored value is a deliberate choice.
"""

import frappe

FOLLOWUP = "Jarz Follow-up Settings"
SEGMENTATION = "Jarz Segmentation Settings"

FOLLOWUP_DEFAULTS = {
	"enabled": 0,
	"start_date": "2026-10-15",
	"apology_discount_percent": 0,
	"apology_code_valid_days": 30,
	"quiet_start": "22:00:00",
	"quiet_end": "11:00:00",
	"call_window_start": "12:00:00",
	"call_window_end": "16:00:00",
	"tpl_msg_3h": "أهلاً {first_name}، معاك Jarz 🍰 اطلبك رقم {order_no} وصلك من شوية. يا رب يكون عجبك، ولو في أي حاجة مش مظبوطة قولنا وهنظبطها فوراً.",
	"tpl_msg_day32": "أهلاً {first_name}، وحشتنا! عدّى شهر على آخر طلب من Jarz. جاهزين نظبطلك جارز جديد وقت ما تحب 🍰",
	"tpl_msg_day50": "أهلاً {first_name}، من فترة مطلبتش من Jarz وحبينا نطمن عليك. {offer} نورنا في أي وقت 🍰",
	"tpl_call_3h": "ألو أهلاً {first_name}، معاك [اسمك] من Jarz. بس حبيت أطمن إن طلبك رقم {order_no} وصلك كويس وعجبك. في أي ملاحظة على الطعم أو التوصيل؟",
	"tpl_call_day2": "أهلاً {first_name}، معاك [اسمك] من Jarz. عدّى يومين على طلبك رقم {order_no}، حبيت أسمع رأيك: الطعم والتغليف والتوصيل كانوا عاملين إزاي؟",
	"tpl_call_day30": "أهلاً {first_name}، معاك [اسمك] من Jarz. بقالك شهر معانا وحبينا نطمن عليك. إيه أكتر نوع عجبك؟ ونحب نعرف لو حابب تجرب حاجة جديدة.",
	"tpl_call_day45": "أهلاً {first_name}، معاك [اسمك] من Jarz. وحشتنا طلباتك! {offer} تحب نظبطلك طلب جديد؟",
}

SEGMENTATION_DEFAULTS = {
	"website_only": 1,
	"regular_window_days": 60,
	"regular_frequency_min": 4,
	"high_value_order_min": 1000,
}


def _stored(doctype: str, field: str):
	"""The value stored in tabSingles, or None when no row exists."""
	rows = frappe.db.sql(
		"SELECT `value` FROM `tabSingles` WHERE doctype = %s AND field = %s",
		(doctype, field),
	)
	return rows[0][0] if rows else None


def _seed(doctype: str, defaults: dict) -> int:
	written = 0
	for field, value in defaults.items():
		if _stored(doctype, field) is None:
			frappe.db.set_single_value(doctype, field, value)
			written += 1
	return written


def execute():
	if frappe.db.exists("DocType", FOLLOWUP):
		_seed(FOLLOWUP, FOLLOWUP_DEFAULTS)
	if frappe.db.exists("DocType", SEGMENTATION):
		_seed(SEGMENTATION, SEGMENTATION_DEFAULTS)
		if str(_stored(SEGMENTATION, "cant_lose_frequency_min")) == "5":
			frappe.db.set_single_value(SEGMENTATION, "cant_lose_frequency_min", 4)
