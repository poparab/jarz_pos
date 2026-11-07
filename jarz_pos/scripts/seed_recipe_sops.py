"""Seed Jarz SOP records from the kitchen's recipe manuals.

Source of truth is ``JARZ/Templates/Recpie manuals/*.docx``, transcribed here so
the SOPs can be rebuilt on any site without the Windows share being reachable.
Instructions carry the original Egyptian Arabic with an English line above it —
the bench reads Arabic, the schema and reports read English, and losing either
would make one of those two audiences guess.

Run::

    bench --site <site> execute jarz_pos.scripts.seed_recipe_sops.run
    bench --site <site> execute jarz_pos.scripts.seed_recipe_sops.run --kwargs "{'items': 'Tiramisu Large,Tiramisu Small'}"

Idempotent by ``(item_code, version)``: re-running updates the existing SOP in
place rather than stacking duplicates, so fixing a typo is just an edit and a
re-run.  Bump ``version`` in the data below to keep the old one for comparison
instead of overwriting it.

Quantities are ``{{item:...}}`` tokens on the BOM lines (base recipes since
v2, 2026-10-08), so every screen quotes the run being made.  Where the manual
and the BOM disagree the step follows the BOM and the recipe's ``notes`` say
what the manual said — the BOM is what stock and cost follow, and a note keeps
the question visible instead of quietly settling it.
"""

from __future__ import annotations

from typing import Any, Optional

import frappe
from frappe.utils import flt

# ── Recipe data ─────────────────────────────────────────────────────────
# `capture` turns a step into a checkpoint the operator must answer.  Used only
# where the manual states a measurable target — inventing thresholds would make
# the SOP look authoritative about things nobody measured.

BASES_V2_NOTE = (
	"v2 (2026-10-08): every amount is a token on the BOM line, so the recipe "
	"sheet on the Bases tab and the Production Board quote the run actually "
	"being made, not one manual batch."
)

CHEESECAKE_MIX = {
	"item_code": "Cheesecake Mix",
	"version": 2,
	"yield_percent": 100,
	"prep_time_mins": 12,
	"equipment": "Planetary mixer, paddle (كف) attachment",
	"notes": (
		"One BOM batch = 9.520 Kg, which fills 120 medium or 77 large jars.\n"
		"Vanilla is 20 g per batch, confirmed by the owner 2026-08-08; the BOM "
		"now bills the same.\n" + BASES_V2_NOTE
	),
	"steps": [
		{
			"title": "Weigh cheese, powder sugar and vanilla",
			"instruction": (
				"Weigh into the mixer bowl: {{item:milkana cheese|grams}} Milkana + "
				"{{item:Remas cheese|grams}} Remas, {{item:powder sugar|grams}} powder "
				"sugar, {{item:kamina vanilla|grams}} vanilla — "
				"{{item:milkana cheese+Remas cheese+powder sugar+kamina vanilla|grams}} "
				"in the bowl.\n"
				"يوزن في حلة المضرب: {{item:milkana cheese|grams}} ميلكانا + "
				"{{item:Remas cheese|grams}} ريماس، {{item:powder sugar|grams}} سكر بودر، "
				"{{item:kamina vanilla|grams}} فانيليا — الإجمالي "
				"{{item:milkana cheese+Remas cheese+powder sugar+kamina vanilla|grams}}."
			),
			"duration_mins": 4,
			"scaling_mode": "Per Batch",
			"capture_type": "Number",
			"capture_label": "Total weighed into bowl (g)",
			"requires_confirmation": 1,
		},
		{
			"title": "Mix on speed 3 for 7 minutes with the paddle",
			"instruction": (
				"Mix on speed 3 for 7 minutes using the paddle attachment.\n"
				"و تخلط علي سرعة 3 لمدة 7 دقائق بسلاح الكف."
			),
			"duration_mins": 7,
			"scaling_mode": "Fixed",
			"requires_confirmation": 1,
		},
		{
			"title": "Drop to speed 1 and add the cream",
			"instruction": (
				"Drop the mixer to speed 1 and add {{item:dr baker cream|grams}} dr "
				"baker cream (unsweetened) at medium speed until well combined — "
				"about one minute. Stop the moment it comes together; over-mixing "
				"after the cream goes in breaks the texture.\n"
				"يتم انزال السرعة الي 1 و يضاف {{item:dr baker cream|grams}} كريمة "
				"بسرعة متوسطة حتي تمتزج جيدا لمدة دقيقة او حتي تمتزج و يغلق بمجرد "
				"الامتزاج."
			),
			"duration_mins": 1,
			"scaling_mode": "Fixed",
			"requires_confirmation": 1,
		},
		{
			"title": "Hand-fold, especially the sides of the bowl",
			"instruction": (
				"Fold by hand to confirm it is fully combined, paying "
				"particular attention to the sides of the bowl where the "
				"paddle does not reach.\n"
				"يقلب الخليط يدويا حتي يتاكد من الامتزاج و خاصة جوانب الحلة."
			),
			"duration_mins": 2,
			"scaling_mode": "Fixed",
			"requires_confirmation": 1,
		},
	],
}

_BAKE_180_45 = {
	"title": "Bake at 180°C for 45 minutes",
	"instruction": ("Bake at 180°C for 45 minutes.\n" "يسوي الخليط علي درجه حراره 180 لمده 45 دقيقه."),
	"duration_mins": 45,
	"scaling_mode": "Fixed",
	"capture_type": "Temperature",
	"capture_label": "Oven temperature (°C)",
	"capture_min": 170,
	"capture_max": 190,
	"requires_confirmation": 1,
}

_CAKE_WHIP = {
	"title": "Whip eggs, sugar and vanilla to near-white",
	"instruction": (
		"Using the whisk, beat {{item:eggs|count}} eggs + {{item:sugar|grams}} "
		"sugar + {{item:kamina vanilla|grams}} vanilla on speed 3 until the "
		"colour is close to white.\n"
		"يستخدم مضرب السلك في ضرب {{item:eggs|count}} بيضة و {{item:sugar|grams}} "
		"سكر و {{item:kamina vanilla|grams}} فانيليا حتي تصل الي لون اقرب الي "
		"الأبيض و المضرب علي سرعه 3."
	),
	"duration_mins": 10,
	"scaling_mode": "Per Batch",
	"requires_confirmation": 1,
}

_CAKE_OIL_WATER = {
	"title": "Combine oil and boiling water, add gradually",
	"instruction": (
		"Mix {{item:oil|grams}} oil with {{item:Water (tap)|grams}} boiling water "
		"in a container, then add gradually to the mixer.\n"
		"يخلط {{item:oil|grams}} زيت و {{item:Water (tap)|grams}} ماء مغلي في وعاء "
		"ثم يضاف علي الخليط في العجان تدريجيا."
	),
	"duration_mins": 5,
	"scaling_mode": "Per Batch",
	"requires_confirmation": 1,
}

_CAKE_DIVIDE = {
	"title": "Stop the mixer, hand-stir, divide over the trays",
	"instruction": (
		"Stop the mixer, stir well by hand with a spoon, then divide evenly "
		"over the trays.\n"
		"يفصل العجان و يقلب الخليط بمعلقه جيدا ثم يوزع بالتساوي علي الصاجات."
	),
	"duration_mins": 5,
	"scaling_mode": "Per Batch",
	"requires_confirmation": 1,
}

_CAKE_NOTE = (
	"The manual's batch (45 eggs, 3.750 kg sugar) is 1.5x the BOM batch (30 "
	"eggs, 2.5 kg sugar) on every line; the steps now follow the BOM. Its "
	'"2 trays" is not restated because it is unclear which batch it meant.'
)

# v3 = Cake A, the official Fudge Cake (owner, 2026-10-29; method approved 2026-11-05).
# The cocoa is bloomed in the boiling water instead of going in with the dry mix,
# baking soda joins the dry mix, and the bake is cooler and pulled on centre
# temperature.  BOM-Fudge Cake-005 is 45 eggs -> 14.29 Kg of batter.
FUDGE_CAKE = {
	"item_code": "Fudge Cake",
	"version": 3,
	"yield_percent": 98,
	"prep_time_mins": 70,
	"equipment": "Planetary mixer (whisk then hand), sieve, trays, oven, probe thermometer",
	"notes": (
		"Cake A, the official Fudge Cake since 2026-10-29. One BOM batch = 45 eggs = "
		"14.29 Kg of batter. Cocoa is Cacao Barry Extra Brute (alkalised), so never "
		"more baking soda than the BOM line. Bake loss target: 10% or less "
		"(over 12% means overbaked). Don't keep plain cake in the fridge for days; "
		"freeze what isn't used today.\n" + BASES_V2_NOTE
	),
	"steps": [
		{
			"title": "Bloom the cocoa in the boiling water",
			"instruction": (
				"Sift {{item:coco powder|grams}} cocoa. Boil {{item:Water (tap)|grams}} water, "
				"whisk in the cocoa until there are no lumps, cover and rest 5 minutes.\n"
				"ينخل {{item:coco powder|grams}} كاكاو. يغلي {{item:Water (tap)|grams}} ماء "
				"و يضاف عليه الكاكاو مع التقليب بالمضرب اليدوي حتي لا يبقي اي تكتل، "
				"ثم يغطي و يترك 5 دقائق."
			),
			"duration_mins": 10,
			"scaling_mode": "Per Batch",
			"requires_confirmation": 1,
		},
		dict(_CAKE_WHIP),
		{
			"title": "Add the dry mix in three additions on speed 1",
			"instruction": (
				"Drop to speed 1 and add the dry mix — {{item:flour|grams}} flour, "
				"{{item:baking powder|grams}} baking powder, {{item:baking soda|grams}} "
				"baking soda, a pinch of salt — in three additions, each until it "
				"disappears. Weigh the soda on the small scale.\n"
				"انزال المضرب علي سرعه 1 ثم يضاف اليه الخليط الجاف "
				"( {{item:flour|grams}} دقيق – {{item:baking powder|grams}} بيكنج بودر – "
				"{{item:baking soda|grams}} بيكنج صودا – رشه ملح ) علي ثلاث مرات حتي يختفي "
				"الخليط. الصودا توزن علي الميزان الصغير."
			),
			"duration_mins": 5,
			"scaling_mode": "Per Batch",
			"requires_confirmation": 1,
		},
		{
			"title": "Combine oil and the cocoa water, add gradually",
			"instruction": (
				"Mix {{item:oil|grams}} oil into the hot cocoa water, then add it "
				"gradually to the mixer.\n"
				"يخلط {{item:oil|grams}} زيت مع ماء الكاكاو الساخن "
				"ثم يضاف علي الخليط في العجان تدريجيا."
			),
			"duration_mins": 5,
			"scaling_mode": "Per Batch",
			"requires_confirmation": 1,
		},
		{
			"title": "Stop the mixer, hand-stir, fill the trays 3–4 cm deep",
			"instruction": (
				"Stop the mixer, stir well by hand with a spoon, then divide evenly "
				"over the trays so the batter is 3–4 cm deep.\n"
				"يفصل العجان و يقلب الخليط بمعلقه جيدا ثم يوزع بالتساوي علي الصاجات "
				"بحيث يكون ارتفاع الخليط 3 الي 4 سم."
			),
			"duration_mins": 5,
			"scaling_mode": "Per Batch",
			"requires_confirmation": 1,
		},
		{
			"title": "Bake at 165–170°C, pull at 95°C in the centre",
			"instruction": (
				"Bake at 165–170°C. Pull the tray when the centre reads about 95°C, or a "
				"skewer comes out with moist crumbs.\n"
				"يسوي الخليط علي درجه حراره 165 الي 170. يخرج الصاج لما حراره النص "
				"توصل حوالي 95 درجه، او لما السيخ يطلع عليه فتافيت رطبه."
			),
			"duration_mins": 45,
			"scaling_mode": "Fixed",
			"capture_type": "Temperature",
			"capture_label": "Centre temperature at pull (°C)",
			"capture_min": 92,
			"capture_max": 98,
			"requires_confirmation": 1,
		},
		{
			"title": "Cover while warm, cool, freeze what isn't used today",
			"instruction": (
				"Cover the trays with film or foil while warm and cool covered to room "
				"temperature. Freeze any cake not used today; don't keep it in the fridge "
				"for days.\n"
				"يغطي الصاج بالاستريتش او الفويل و هو دافي و يبرد و هو مغطي لحد درجه "
				"حراره الغرفه. اي كيك مش هيستخدم النهارده يدخل الفريزر، و ميتسابش في "
				"التلاجه ايام."
			),
			"duration_mins": 5,
			"scaling_mode": "Fixed",
			"requires_confirmation": 1,
		},
	],
}

RED_VELVET_CAKE = {
	"item_code": "Red Velvet Cake",
	"version": 2,
	"yield_percent": 98,
	"prep_time_mins": 60,
	"equipment": "Planetary mixer (whisk then hand), trays, oven",
	"notes": "One BOM batch = 9.278 Kg.\n" + _CAKE_NOTE + "\n" + BASES_V2_NOTE,
	"steps": [
		dict(_CAKE_WHIP),
		{
			"title": "Add the dry mix with the red colour, three additions",
			"instruction": (
				"Drop to speed 1 and add the dry mix — {{item:flour|grams}} flour, "
				"{{item:baking powder|grams}} baking powder, {{item:coco powder|grams}} "
				"cocoa powder, a pinch of salt, {{item:red color|grams}} red colour "
				"(دم الغزال) — in three additions until it disappears.\n"
				"انزال المضرب علي سرعه 1 ثم يضاف اليه الخليط الجاف "
				"( {{item:flour|grams}} دقيق – {{item:baking powder|grams}} بيكنج بودر – "
				"{{item:coco powder|grams}} كاكاو – رشه ملح – {{item:red color|grams}} "
				"اللون الاحمر ) علي ثلاث مرات حتي يختفي الخليط."
			),
			"duration_mins": 5,
			"scaling_mode": "Per Batch",
			"requires_confirmation": 1,
		},
		dict(_CAKE_OIL_WATER),
		dict(_CAKE_DIVIDE),
		dict(_BAKE_180_45),
	],
}

SAVOIARDI = {
	"item_code": "Savoiardi",
	"version": 2,
	"yield_percent": 80,
	"prep_time_mins": 45,
	"equipment": "Large planetary mixer (whisk), small mixer, sieve, silicone-lined trays, oven",
	"notes": (
		"One BOM batch = 2.5 Kg from 30 eggs, two trays. Whites and yolks are "
		"whipped separately; the whites are the structure, so stop the moment "
		"they hold stiff peaks. The sugar is split in two equal halves: one into "
		"the yolks, one into the whites. Salt is on the BOM but not in the "
		"manual; it goes in with the dry mix.\n" + BASES_V2_NOTE
	),
	"steps": [
		{
			"title": "Preheat oven to 180°C and separate the eggs",
			"instruction": (
				"Preheat the oven to 180°C. Separate {{item:eggs|count}} eggs, whites "
				"from yolks. Split {{item:sugar|grams}} sugar into two halves of "
				"{{item:sugar|grams|x0.5}}.\n"
				"يتم تشغيل الفرن للتسخين علي درجة حرارة 180. {{item:eggs|count}} بيضه "
				"مفصول البياض عن الصفار. {{item:sugar|grams}} سكر مقسمين نصين، كل نص "
				"{{item:sugar|grams|x0.5}}."
			),
			"duration_mins": 8,
			"scaling_mode": "Per Batch",
			"requires_confirmation": 1,
		},
		{
			"title": "Whip yolks with sugar, glucose and vanilla",
			"instruction": (
				"To the yolks add {{item:sugar|grams|x0.5}} sugar, "
				"{{item:Glucose honey|grams}} glucose honey and "
				"{{item:kamina vanilla|grams}} vanilla. Whip in the small mixer with "
				"the whisk until the colour turns creamy.\n"
				"يضاف الي الصفار {{item:sugar|grams|x0.5}} سكر و "
				"{{item:Glucose honey|grams}} عسل جلوكوز و {{item:kamina vanilla|grams}} "
				"فانيليا. و يخفق في المضرب الصغير بالسلك حتي يبقي اللون كريمي."
			),
			"duration_mins": 5,
			"scaling_mode": "Per Batch",
			"requires_confirmation": 1,
		},
		{
			"title": "Start the whites on speed 2 to light bubbles",
			"instruction": (
				"Put the whites in the mixer bowl and start whisking on speed "
				"2 until light bubbles form.\n"
				"يضاف البياض في حلة العجان و يبدا الخفق بالسلك علي سرعة 2 حتي "
				"تكون فقاعات بسيطة."
			),
			"duration_mins": 3,
			"scaling_mode": "Fixed",
			"requires_confirmation": 1,
		},
		{
			"title": "Speed 3, add sugar gradually to stiff glossy peaks",
			"instruction": (
				"Move to speed 3 and add the other {{item:sugar|grams|x0.5}} sugar "
				"gradually until the mix is glossy and — most importantly — holds "
				"stiff peaks.\n"
				"يتم نقل المضرب علي سرعة 3 و يتم إضافة {{item:sugar|grams|x0.5}} سكر "
				"تدريجيا حتي وصول الخليط الي لمعة و الأهم ان يكون قمم قوية."
			),
			"duration_mins": 6,
			"scaling_mode": "Per Batch",
			"capture_type": "Photo",
			"capture_label": "Photo of the peak on the whisk",
			"requires_confirmation": 1,
		},
		{
			"title": "Stop the mixer immediately once peaks are stiff",
			"instruction": (
				"Test by hand. The moment it holds stiff peaks, stop the mixer "
				"immediately — whipping past this point dries the whites and "
				"the sheet will crack.\n"
				"اول ما نختبرة بايدينا و يكون قمم قوية يتم غلق المضرب مباشرة."
			),
			"duration_mins": 1,
			"scaling_mode": "Fixed",
			"requires_confirmation": 1,
		},
		{
			"title": "Lighten the yolks with some whites",
			"instruction": (
				"Take some of the whites and fold into the whipped yolks with "
				"the paddle, mixing well but not excessively.\n"
				"يتم اخذ بعض من البياض و اضافتة علي الصفار المضروب بمضرب الكف "
				"و يخلط جيدا بدون الخلط كثيرا."
			),
			"duration_mins": 2,
			"scaling_mode": "Fixed",
			"requires_confirmation": 1,
		},
		{
			"title": "Return everything to the large mixer on speed 1",
			"instruction": (
				"On speed 1 return the whole mixture to the large mixer and "
				"fold with the paddle until no lumps of white remain.\n"
				"علي سرعة 1 يعاد المزيج كاملا الي المضرب الكبير و يتم التقليب "
				"بمضرب الكف حتي يختفي جميع تكتلات البياض."
			),
			"duration_mins": 3,
			"scaling_mode": "Fixed",
			"requires_confirmation": 1,
		},
		{
			"title": "Sieve in flour, starch and baking powder on speed 1",
			"instruction": (
				"Add the dry mix ({{item:flour|grams}} flour, {{item:Cornstarch|grams}} "
				"cornstarch, {{item:baking powder|grams}} baking powder, "
				"{{item:Salt|grams}} salt) through the sieve, on speed 1.\n"
				"يتم إضافة الخليط الجاف ({{item:flour|grams}} دقيق، "
				"{{item:Cornstarch|grams}} نشا، {{item:baking powder|grams}} بيكنج بودر، "
				"{{item:Salt|grams}} ملح) علي سرعة 1 عن طريق المصفاة في المضرب."
			),
			"duration_mins": 3,
			"scaling_mode": "Per Batch",
			"requires_confirmation": 1,
		},
		{
			"title": "Stop and hand-check for lumps",
			"instruction": (
				"As the mix comes together, stop the mixer and stir a little by "
				"spoon to confirm there are no lumps.\n"
				"مع امتزاج الخليط يتم الفصل و التقليب بالمعلقة قليلا لتاكيد عدم "
				"وجود أي تكتلات."
			),
			"duration_mins": 2,
			"scaling_mode": "Fixed",
			"requires_confirmation": 1,
		},
		{
			"title": "Divide over silicone trays and bake 180°C / 15 min",
			"instruction": (
				"Divide over silicone-lined trays, two trays for every 30 eggs, "
				"and bake at 180°C for 15 minutes.\n"
				"يقسم الخليط علي صاجات اسفلها سليكون، صاجين لكل 30 بيضة، و يدخل "
				"الفرن علي حرارة 180 لمدة 15 دقيقة."
			),
			"duration_mins": 15,
			"scaling_mode": "Fixed",
			"capture_type": "Temperature",
			"capture_label": "Oven temperature (°C)",
			"capture_min": 170,
			"capture_max": 190,
			"requires_confirmation": 1,
		},
	],
}

SPONGE_CAKE = {
	"item_code": "Sponge Cake",
	"version": 2,
	"yield_percent": 80,
	"prep_time_mins": 50,
	"equipment": "Planetary mixer (whisk then paddle), sieve, trays, oven",
	"notes": (
		"One BOM batch = 4.0 Kg from 45 eggs, three trays. Note the last step: "
		"after cooling the sheet is ground and dried at 140°C for 30 minutes, "
		"stirred halfway. The manual says 9 g salt per batch; the BOM bills "
		"5 g, which is what the step now quotes.\n" + BASES_V2_NOTE
	),
	"steps": [
		{
			"title": "Preheat oven to 175°C",
			"instruction": "Preheat the oven to 175°C.\nيتم تسخين الفرن علي 175.",
			"duration_mins": 2,
			"scaling_mode": "Fixed",
			"requires_confirmation": 1,
		},
		{
			"title": "Prepare the dry mix",
			"instruction": (
				"Combine {{item:flour|grams}} flour, {{item:Cornstarch|grams}} "
				"cornstarch, {{item:Salt|grams}} salt and {{item:baking powder|grams}} "
				"baking powder to make the dry mix.\n"
				"خلط {{item:flour|grams}} دقيق و {{item:Cornstarch|grams}} نشا و "
				"{{item:Salt|grams}} ملح و {{item:baking powder|grams}} بيكنج بودر "
				"لتجهيز الخليط الناشف."
			),
			"duration_mins": 5,
			"scaling_mode": "Per Batch",
			"requires_confirmation": 1,
		},
		{
			"title": "Whip the eggs with sugar and vanilla to triple volume",
			"instruction": (
				"Whip {{item:eggs|count}} eggs with {{item:sugar|grams}} sugar and "
				"{{item:kamina vanilla|grams}} vanilla on speed 3 with the whisk "
				"until the volume triples.\n"
				"يتم خفق {{item:eggs|count}} بيضة مع {{item:sugar|grams}} سكر و "
				"{{item:kamina vanilla|grams}} فانيليا حتي يتضاعف حجمة الي 3 اضعاف "
				"بمضرب السلك علي سرعة 3."
			),
			"duration_mins": 12,
			"scaling_mode": "Per Batch",
			"capture_type": "Number",
			"capture_label": "Density check — weight of 100 ml batter (g)",
			"capture_min": 48,
			"capture_max": 52,
			"requires_confirmation": 1,
		},
		{
			"title": "Switch to paddle, fold the dry mix in through a sieve",
			"instruction": (
				"Change to the paddle and fold on speed 1, adding the dry mix "
				"a spoon at a time — preferably through a sieve.\n"
				"يتم تغيير الي مضرب الكف و يتم التقليب علي سرعة 1 مع إضافة "
				"الخليط الناشف معلقة معلقة و يفضل وضعة بواسطة المصفاة."
			),
			"duration_mins": 6,
			"scaling_mode": "Per Batch",
			"requires_confirmation": 1,
		},
		{
			"title": "Oil liaison at 45–50°C, then return and stop",
			"instruction": (
				"Take a portion of the batter and mix it thoroughly with "
				"{{item:oil|grams}} oil warmed to 45–50°C. Once combined, return it "
				"to the mixer, fold briefly, then stop.\n"
				"بعد ان يمتزج يتم اخذ جزء من الخليط ووضعة علي {{item:oil|grams}} زيت "
				"بدرجة حرارة من 45 الي 50 و يتم تقليبة جيدا و بعد الامتزاج يعاد "
				"الخليط الي المضرب و يتم التقليب قليلا ثم الفصل."
			),
			"duration_mins": 5,
			"scaling_mode": "Per Batch",
			"capture_type": "Temperature",
			"capture_label": "Oil temperature (°C)",
			"capture_min": 45,
			"capture_max": 50,
			"requires_confirmation": 1,
		},
		{
			"title": "Hand-fold, divide over the trays, bake 175°C / 20 min",
			"instruction": (
				"Fold by hand a little to confirm it is combined, divide over the "
				"trays, three trays for every 45 eggs, and bake at 175°C for 20 "
				"minutes.\n"
				"يتم التقليب يدويا قليلا للتاكد من الامتزاج و توزيع الخليط علي "
				"الصاجات، 3 صاجات لكل 45 بيضة، و ادخالة الي الفرن في درجة حراره 175 "
				"لمدة 20 دقيقة."
			),
			"duration_mins": 20,
			"scaling_mode": "Fixed",
			"requires_confirmation": 1,
		},
		{
			"title": "Cool, grind, then dry at 140°C for 30 minutes",
			"instruction": (
				"Once cooled, grind the sheet and dry it in the oven at 140°C "
				"for 30 minutes, stirring halfway through.\n"
				"بعد ان يبرد الخليط يطحن و ينشف في الفرن علي درجة حرارة 140 و "
				"يقلب في منتصف المدة. و المدة نصف ساعة."
			),
			"duration_mins": 30,
			"scaling_mode": "Fixed",
			"capture_type": "Temperature",
			"capture_label": "Drying temperature (°C)",
			"capture_min": 130,
			"capture_max": 150,
			"requires_confirmation": 1,
		},
	],
}

# The Tiramisu jars, per the owner's method of 2026-11-07 (superseding the
# 2026-10-08 one: brew 1:3, sugar = liquid x 0.3, syrup into the cream = the
# sugar weight).
#
#   brew       grinds : water = 1 : 5 by weight, giving liquid = 3 x grinds
#              (1 kg grinds + 5 kg water -> 3 kg liquid coffee)
#   liquid     Large 26 g, Medium 18 g per jar; Small = 2/3 of Medium (12 g)
#   sugar      = 20 % of the liquid (0.6 x grinds), dissolved once brewed
#   cream      = cheesecake mix + 30 % of the liquid as sweet coffee (0.9 x grinds)
#   savoiardi  = the rest of the sweet coffee (liquid + sugar - cream share,
#                i.e. 90 % of the liquid = 2.7 x grinds)
#
# The cream weight per jar stays 97 / 70 / 46.7 g, so the mix line drops by
# the coffee folded into it.  Water is not a BOM line (tap water, not stocked);
# the brew step quotes it as 5 x the grinds.
#
# Every size is made in the same session: one pot of coffee, one bowl of
# cream, then the jars.  So the three SOPs share ONE set of steps, word for
# word, and ``get_recipe_sheet`` merges them into a single sheet: the bowl
# steps quote the whole run (all sizes summed) and the jar step, written with
# ``|each`` tokens, prints one line per size.  Nothing in the steps is static
# text - every figure follows the jar BOM.
#
# Per-jar spec in grams, kept for the notes and checked by the tests against
# what the ``|each`` tokens render.  ``version`` is bumped per method change
# (L 2->3, M 3->4, S 2->3) so the earlier SOPs stay on file.
_SMALL = 2 / 3
TIRAMISU_JAR_PORTIONS: dict[str, dict[str, Any]] = {
	"Tiramisu Large": {
		"version": 3,
		"liquid": 26,
		"cream": 97,
		"savoiardi": 40,
		"cocoa": 3,
	},
	"Tiramisu Medium": {
		"version": 4,
		"liquid": 18,
		"cream": 70,
		"savoiardi": 28,
		"cocoa": 2,
	},
	# Small is exactly 2/3 of the Medium.
	"Tiramisu Small": {
		"version": 3,
		"liquid": 18 * _SMALL,
		"cream": 70 * _SMALL,
		"savoiardi": 28 * _SMALL,
		"cocoa": 2 * _SMALL,
	},
}

# Ratios on the grinds (the BOM line every coffee figure is quoted from).
TIRAMISU_WATER_PER_GRIND = 5
TIRAMISU_LIQUID_PER_GRIND = 3
TIRAMISU_SUGAR_SHARE = 0.2  # of the liquid
TIRAMISU_CREAM_COFFEE_SHARE = 0.3  # of the liquid


def tiramisu_jar_spec(portions: dict[str, Any]) -> dict[str, float]:
	"""Every per-jar figure in grams, derived from the liquid target."""
	liquid = float(portions["liquid"])
	sugar = liquid * TIRAMISU_SUGAR_SHARE
	cream_coffee = liquid * TIRAMISU_CREAM_COFFEE_SHARE
	return {
		"grinds": liquid / TIRAMISU_LIQUID_PER_GRIND,
		"water": liquid / TIRAMISU_LIQUID_PER_GRIND * TIRAMISU_WATER_PER_GRIND,
		"liquid": liquid,
		"sugar": sugar,
		"cream_coffee": cream_coffee,
		"savoiardi_coffee": liquid + sugar - cream_coffee,
		"mix": float(portions["cream"]) - cream_coffee,
		"cream": float(portions["cream"]),
		"savoiardi": float(portions["savoiardi"]),
		"cocoa": float(portions["cocoa"]),
	}


_GRINDS = "{{item:Coffee beans|grams}}"
_WATER = "{{item:Coffee beans|grams|x5}}"
_LIQUID = "{{item:Coffee beans|grams|x3}}"
_SUGAR = "{{item:powder sugar|grams}}"
_MIX = "{{item:Cheesecake Mix|grams}}"
# 30 % of the liquid = 0.9 x the grinds; the rest of the sweet coffee
# (liquid 3 + sugar 0.6 - 0.9) = 2.7 x the grinds.
_CREAM_COFFEE = "{{item:Coffee beans|grams|x0.9}}"
_SAVOIARDI_COFFEE = "{{item:Coffee beans|grams|x2.7}}"
# The cream is the mix plus the sweet coffee folded into it.
_CREAM = "{{item:Cheesecake Mix+Coffee beans*0.9|grams}}"

TIRAMISU_STEPS: list[dict[str, Any]] = [
	{
		"title": "Brew the coffee",
		"instruction": (
			f"Brew {_GRINDS} of coffee grinds with {_WATER} of water (1 to 5). "
			f"It gives {_LIQUID} of liquid coffee. Weigh the liquid: if it is "
			f"short brew a little more, if it is over keep only {_LIQUID}.\n"
			f"اعمل {_GRINDS} بن مطحون على {_WATER} مية (1 لـ 5). "
			f"هيطلع {_LIQUID} قهوة سايلة. اوزن القهوة السايلة: لو ناقصة اعمل "
			f"شوية كمان، و لو زادت خد {_LIQUID} بس."
		),
		"duration_mins": 8,
		"scaling_mode": "Fixed",
		"capture_type": "Number",
		"capture_label": "Liquid coffee weighed (g)",
		"requires_confirmation": 1,
	},
	{
		"title": "Sweeten the coffee",
		"instruction": (
			f"When the coffee is done, dissolve {_SUGAR} of powder sugar in it "
			"(20% of the liquid), then let it cool.\n"
			f"لما القهوة تخلص، دوب فيها {_SUGAR} سكر بودر (20% من القهوة "
			"السايلة)، و سيبها تبرد."
		),
		"duration_mins": 5,
		"scaling_mode": "Fixed",
		"requires_confirmation": 1,
	},
	{
		"title": "Make the cream in one bowl",
		"instruction": (
			f"Mix {_CREAM_COFFEE} of the sweet coffee (30% of the liquid) into "
			f"{_MIX} of cheesecake mix = {_CREAM} of cream. The rest of the "
			f"sweet coffee ({_SAVOIARDI_COFFEE}) is for the savoiardi.\n"
			f"اخلط {_CREAM_COFFEE} من القهوة المحلاة (30% من القهوة السايلة) "
			f"مع {_MIX} خليط تشيز كيك = {_CREAM} كريمة. باقي القهوة المحلاة "
			f"({_SAVOIARDI_COFFEE}) للسافوياردي."
		),
		"duration_mins": 10,
		"scaling_mode": "Fixed",
		"requires_confirmation": 1,
	},
	{
		"title": "Fill each jar",
		"instruction": (
			"{{item:Savoiardi|grams|each}} savoiardi + "
			"{{item:Coffee beans|grams|x2.7|each}} coffee, then "
			"{{item:Cheesecake Mix+Coffee beans*0.9|grams|each}} cream, then "
			"{{item:coco powder|grams|each}} cocoa on top.\n"
			"{{item:Savoiardi|grams|each}} سافوياردي + "
			"{{item:Coffee beans|grams|x2.7|each}} قهوة، بعدين "
			"{{item:Cheesecake Mix+Coffee beans*0.9|grams|each}} كريمة، بعدين "
			"{{item:coco powder|grams|each}} كاكاو على الوش."
		),
		"duration_mins": 1,
		"scaling_mode": "Per Unit",
		"requires_confirmation": 1,
	},
	{
		"title": "Lid and label",
		"instruction": ("Close each jar and stick on its label.\n" "اقفل كل برطمان و الزق الليبل بتاعه."),
		"duration_mins": 1,
		"scaling_mode": "Per Unit",
		"requires_confirmation": 1,
	},
]


def _grams(value: float) -> str:
	"""``97`` -> ``"97 g"``; ``46.667`` -> ``"46.7 g"`` (one decimal, no ``.0``)."""
	text = f"{value:.1f}"
	if text.endswith(".0"):
		text = text[:-2]
	return f"{text} g"


def build_tiramisu_sop(item_code: str, portions: dict[str, Any]) -> dict[str, Any]:
	"""One Tiramisu jar SOP: the shared steps plus this size's spec in the notes."""
	spec = {key: _grams(value) for key, value in tiramisu_jar_spec(portions).items()}

	return {
		"item_code": item_code,
		"version": portions["version"],
		"yield_percent": 100,
		"prep_time_mins": 30,
		"equipment": "Coffee brewer, scales, mixing bowl, jars",
		"notes": (
			"Method per the owner, 2026-11-07, superseding 2026-10-08 (brew 1:3, "
			"sugar = liquid x 0.3, syrup into the cream = the sugar weight).\n"
			"Brew coffee grinds with water at 1:5 by weight; it gives liquid "
			"coffee at 3 x the grinds (1 kg grinds + 5 kg water -> 3 kg liquid). "
			"Extraction varies, so the kitchen weighs the liquid: the liquid "
			"weight is what the recipe needs. Powder sugar = 20% of the liquid, "
			"dissolved once the coffee is done. 30% of the liquid weight, as sweet "
			"coffee, goes into the cheesecake mix to make the tiramisu cream; the "
			"rest of the sweet coffee goes onto the savoiardi.\n"
			"All sizes are made together, so every Tiramisu SOP has the same "
			"steps and the app shows them as one sheet.\n"
			f"Per jar: grinds {spec['grinds']}, water {spec['water']}, liquid "
			f"coffee {spec['liquid']}, sugar {spec['sugar']}, sweet coffee into "
			f"the cream {spec['cream_coffee']}, sweet coffee onto the savoiardi "
			f"{spec['savoiardi_coffee']}, cheesecake mix {spec['mix']}, cream "
			f"{spec['cream']}, savoiardi {spec['savoiardi']}, cocoa {spec['cocoa']}. "
			"Every figure in the steps comes from the jar BOM."
		),
		# A copy per SOP: ``_apply`` only reads, but shared dicts invite a
		# later edit that changes all three sizes by accident.
		"steps": [dict(step) for step in TIRAMISU_STEPS],
	}


TIRAMISU_SOPS: list[dict[str, Any]] = [
	build_tiramisu_sop(item_code, portions) for item_code, portions in TIRAMISU_JAR_PORTIONS.items()
]

RECIPES: list[dict[str, Any]] = [
	CHEESECAKE_MIX,
	FUDGE_CAKE,
	RED_VELVET_CAKE,
	SAVOIARDI,
	SPONGE_CAKE,
	*TIRAMISU_SOPS,
]


# ── Seeder ──────────────────────────────────────────────────────────────


def _default_bom(item_code: str) -> str | None:
	return frappe.db.get_value("BOM", {"item": item_code, "is_default": 1, "docstatus": 1}, "name")


def _apply(doc, recipe: dict[str, Any]) -> None:
	doc.item_code = recipe["item_code"]
	doc.version = recipe["version"]
	doc.is_active = 1
	doc.yield_percent = recipe.get("yield_percent") or 100
	doc.prep_time_mins = recipe.get("prep_time_mins") or 0
	doc.equipment = recipe.get("equipment") or ""
	doc.notes = recipe.get("notes") or ""
	doc.bom = _default_bom(recipe["item_code"])

	doc.set("steps", [])
	for index, step in enumerate(recipe["steps"], start=1):
		doc.append(
			"steps",
			{
				"step_no": index,
				"title": step["title"],
				"instruction": step.get("instruction") or "",
				"duration_mins": step.get("duration_mins") or 0,
				"scaling_mode": step.get("scaling_mode") or "Fixed",
				"requires_confirmation": step.get("requires_confirmation") or 0,
				"capture_type": step.get("capture_type") or "None",
				"capture_label": step.get("capture_label") or "",
				"capture_min": flt(step.get("capture_min")) if step.get("capture_min") is not None else None,
				"capture_max": flt(step.get("capture_max")) if step.get("capture_max") is not None else None,
			},
		)


def _parse_items(items: Any) -> list[str] | None:
	"""``None``/blank -> no filter; else a de-duplicated list of item codes.

	``bench execute --kwargs`` hands over a string, a script may hand over a
	list; a comma-separated string is the form a person types.
	"""
	if items is None:
		return None
	if isinstance(items, str):
		raw = items.split(",")
	else:
		raw = list(items)
	codes: list[str] = []
	for code in raw:
		code = str(code).strip()
		if code and code not in codes:
			codes.append(code)
	return codes or None


def run(dry_run: Any = False, items: Any = None) -> dict[str, Any]:
	"""Create or refresh one SOP per recipe.  Reports before it writes.

	``items`` (a list, or a comma-separated string of item codes) restricts the
	run to those recipes; the default seeds everything.  A recipe with a newer
	``version`` than the one on the site is inserted as a new record, and the
	Jarz SOP controller's ``on_update`` deactivates the older active version,
	so seeding a v2 leaves v2 active and the v1 on file, inactive.
	"""
	created: list[str] = []
	updated: list[str] = []
	skipped: list[dict[str, str]] = []

	wanted = _parse_items(items)
	recipes = [r for r in RECIPES if wanted is None or r["item_code"] in wanted]
	unmatched = [] if wanted is None else [c for c in wanted if c not in {r["item_code"] for r in RECIPES}]

	for recipe in recipes:
		item_code = recipe["item_code"]
		if not frappe.db.exists("Item", item_code):
			skipped.append({"item_code": item_code, "reason": "item not found"})
			continue

		existing = frappe.db.get_value(
			"Jarz SOP", {"item_code": item_code, "version": recipe["version"]}, "name"
		)

		if dry_run:
			(updated if existing else created).append(item_code)
			continue

		if existing:
			doc = frappe.get_doc("Jarz SOP", existing)
			_apply(doc, recipe)
			doc.save()
			updated.append(f"{item_code} ({doc.name})")
		else:
			doc = frappe.new_doc("Jarz SOP")
			_apply(doc, recipe)
			doc.insert()
			created.append(f"{item_code} ({doc.name})")

	if not dry_run:
		frappe.db.commit()

	result = {
		"dry_run": bool(dry_run),
		"created": created,
		"updated": updated,
		"skipped": skipped,
		"unmatched": unmatched,
	}

	print("=" * 78)
	print("JARZ SOP SEED" + (" (dry run)" if dry_run else ""))
	print("=" * 78)
	for label, rows in (("created", created), ("updated", updated)):
		print(f"{label}: {len(rows)}")
		for row in rows:
			print(f"   {row}")
	if unmatched:
		print(f"no recipe for: {', '.join(unmatched)}")
	if skipped:
		print(f"skipped: {len(skipped)}")
		for row in skipped:
			print(f"   {row['item_code']} — {row['reason']}")
	return result
