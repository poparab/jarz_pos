"""Seed the generated B2B price list (after_migrate).

Creates ONE ``Jarz Sales Material`` of source type "Generated Price List", laid
out exactly like the owner's Canva PDF: eight flavours, Medium and Large, and
the seven Arabic terms.

Create-only, and deliberately coarse about what "already there" means: if ANY
generated material exists the seeder does nothing. Matching on the seed's title
would re-create it on every migrate the day a manager renames it, and a manager
who deleted it on purpose would see it come back.

Wired on ``after_migrate`` and every failure is swallowed (logged at ERROR so
the servers keep it): a raising seeder aborts the shared ``bench migrate`` for
every app on the site. Must import cleanly with no top-level frappe calls.
"""

from __future__ import annotations

import frappe

MATERIAL_DOCTYPE = "Jarz Sales Material"

SEED_TITLE = "Jarz B2B Price List"
SEED_TITLE_AR = "قائمة أسعار جارز"

#: (flavour stem, printed name), in the order the PDF lists them. The stem is
#: the Item-name stem: "Redvelvet Large", "Chocolate Hazelnut Medium".
SEED_ROWS = (
	("Strawberry", "Strawberry cheesecake jar"),
	("Blueberry", "Blueberry cheesecake jar"),
	("Chocolate Hazelnut", "Chocolate hazelnut cheesecake jar"),
	("Lotus", "Lotus cheesecake jar"),
	("Redvelvet", "Red velvet raspberry cheesecake jar"),
	("Pistachio", "Pistachio cheesecake jar"),
	("Tiramisu", "Tiramisu jar"),
	("Molten", "Molten jar"),
)

SEED_TERMS = (
	"الحد الأدنى للطلب: ١٢ جار",
	"تشكيل النكهات: يحق للعميل تشكيل النكهات بحرية داخل الـ ١٢ جار حسب المتاح",
	"الأسعار والضريبة: الأسعار غير شاملة الضريبة. عند طلب فاتورة ضريبية يتم إضافة ١٤٪ على إجمالي الفاتورة",
	"التسليم: التوصيل بواسطة جارز، ويلتزم العميل بفحص الكمية والأصناف وسلامة التغليف وقت الاستلام",
	"بلاغات النقص/الاختلاف/التلف الظاهر: يتم الإبلاغ خلال ٢٤ ساعة من الاستلام مع صور واضحة من المشكلة",
	"التخزين: يجب حفظ المنتج في الفريزر فور الاستلام، وصلاحية جميع المنتجات ثلاثة شهور في الفريزر وسبعة أيام في الثلاجة",
	"التشغيل: جميع المنتجات تقدم باردة بدرجة حرارة الثلاجة ماعدا المولتن، يوضع في الميكروويف لمدة ٢٠ إلى ٣٠ ثانية بدون غطاء ثم يقدم مباشرة",
)


def _logger():
	return frappe.logger("jarz_materials", allow_site=True)


def ensure_price_sheet_material() -> dict[str, str]:
	"""Idempotently create the seed material. Never raises."""
	result = {"status": "skipped"}
	try:
		if not frappe.db.exists("DocType", MATERIAL_DOCTYPE) or not frappe.db.has_column(
			MATERIAL_DOCTYPE, "source_type"
		):
			result["status"] = "skipped_no_doctype"
			return result

		if frappe.db.exists(MATERIAL_DOCTYPE, {"source_type": "Generated Price List"}):
			result["status"] = "exists"
			return result

		doc = frappe.get_doc(
			{
				"doctype": MATERIAL_DOCTYPE,
				"title": SEED_TITLE,
				"title_ar": SEED_TITLE_AR,
				"material_type": "Price List",
				"source_type": "Generated Price List",
				"enabled": 1,
				"is_default": 0,
				"sort_order": 0,
				"sheet_heading": "Price List",
				"sheet_sizes": "Medium,Large",
				"sheet_show_deals": 1,
				"sheet_footer": "More Than Just a Jar",
				"sheet_terms": "\n".join(SEED_TERMS),
				"sheet_rows": [
					{"flavour": flavour, "label": label, "enabled": 1} for flavour, label in SEED_ROWS
				],
			}
		)
		doc.insert(ignore_permissions=True)
		result["status"] = "seeded"
		_logger().error(f"Price sheet: seeded generated material {doc.name}")
	except Exception:
		result["status"] = "failed"
		try:
			_logger().error("ensure_price_sheet_material failed", exc_info=True)
		except Exception:
			pass
	return result
