"""Meta messages that used to land as "Unsupported" (chat_ingest.parse_webhook) -- pure, no site.

WhatsApp document name + caption, location, shared contacts; Messenger / Instagram
location, link-like attachments (fallback, share, template, posts), story mentions,
reels and story replies; Facebook comment photos. The body carries the URL / phone so
the app can render it as a tappable link.

Plain ``unittest.TestCase``; a stub ``frappe`` is registered when frappe is not installed.
"""

import datetime
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

try:  # pragma: no cover - depends on the environment
	import frappe
except ImportError:  # pragma: no cover
	frappe = types.ModuleType("frappe")
	frappe.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
	frappe._ = lambda m: m
	frappe.local = SimpleNamespace()
	frappe.session = SimpleNamespace(user="Guest")
	frappe.db = SimpleNamespace()
	frappe.log_error = lambda *a, **k: None
	frappe.get_traceback = lambda *a, **k: ""
	sys.modules["frappe"] = frappe
	_utils = types.ModuleType("frappe.utils")
	_utils.get_datetime = lambda d=None: d
	_utils.now_datetime = lambda: datetime.datetime(2026, 11, 16, 18, 0)
	_utils.nowdate = lambda: "2026-11-16"
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_ingest as ing


def wa(message):
	message = {"from": "201111034268", "id": "wamid.1", "timestamp": "1760608800", **message}
	value = {
		"messaging_product": "whatsapp",
		"metadata": {"phone_number_id": "PNID"},
		"contacts": [{"wa_id": "201111034268", "profile": {"name": "Sara"}}],
		"messages": [message],
	}
	payload = {
		"object": "whatsapp_business_account",
		"entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
	}
	return ing.parse_webhook(payload)[0]


def page(message, obj="page"):
	item = {
		"sender": {"id": "PSID1"},
		"recipient": {"id": "PAGE1"},
		"timestamp": 1760608800000,
		"message": {"mid": "m_1", **message},
	}
	return ing.parse_webhook({"object": obj, "entry": [{"id": "PAGE1", "time": 1, "messaging": [item]}]})


class WhatsAppTest(unittest.TestCase):
	def test_document_filename_then_caption(self):
		doc = {"id": "M1", "filename": "menu.pdf", "caption": "here is the menu"}
		e = wa({"type": "document", "document": doc})
		self.assertEqual(e["message_type"], "document")
		self.assertEqual(e["body"], "menu.pdf\nhere is the menu")
		self.assertEqual(e["media_id"], "M1")

	def test_document_without_caption_or_filename(self):
		e = wa({"type": "document", "document": {"id": "M1", "filename": "menu.pdf"}})
		self.assertEqual(e["body"], "menu.pdf")
		e = wa({"type": "document", "document": {"id": "M1", "caption": "only caption"}})
		self.assertEqual(e["body"], "only caption")
		self.assertEqual(e["message_type"], "document")

	def test_image_caption_unchanged(self):
		e = wa({"type": "image", "image": {"id": "M1", "mime_type": "image/jpeg", "caption": "hi"}})
		self.assertEqual(e["body"], "hi")

	def test_location_body_is_exactly_lat_lng(self):
		# The app splits the body on "," -- a name / address line would break the pin.
		loc = {"latitude": 30.05, "longitude": 31.23, "name": "Home", "address": "1 Street"}
		e = wa({"type": "location", "location": loc})
		self.assertEqual(e["message_type"], "location")
		self.assertEqual(e["body"], "30.05,31.23")

	def test_contacts_become_text(self):
		e = wa(
			{
				"type": "contacts",
				"contacts": [
					{
						"name": {"formatted_name": "Omar Hassan", "first_name": "Omar"},
						"phones": [{"phone": "+20 111 111 1111", "type": "CELL"}, {"phone": "+20 122 222 2222"}],
						"emails": [{"email": "omar@example.com"}],
					},
					{"name": {"formatted_name": "Mona"}, "phones": [{"phone": "+20 100 000 0000"}]},
				],
			}
		)
		self.assertEqual(e["message_type"], "text")
		self.assertEqual(
			e["body"],
			"Contact: Omar Hassan\n+20 111 111 1111\n+20 122 222 2222\nomar@example.com"
			"\n\nContact: Mona\n+20 100 000 0000",
		)
		self.assertIsNone(e["raw"])

	def test_empty_contacts_stay_unsupported(self):
		e = wa({"type": "contacts", "contacts": []})
		self.assertEqual(e["message_type"], "unsupported")
		self.assertIsNotNone(e["raw"])

	def test_order_still_unsupported(self):
		self.assertEqual(wa({"type": "order", "order": {}})["message_type"], "unsupported")


class MessengerAttachmentTest(unittest.TestCase):
	def one(self, attachment, text=None, obj="page"):
		message = {"attachments": [attachment]}
		if text is not None:
			message["text"] = text
		events = page(message, obj)
		self.assertEqual(len(events), 1)
		return events[0]

	def test_location(self):
		e = self.one(
			{
				"type": "location",
				"title": "Sara's Location",
				"url": "https://l.facebook.com/l.php?u=maps",
				"payload": {"coordinates": {"lat": 30.0444, "long": 31.2357}},
			}
		)
		self.assertEqual(e["message_type"], "location")
		self.assertEqual(e["body"], "30.0444,31.2357")

	def test_location_without_coordinates_keeps_link(self):
		e = self.one({"type": "location", "title": "Pin", "url": "https://maps.example/x", "payload": None})
		self.assertEqual(e["message_type"], "text")
		self.assertEqual(e["body"], "Pin\nhttps://maps.example/x")

	def test_fallback_title_and_url(self):
		att = {"type": "fallback", "title": "Cheesecake Jar", "URL": "https://jarz.example/p/1", "payload": None}
		e = self.one(att, text="check this")
		self.assertEqual(e["message_type"], "text")
		self.assertEqual(e["body"], "check this\nCheesecake Jar\nhttps://jarz.example/p/1")
		self.assertIsNone(e["raw"])

	def test_share_dedupes_text_already_there(self):
		url = "https://www.instagram.com/p/ABC/"
		e = self.one({"type": "share", "payload": {"url": url}}, text=url, obj="instagram")
		self.assertEqual(e["body"], url)

	def test_generic_template(self):
		e = self.one(
			{
				"type": "template",
				"payload": {
					"template_type": "generic",
					"elements": [
						{
							"title": "Tiramisu",
							"subtitle": "",
							"default_action": {"type": "web_url", "url": "https://jarz.example/t"},
						}
					],
				},
			}
		)
		self.assertEqual(e["message_type"], "text")
		self.assertEqual(e["body"], "Tiramisu\nhttps://jarz.example/t")

	def test_ig_post_and_post(self):
		for kind in ("ig_post", "post"):
			e = self.one({"type": kind, "payload": {"title": "Our cake", "url": "https://ig.example/p/1"}})
			self.assertEqual(e["message_type"], "text")
			self.assertEqual(e["body"], "Our cake\nhttps://ig.example/p/1")

	def test_story_mention_image(self):
		url = "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1&signature=x"
		e = self.one({"type": "story_mention", "payload": {"url": url}}, obj="instagram")
		self.assertEqual(e["message_type"], "image")
		self.assertEqual(e["body"], "Mentioned you in their story")
		self.assertEqual(e["media_url"], url)

	def test_story_mention_video_by_url_hint(self):
		url = "https://scontent.cdninstagram.com/v/t50/story.mp4?_nc=1"
		e = self.one({"type": "story_mention", "payload": {"url": url}}, obj="instagram")
		self.assertEqual(e["message_type"], "video")
		self.assertEqual(e["media_url"], url)

	def test_ig_reel_video_with_title_and_link(self):
		e = self.one(
			{
				"type": "ig_reel",
				"payload": {
					"reel_video_id": "9",
					"title": "Mango jar",
					"url": "https://cdn.example/reel.mp4",
					"link": "https://www.instagram.com/reel/XYZ/",
				},
			},
			obj="instagram",
		)
		self.assertEqual(e["message_type"], "video")
		self.assertEqual(e["media_url"], "https://cdn.example/reel.mp4")
		self.assertEqual(e["body"], "Mango jar\nhttps://www.instagram.com/reel/XYZ/")

	def test_reel_without_media_url_is_text(self):
		e = self.one({"type": "reel", "payload": {"title": "Mango jar"}})
		self.assertEqual(e["message_type"], "text")
		self.assertEqual(e["body"], "Mango jar")

	def test_plain_image_unchanged(self):
		e = self.one({"type": "image", "payload": {"url": "https://cdn.example/i.jpg"}}, text="look")
		self.assertEqual(e["message_type"], "image")
		self.assertEqual(e["body"], "look")
		self.assertEqual(e["media_url"], "https://cdn.example/i.jpg")

	def test_unknown_type_stays_unsupported_with_raw(self):
		att = {"type": "something_new", "payload": {"foo": "bar"}}
		e = self.one(att)
		self.assertEqual(e["message_type"], "unsupported")
		self.assertEqual(e["raw"], att)

	def test_link_type_with_nothing_to_show_stays_unsupported(self):
		e = self.one({"type": "fallback", "payload": None})
		self.assertEqual(e["message_type"], "unsupported")


class StoryReplyTest(unittest.TestCase):
	def test_text_reply_gets_prefix(self):
		story = {"url": "https://cdn/s.jpg", "id": "S1"}
		e = page({"text": "Where can I buy this?", "reply_to": {"story": story}}, obj="instagram")[0]
		self.assertEqual(e["channel"], cc.INSTAGRAM)
		self.assertEqual(e["message_type"], "text")
		self.assertEqual(e["body"], "↩ Replied to your story\nWhere can I buy this?")

	def test_reply_without_text(self):
		e = page({"reply_to": {"story": {"id": "S1"}}, "text": ""}, obj="instagram")[0]
		self.assertEqual(e["body"], "↩ Replied to your story")

	def test_attachment_reply_keeps_prefix_on_first_event(self):
		e = page(
			{
				"reply_to": {"story": {"id": "S1"}},
				"attachments": [{"type": "image", "payload": {"url": "https://cdn/i.jpg"}}],
			},
			obj="instagram",
		)[0]
		self.assertEqual(e["message_type"], "image")
		self.assertEqual(e["body"], "↩ Replied to your story")

	def test_ordinary_reply_to_message_has_no_prefix(self):
		e = page({"text": "yes", "reply_to": {"mid": "m_0"}})[0]
		self.assertEqual(e["body"], "yes")


class CommentPhotoTest(unittest.TestCase):
	def comment(self, **value):
		base = {"item": "comment", "verb": "add", "comment_id": "P_C1", "post_id": "P", "from": {"id": "U1"}}
		change = {"field": "feed", "value": {**base, **value}}
		payload = {"object": "page", "entry": [{"id": "PAGE1", "time": 1, "changes": [change]}]}
		return ing.parse_webhook(payload)[0]

	def test_photo_url_appended(self):
		e = self.comment(message="love it", photo="https://scontent.example/p.jpg")
		self.assertEqual(e["message_type"], "comment")
		self.assertEqual(e["external_id"], "P_C1")
		self.assertEqual(e["body"], "love it\nPhoto: https://scontent.example/p.jpg")

	def test_photo_without_message(self):
		e = self.comment(photo="https://scontent.example/p.jpg")
		self.assertEqual(e["body"], "Photo: https://scontent.example/p.jpg")

	def test_comment_without_photo_unchanged(self):
		self.assertEqual(self.comment(message="hi")["body"], "hi")


if __name__ == "__main__":
	unittest.main()


def wa_value(field, value):
	return {
		"object": "whatsapp_business_account",
		"entry": [{"id": "WABA", "changes": [{"field": field, "value": value}]}],
	}


class WhatsAppUsernameUserTest(unittest.TestCase):
	"""2026-11-16: a customer chatting by WhatsApp username (no phone in the webhook) never
	reached the app, inbound or our echoes. Their chat is now keyed by the user id."""

	def test_message_without_from_uses_the_user_id(self):
		value = {
			"messaging_product": "whatsapp",
			"contacts": [{"user_id": "EG.1A2B", "profile": {"name": "A.M569"}}],
			"messages": [
				{"from_user_id": "EG.1A2B", "id": "wamid.U1", "timestamp": "1760608800", "type": "text",
					"text": {"body": "في توصيل المعادي الجديدة"}}
			],
		}
		events = [e for e in ing.parse_webhook(wa_value("messages", value)) if e.get("kind") == "message"]
		self.assertEqual(len(events), 1)
		e = events[0]
		self.assertEqual(e["external_id"], "EG.1A2B")
		self.assertEqual(e["contact_name"], "A.M569")
		self.assertIsNone(e["phone"])
		self.assertEqual(e["body"], "في توصيل المعادي الجديدة")

	def test_single_contact_user_id_is_the_fallback(self):
		value = {
			"contacts": [{"user_id": "EG.9Z", "profile": {"name": "Mona"}}],
			"messages": [{"id": "wamid.U2", "timestamp": "1760608800", "type": "text", "text": {"body": "hi"}}],
		}
		events = [e for e in ing.parse_webhook(wa_value("messages", value)) if e.get("kind") == "message"]
		self.assertEqual(events[0]["external_id"], "EG.9Z")

	def test_echo_to_a_username_user(self):
		value = {"message_echoes": [{"to_user_id": "EG.1A2B", "id": "wamid.E1", "timestamp": "1760608800",
			"type": "text", "text": {"body": "Jarz Royal Feast"}}]}
		events = [e for e in ing.parse_webhook(wa_value("smb_message_echoes", value)) if e.get("kind") == "message"]
		self.assertEqual(events[0]["external_id"], "EG.1A2B")
		self.assertEqual(events[0]["direction"], "Outbound")
		self.assertIsNone(events[0]["phone"])

	def test_unplaceable_message_is_logged_not_silently_lost(self):
		value = {"messages": [{"id": "wamid.X", "type": "text", "text": {"body": "secret text"}}]}
		with mock.patch.object(cc, "safe_log_error") as log:
			events = [e for e in ing.parse_webhook(wa_value("messages", value)) if e.get("kind") == "message"]
		self.assertEqual(events, [])
		log.assert_called_once()
		self.assertIn("dropped", log.call_args.args[0])
		self.assertNotIn("secret text", log.call_args.args[1])  # shape only, never the text

	def test_normal_phone_sender_unchanged(self):
		e = wa({"type": "text", "text": {"body": "hello"}})
		self.assertEqual((e["external_id"], e["phone"], e["contact_name"]), ("201111034268", "201111034268", "Sara"))


class WhatsAppUnavailableMessageTest(unittest.TestCase):
	"""2026-11-16 (CHAT-07804): WhatsApp delivered error 131060 instead of the content. Staff now
	see a note to read it on the phone, not a bare «Unsupported message»."""

	def test_131060_says_read_it_on_the_phone(self):
		e = wa(
			{
				"type": "unsupported",
				"errors": [{"code": 131060, "title": "This message is unavailable."}],
				"unsupported": {"type": "unknown"},
			}
		)
		self.assertEqual(e["message_type"], "unsupported")
		self.assertEqual(e["body"], ing.WA_UNAVAILABLE_NOTE)
		self.assertIn("WhatsApp Business", e["body"])

	def test_other_unknown_type_gets_the_generic_note(self):
		e = wa({"type": "poll", "poll": {}})
		self.assertEqual(e["message_type"], "unsupported")
		self.assertEqual(e["body"], ing.WA_UNSUPPORTED_NOTE)

	def test_real_edit_and_revoke_shapes_are_revisions_not_bubbles(self):
		edit = {
			"type": "edit",
			"edit": {
				"original_message_id": "wamid.ORIG",
				"message": {"type": "text", "text": {"body": "عاوز معاد التوصيل الساعه 2"}},
			},
		}
		self.assertEqual(
			ing._wa_revision(edit),
			{"revision": "edit", "target": "wamid.ORIG", "body": "عاوز معاد التوصيل الساعه 2"},
		)
		revoke = {"type": "revoke", "revoke": {"original_message_id": "wamid.ORIG"}}
		self.assertEqual(ing._wa_revision(revoke), {"revision": "delete", "target": "wamid.ORIG", "body": ""})
