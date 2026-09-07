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
from unittest.mock import patch

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
        expanded, _ = self._expand(
            {"https://maps.app.goo.gl/aBcDeF123": (200, _Headers())}
        )
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
        with patch.object(
            geo, "_redirect_status_and_headers", side_effect=OSError("boom")
        ):
            self.assertEqual(
                geo.expand_short_link("https://maps.app.goo.gl/aBcDeF123"), ""
            )


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
            patch.object(
                geo_resolution.frappe, "generate_hash", return_value="a" * 32
            ),
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
            self.assertTrue(
                self.service.preview_link(self.SHORT, user="pos@jarz")["pending"]
            )
        capped = self.service.preview_link(self.SHORT, user="pos@jarz")
        self.assertFalse(capped["pending"])
        self.assertEqual(capped["reason"], "rate_limited")

    def test_budget_fails_closed_without_redis(self):
        """No cache means no worker will ever answer, so do not promise one."""
        with patch.object(
            self.service.frappe, "cache", side_effect=RuntimeError("no redis")
        ):
            result = self.service.preview_link(self.SHORT, user="pos@jarz")
        self.assertFalse(result["pending"])
        self.assertEqual(result["reason"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
