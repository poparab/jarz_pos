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

Quantities are stated per the manual's own batch, not per BOM run.  Where the
two disagree the manual wins here and the difference is reported at the end —
a BOM is what the system bills, but the SOP is what the bench actually does,
and quietly rewriting one to match the other would hide a real question.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import flt

# ── Recipe data ─────────────────────────────────────────────────────────
# `capture` turns a step into a checkpoint the operator must answer.  Used only
# where the manual states a measurable target — inventing thresholds would make
# the SOP look authoritative about things nobody measured.

CHEESECAKE_MIX = {
    "item_code": "Cheesecake Mix",
    "version": 1,
    "yield_percent": 100,
    "prep_time_mins": 12,
    "equipment": "Planetary mixer, paddle (كف) attachment",
    "notes": (
        "Batch = 9.520 Kg, which fills 120 medium or 77 large jars.\n"
        "Vanilla is 20 g, confirmed by the owner 2026-08-08. BOM-Cheesecake "
        "Mix-006 still bills 18 g and a yield of 9.518 Kg — both need "
        "correcting. 9.520 is also what the jar BOMs already assume: the medium "
        "carries exactly 9.520/120 = 79.333 g of mix."
    ),
    "steps": [
        {
            "title": "Weigh cheese, powder sugar and vanilla",
            "instruction": (
                "Weigh into the mixer bowl: 2.5 kg Milkana + 2.5 kg Remas "
                "(5 kg cheese total), 1.5 kg powder sugar, 20 g vanilla.\n"
                "يوزن في حلة المضرب: 2.5 كيلو ميلكانا + 2.5 كيلو ريماس "
                "(إجمالي 5 كيلو جبنة)، 1.5 كيلو سكر بودر، 20 جرام فانيليا."
            ),
            "duration_mins": 4,
            "scaling_mode": "Per Batch",
            "capture_type": "Number",
            "capture_label": "Total weighed into bowl (Kg)",
            "capture_min": 6.0,
            "capture_max": 7.0,
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
                "Drop the mixer to speed 1 and add 3 kg dr baker cream "
                "(unsweetened) at medium speed until well combined — about one "
                "minute. Stop the moment it comes together; over-mixing after "
                "the cream goes in breaks the texture.\n"
                "يتم انزال السرعة الي 1 و يضاف الكريمة بسرعة متوسطة حتي تمتزج "
                "جيدا لمدة دقيقة او حتي تمتزج و يغلق بمجرد الامتزاج."
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

FUDGE_CAKE = {
    "item_code": "Fudge Cake",
    "version": 1,
    "yield_percent": 98,
    "prep_time_mins": 60,
    "equipment": "Planetary mixer (whisk then hand), 2 trays, oven",
    "notes": "Batch = 9.258 Kg over 2 trays. BOM inputs total 9.278 Kg — the 0.02 gap looks like a typo in the BOM quantity.",
    "steps": [
        {
            "title": "Whip eggs, sugar and vanilla to near-white",
            "instruction": (
                "Using the whisk, beat 45 eggs + 3.750 kg sugar + 30 g vanilla "
                "on speed 3 until the colour is close to white.\n"
                "يستخدم مضرب السلك في ضرب البيض و السكر و الفانيليا حتي تصل الي "
                "لون اقرب الي الأبيض و المضرب علي سرعه 3."
            ),
            "duration_mins": 10,
            "scaling_mode": "Per Batch",
            "requires_confirmation": 1,
        },
        {
            "title": "Add the dry mix in three additions on speed 1",
            "instruction": (
                "Drop to speed 1 and add the dry mix — 2.700 kg flour, 120 g "
                "baking soda, 570 g cocoa powder, a pinch of salt — in three "
                "additions, each until it disappears.\n"
                "انزال المضرب علي سرعه 1 ثم يضاف اليه الخليط الجاف "
                "( الدقيق – البيكنج بودر – الكاكاو – رشه ملح ) علي ثلاث مرات "
                "حتي يختفي الخليط."
            ),
            "duration_mins": 5,
            "scaling_mode": "Per Batch",
            "requires_confirmation": 1,
        },
        {
            "title": "Combine oil and boiling water, add gradually",
            "instruction": (
                "Mix 2.250 kg oil with 2.250 kg boiling water in a container, "
                "then add gradually to the mixer.\n"
                "يخلط الزيت و الماء المغلي في وعاء ثم يضاف علي الخليط في العجان "
                "تدريجيا."
            ),
            "duration_mins": 5,
            "scaling_mode": "Per Batch",
            "requires_confirmation": 1,
        },
        {
            "title": "Stop the mixer, hand-stir, divide over 2 trays",
            "instruction": (
                "Stop the mixer, stir well by hand with a spoon, then divide "
                "over 2 trays.\n"
                "يفصل العجان و يقلب الخليط بمعلقه جيدا ثم يوزع علي 2 صاج."
            ),
            "duration_mins": 5,
            "scaling_mode": "Per Batch",
            "requires_confirmation": 1,
        },
        {
            "title": "Bake at 180°C for 45 minutes",
            "instruction": (
                "Bake at 180°C for 45 minutes.\n"
                "يسوي الخليط علي درجه حراره 180 لمده 45 دقيقه."
            ),
            "duration_mins": 45,
            "scaling_mode": "Fixed",
            "capture_type": "Temperature",
            "capture_label": "Oven temperature (°C)",
            "capture_min": 170,
            "capture_max": 190,
            "requires_confirmation": 1,
        },
    ],
}

RED_VELVET_CAKE = {
    "item_code": "Red Velvet Cake",
    "version": 1,
    "yield_percent": 98,
    "prep_time_mins": 60,
    "equipment": "Planetary mixer (whisk then hand), 2 trays, oven",
    "notes": "Batch = 9.278 Kg over 2 trays.",
    "steps": [
        {
            "title": "Whip eggs, sugar and vanilla to near-white",
            "instruction": (
                "Using the whisk, beat 45 eggs + 3.750 kg sugar + 30 g vanilla "
                "on speed 3 until the colour is close to white.\n"
                "يستخدم مضرب السلك في ضرب البيض و السكر و الفانيليا حتي تصل الي "
                "لون اقرب الي الأبيض و المضرب علي سرعه 3."
            ),
            "duration_mins": 10,
            "scaling_mode": "Per Batch",
            "requires_confirmation": 1,
        },
        {
            "title": "Add the dry mix with the red colour, three additions",
            "instruction": (
                "Drop to speed 1 and add the dry mix — 3.150 kg flour, 120 g "
                "baking soda, 75 g cocoa powder, a pinch of salt, 45 g red "
                "colour (دم الغزال) — in three additions until it disappears.\n"
                "انزال المضرب علي سرعه 1 ثم يضاف اليه الخليط الجاف "
                "( الدقيق – البيكنج بودر – الكاكاو – رشه ملح – اللون الاحمر ) "
                "علي ثلاث مرات حتي يختفي الخليط."
            ),
            "duration_mins": 5,
            "scaling_mode": "Per Batch",
            "requires_confirmation": 1,
        },
        {
            "title": "Combine oil and boiling water, add gradually",
            "instruction": (
                "Mix 2.250 kg oil with 2.250 kg boiling water, then add "
                "gradually to the mixer.\n"
                "يخلط الزيت و الماء المغلي في وعاء ثم يضاف علي الخليط في العجان "
                "تدريجيا."
            ),
            "duration_mins": 5,
            "scaling_mode": "Per Batch",
            "requires_confirmation": 1,
        },
        {
            "title": "Stop the mixer, hand-stir, divide over 2 trays",
            "instruction": (
                "Stop the mixer, stir well by hand, divide over 2 trays.\n"
                "يفصل العجان و يقلب الخليط بمعلقه جيدا ثم يوزع علي 2 صاج."
            ),
            "duration_mins": 5,
            "scaling_mode": "Per Batch",
            "requires_confirmation": 1,
        },
        {
            "title": "Bake at 180°C for 45 minutes",
            "instruction": (
                "Bake at 180°C for 45 minutes.\n"
                "يسوي الخليط علي درجه حراره 180 لمده 45 دقيقه."
            ),
            "duration_mins": 45,
            "scaling_mode": "Fixed",
            "capture_type": "Temperature",
            "capture_label": "Oven temperature (°C)",
            "capture_min": 170,
            "capture_max": 190,
            "requires_confirmation": 1,
        },
    ],
}

SAVOIARDI = {
    "item_code": "Savoiardi",
    "version": 1,
    "yield_percent": 80,
    "prep_time_mins": 45,
    "equipment": "Large planetary mixer (whisk), small mixer, sieve, 2 silicone-lined trays, oven",
    "notes": (
        "Batch = 2.5 Kg from 30 eggs. Whites and yolks are whipped separately; "
        "the whites are the structure, so stop the moment they hold stiff peaks."
    ),
    "steps": [
        {
            "title": "Preheat oven to 180°C and separate 30 eggs",
            "instruction": (
                "Preheat the oven to 180°C. Separate 30 eggs, whites from "
                "yolks. Split 900 g sugar into 450 g and 450 g.\n"
                "يتم تشغيل الفرن للتسخين علي درجة حرارة 180. 30 بيضه مفصول "
                "البياض عن الصفار. 900 جرام سكر مقسمين الي 450 و 450 جرام."
            ),
            "duration_mins": 8,
            "scaling_mode": "Per Batch",
            "requires_confirmation": 1,
        },
        {
            "title": "Whip yolks with sugar, glucose and vanilla",
            "instruction": (
                "To the yolks add 450 g sugar, 25 g glucose honey and 12 g "
                "vanilla. Whip in the small mixer with the whisk until the "
                "colour turns creamy.\n"
                "يضاف الي الصفار 450 جرام سكر و 25 جرام عسل جلوكوز و 12 جرام "
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
                "Move to speed 3 and add the remaining 450 g sugar gradually "
                "until the mix is glossy and — most importantly — holds stiff "
                "peaks.\n"
                "يتم نقل المضرب علي سرعة 3 و يتم إضافة السكر تدريجيا حتي وصول "
                "الخليط الي لمعة و الأهم ان يكون قمم قوية."
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
                "Add the flour + starch + baking powder mix (480 g flour, 200 g "
                "cornstarch, 3 g baking powder) through the sieve, on speed 1.\n"
                "يتم إضافة خليط الدقيق و النشا و البيكنج بودر علي سرعة 1 عن "
                "طريق المصفاة في المضرب."
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
            "title": "Divide over 2 silicone trays and bake 180°C / 15 min",
            "instruction": (
                "Divide over two silicone-lined trays and bake at 180°C for 15 "
                "minutes.\n"
                "يقسم الخليط علي صاجين اسفلهم سليكون و يدخل الفرن علي حرارة 180 "
                "لمدة 15 دقيقة."
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
    "version": 1,
    "yield_percent": 80,
    "prep_time_mins": 50,
    "equipment": "Planetary mixer (whisk then paddle), sieve, 3 trays, oven",
    "notes": (
        "Batch = 4.0 Kg from 45 eggs. Note the last step: after cooling the "
        "sheet is ground and dried at 140°C for 30 minutes, stirred halfway."
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
                "Combine 1050 g flour, 210 g cornstarch, 9 g salt and 9 g "
                "baking powder to make the dry mix.\n"
                "خلط الدقيق و النشا و الملح و البيكنج بودر لتجهيز الخليط الناشف."
            ),
            "duration_mins": 5,
            "scaling_mode": "Per Batch",
            "requires_confirmation": 1,
        },
        {
            "title": "Whip 45 eggs with sugar and vanilla to triple volume",
            "instruction": (
                "Whip 45 eggs with 1260 g sugar and 50 g vanilla on speed 3 "
                "with the whisk until the volume triples.\n"
                "يتم خفق البيض مع السكر و الفانيليا حتي يتضاعف حجمة الي 3 اضعاف "
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
                "Take a portion of the batter and mix it thoroughly with 180 g "
                "oil warmed to 45–50°C. Once combined, return it to the mixer, "
                "fold briefly, then stop.\n"
                "بعد ان يمتزج يتم اخذ جزء من الخليط ووضعة علي الزيت بدرجة حرارة "
                "من 45 الي 50 و يتم تقليبة جيدا و بعد الامتزاج يعاد الخليط الي "
                "المضرب و يتم التقليب قليلا ثم الفصل."
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
            "title": "Hand-fold, divide over 3 trays, bake 175°C / 20 min",
            "instruction": (
                "Fold by hand a little to confirm it is combined, divide over 3 "
                "trays and bake at 175°C for 20 minutes.\n"
                "يتم التقليب يدويا قليلا للتاكد من الامتزاج و توزيع الخليط علي 3 "
                "صاجات و ادخالة الي الفرن في درجة حراره 175 لمدة 20 دقيقة."
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

# The Tiramisu jars, per the owner's method of 2026-10-08 (superseding the
# 2026-08-08 "half a shot per jar" note, which is why the old single-recipe
# TIRAMISU_ASSEMBLY for the Medium is gone).
#
#   grinds -> liquid coffee at 1:3 by weight        (16 g -> 48 g)
#   powder sugar        = liquid x 0.3, dissolved in the hot coffee
#   syrup into the cream = the SUGAR weight, folded into the cheesecake mix
#   syrup onto savoiardi = the rest, i.e. the liquid weight
#
# Every size is made in the same session: one pot of coffee, one bowl of
# cream, then the jars.  So the three SOPs share ONE set of steps, word for
# word, and ``get_recipe_sheet`` merges them into a single sheet: the bowl
# steps quote the whole run (all sizes summed) and the jar step, written with
# ``|each`` tokens, prints one line per size.  Nothing in the steps is static
# text any more - every figure follows the jar BOM.
#
# Per-jar spec in grams, kept for the notes and checked by the tests against
# what the ``|each`` tokens render.  ``version`` was bumped when the steps
# became shared (L 1->2, M 2->3, S 1->2) so the per-size v1/v2 stay on file.
TIRAMISU_JAR_PORTIONS: Dict[str, Dict[str, Any]] = {
    "Tiramisu Large": {"version": 2, "cream": 97, "syrup": 24, "savoiardi": 40, "cocoa": 3},
    "Tiramisu Medium": {"version": 3, "cream": 70, "syrup": 16, "savoiardi": 28, "cocoa": 2},
    # Small is exactly 2/3 of the Medium.
    "Tiramisu Small": {
        "version": 2,
        "cream": 70 * 2 / 3,
        "syrup": 16 * 2 / 3,
        "savoiardi": 28 * 2 / 3,
        "cocoa": 2 * 2 / 3,
    },
}

_GRINDS = "{{item:Coffee beans|grams}}"
_LIQUID = "{{item:Coffee beans|grams|x3}}"
_SUGAR = "{{item:powder sugar|grams}}"
_MIX = "{{item:Cheesecake Mix|grams}}"
# The cream is the mix plus the sweet coffee folded into it (= the sugar weight).
_CREAM = "{{item:Cheesecake Mix+powder sugar|grams}}"

TIRAMISU_STEPS: List[Dict[str, Any]] = [
    {
        "title": "Brew the coffee",
        "instruction": (
            f"Brew {_GRINDS} of coffee grinds into {_LIQUID} of liquid coffee "
            "(1 to 3). Weigh the liquid: if it is short brew a little more, if "
            f"it is over keep only {_LIQUID}.\n"
            f"اعمل {_LIQUID} قهوة سايلة من {_GRINDS} بن مطحون (1 لـ 3). "
            "اوزن القهوة السايلة: لو ناقصة اعمل شوية كمان، و لو زادت خد "
            f"{_LIQUID} بس."
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
            f"Dissolve {_SUGAR} of powder sugar in the hot coffee, then let it "
            "cool.\n"
            f"دوب {_SUGAR} سكر بودر في القهوة و هي سخنة، و سيبها تبرد."
        ),
        "duration_mins": 5,
        "scaling_mode": "Fixed",
        "requires_confirmation": 1,
    },
    {
        "title": "Make the cream in one bowl",
        "instruction": (
            f"Mix {_MIX} of cheesecake mix with {_SUGAR} of the sweet coffee = "
            f"{_CREAM} of cream. The rest of the coffee ({_LIQUID}) is for the "
            "savoiardi.\n"
            f"اخلط {_MIX} خليط تشيز كيك مع {_SUGAR} من القهوة المحلاة = "
            f"{_CREAM} كريمة. باقي القهوة ({_LIQUID}) للسافوياردي."
        ),
        "duration_mins": 10,
        "scaling_mode": "Fixed",
        "requires_confirmation": 1,
    },
    {
        "title": "Fill each jar",
        "instruction": (
            "{{item:Savoiardi|grams|each}} savoiardi + "
            "{{item:Coffee beans|grams|x3|each}} coffee, then "
            "{{item:Cheesecake Mix+powder sugar|grams|each}} cream, then "
            "{{item:coco powder|grams|each}} cocoa on top.\n"
            "{{item:Savoiardi|grams|each}} سافوياردي + "
            "{{item:Coffee beans|grams|x3|each}} قهوة، بعدين "
            "{{item:Cheesecake Mix+powder sugar|grams|each}} كريمة، بعدين "
            "{{item:coco powder|grams|each}} كاكاو على الوش."
        ),
        "duration_mins": 1,
        "scaling_mode": "Per Unit",
        "requires_confirmation": 1,
    },
    {
        "title": "Lid and label",
        "instruction": (
            "Close each jar and stick on its label.\n"
            "اقفل كل برطمان و الزق الليبل بتاعه."
        ),
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


def build_tiramisu_sop(item_code: str, portions: Dict[str, Any]) -> Dict[str, Any]:
    """One Tiramisu jar SOP: the shared steps plus this size's spec in the notes."""
    cream = _grams(portions["cream"])
    syrup = _grams(portions["syrup"])
    savoiardi = _grams(portions["savoiardi"])
    cocoa = _grams(portions["cocoa"])

    return {
        "item_code": item_code,
        "version": portions["version"],
        "yield_percent": 100,
        "prep_time_mins": 30,
        "equipment": "Coffee brewer, scales, mixing bowl, jars",
        "notes": (
            "Method per the owner, 2026-10-08, superseding the 2026-08-08 "
            "half-shot note.\n"
            "Coffee grinds become liquid coffee at 1:3 by weight (16 g -> 48 g). "
            "Extraction varies, so the kitchen weighs the liquid: the liquid "
            "weight is what the recipe needs. Powder sugar = liquid x 0.3, "
            "dissolved in the hot coffee. An amount of sweetened coffee equal to "
            "the SUGAR weight is folded into the cheesecake mix to make the "
            "tiramisu cream; the rest (= the liquid weight) goes onto the "
            "savoiardi.\n"
            "All sizes are made together, so every Tiramisu SOP has the same "
            "steps and the app shows them as one sheet.\n"
            f"Per jar: cream {cream}, savoiardi {savoiardi}, coffee syrup onto "
            f"the savoiardi {syrup}, cocoa {cocoa}. Every figure in the steps "
            "comes from the jar BOM."
        ),
        # A copy per SOP: ``_apply`` only reads, but shared dicts invite a
        # later edit that changes all three sizes by accident.
        "steps": [dict(step) for step in TIRAMISU_STEPS],
    }


TIRAMISU_SOPS: List[Dict[str, Any]] = [
    build_tiramisu_sop(item_code, portions)
    for item_code, portions in TIRAMISU_JAR_PORTIONS.items()
]

RECIPES: List[Dict[str, Any]] = [
    CHEESECAKE_MIX,
    FUDGE_CAKE,
    RED_VELVET_CAKE,
    SAVOIARDI,
    SPONGE_CAKE,
    *TIRAMISU_SOPS,
]


# ── Seeder ──────────────────────────────────────────────────────────────


def _default_bom(item_code: str) -> Optional[str]:
    return frappe.db.get_value(
        "BOM", {"item": item_code, "is_default": 1, "docstatus": 1}, "name"
    )


def _apply(doc, recipe: Dict[str, Any]) -> None:
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


def _parse_items(items: Any) -> Optional[List[str]]:
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
    codes: List[str] = []
    for code in raw:
        code = str(code).strip()
        if code and code not in codes:
            codes.append(code)
    return codes or None


def run(dry_run: Any = False, items: Any = None) -> Dict[str, Any]:
    """Create or refresh one SOP per recipe.  Reports before it writes.

    ``items`` (a list, or a comma-separated string of item codes) restricts the
    run to those recipes; the default seeds everything.  A recipe with a newer
    ``version`` than the one on the site is inserted as a new record, and the
    Jarz SOP controller's ``on_update`` deactivates the older active version,
    so seeding a v2 leaves v2 active and the v1 on file, inactive.
    """
    created: List[str] = []
    updated: List[str] = []
    skipped: List[Dict[str, str]] = []

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
