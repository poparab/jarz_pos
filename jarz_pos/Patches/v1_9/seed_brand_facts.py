"""Seed Jarz Brand Fact with every fact the chat AI used to carry in code.

The AI's product, delivery, payment and storage claims were sentences inside
``chat_drafts.SYSTEM_PROMPT`` and the built-in ready answers. They are now rows
the owner can edit, approve or put on hold. This seeds the table once:

* Approved: what the prompt and the ready answers already said, plus the items
  the Approved Facts Registry (6 Oct 2026, with the 17 Oct additions) clears,
  each with its scope. Natural ingredients, no preservatives and less sugar carry
  the owner's approval of 2026-10-29.
* On Hold / Retired: what the registry holds back (allergens, diet, servings per
  jar, the Tiramisu and Molten Cake layer count, thawing ...), so the AI is told
  never to state them.

Skipped when the table already has a row, so an edit is never overwritten. The
topic names of the claims check (``chat_drafts.CLAIM_TOPICS``) and the
``DEFAULT_FACTS`` fallback are pinned to this list by a test.
"""

import frappe

DOCTYPE = "Jarz Brand Fact"

OWNER_SOURCE = "Owner approval in chat 2026-10-29"
REGISTRY = "Approved Facts Registry"
CODE_SOURCE = "Wording already in the chat drafts (system prompt / ready answers) before this table existed"
CODE_NOTE = "No approval date recorded for this wording: the owner should confirm it once."


def _approved(topic, category, ar, en, source, by="Abdelrahman", on=None, notes=""):
	return {
		"topic": topic,
		"category": category,
		"status": "Approved",
		"fact_ar": ar,
		"fact_en": en,
		"source": source,
		"approved_by": by,
		"approved_on": on,
		"notes": notes,
	}


def _held(topic, category, notes, source=REGISTRY, status="On Hold"):
	return {
		"topic": topic,
		"category": category,
		"status": status,
		"source": source,
		"notes": notes,
	}


def _owner(topic, ar, en, notes):
	return _approved(topic, "Ingredients and claims", ar, en, OWNER_SOURCE, "Owner", "2026-10-29", notes)


SEED = [
	# ── Approved ────────────────────────────────────────────────────────
	_approved(
		"Delivery time",
		"Delivery",
		"الأوردر بيوصل من 30 دقيقة لحد ساعتين بالكتير من تأكيده",
		"Delivery takes from 30 minutes to 2 hours at most, counted from order confirmation "
		"[never promise faster, never a fixed hour]",
		f"{REGISTRY} (3 Oct 2026); counted from order confirmation per the owner",
		on="2026-10-03",
		notes="One wording everywhere: the ready answers and the prompt used to differ on where the clock starts.",
	),
	_approved(
		"Delivery coverage",
		"Delivery",
		"بنوصّل القاهرة والجيزة",
		"We deliver across Greater Cairo and Giza [only inside the listed delivery areas; no other governorates]",
		f"{REGISTRY} (11 Sep 2026)",
		on="2026-09-11",
	),
	_approved(
		"Same-day delivery",
		"Delivery",
		"التوصيل في نفس اليوم",
		"Same-day delivery, which follows from the 2-hour maximum "
		"[say it only with the delivery time; never promise a cutoff or a fixed hour]",
		f"{REGISTRY} (3 Oct 2026)",
		on="2026-10-03",
	),
	_approved(
		"Arrival condition",
		"Delivery",
		"بيوصلك زي ما خرج من المطبخ",
		"It arrives exactly as it left our kitchen [credit the packing, never freshness to the jar]",
		f"{REGISTRY} (3 Oct 2026)",
		on="2026-10-03",
	),
	_approved(
		"Payment methods",
		"Payment",
		"متاح الدفع كاش عند الاستلام أو InstaPay، وكمان أونلاين على orderjarz.com",
		"Payment: cash on delivery, InstaPay, or online at orderjarz.com",
		CODE_SOURCE,
		by="",
		notes=CODE_NOTE,
	),
	_approved(
		"Ordering in the chat",
		"Ordering",
		"تقدر تطلب هنا في الشات على طول",
		"Orders are taken here in the chat; orderjarz.com is only an extra option "
		"[never send the customer to the website instead of helping]",
		CODE_SOURCE,
		by="",
		notes=CODE_NOTE,
	),
	_approved(
		"Order details we need",
		"Ordering",
		"ابعتلنا في رسالة واحدة: النكهات والحجم، الاسم، رقم الموبايل، والعنوان بالتفصيل",
		"To order: flavours and size, name, phone and detailed address, in ONE message",
		CODE_SOURCE,
		by="",
		notes=CODE_NOTE,
	),
	_approved(
		"Ordering website",
		"Ordering",
		"orderjarz.com",
		"orderjarz.com is the only website to mention [no other domains, no social handles]",
		f"{REGISTRY} (11 Sep 2026)",
		on="2026-09-11",
	),
	_approved(
		"Café white label",
		"Ordering",
		"اسم الكافيه بتاعك. والـ 7 طبقات علينا.",
		"Your café's name. Our 7 layers. [cafés stock Jarz jars with their own label; "
		"never quote wholesale prices, margins or minimums]",
		f"{REGISTRY} (7 Oct 2026)",
		on="2026-10-07",
	),
	_approved(
		"Sizes",
		"Sizes",
		"الوسط 212 ML والكبير 330 ML",
		"Medium 212 ML, Large 330 ML [say nothing about how many people a size serves]",
		f"{REGISTRY} (11 Sep 2026)",
		on="2026-09-11",
	),
	_approved(
		"Glass jar with a lid",
		"Product",
		"برطمان زجاج بغطا، وتقدر تستخدمه تاني",
		'Reusable glass jar with a lid [say "closed", never "sealed"]',
		f"{REGISTRY} (11 Sep 2026)",
		on="2026-09-11",
	),
	_approved(
		"Ready to eat",
		"Product",
		"جاهز للأكل من غير طبق ولا سكينة، وكل ضيف بياخد برطمانه",
		"Ready to eat with no plate or cutting; every guest gets their own jar",
		f"{REGISTRY}: format description, not a performance promise",
	),
	_approved(
		"Layers: cheesecakes",
		"Product",
		"7 طبقات",
		"7 layers [the cheesecakes only: Blueberry, Chocolate Hazelnut, Strawberry, Lotus, "
		"Pistachio, Mango, Red Velvet; always the numeral 7]",
		f"{REGISTRY} (11 Sep 2026)",
		on="2026-09-11",
	),
	_approved(
		"Layers: Tiramisu and Molten Cake",
		"Product",
		"بطبقات",
		"Layered, every layer [never a layer count for Tiramisu or Molten Cake]",
		f"{REGISTRY}: use layered language until the count is confirmed",
	),
	_approved(
		"Layer order: Blueberry Cheesecake",
		"Product",
		"من تحت لفوق: تشيز كيك، بسكويت، بلوبيري، تشيز كيك، بسكويت، بلوبيري، تشيز كيك",
		"Bottom to top: cheesecake, biscuit, blueberry, cheesecake, biscuit, blueberry, cheesecake "
		'[names only: no weights, no "Belgian", no other flavour]',
		f"{REGISTRY} (4 Oct 2026)",
		on="2026-10-04",
	),
	_approved(
		"Favourite flavours",
		"Product",
		"Blueberry و Chocolate Hazelnut أكتر اتنين الناس بتحبهم",
		"Blueberry and Chocolate Hazelnut are the favourites "
		"[recommend them to a hesitating customer; no sales numbers or rank]",
		CODE_SOURCE,
		by="",
		notes="The registry holds bestseller-rank claims for customer-facing copy. Recommending these two "
		"was already the chat behaviour: the owner should confirm it, or put it On Hold.",
	),
	_owner(
		"Natural ingredients",
		"مكونات طبيعية",
		'Natural ingredients [never "100% natural", "all-natural" or «طبيعية 100%»]',
		"Fixed wording only. Natural colour, natural cocoa butter and natural cream cheese stay On Hold.",
	),
	_owner(
		"No preservatives",
		"من غير أي مواد حافظة",
		"No preservatives [never give the jar, lid or glass as the reason it keeps]",
		"Fixed wording only.",
	),
	_owner(
		"Less sugar",
		"سكر أهدى",
		"Less sugar, also «قليل السكر» / low sugar "
		'[never a sugar figure, "light", or a wellness frame: healthier, guilt-free, diet]',
		"Fixed wording only. A sugar number stays On Hold.",
	),
	_approved(
		"Real chocolate",
		"Ingredients and claims",
		"شيكولاتة حقيقية",
		"Real chocolate [only Chocolate Hazelnut and Molten Cake; never a whole-collection claim, "
		'never "100%", a cocoa percentage, "Belgian" or a supplier]',
		f"{REGISTRY} (17 Oct 2026)",
		on="2026-10-17",
	),
	_approved(
		"Real fruit fillings",
		"Ingredients and claims",
		"حشوات معمولة بفاكهة حقيقية",
		'Fillings made with real fruit [only Blueberry, Strawberry and Mango; never "100%", '
		'"fresh fruit", a fruit percentage or a health frame]',
		f"{REGISTRY} (17 Oct 2026)",
		on="2026-10-17",
	),
	_approved(
		"Made in our kitchen",
		"Ingredients and claims",
		"بنعمله عندنا في المطبخ",
		"Made in our kitchen [only these components, each for its own flavours: the biscuit; the fudge "
		"cake (Pistachio, Molten Cake, Chocolate Hazelnut); the chocolate ganache (Molten Cake); the "
		"Savoiardi and the espresso (Tiramisu); the Red Velvet cake; the cheesecake mixture. The sauces "
		'are bought ready: never say they are made in-house. Never "everything from scratch" or "handmade"]',
		f"{REGISTRY} (16 Oct 2026)",
		on="2026-10-16",
	),
	_approved(
		"Storage: fridge",
		"Storage",
		"بتقعد 7 أيام في التلاجة من يوم ما توصلك",
		"Keeps 7 days in the fridge from the day it arrives [never credit the jar, lid or glass for it]",
		f"{REGISTRY} (3 Oct 2026)",
		on="2026-10-03",
	),
	_approved(
		"Storage: freezer",
		"Storage",
		"بتقعد 3 شهور في الفريزر",
		"Keeps 3 months in the freezer [never credit the jar, lid or glass for it]",
		f"{REGISTRY} (3 Oct 2026)",
		on="2026-10-03",
	),
	_approved(
		"Serving instructions",
		"Storage",
		"تابع تعليمات التقديم اللي جاية مع الأوردر",
		"Follow the serving instructions supplied with the order [no thawing method or time]",
		f"{REGISTRY}: the wording to use while thawing is On Hold",
	),
	# ── On Hold ─────────────────────────────────────────────────────────
	_held(
		"Allergens (nuts, gluten, lactose, eggs, dairy)",
		"Allergens and nutrition",
		"No food-safety allergen matrix exists. Never infer an allergen from a flavour name. "
		"Say a team member will confirm.",
	),
	_held(
		"Diet and health suitability (diabetic, vegan, keto, pregnancy)",
		"Allergens and nutrition",
		'Dietary suitability and "suitable for" claims are closed. Say a team member will confirm.',
	),
	_held(
		"Nutrition figures and health benefits (calories, protein, sugar numbers)",
		"Allergens and nutrition",
		"No nutrition figure or health benefit is approved. A sugar number stays closed.",
	),
	_held(
		"Servings per jar (how many people a jar serves)",
		"Sizes",
		"No approved answer. The Sizes wording must not imply one. Ask the headcount and recommend a "
		"size or a bundle, or say a team member will confirm.",
		source="Owner decision, 2026-10-29",
	),
	_held(
		"Layer count for Tiramisu and Molten Cake",
		"Product",
		"Use layered language until the owner confirms the actual count (Food Safety Intake, Part 3). "
		"Then both rows can become Approved.",
	),
	_held(
		"Thawing method and time",
		"Storage",
		"Until confirmed, use the Serving instructions fact.",
	),
	_held(
		"Natural or no artificial colour",
		"Ingredients and claims",
		"Needs the supplier specifications and a precise definition.",
	),
	_held(
		"Natural cocoa butter, natural cream cheese, nut content",
		"Ingredients and claims",
		'Needs supplier specifications. "Highest nut content in the market" is a superlative and also closed.',
	),
	_held(
		"Glass versus plastic (microplastics, safer)",
		"Product",
		"Never say plastic is harmful or that Jarz is safer. The adopted line is a personal precaution, "
		"not a claim.",
	),
	_held(
		"Closes without air, keeps longer because of the lid",
		"Storage",
		"Contradicts the factory record: the jars close with a lid, there is no vacuum or aluminium seal. "
		"Storage life belongs to the freezer and the recipe.",
	),
	_held(
		"Ingredient brands and suppliers (Nutella, Lotus, Belgian)",
		"Ingredients and claims",
		"Never name an ingredient brand, a supplier or a country of origin.",
	),
	_held(
		"Wholesale prices, margins and minimum orders",
		"Ordering",
		"Never quoted in chat. Hand a cafe or company over to the team.",
	),
	_held(
		"Opening hours, order cutoff and branch details",
		"Delivery",
		"Not approved. Never promise a cutoff or a fixed hour.",
		source="Chat drafts rule: do not invent opening hours or branches",
	),
	_held(
		"Business numbers (customer counts, rankings, growth, reorder rate)",
		"Other",
		"Internal figures are not approved for customer-facing use.",
	),
	_held(
		"Other websites and social media handles",
		"Other",
		"Only orderjarz.com. Pull handles from the live account when needed.",
	),
	_held(
		"Refund, replacement, discount and compensation",
		"Other",
		"Never promised by the AI. A complaint gets an apology and the team looks into it.",
		source="Chat drafts rule 5",
	),
	# ── Retired ─────────────────────────────────────────────────────────
	_held(
		"The word sealed",
		"Brand voice",
		'Retired 6 Oct 2026. Say "closed", "glass jar with a lid" or "closed and on its way".',
		status="Retired",
	),
	_held(
		"Palm oil claims",
		"Ingredients and claims",
		"Retired as a brand-wide claim; any single-product claim stays closed pending recipe review.",
		status="Retired",
	),
	_held(
		"Fresh from frozen",
		"Storage",
		"Retired. Do not use.",
		status="Retired",
	),
]


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return
	if frappe.db.count(DOCTYPE):
		print("seed_brand_facts: the table already has rows, nothing seeded")
		return
	for row in SEED:
		doc = frappe.get_doc({"doctype": DOCTYPE, **row})
		doc.insert(ignore_permissions=True)
	from jarz_pos.services.chat_settings import clear_draft_context_cache

	clear_draft_context_cache()
	print(f"seed_brand_facts: seeded {len(SEED)} facts")
