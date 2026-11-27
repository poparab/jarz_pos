"""Recipe Book recipes that are not ordinary BOM SOPs.

Kitchen trial recipes (from the Kitchen Trial Sheet) and approved recipes that
have no Item/BOM in ERPNext yet. Pure data: imported by seed_recipe_sops.py.

Step quantities are tokens ({{item:<ingredient name>|grams}} / |count) that
must match an ingredient of the same recipe exactly. Quantities are for the
recipe's base batch (the "yield" below).
"""


def _ing(ingredient, qty, uom="g", section=None, note=None):
	return {
		"ingredient": ingredient,
		"qty": float(qty),
		"uom": uom,
		"section": section,
		"note": note,
		"item_code": None,
	}


def _step(title, instruction, mins, mode="Fixed", cap=None):
	step = {
		"title": title,
		"instruction": instruction,
		"duration_mins": float(mins),
		"scaling_mode": mode,
		"requires_confirmation": 1,
	}
	if cap:
		step["capture_type"] = "Temperature"
		step["capture_label"] = cap[0]
		step["capture_min"] = cap[1]
		step["capture_max"] = cap[2]
	return step


def _recipe(title, stage, track, yield_qty, yield_uom, prep, equipment, notes,
		ingredients, steps, version=1, approved_on=None, change_summary=""):
	return {
		"recipe_title": title,
		"item_code": None,
		"version": version,
		"stage": stage,
		"track": track,
		"approved_on": approved_on,
		"change_summary": change_summary,
		"yield_qty": float(yield_qty),
		"yield_uom": yield_uom,
		"prep_time_mins": prep,
		"equipment": equipment,
		"notes": notes,
		"ingredients": ingredients,
		"steps": steps,
	}


STD = "Standard line"
COOKIE = "Cookie jar line"

# ---------------------------------------------------------------- C
COCOA_SYRUP = _recipe(
	"S1-L cocoa syrup (the official soak)", "Trial", STD, 1830, "g", 15,
	"Pot, strainer, thermometer",
	(
		"Decided 2026-11-07: soak = 15% of the cake weight, for the Molten and London cake only. "
		"In ERPNext it is the sub-assembly Cocoa soak syrup (1.824 kg per batch, without the optional coffee).\n"
		"Cocoa: Cacao Barry Extra Brute (alkalised) only.\n"
		"The trial used a sixth of the batch (300 g) because the London jars need only 117 g.\n"
		"Why the coffee: you won't taste coffee, it deepens the chocolate and cuts the sweetness.\n"
		"Watch: a small pot loses water fast, so top up with hot water to the right weight. "
		"Throw away leftover syrup at the end of the day; make it fresh on each trial day."
	),
	[
		_ing("Water", 1260),
		_ing("Sugar", 360),
		_ing("Glucose", 120),
		_ing("Cocoa", 72, note="Cacao Barry Extra Brute"),
		_ing("Salt", 6),
		_ing("Vanilla", 6),
		_ing("Instant coffee", 6, note="optional"),
	],
	[
		_step(
			"Mix the cocoa into the sugar",
			"Mix {{item:Cocoa|grams}} cocoa (and {{item:Instant coffee|grams}} instant coffee, if used) into "
			"{{item:Sugar|grams}} sugar first so it doesn't clump.\n"
			"اخلط {{item:Cocoa|grams}} كاكاو (و {{item:Instant coffee|grams}} نسكافيه لو هتستخدمه) في "
			"{{item:Sugar|grams}} سكر الأول عشان ما يتكتلش.",
			3,
		),
		_step(
			"Add water, glucose and salt, and boil",
			"Add {{item:Water|grams}} water, {{item:Glucose|grams}} glucose and {{item:Salt|grams}} salt. "
			"Boil 1-2 minutes, stirring.\n"
			"ضيف {{item:Water|grams}} مية و {{item:Glucose|grams}} جلوكوز و {{item:Salt|grams}} ملح. "
			"سيبه يغلي من دقيقة لدقيقتين وانت بتقلب.",
			5,
		),
		_step(
			"Vanilla, strain, top up",
			"Off the heat, add {{item:Vanilla|grams}} vanilla and strain. If it boiled down, top up with hot "
			"water until the syrup weighs the full batch again.\n"
			"بعيد عن النار ضيف {{item:Vanilla|grams}} فانيليا وصفّي الشربات. لو نقص من الغليان ضيف مية سخنة "
			"لحد ما الوزن يرجع زي الأول.",
			3,
		),
		_step(
			"Cool to pouring temperature",
			"Cool to 40-50 °C. Pour it warm onto room-temperature cake.\n"
			"سيبه يبرد لـ 40-50 درجة. صبّه دافي على كيك في درجة حرارة الغرفة.",
			15,
			cap=("Syrup temperature before pouring (°C)", 40, 50),
		),
	],
)

# ---------------------------------------------------------------- D
SOAK_15 = _recipe(
	"Soak the cake at 15%", "Trial", STD, 897, "g", 45,
	"Tray, spoon, flat spatula",
	(
		"Base here is the trial run: 12 London Medium jars (780 g cake + 117 g syrup = 897 g soaked cake, "
		"75 g per jar, then 15 + 15 g ganache).\n"
		"Per jar once the soaked Molten is launched: Large 105 g cake + 15.75 g syrup = 121 g soaked (then 75 g ganache); "
		"Medium 75 + 11.25 = 86 g (45 g ganache); Small 50 + 7.5 = 57.5 g (30 g ganache).\n"
		"Watch: the cake must be at room temperature. Never soak cold cake from the fridge, and never pour a hot "
		"soak onto hot cake. Soak only the cake you will fill now."
	),
	[
		_ing("Fudge Cake", 780, note="room temperature, cut in jar-size pieces"),
		_ing("Cocoa soak syrup", 117, note="15% of the cake weight, 40-50 °C"),
	],
	[
		_step(
			"Cut and spread the cake",
			"Cut {{item:Fudge Cake|grams}} cake into jar-size pieces, no crumbs. Spread in a tray in one layer.\n"
			"قطّع {{item:Fudge Cake|grams}} كيك قطع على قد البرطمان من غير فتات. فردها في صينية طبقة واحدة.",
			8,
			"Per Batch",
		),
		_step(
			"Pour the syrup in two halves",
			"Weigh the syrup: cake weight x 15% = {{item:Cocoa soak syrup|grams}}. Pour half evenly with a spoon, "
			"wait 2 minutes, pour the rest. Do not stir.\n"
			"زن الشربات: وزن الكيك × 15% = {{item:Cocoa soak syrup|grams}}. صب النص بالتساوي بالمعلقة، "
			"استنى دقيقتين، وصب الباقي. ما تقلبش.",
			5,
			"Per Batch",
		),
		_step(
			"Cover and rest",
			"Cover and rest 15-30 minutes, until no liquid is left in the tray. Press lightly with a flat spatula.\n"
			"غطي واسيبه من 15 لـ 30 دقيقة لحد ما مفيش سائل في الصينية. اضغط بخفة بسباتولا مسطحة.",
			25,
		),
	],
)

# ---------------------------------------------------------------- L
LONDON_CARAMEL = _recipe(
	"London cake: whipped salted caramel", "Trial", STD, 367, "g", 40,
	"Small pan, long whisk, probe thermometer, infrared thermometer, stick blender",
	(
		"Three small batches side by side (W1, W2, W3) to find the one that still holds its shape on day 3 in a sealed jar. "
		"Make them the evening before. Edd Kimber's original ratio: sugar 150 : cream 200 : butter 15.\n"
		"W1 = original (first four ingredients). W2 = W1 + 40 g white chocolate. W3 = W2 + 2 g gelatine (halal beef) "
		"soaked in 12 g cold water. The quantities are one small batch of each. Yield shown is W1.\n"
		"London jar (Medium): 75 g soaked cake (65 g Fudge Cake soaked at 15%), 15 g ganache (fridge 10 min), "
		"45 g whipped caramel piped, 15 g ganache on top at 30 °C or cooler, a pinch of flaky salt; chill uncovered 30 min, then lid.\n"
		"Watch: a candy probe can't read a thin layer of melted sugar, so use the infrared for the caramel (aim at the caramel, "
		"not the shiny pan). Over-whipped caramel turns grainy: stop at once. Top ganache at most 30 °C."
	),
	[
		_ing("Sugar", 150),
		_ing("Cream 35%", 200),
		_ing("Butter", 15),
		_ing("Fine sea salt", 2),
		_ing("White chocolate", 40, section="W2 and W3 only"),
		_ing("Gelatine", 2, section="W3 only", note="halal beef"),
		_ing("Cold water for gelatine", 12, section="W3 only"),
	],
	[
		_step(
			"Weigh everything first",
			"Weigh {{item:Sugar|grams}} sugar, {{item:Cream 35%|grams}} cream, {{item:Butter|grams}} butter and "
			"{{item:Fine sea salt|grams}} salt. W3: soak {{item:Gelatine|grams}} gelatine in "
			"{{item:Cold water for gelatine|grams}} cold water.\n"
			"زن كل حاجة الأول: {{item:Sugar|grams}} سكر، {{item:Cream 35%|grams}} كريمة، {{item:Butter|grams}} زبدة، "
			"{{item:Fine sea salt|grams}} ملح. في W3 انقع {{item:Gelatine|grams}} جيلاتين في "
			"{{item:Cold water for gelatine|grams}} مية ساقعة.",
			5,
		),
		_step(
			"Heat the cream",
			"Heat the cream to 85-90 °C and keep it hot. Cold cream makes the caramel seize.\n"
			"سخن الكريمة لـ 85-90 درجة وسيبها سخنة. الكريمة الباردة بتخلي الكراميل يتجمد.",
			5,
			cap=("Cream temperature (°C)", 85, 90),
		),
		_step(
			"Make the dry caramel",
			"Melt the sugar dry on medium heat. Swirl the pan; no spoon until it is all liquid. Take it to 185 °C, dark copper. "
			"Never above 195 °C (burnt).\n"
			"دوّب السكر ناشف على نار متوسطة. حرك الحلة بإيدك من غير معلقة لحد ما كله يسيح. وصله لـ 185 درجة لون نحاسي غامق. "
			"ما يعدّيش 195 (يتحرق).",
			10,
			cap=("Caramel temperature (°C)", 185, 195),
		),
		_step(
			"Butter, salt and hot cream",
			"Off the heat: add the butter and salt, then the hot cream in 2 additions. It spits and steams: long sleeves, "
			"long whisk. If hard lumps stay, stir on low heat only until smooth. Do not boil it down.\n"
			"بعيد عن النار: ضيف الزبدة والملح وبعدين الكريمة السخنة على مرتين. بيطرطش وفيه بخار: اكمام طويلة وخفاقة طويلة. "
			"لو فضل كتل، قلب على نار هادية لحد ما يبقى ناعم من غير ما تغلي.",
			5,
		),
		_step(
			"W3: add the gelatine",
			"W3 only: stir in the soaked gelatine once the caramel is below 80 °C.\n"
			"W3 بس: قلّب الجيلاتين لما الكراميل ينزل تحت 80 درجة.",
			1,
			cap=("Caramel temperature when gelatine goes in (°C)", 60, 80),
		),
		_step(
			"W2 and W3: pour over the white chocolate",
			"W2 and W3: at 70-80 °C pour the caramel over {{item:White chocolate|grams}} white chocolate, wait 1 minute, "
			"then stick-blend with the blade under the surface.\n"
			"W2 و W3: على 70-80 درجة صب الكراميل على {{item:White chocolate|grams}} وايت شوكولاتة، استنى دقيقة، "
			"وبعدين اخلط بالهاند بلندر والسلاح تحت السطح.",
			4,
			cap=("Temperature when poured over the chocolate (°C)", 70, 80),
		),
		_step(
			"Cool and chill overnight",
			"Cool to about 30 °C, press film onto the surface, label W1 / W2 / W3, fridge overnight.\n"
			"برّده لحوالي 30 درجة، حط نايلون لازق على السطح، اكتب W1 / W2 / W3، وحطه في التلاجة لحد تاني يوم.",
			10,
		),
		_step(
			"Next day: whip cold",
			"Next day, at 4-6 °C: whip in a cold bowl on medium-high to soft peaks (1-3 minutes). Stop at once: "
			"over-whipped turns grainy.\n"
			"تاني يوم على 4-6 درجة: اخفق في بولة ساقعة على سرعة فوق المتوسطة لحد قمم طرية (1-3 دقائق). "
			"وقف فورا: لو زودت بيحبب.",
			3,
			cap=("Caramel temperature before whipping (°C)", 4, 6),
		),
	],
)

# ---------------------------------------------------------------- P
APPLE_FILLING = _recipe(
	"Apple pie cheesecake: apple filling", "Trial", STD, 1300, "g", 45,
	"Wide pan, tray",
	(
		"Loosened version, changed 2026-11-10: the first batch, cooked short for the bite, came out dry. The 20-minute "
		"rest draws the juice out before the pan, and 80 g water per kg makes enough sauce. Cornstarch down from 15 g to 12 g "
		"per kg because 15 g made the small amount of juice a paste.\n"
		"Quantities are per 1 kg of apples; the trial used 300 g apples (3 medium apples about 420 g). "
		"Yield is approximate (weight after cooking).\n"
		"Apples: firm and tart (green), 1 cm dice, weighed after peeling and coring; buy about 1.4 kg whole per 1 kg. "
		"Soft red apples turn to mush and taste flat.\n"
		"Watch: the cornstarch must boil 1 minute to thicken. The sauce should still flow a little in the pan; it sets firmer in the fridge. "
		"Cook the day before."
	),
	[
		_ing("Apples", 1000, note="firm, tart, green; 1 cm dice; weighed after peeling and coring"),
		_ing("Butter", 40),
		_ing("Brown sugar", 120),
		_ing("Lemon juice", 20),
		_ing("Cinnamon", 6),
		_ing("Salt", 1),
		_ing("Water", 80, note="for the sauce"),
		_ing("Cornstarch", 12),
		_ing("Cold water for cornstarch", 40),
	],
	[
		_step(
			"Toss the apples with lemon and sugar, and rest",
			"Toss {{item:Apples|grams}} diced apple with {{item:Lemon juice|grams}} lemon juice at once so it doesn't brown, "
			"then with {{item:Brown sugar|grams}} brown sugar. Rest 20 minutes: the sugar pulls juice out of the apple.\n"
			"قلّب {{item:Apples|grams}} تفاح مقطع مكعبات مع {{item:Lemon juice|grams}} عصير ليمون على طول عشان ما يسمرش، "
			"وبعدين مع {{item:Brown sugar|grams}} سكر بني. سيبه 20 دقيقة: السكر بيسحب عصير التفاح.",
			22,
			"Per Batch",
		),
		_step(
			"Cook the apples",
			"Melt {{item:Butter|grams}} butter in a wide pan. Add the apple with all its juice, {{item:Water|grams}} water, "
			"{{item:Cinnamon|grams}} cinnamon and {{item:Salt|grams}} salt. Cook on medium 4-5 minutes, stirring, until the apple "
			"is just tender but keeps a little bite.\n"
			"دوّب {{item:Butter|grams}} زبدة في حلة واسعة. ضيف التفاح بكل عصيره و {{item:Water|grams}} مية و "
			"{{item:Cinnamon|grams}} قرفة و {{item:Salt|grams}} ملح. اطبخ على نار متوسطة 4-5 دقائق وانت بتقلب لحد ما "
			"التفاح يطرى بس يفضل فيه قوام.",
			6,
			"Per Batch",
		),
		_step(
			"Thicken with the cornstarch",
			"Mix {{item:Cornstarch|grams}} cornstarch in {{item:Cold water for cornstarch|grams}} cold water. Stir it in and boil "
			"1 minute until the sauce is glossy and coats the pieces. It should still flow a little in the pan.\n"
			"اخلط {{item:Cornstarch|grams}} نشا في {{item:Cold water for cornstarch|grams}} مية ساقعة. قلّبها في الحلة واغلي "
			"دقيقة لحد ما الصوص يلمع ويغطي القطع. لازم يفضل بيسيح شوية في الحلة.",
			2,
		),
		_step(
			"Cool fast and chill",
			"Spread on a tray to cool fast, then fridge, covered.\n"
			"فردها في صينية عشان تبرد بسرعة، وبعدين في التلاجة مغطاة.",
			10,
		),
	],
)

APPLE_CRUMBS = _recipe(
	"Apple pie cheesecake: cinnamon biscuit crumbs", "Trial", STD, 1070, "g", 20,
	"Tray, airtight boxes",
	(
		"Our normal Butter Biscuit with cinnamon added after it is crumbled, so the main biscuit batch (Lotus, Blueberry, "
		"Strawberry) never changes. 1% cinnamon + 4% sugar + 2% butter on the crumbs: the butter makes the cinnamon stick, the "
		"sugar spreads it and softens its bitterness.\n"
		"Trial: AP-M (mixed cold) against AP-T (warmed 5 min at 150 °C then cooled completely). If AP-M tastes as good, skip the oven step.\n"
		"Quantities are per 1 kg of crumbs; the trial used 220 g biscuit, 2 g cinnamon, 9 g powder sugar, 4 g butter.\n"
		"Coarse pieces (0.5-1 cm) go on top and stay crunchy longer; finer crumbs are for the base."
	),
	[
		_ing("Butter Biscuit", 1000, note="our normal biscuit, crumbled"),
		_ing("Cinnamon", 10),
		_ing("Powder sugar", 40),
		_ing("Butter", 20, note="melted"),
	],
	[
		_step(
			"Crumble the biscuit",
			"Crumble {{item:Butter Biscuit|grams}} biscuit: coarse pieces 0.5-1 cm for the top, finer for the base.\n"
			"فتّت {{item:Butter Biscuit|grams}} بسكويت: قطع خشنة 0.5-1 سم للوش، وأنعم للقاعدة.",
			5,
			"Per Batch",
		),
		_step(
			"Cinnamon into the powder sugar",
			"Mix {{item:Cinnamon|grams}} cinnamon into {{item:Powder sugar|grams}} powder sugar first so it spreads evenly.\n"
			"اخلط {{item:Cinnamon|grams}} قرفة في {{item:Powder sugar|grams}} سكر بودر الأول عشان تتوزع بالتساوي.",
			2,
		),
		_step(
			"Butter, then the cinnamon sugar",
			"Drizzle {{item:Butter|grams}} melted butter over the crumbs, toss, then toss in the cinnamon sugar until every "
			"piece is coated.\n"
			"رش {{item:Butter|grams}} زبدة مدوبة على الفتات وقلّب، وبعدين قلّب فيها سكر القرفة لحد ما كل قطعة تتغطى.",
			5,
			"Per Batch",
		),
		_step(
			"Split in two and store",
			"Split in two. AP-M: use as it is. AP-T: spread on a tray, 150 °C for 5 minutes, then cool completely. "
			"Keep both in airtight boxes.\n"
			"قسّمه نصين. AP-M: استخدمه زي ما هو. AP-T: فرده في صينية على 150 درجة لمدة 5 دقائق وسيبه يبرد خالص. "
			"احفظ الاتنين في علب مقفولة.",
			10,
		),
	],
)

APPLE_JAR = _recipe(
	"Apple pie cheesecake jar (Medium)", "Trial", STD, 1, "jar", 10,
	"Spoon, tamper",
	(
		"A winter jar (Nov-Feb). About 167 g per jar. Check there is still a little room under the lid; if not, take the "
		"Cheesecake Mix down to 65 g.\n"
		"Why: our Butter Biscuit is a butter shortbread, so it tastes like pie crust and like crumble (no new crumble recipe, no new stock item). "
		"The apple layer is 45 g instead of 30 g because the apple is the hero and is cheaper than the spreads. "
		"No caramel for now; if the jar tastes plain on day 1, a small caramel drizzle is the first thing to add.\n"
		"Tasted on day 1 and day 3 (AP-M cold crumbs against AP-T warmed crumbs)."
	),
	[
		_ing("Cinnamon biscuit crumbs, fine", 40, note="pressed"),
		_ing("Cheesecake Mix", 70),
		_ing("Cold apple filling", 45),
		_ing("Cinnamon biscuit pieces, coarse", 12, note="on top, last"),
	],
	[
		_step(
			"Base",
			"Press {{item:Cinnamon biscuit crumbs, fine|grams}} of the finer cinnamon biscuit crumbs into the jar.\n"
			"اكبس {{item:Cinnamon biscuit crumbs, fine|grams}} من فتات البسكويت بالقرفة الناعم في البرطمان.",
			1,
			"Per Batch",
		),
		_step(
			"Cheesecake Mix",
			"Add {{item:Cheesecake Mix|grams}} Cheesecake Mix.\n"
			"ضيف {{item:Cheesecake Mix|grams}} تشيز كيك ميكس.",
			1,
			"Per Batch",
		),
		_step(
			"Apple filling",
			"Add {{item:Cold apple filling|grams}} cold apple filling.\n"
			"ضيف {{item:Cold apple filling|grams}} حشو التفاح بارد.",
			1,
			"Per Batch",
		),
		_step(
			"Coarse pieces and chill",
			"Put {{item:Cinnamon biscuit pieces, coarse|grams}} coarse cinnamon biscuit pieces on top, last. Chill uncovered "
			"30 minutes, then lid.\n"
			"حط {{item:Cinnamon biscuit pieces, coarse|grams}} قطع بسكويت بالقرفة الخشنة على الوش في الآخر. سيبه في "
			"التلاجة من غير غطا 30 دقيقة وبعدين اقفله.",
			30,
		),
	],
)

# ---------------------------------------------------------------- K
COOKIE_V2 = _recipe(
	"Cookie jar: second bake", "Trial", COOKIE, 4, "jars", 90,
	"Light-coloured pot, spatula, sieve, probe thermometer, filling machine, rack",
	(
		"One batch of dough makes 4 jars (330 jar, 250 g dough + 20 g core). Jars 1-2 are eaten fresh; jars 3-4 are frozen, "
		"thawed 24 h in the fridge and reheated like a branch would (lid off, microwave 20 s, then 5 s more at a time).\n"
		"Style: thick, chewy, gooey, eaten with a spoon from a jar and reheated.\n"
		"Why: browned butter gives a nutty toffee taste; only 5 g of the boiled-off water goes back (the first bake put back "
		"35 g and steam lifted it); spatula only, because whisking beats in air; no cornstarch, for more bite; chocolate in "
		"sieved 1 cm chunks so you taste cookie first; 24 h rest in the fridge (no more than 72 h).\n"
		"Watch: butter at 35 °C or below before the eggs go in; dough fridge-cold, never frozen; cool on a rack, never on cold "
		"steel; lid only when fully cool; at least 1.5 cm of dough under and over the core and 1 cm of core from the glass.\n"
		"If it still tastes mostly of chocolate, next time use 100 g. If it is now too dense or dry, put back 10 g more water "
		"and 1 g more soda first.\n"
		"Cost per jar at 23 Nov production prices about 82.5 EGP (first bake about 92.6 EGP), energy and labour not included."
	),
	[
		_ing("Butter", 227, section="Dough", note="Flechard, browned"),
		_ing("Water", 5, section="Dough", note="put back after browning, only part of what boils off"),
		_ing("Brown sugar", 200, section="Dough", note="or 185 g white + 15 g black molasses"),
		_ing("White sugar", 100, section="Dough"),
		_ing("Whole eggs", 75, section="Dough", note="beaten and weighed; 1.5 eggs, 3 eggs = 2 batches"),
		_ing("Vanilla", 5, section="Dough"),
		_ing("Flour", 300, section="Dough", note="the cake flour we already use"),
		_ing("Baking soda", 3, section="Dough", note="small scale"),
		_ing("Salt", 5, section="Dough", note="small scale"),
		_ing("Dark chocolate 60%", 150, section="Dough", note="Belcolade, 1 cm chunks, sieved"),
		_ing("Chocolate hazelnut spread", 60, section="Cores", note="3 jars x 20 g (jars 1-3)"),
		_ing("Ganache", 20, section="Cores", note="1 jar x 20 g (jar 4), from production"),
	],
	[
		_step(
			"Brown the butter",
			"Put {{item:Butter|grams}} butter in a light-coloured pot and weigh pot + butter. Brown on medium heat, stirring and "
			"scraping the bottom: it foams, goes quiet, then the bits turn hazelnut brown and smell of toasted nuts (8-10 minutes). "
			"Pour at once into a bowl with all the brown bits. Weigh again: the weight lost is the water that boiled off. "
			"Write it down, but put back only {{item:Water|grams}} of water.\n"
			"حط {{item:Butter|grams}} زبدة في حلة فاتحة اللون وزن الحلة مع الزبدة. سمّر على نار متوسطة وانت بتقلب وبتحك القاع: "
			"بتعمل رغوة وبعدين تهدى وبعدين الفتات اللي تحت يبقى بني بلون البندق وريحته زي المكسرات المحمصة (8-10 دقائق). "
			"صبها فورا في بولة بكل الفتات البني. زن تاني: الفرق هو المية اللي اتبخرت. اكتبه، بس رجّع "
			"{{item:Water|grams}} مية بس.",
			12,
			"Per Batch",
		),
		_step(
			"Cool the butter",
			"Cool the butter to 35 °C or below.\n"
			"برّد الزبدة لـ 35 درجة أو أقل.",
			15,
			cap=("Butter temperature (°C)", 20, 35),
		),
		_step(
			"Wet mix, spatula only",
			"Stir the butter, {{item:Brown sugar|grams}} brown sugar and {{item:White sugar|grams}} white sugar with a spatula "
			"until even. Beat the eggs in a cup, weigh {{item:Whole eggs|grams}}, and stir them in with {{item:Vanilla|grams}} "
			"vanilla and the water until glossy. No whisk: it beats in air.\n"
			"قلّب الزبدة مع {{item:Brown sugar|grams}} سكر بني و {{item:White sugar|grams}} سكر أبيض بالسباتولا لحد ما يتجانس. "
			"اضرب البيض في كوباية وزن {{item:Whole eggs|grams}} وقلّبهم مع {{item:Vanilla|grams}} فانيليا والمية لحد ما يلمع. "
			"من غير خفاقة: بتدخل هوا.",
			5,
			"Per Batch",
		),
		_step(
			"Fold in the dry mix",
			"Mix {{item:Flour|grams}} flour, {{item:Baking soda|grams}} baking soda and {{item:Salt|grams}} salt in a bowl. "
			"Fold into the wet mix with a spatula only until no dry flour shows.\n"
			"اخلط {{item:Flour|grams}} دقيق و {{item:Baking soda|grams}} بيكنج صودا و {{item:Salt|grams}} ملح في بولة. "
			"اطوي الخليط في المبلل بالسباتولا بس لحد ما الدقيق الناشف يختفي.",
			4,
			"Per Batch",
		),
		_step(
			"Chocolate chunks",
			"Cut {{item:Dark chocolate 60%|grams}} chocolate into 1 cm chunks and shake them in a sieve: the dust and thin shards "
			"go to the ganache, not the dough. Fold in the chunks.\n"
			"قطّع {{item:Dark chocolate 60%|grams}} شوكولاتة مكعبات 1 سم وهزها في مصفاة: الغبرة والشظايا الرفيعة تروح للجاناش "
			"مش للعجين. اطوي المكعبات في العجين.",
			6,
			"Per Batch",
		),
		_step(
			"Rest the dough 24 h",
			"Film on the surface, fridge at 4 °C or below for 24 hours (no more than 72 hours).\n"
			"غطي السطح بنايلون لازق وحطه في التلاجة على 4 درجات أو أقل لمدة 24 ساعة (مش أكتر من 72 ساعة).",
			1,
		),
		_step(
			"Fill the jars",
			"Oven 160 °C fan (170-175 °C without fan), middle shelf; jars at room temperature on a tray. Per jar: 125 g cold dough "
			"pressed into the bottom with a dip in the middle. Put 20 g core in the dip ({{item:Chocolate hazelnut spread|grams}} "
			"of spread for jars 1-3, {{item:Ganache|grams}} of ganache for jar 4), at least 1 cm from the glass. Lay 125 g of dough, "
			"flattened first into a disc a little smaller than the jar, on top and press only the edges to seal.\n"
			"الفرن 160 فان (170-175 من غير فان) على الرف الأوسط، والبرطمانات في حرارة الغرفة على صينية. لكل برطمان: 125 جرام عجين "
			"بارد مكبوس في القاع بحفرة في النص. حط 20 جرام حشو في الحفرة ({{item:Chocolate hazelnut spread|grams}} سبريد "
			"للبرطمانات 1-3، {{item:Ganache|grams}} جاناش للبرطمان 4) بعيد عن الإزاز 1 سم على الأقل. حط 125 جرام عجين مفرود "
			"قرص أصغر شوية من البرطمان فوقها واضغط الحواف بس.",
			10,
			"Per Batch",
		),
		_step(
			"Bake and probe",
			"Bake all 4 jars together. Probe the dough next to the core, not the core itself, and pull at 78-82 °C. Write the minutes.\n"
			"اخبز الـ 4 برطمانات مع بعض. قيس حرارة العجين جنب الحشو مش الحشو نفسه، واطلعهم على 78-82 درجة. اكتب الدقايق.",
			35,
			cap=("Dough temperature next to the core (°C)", 78, 82),
		),
		_step(
			"Cool on a rack and check jar 1",
			"Cool on a rack, never on cold steel. After 20 minutes open jar 1: check for bite (does a spoon meet some resistance?), "
			"whether you taste cookie first, and that the core still flows.\n"
			"برّد على رف مش على استانلس بارد. بعد 20 دقيقة افتح برطمان 1: شوف القوام (المعلقة بتحس بمقاومة؟)، وهل بتحس بطعم "
			"الكوكيز الأول، وإن الحشو لسه بيسيح.",
			20,
		),
		_step(
			"Freeze jars 3 and 4",
			"Jars 3 and 4: cool fully to room temperature (about 2 hours), check the glass for cracks, then lid. Freezer at -18 °C "
			"overnight. Day 2: fridge for 24 hours to thaw. Day 3: lid off, microwave 20 seconds, then 5 seconds more at a time. "
			"Write the seconds.\n"
			"برطمان 3 و 4: سيبهم يبردوا تماما لحرارة الغرفة (حوالي ساعتين)، اتأكد إن الإزاز مش مشروخ، وبعدين اقفلهم. "
			"فريزر على -18 طول الليل. تاني يوم: التلاجة 24 ساعة يفكوا. اليوم التالت: من غير غطا، ميكروويف 20 ثانية وبعدين "
			"5 ثواني كل مرة. اكتب الثواني.",
			1,
		),
	],
	version=2,
	change_summary=(
		"After the first bake you tasted only chocolate and the dough was too airy: half the chocolate (150 g sieved 1 cm chunks), "
		"less soda (3 g), egg (75 g) and water put back (5 g), no cornstarch, stirred not whisked, and pulled later at 78-82 °C."
	),
)

# ---------------------------------------------------------------- Carrot cake (approved)
CARROT_CAKE = _recipe(
	"Carrot Cake", "Approved", STD, 7674, "g", 90,
	"Mixer with whisk, two 40x60 trays, probe thermometer, grater",
	(
		"Approved 2026-11-23. Batter 7674 g fills two 40x60 trays (about 3.8 kg each, 2.5-3 cm deep).\n"
		"Brown sugar can be 1116 g white sugar + 84 g black molasses.\n"
		"Carrots: peeled, finely grated, not squeezed; their juice goes in. Weigh after peeling (buy about 2.1 kg for 1.8 kg).\n"
		"Toasted walnuts are not in the cake: they go on in the jar, 160 °C for 8-10 minutes.\n"
		"Store: fridge up to 3 days, or freeze."
	),
	[
		_ing("Eggs", 24, "piece"),
		_ing("Brown sugar", 1200, note="or 1116 g white sugar + 84 g black molasses"),
		_ing("White sugar", 600),
		_ing("Oil", 1200),
		_ing("Vanilla", 30),
		_ing("Flour", 1500),
		_ing("Baking powder", 36),
		_ing("Baking soda", 24),
		_ing("Salt", 18),
		_ing("Cinnamon", 48),
		_ing("Ginger", 12),
		_ing("Nutmeg", 6),
		_ing("Carrots", 1800, note="peeled, finely grated, not squeezed"),
	],
	[
		_step(
			"Oven and trays",
			"Heat the oven to 165-170 °C. Prepare two 40x60 trays: about 3.8 kg of batter each, 2.5-3 cm deep.\n"
			"شغّل الفرن على 165-170 درجة. جهّز صينيتين 40×60: حوالي 3.8 كيلو عجين في كل واحدة، بسمك 2.5-3 سم.",
			10,
		),
		_step(
			"Peel, weigh and grate the carrots",
			"Weigh the carrots after peeling: {{item:Carrots|grams}} (buy about 17% more than that). Grate finely. Do not squeeze them.\n"
			"زن الجزر بعد التقشير: {{item:Carrots|grams}} (اشتري أكتر بحوالي 17%). اسحبه ناعم. ما تعصرهوش.",
			20,
			"Per Batch",
		),
		_step(
			"Whisk the eggs and sugars",
			"Whisk {{item:Eggs|count}} eggs with {{item:Brown sugar|grams}} brown sugar and {{item:White sugar|grams}} white sugar "
			"for 2 minutes on speed 3.\n"
			"اخفق {{item:Eggs|count}} بيضة مع {{item:Brown sugar|grams}} سكر بني و {{item:White sugar|grams}} سكر أبيض "
			"لمدة دقيقتين على سرعة 3.",
			2,
		),
		_step(
			"Add the oil and vanilla",
			"On speed 1, pour in {{item:Oil|grams}} oil and {{item:Vanilla|grams}} vanilla.\n"
			"على سرعة 1 ضيف {{item:Oil|grams}} زيت و {{item:Vanilla|grams}} فانيليا.",
			2,
			"Per Batch",
		),
		_step(
			"Add the dry mix in 3 parts",
			"Mix {{item:Flour|grams}} flour, {{item:Baking powder|grams}} baking powder, {{item:Baking soda|grams}} baking soda, "
			"{{item:Salt|grams}} salt, {{item:Cinnamon|grams}} cinnamon, {{item:Ginger|grams}} ginger and "
			"{{item:Nutmeg|grams}} nutmeg. Add in 3 parts on speed 1.\n"
			"اخلط {{item:Flour|grams}} دقيق و {{item:Baking powder|grams}} بيكنج باودر و {{item:Baking soda|grams}} بيكنج صودا "
			"و {{item:Salt|grams}} ملح و {{item:Cinnamon|grams}} قرفة و {{item:Ginger|grams}} زنجبيل و "
			"{{item:Nutmeg|grams}} جوزة الطيب. ضيفهم على 3 مرات على سرعة 1.",
			5,
			"Per Batch",
		),
		_step(
			"Fold in the carrots by hand",
			"Fold in the grated carrots with their juice by hand.\n"
			"اطوي الجزر المبشور بعصيره بإيدك.",
			5,
			"Per Batch",
		),
		_step(
			"Bake",
			"Bake 30-40 minutes. Pull when the centre reaches 95 °C.\n"
			"اخبز من 30 لـ 40 دقيقة. اطلعه لما قلب الكيكة يوصل 95 درجة.",
			35,
			cap=("Centre temperature (°C)", 92, 98),
		),
		_step(
			"Cover while warm and store",
			"Cover while warm. Fridge up to 3 days, or freeze.\n"
			"غطيه وهو دافي. في التلاجة لحد 3 أيام أو في الفريزر.",
			5,
		),
	],
	approved_on="2026-11-23",
	change_summary="First approved version: the new carrot cake replacing the old 27-egg recipe.",
)

CARROT_JAR = _recipe(
	"Carrot cake jar (Medium)", "Approved", STD, 1, "jar", 5,
	"Spoon",
	(
		"Approved 2026-11-23. Total 149 g per jar. The Large jar (75 / 124 / 18 / 12 g = 229 g) is untrialled.\n"
		"Walnuts are toasted first: 160 °C for 8-10 minutes."
	),
	[
		_ing("Carrot cake", 50),
		_ing("Cheesecake Mix", 79),
		_ing("Biscuit crumbs", 12),
		_ing("Walnuts", 8, note="toasted, 160 °C for 8-10 minutes"),
		_ing("Cinnamon", 0.5, note="light dust"),
	],
	[
		_step(
			"Carrot cake layer",
			"Press {{item:Carrot cake|grams}} carrot cake into the bottom of the jar.\n"
			"اكبس {{item:Carrot cake|grams}} كيك جزر في قاع البرطمان.",
			1,
			"Per Batch",
		),
		_step(
			"Cheesecake Mix layer",
			"Add {{item:Cheesecake Mix|grams}} Cheesecake Mix.\n"
			"ضيف {{item:Cheesecake Mix|grams}} تشيز كيك ميكس.",
			1,
			"Per Batch",
		),
		_step(
			"Crumbs, walnuts and cinnamon",
			"Add {{item:Biscuit crumbs|grams}} crumbs, then {{item:Walnuts|grams}} toasted walnuts, and a light dust of "
			"cinnamon ({{item:Cinnamon|grams}}).\n"
			"ضيف {{item:Biscuit crumbs|grams}} فتات بسكويت وبعدين {{item:Walnuts|grams}} عين جمل محمص، وبعدين رشة قرفة "
			"خفيفة ({{item:Cinnamon|grams}}).",
			1,
			"Per Batch",
		),
	],
	approved_on="2026-11-23",
	change_summary="First approved version.",
)


BOOK_RECIPES: list[dict] = [
	COCOA_SYRUP,
	SOAK_15,
	LONDON_CARAMEL,
	APPLE_FILLING,
	APPLE_CRUMBS,
	APPLE_JAR,
	COOKIE_V2,
	CARROT_CAKE,
	CARROT_JAR,
]
