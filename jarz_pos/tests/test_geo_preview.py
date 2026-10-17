"""The POS "paste a Maps link" preview: expansion safety and the ticket flow.

Two things are covered here, and neither is exercised by ``test_geo_parser``,
which is deliberately pure.

**The share sheet produces a shortener, every time.** Staff do not hand-craft a
``/maps/place/...@lat,lng`` URL; they tap share and paste
``https://maps.app.goo.gl/x`` or ``https://share.google/x``. Those carry no
coordinates at all, so a preview that refuses to expand them answers "could not
read a location from this link" for the *normal* input. The ticket flow is what
turns that into an answer, and these tests pin its three outcomes.

**Expansion follows hops chosen by a remote server.** The starting host is ours
to constrain; every hop after it is not. Handing the chain to urllib's automatic
follower turns "expand this short link" into "fetch whatever the shortener now
points at", with a POS user's session behind it. ``_hop_is_permitted`` is the
guard and the redirect cases below are its test.

Pure ``unittest``: no site, no network — every HTTP call is stubbed.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.utils import geo


class _Headers(dict):
	"""``urllib`` returns a case-insensitive mapping; only ``get`` is used."""

	def get(self, key, default=None):
		for existing, value in self.items():
			if existing.lower() == str(key).lower():
				return value
		return default


class TestShortHostRecognition(unittest.TestCase):
	def test_share_google_is_a_short_link(self):
		"""The current Android share sheet emits share.google, not goo.gl.

		Missing from ``SHORT_LINK_HOSTS`` this read as "a URL with no
		coordinates in it" — a dead end — instead of "expandable".
		"""
		self.assertTrue(geo.is_short_maps_link("https://share.google/aBcDeF123"))

	def test_goo_gl_forms_still_recognised(self):
		for url in (
			"https://maps.app.goo.gl/aBcDeF123?g_st=ic",
			"https://goo.gl/maps/xyz",
		):
			with self.subTest(url=url):
				self.assertTrue(geo.is_short_maps_link(url))


class TestHopPermission(unittest.TestCase):
	def test_maps_and_shortener_hosts_allowed(self):
		for url in (
			"https://maps.app.goo.gl/x",
			"https://share.google/x",
			"https://www.google.com/maps/place/X/@30.0,31.0,17z",
		):
			with self.subTest(url=url):
				self.assertTrue(geo._hop_is_permitted(url))

	def test_foreign_host_refused(self):
		for url in (
			"https://evil.example.com/",
			"https://google.com.evil.example.com/",
			"https://169.254.169.254/latest/meta-data/",
			"http://www.google.com/maps",  # plain HTTP is never followed
			"file:///etc/passwd",
		):
			with self.subTest(url=url):
				self.assertFalse(geo._hop_is_permitted(url))


class TestExpandShortLink(unittest.TestCase):
	LONG = (
		"https://www.google.com/maps/place/Jarz/@30.0444,31.2357,17z/"
		"data=!4m6!3m5!1s0x0:0x0!8m2!3d30.0500!4d31.2400"
	)

	def _expand(self, hops):
		"""Run the expander against a scripted redirect chain."""
		calls = []

		def fake(url, method, timeout):
			calls.append(url)
			return hops[url]

		with patch.object(geo, "_redirect_status_and_headers", side_effect=fake):
			return geo.expand_short_link("https://maps.app.goo.gl/aBcDeF123"), calls

	def test_follows_a_redirect_to_the_long_url(self):
		expanded, _ = self._expand(
			{
				"https://maps.app.goo.gl/aBcDeF123": (
					302,
					_Headers({"Location": self.LONG}),
				),
				self.LONG: (200, _Headers()),
			}
		)
		self.assertEqual(expanded, self.LONG)
		# And the whole point: the expanded URL parses to the pin.
		lat, lng, precision = geo.parse_maps_link(expanded)
		self.assertAlmostEqual(lat, 30.05, places=4)
		self.assertEqual(precision, geo.PRECISION_PIN)

	def test_stops_at_a_redirect_off_the_allowlist(self):
		expanded, calls = self._expand(
			{
				"https://maps.app.goo.gl/aBcDeF123": (
					302,
					_Headers({"Location": "https://evil.example.com/pwn"}),
				)
			}
		)
		self.assertEqual(expanded, "")
		# Never fetched. A refusal that still made the request would leak the
		# fact of the visit and could still hit an internal address.
		self.assertNotIn("https://evil.example.com/pwn", calls)

	def test_retries_a_hop_with_get_when_head_is_refused(self):
		"""Some shorteners answer 405 to HEAD; the redirect header is the same."""
		seen = []

		def fake(url, method, timeout):
			seen.append((url, method))
			if url.endswith("aBcDeF123"):
				if method == "HEAD":
					return 405, _Headers()
				return 302, _Headers({"Location": self.LONG})
			return 200, _Headers()

		with patch.object(geo, "_redirect_status_and_headers", side_effect=fake):
			expanded = geo.expand_short_link("https://maps.app.goo.gl/aBcDeF123")
		self.assertEqual([method for _, method in seen[:2]], ["HEAD", "GET"])
		self.assertEqual(expanded, self.LONG)

	def test_landing_back_on_a_shortener_is_not_an_answer(self):
		expanded, _ = self._expand({"https://maps.app.goo.gl/aBcDeF123": (200, _Headers())})
		self.assertEqual(expanded, "")

	def test_legacy_http_shortener_is_upgraded_not_refused(self):
		"""The stored corpus predates the shorteners moving to TLS."""
		seen = []

		def fake(url, method, timeout):
			seen.append(url)
			if url == self.LONG:
				return 200, _Headers()
			return 302, _Headers({"Location": self.LONG})

		with patch.object(geo, "_redirect_status_and_headers", side_effect=fake):
			expanded = geo.expand_short_link("http://goo.gl/maps/xyz")
		self.assertEqual(seen[0], "https://goo.gl/maps/xyz")
		self.assertEqual(expanded, self.LONG)

	def test_non_short_input_is_refused_without_a_request(self):
		with patch.object(geo, "_redirect_status_and_headers") as request:
			self.assertEqual(geo.expand_short_link("https://evil.example.com/"), "")
			self.assertEqual(geo.expand_short_link(""), "")
			self.assertEqual(geo.expand_short_link(None), "")
		request.assert_not_called()

	def test_never_raises(self):
		with patch.object(geo, "_redirect_status_and_headers", side_effect=OSError("boom")):
			self.assertEqual(geo.expand_short_link("https://maps.app.goo.gl/aBcDeF123"), "")


class _FakeCache:
	"""Just enough of ``frappe.cache()`` for the ticket and rate-limit paths."""

	def __init__(self):
		self.store = {}
		self.counters = {}

	def set_value(self, key, value, expires_in_sec=None):
		self.store[key] = value

	def get_value(self, key, use_local_cache=True):
		return self.store.get(key)

	def make_key(self, key):
		return key

	def incrby(self, key, amount):
		self.counters[key] = self.counters.get(key, 0) + amount
		return self.counters[key]

	def expire(self, key, ttl):
		return None


class TestPreviewTicketFlow(unittest.TestCase):
	"""The three outcomes of ``preview_link``, and the poll that follows one."""

	LONG = "https://www.google.com/maps/place/Jarz/@30.0444,31.2357,17z"
	SHORT = "https://maps.app.goo.gl/aBcDeF123"

	def setUp(self):
		from jarz_pos.services import geo_resolution

		self.service = geo_resolution
		self.cache = _FakeCache()
		self.enqueued = []

		self._patches = [
			patch.object(geo_resolution.frappe, "cache", return_value=self.cache),
			patch.object(
				geo_resolution.frappe,
				"enqueue",
				side_effect=lambda *a, **kw: self.enqueued.append((a, kw)),
			),
			patch.object(geo_resolution.frappe, "generate_hash", return_value="a" * 32),
		]
		for item in self._patches:
			item.start()
		self.addCleanup(lambda: [item.stop() for item in self._patches])

	def test_long_url_resolves_inline_and_queues_nothing(self):
		result = self.service.preview_link(self.LONG, user="pos@jarz")
		self.assertTrue(result["resolved"])
		self.assertFalse(result["pending"])
		self.assertAlmostEqual(result["latitude"], 30.0444, places=4)
		self.assertEqual(self.enqueued, [])

	def test_short_link_returns_a_ticket_and_queues_the_expansion(self):
		result = self.service.preview_link(self.SHORT, user="pos@jarz")
		self.assertFalse(result["resolved"])
		self.assertTrue(result["pending"])
		self.assertEqual(result["request_id"], "a" * 32)
		self.assertEqual(len(self.enqueued), 1)
		self.assertEqual(
			self.enqueued[0][0][0],
			"jarz_pos.services.geo_resolution.preview_short_link_job",
		)

	def test_unrecognisable_link_is_a_plain_no(self):
		result = self.service.preview_link("https://example.com/x", user="pos@jarz")
		self.assertFalse(result["resolved"])
		self.assertFalse(result["pending"])
		self.assertEqual(result["reason"], "no_coordinates_in_link")
		self.assertEqual(self.enqueued, [])

	def test_link_outside_egypt_is_read_but_refused(self):
		result = self.service.preview_link("https://maps.google.com/?q=48.8566,2.3522", user="pos@jarz")
		self.assertFalse(result["resolved"])
		self.assertFalse(result["pending"])
		self.assertEqual(result["reason"], self.service.OUTSIDE_SERVICE_AREA)
		self.assertAlmostEqual(result["outside_latitude"], 48.8566, places=4)
		# An installed app that ignores `resolved` saves any answer carrying
		# latitude/longitude, so a refused point must never travel under them.
		self.assertNotIn("latitude", result)
		self.assertNotIn("longitude", result)
		self.assertEqual(self.enqueued, [])

	def test_typed_bare_coordinates_resolve_inline(self):
		result = self.service.preview_link("30.0444, 31.2357", user="pos@jarz")
		self.assertTrue(result["resolved"])
		self.assertAlmostEqual(result["longitude"], 31.2357, places=4)

	def test_short_link_expanding_outside_egypt_is_refused(self):
		token = self.service.preview_link(self.SHORT, user="pos@jarz")["request_id"]
		with patch.object(
			self.service.geo,
			"expand_short_link",
			return_value="https://www.google.com/maps/place/X/data=!3d48.8566!4d2.3522",
		):
			self.service.preview_short_link_job(token, self.SHORT, "pos@jarz")
		done = self.service.preview_link(request_id=token, user="pos@jarz")
		self.assertFalse(done["resolved"])
		self.assertTrue(done["short_link"])
		self.assertEqual(done["reason"], self.service.OUTSIDE_SERVICE_AREA)
		self.assertNotIn("latitude", done)

	def test_poll_reports_pending_then_the_worker_result(self):
		ticket = self.service.preview_link(self.SHORT, user="pos@jarz")
		token = ticket["request_id"]

		still_waiting = self.service.preview_link(request_id=token, user="pos@jarz")
		self.assertTrue(still_waiting["pending"])

		with patch.object(
			self.service.geo,
			"expand_short_link",
			return_value=self.LONG + "/data=!3m1!4b1!4m2!3d30.05!4d31.24",
		):
			self.service.preview_short_link_job(token, self.SHORT, "pos@jarz")

		done = self.service.preview_link(request_id=token, user="pos@jarz")
		self.assertFalse(done["pending"])
		self.assertTrue(done["resolved"])
		self.assertAlmostEqual(done["latitude"], 30.05, places=4)

	def test_a_ticket_belongs_to_the_user_who_created_it(self):
		token = self.service.preview_link(self.SHORT, user="pos@jarz")["request_id"]
		stolen = self.service.preview_link(request_id=token, user="someone@else")
		self.assertFalse(stolen["success"])
		self.assertEqual(stolen["reason"], "request_expired")

	def test_a_hand_made_request_id_is_refused(self):
		result = self.service.preview_link(request_id="../../etc", user="pos@jarz")
		self.assertFalse(result["success"])
		self.assertEqual(result["reason"], "invalid_request_id")

	def test_worker_reports_a_shortener_that_never_resolved(self):
		token = self.service.preview_link(self.SHORT, user="pos@jarz")["request_id"]
		with patch.object(self.service.geo, "expand_short_link", return_value=""):
			self.service.preview_short_link_job(token, self.SHORT, "pos@jarz")
		done = self.service.preview_link(request_id=token, user="pos@jarz")
		self.assertFalse(done["resolved"])
		self.assertEqual(done["reason"], "short_link_unresolved")

	def test_expansion_budget_is_bounded_per_user(self):
		for _ in range(self.service.PREVIEW_RATE_MAX_REQUESTS):
			self.assertTrue(self.service.preview_link(self.SHORT, user="pos@jarz")["pending"])
		capped = self.service.preview_link(self.SHORT, user="pos@jarz")
		self.assertFalse(capped["pending"])
		self.assertEqual(capped["reason"], "rate_limited")

	def test_budget_fails_closed_without_redis(self):
		"""No cache means no worker will ever answer, so do not promise one."""
		with patch.object(self.service.frappe, "cache", side_effect=RuntimeError("no redis")):
			result = self.service.preview_link(self.SHORT, user="pos@jarz")
		self.assertFalse(result["pending"])
		self.assertEqual(result["reason"], "rate_limited")


class TestLinkWritesStayInEgypt(unittest.TestCase):
	"""A pasted link outside the service area must never reach the Address."""

	def setUp(self):
		from jarz_pos.services import geo_resolution

		self.service = geo_resolution

	def test_resolve_from_link_refuses_without_writing(self):
		with patch.object(self.service, "set_address_pin", side_effect=AssertionError("must not write")):
			result = self.service.resolve_address_from_link("ADDR-1", "https://maps.google.com/?q=48.8566,2.3522")
		self.assertFalse(result["accepted"])
		self.assertEqual(result["reason"], self.service.OUTSIDE_SERVICE_AREA)

	def test_short_link_job_refuses_without_writing(self):
		with (
			patch.object(self.service.geo, "expand_short_link", return_value="https://maps.google.com/?q=48.8566,2.3522"),
			patch.object(self.service, "set_address_pin", side_effect=AssertionError("must not write")),
		):
			result = self.service.resolve_short_link_job("ADDR-1", "https://maps.app.goo.gl/aBcDeF123")
		self.assertFalse(result["accepted"])
		self.assertEqual(result["reason"], self.service.OUTSIDE_SERVICE_AREA)

	def test_cairo_link_still_writes(self):
		with patch.object(self.service, "set_address_pin", return_value={"success": True, "accepted": True}) as write:
			result = self.service.resolve_address_from_link("ADDR-1", "30.0444, 31.2357")
		self.assertTrue(result["accepted"])
		self.assertAlmostEqual(write.call_args.kwargs["latitude"], 30.0444, places=4)


class _ReferenceTestBase(unittest.TestCase):
	"""Shared setup: a clean reference cache and controllable Territory / POS Profile points."""

	def setUp(self):
		from jarz_pos.services import geo_resolution

		self.service = geo_resolution
		self.service.clear_reference_cache()
		self.addCleanup(self.service.clear_reference_cache)
		self.points = {"Territory": [], "POS Profile": []}
		self.loads = []

		def _load(doctype):
			self.loads.append(doctype)
			return list(self.points.get(doctype, []))

		patcher = patch.object(geo_resolution, "_load_reference_points", side_effect=_load)
		patcher.start()
		self.addCleanup(patcher.stop)


class TestSwappedBarePair(_ReferenceTestBase):
	"""L6: a typed Cairo pair with its axes transposed lands near Alexandria and was accepted."""

	def test_swapped_cairo_pair_is_corrected_in_the_preview(self):
		result = self.service.preview_link("31.2357, 30.0444", user="pos@jarz")
		self.assertTrue(result["resolved"])
		self.assertTrue(result["swapped"])
		self.assertAlmostEqual(result["latitude"], 30.0444, places=4)
		self.assertAlmostEqual(result["longitude"], 31.2357, places=4)
		self.assertAlmostEqual(result["typed_latitude"], 31.2357, places=4)
		self.assertAlmostEqual(result["typed_longitude"], 30.0444, places=4)
		self.assertTrue(result["precision_note"])
		self.assertEqual(result["swap_reference"], self.service.REFERENCE_CAIRO_FALLBACK)

	def test_correctly_typed_pair_is_not_swapped(self):
		result = self.service.preview_link("30.0444, 31.2357", user="pos@jarz")
		self.assertFalse(result["swapped"])
		self.assertAlmostEqual(result["latitude"], 30.0444, places=4)
		self.assertNotIn("precision_note", result)
		self.assertNotIn("typed_latitude", result)

	def test_urls_are_never_swapped(self):
		for url in (
			"https://maps.google.com/?q=31.2357,30.0444",
			"https://www.google.com/maps/@31.2357,30.0444,17z",
		):
			with self.subTest(url=url):
				result = self.service.preview_link(url, user="pos@jarz")
				self.assertFalse(result["swapped"])
				self.assertAlmostEqual(result["latitude"], 31.2357, places=4)

	def test_hemisphere_labelled_pair_is_never_swapped(self):
		result = self.service.preview_link("31.2357 N, 30.0444 E", user="pos@jarz")
		self.assertFalse(result["swapped"])
		self.assertAlmostEqual(result["latitude"], 31.2357, places=4)

	def test_territory_centroids_are_the_reference_when_they_exist(self):
		"""With a delivery area in Alexandria, the typed order is near a centroid: keep it."""
		self.points["Territory"] = [(31.2001, 29.9187), (30.0444, 31.2357)]
		result = self.service.preview_link("31.2001, 29.9187", user="pos@jarz")
		self.assertFalse(result["swapped"])
		self.assertAlmostEqual(result["latitude"], 31.2001, places=4)
		self.assertEqual(result["distance_reference"], self.service.REFERENCE_TERRITORY)

	def test_swap_uses_territory_centroids_when_present(self):
		self.points["Territory"] = [(30.0444, 31.2357)]
		result = self.service.preview_link("31.2357, 30.0444", user="pos@jarz")
		self.assertTrue(result["swapped"])
		self.assertEqual(result["swap_reference"], self.service.REFERENCE_TERRITORY)

	def test_ambiguous_pair_keeps_the_typed_order(self):
		# Swapped reading is ~56 km from the only centroid: neither close nor far enough.
		self.points["Territory"] = [(30.0444, 31.2357)]
		result = self.service.preview_link("31.2357, 30.55", user="pos@jarz")
		self.assertFalse(result["swapped"])
		self.assertAlmostEqual(result["latitude"], 31.2357, places=4)

	def test_resolve_address_from_link_saves_the_corrected_pair(self):
		with patch.object(
			self.service, "set_address_pin", return_value={"success": True, "accepted": True}
		) as write:
			result = self.service.resolve_address_from_link("ADDR-1", "31.2357, 30.0444")
		self.assertAlmostEqual(write.call_args.kwargs["latitude"], 30.0444, places=4)
		self.assertAlmostEqual(write.call_args.kwargs["longitude"], 31.2357, places=4)
		self.assertTrue(result["swapped"])

	def test_staff_correction_flag_reaches_the_writer_only_when_asked(self):
		for asked in (True, False):
			with self.subTest(asked=asked):
				with patch.object(
					self.service, "set_address_pin", return_value={"success": True, "accepted": True}
				) as write:
					self.service.resolve_address_from_link("ADDR-1", "30.0444, 31.2357", staff_correction=asked)
				self.assertIs(write.call_args.kwargs["staff_correction"], asked)

	def test_the_background_pin_job_never_passes_staff_correction(self):
		with (
			patch.object(
				self.service.geo,
				"expand_short_link",
				return_value="https://www.google.com/maps/@30.0444,31.2357,17z",
			),
			patch.object(
				self.service, "set_address_pin", return_value={"success": True, "accepted": True}
			) as write,
			patch.object(self.service.frappe, "db", MagicMock()),
		):
			self.service.resolve_short_link_job("ADDR-1", "https://maps.app.goo.gl/aBcDeF123")
		self.assertNotIn("staff_correction", write.call_args.kwargs)

	def test_swap_decision_never_raises(self):
		with patch.object(self.service, "territory_reference_points", side_effect=RuntimeError("db down")):
			out = self.service.apply_bare_pair_swap("31.2357, 30.0444", 31.2357, 30.0444)
		self.assertFalse(out["swapped"])
		self.assertEqual((out["latitude"], out["longitude"]), (31.2357, 30.0444))


class TestDistanceFromBranch(_ReferenceTestBase):
	"""L7: the Flutter field rejects a preview above 150 km, so the response must carry it."""

	FLUTTER_LIMIT_M = 150_000

	def test_cairo_is_close(self):
		result = self.service.preview_link("30.0444, 31.2357", user="pos@jarz")
		self.assertLess(result["distance_from_branch_m"], 5_000)
		self.assertEqual(result["distance_reference"], self.service.REFERENCE_CAIRO_FALLBACK)

	def test_aswan_is_beyond_the_client_limit(self):
		result = self.service.preview_link("24.0889, 32.8998", user="pos@jarz")
		self.assertTrue(result["resolved"])
		self.assertGreater(result["distance_from_branch_m"], self.FLUTTER_LIMIT_M)

	def test_a_value_is_always_an_int_number_of_metres(self):
		result = self.service.preview_link("30.0500, 31.2400", user="pos@jarz")
		self.assertIsInstance(result["distance_from_branch_m"], int)

	def test_pos_profile_coordinates_take_precedence(self):
		self.points["POS Profile"] = [(24.0889, 32.8998)]
		self.points["Territory"] = [(30.0444, 31.2357)]
		result = self.service.preview_link("24.0889, 32.8998", user="pos@jarz")
		self.assertLess(result["distance_from_branch_m"], 100)
		self.assertEqual(result["distance_reference"], self.service.REFERENCE_POS_PROFILE)

	def test_nearest_of_several_references_wins(self):
		self.points["Territory"] = [(30.0444, 31.2357), (24.0889, 32.8998)]
		result = self.service.preview_link("24.1, 32.9", user="pos@jarz")
		self.assertLess(result["distance_from_branch_m"], 5_000)
		self.assertEqual(result["distance_reference"], self.service.REFERENCE_TERRITORY)

	def test_territory_centroids_are_used_without_pos_profile_coordinates(self):
		self.points["Territory"] = [(30.0444, 31.2357)]
		result = self.service.preview_link("30.0444, 31.2357", user="pos@jarz")
		self.assertLess(result["distance_from_branch_m"], 100)
		self.assertEqual(result["distance_reference"], self.service.REFERENCE_TERRITORY)

	def test_short_link_background_job_carries_the_distance_too(self):
		cache = _FakeCache()
		key = self.service._preview_ticket_key("a" * 32)
		cache.store[key] = {"user": "pos@jarz", "url": "https://maps.app.goo.gl/x", "result": None}
		with (
			patch.object(self.service.frappe, "cache", return_value=cache),
			patch.object(
				self.service.geo,
				"expand_short_link",
				return_value="https://www.google.com/maps/@24.0889,32.8998,17z",
			),
		):
			result = self.service.preview_short_link_job("a" * 32, "https://maps.app.goo.gl/x", "pos@jarz")
			polled = self.service.preview_link(request_id="a" * 32, user="pos@jarz")
		self.assertTrue(result["resolved"])
		self.assertGreater(result["distance_from_branch_m"], self.FLUTTER_LIMIT_M)
		self.assertEqual(polled["distance_from_branch_m"], result["distance_from_branch_m"])

	def test_unmeasurable_point_degrades_to_none_not_an_error(self):
		with patch.object(self.service, "branch_reference_points", side_effect=RuntimeError("boom")):
			metres, basis = self.service.distance_from_branch_m(30.0, 31.0)
		self.assertIsNone(metres)
		self.assertEqual(basis, self.service.REFERENCE_CAIRO_FALLBACK)

	def test_refused_point_carries_no_distance_under_latitude(self):
		result = self.service.preview_link("https://maps.google.com/?q=48.8566,2.3522", user="pos@jarz")
		self.assertFalse(result["resolved"])
		self.assertNotIn("latitude", result)


class TestReferencePointLoading(unittest.TestCase):
	"""Neither Territory nor POS Profile carries coordinates today; the loader probes for them."""

	def setUp(self):
		from jarz_pos.services import geo_resolution

		self.service = geo_resolution
		self.service.clear_reference_cache()
		self.addCleanup(self.service.clear_reference_cache)

	def _frappe(self, fields, rows):
		fake = MagicMock()
		fake.get_meta.return_value.has_field.side_effect = lambda f: f in fields
		fake.get_all.return_value = rows
		return fake

	def test_no_coordinate_fields_means_no_query_and_the_cairo_fallback(self):
		fake = self._frappe({"disabled"}, [])
		with patch.object(self.service, "frappe", fake):
			points, basis = self.service.territory_reference_points()
		self.assertEqual(points, [self.service.geo.GREATER_CAIRO])
		self.assertEqual(basis, self.service.REFERENCE_CAIRO_FALLBACK)
		fake.get_all.assert_not_called()

	def test_custom_coordinate_fields_are_read_and_filtered(self):
		rows = [
			{"custom_latitude": 30.0444, "custom_longitude": 31.2357},
			{"custom_latitude": 0, "custom_longitude": 0},  # unset
			{"custom_latitude": 48.85, "custom_longitude": 2.35},  # outside Egypt
			{"custom_latitude": None, "custom_longitude": None},
		]
		fake = self._frappe({"custom_latitude", "custom_longitude", "disabled"}, rows)
		with patch.object(self.service, "frappe", fake):
			points, basis = self.service.territory_reference_points()
		self.assertEqual(points, [(30.0444, 31.2357)])
		self.assertEqual(basis, self.service.REFERENCE_TERRITORY)
		self.assertEqual(fake.get_all.call_args.kwargs["filters"], {"disabled": 0})

	def test_plain_latitude_longitude_fields_are_a_fallback_pair(self):
		fake = self._frappe({"latitude", "longitude"}, [{"latitude": 31.2, "longitude": 29.9}])
		with patch.object(self.service, "frappe", fake):
			points, _ = self.service.territory_reference_points()
		self.assertEqual(points, [(31.2, 29.9)])

	def test_points_are_cached_between_calls(self):
		fake = self._frappe(
			{"custom_latitude", "custom_longitude"}, [{"custom_latitude": 30.0, "custom_longitude": 31.0}]
		)
		with patch.object(self.service, "frappe", fake):
			self.service.territory_reference_points()
			self.service.territory_reference_points()
			self.service.branch_reference_points()
		# One load per DocType (Territory, then POS Profile for the branch lookup), not per call.
		self.assertEqual(fake.get_all.call_count, 2)
		self.service.clear_reference_cache()
		with patch.object(self.service, "frappe", fake):
			self.service.territory_reference_points()
		self.assertEqual(fake.get_all.call_count, 3)

	def test_query_failure_degrades_to_no_points(self):
		fake = self._frappe({"custom_latitude", "custom_longitude"}, [])
		fake.get_all.side_effect = RuntimeError("db down")
		with patch.object(self.service, "frappe", fake):
			points, basis = self.service.territory_reference_points()
		self.assertEqual(basis, self.service.REFERENCE_CAIRO_FALLBACK)
		self.assertEqual(points, [self.service.geo.GREATER_CAIRO])


if __name__ == "__main__":
	unittest.main()
