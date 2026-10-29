"""Chat photos and voice notes for the AI -- mocked, no site, no network.

Pins: which photos reach Claude (newest three, image types only, size cap,
labels, a failed read is skipped), how a transcribed voice note reads in the
transcript, how the transcription request is built (fields, file name, prompt
under 800 characters, no key in logs), the settings defaults (off, model), and
which job follows a media message (a photo / voice note is drafted by the
download job, never twice, never before its file).

Plain ``unittest.TestCase``; a stub ``frappe`` is registered when frappe is not
installed. Nothing here can reach OpenAI or Anthropic.
"""

import base64
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
	_utils.now_datetime = lambda: datetime.datetime(2026, 10, 29, 12, 0)
	_utils.nowdate = lambda: "2026-10-29"
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_drafts as d
from jarz_pos.services import chat_ingest as ing
from jarz_pos.services import chat_media as media
from jarz_pos.services import chat_order as order
from jarz_pos.services import chat_settings as cs
from jarz_pos.services import chat_transcribe as tr

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
GIF = b"GIF89a" + b"\x00" * 20
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20


def when(minute):
	return datetime.datetime(2026, 10, 29, 19, minute)


def photo(n, minute=None, **kw):
	row = {
		"name": f"MSG-{n}",
		"direction": "Inbound",
		"message_type": "image",
		"body": "",
		"media_file": f"/private/files/chat-{n}.jpg",
		"media_mime": "image/jpeg",
		"timestamp": when(minute if minute is not None else n),
	}
	row.update(kw)
	return row


def files(**by_url):
	"""A reader returning the bytes registered per file url (default: a JPEG)."""
	return lambda url: by_url.get(url, JPEG)


class PhotoSelectionTest(unittest.TestCase):
	def test_newest_three_in_chat_order(self):
		rows = [photo(n) for n in range(1, 6)]
		blocks = media.image_blocks(rows, read=files())
		labels = [b["text"] for b in blocks if b["type"] == "text"]
		self.assertEqual(len([b for b in blocks if b["type"] == "image"]), 3)
		self.assertEqual(
			[x.split("at ")[-1] for x in labels],
			["[Thu 29 Oct 19:03]", "[Thu 29 Oct 19:04]", "[Thu 29 Oct 19:05]"],
		)

	def test_each_image_follows_its_label(self):
		blocks = media.image_blocks([photo(1)], read=files())
		self.assertEqual([b["type"] for b in blocks], ["text", "image"])
		source = blocks[1]["source"]
		self.assertEqual(source["type"], "base64")
		self.assertEqual(source["media_type"], "image/jpeg")
		self.assertEqual(base64.b64decode(source["data"]), JPEG)

	def test_label_with_a_message_number(self):
		blocks = media.image_blocks([photo(7)], numbers={"MSG-7": 4}, read=files())
		self.assertTrue(blocks[0]["text"].startswith("Photo from message [4]"))
		self.assertIn("19:07", blocks[0]["text"])

	def test_label_without_a_number_gives_the_time(self):
		blocks = media.image_blocks([photo(7)], read=files())
		self.assertIn("Photo from the customer's message at [Thu 29 Oct 19:07]", blocks[0]["text"])

	def test_only_customer_photos_of_image_type(self):
		rows = [
			photo(1, direction="Outbound"),
			photo(2, message_type="sticker"),
			photo(3, message_type="audio", media_mime="audio/ogg"),
			photo(4, message_type="video", media_mime="video/mp4"),
			photo(5, media_file=""),
			photo(6, media_file="https://example.com/x.jpg"),
			photo(7, media_mime="image/svg+xml"),
			photo(8, media_mime="application/pdf"),
		]
		read = mock.Mock(return_value=JPEG)
		self.assertEqual(media.image_blocks(rows, read=read), [])
		read.assert_not_called()

	def test_allowed_types_and_unknown_mime_falls_back_to_the_extension(self):
		rows = [
			photo(1, media_mime="image/png", media_file="/private/files/a.png"),
			photo(2, media_mime="image/webp", media_file="/private/files/b.webp"),
			photo(3, media_mime="image/gif", media_file="/private/files/c.gif"),
			photo(4, media_mime="", media_file="/private/files/d.jpg"),
			photo(5, media_mime="image/jpeg; charset=binary"),
		]
		read = files(
			**{
				"/private/files/a.png": PNG,
				"/private/files/b.webp": WEBP,
				"/private/files/c.gif": GIF,
			}
		)
		blocks = media.image_blocks(rows[:3], read=read)
		kinds = [b["source"]["media_type"] for b in blocks if b["type"] == "image"]
		self.assertEqual(kinds, ["image/png", "image/webp", "image/gif"])
		self.assertEqual(len(media.photo_candidates(rows[3:])), 2)

	def test_declared_type_is_replaced_by_the_real_one(self):
		blocks = media.image_blocks(
			[photo(1, media_mime="image/jpeg")], read=files(**{"/private/files/chat-1.jpg": PNG})
		)
		self.assertEqual(blocks[1]["source"]["media_type"], "image/png")

	def test_not_an_image_is_skipped(self):
		self.assertEqual(
			media.image_blocks([photo(1)], read=files(**{"/private/files/chat-1.jpg": b"%PDF-1.4"})), []
		)

	def test_oversize_file_is_skipped_and_an_older_one_takes_its_place(self):
		big = JPEG + b"\x00" * media.MAX_IMAGE_BYTES
		rows = [photo(1), photo(2), photo(3)]
		blocks = media.image_blocks(rows, read=files(**{"/private/files/chat-3.jpg": big}))
		labels = [b["text"] for b in blocks if b["type"] == "text"]
		self.assertEqual(len(labels), 2)
		self.assertTrue(labels[0].endswith("19:01]"))
		self.assertTrue(labels[1].endswith("19:02]"))

	def test_exactly_at_the_cap_is_kept(self):
		edge = JPEG + b"\x00" * (media.MAX_IMAGE_BYTES - len(JPEG))
		self.assertEqual(len(edge), media.MAX_IMAGE_BYTES)
		self.assertEqual(len(media.image_blocks([photo(1)], read=lambda url: edge)), 2)

	def test_failed_read_leaves_the_line_alone_and_never_raises(self):
		def boom(url):
			raise OSError("disk")

		self.assertEqual(media.image_blocks([photo(1), photo(2)], read=boom), [])
		self.assertEqual(media.image_blocks([photo(1)], read=lambda url: None), [])

	def test_read_attempts_are_bounded(self):
		read = mock.Mock(return_value=None)
		media.image_blocks([photo(n) for n in range(1, 30)], read=read)
		self.assertEqual(read.call_count, media.MAX_READ_ATTEMPTS)

	def test_read_image_never_raises_and_respects_the_size(self):
		with mock.patch.object(media.frappe, "get_doc", side_effect=Exception("missing"), create=True):
			self.assertIsNone(media.read_image("/private/files/x.jpg"))
		big = {"file_size": media.MAX_IMAGE_BYTES + 1}
		doc = mock.Mock(get=lambda key, default=None: big.get(key), get_content=mock.Mock(return_value=JPEG))
		with mock.patch.object(media.frappe, "get_doc", return_value=doc, create=True):
			self.assertIsNone(media.read_image("/private/files/x.jpg"))
			doc.get_content.assert_not_called()
		small = mock.Mock(get=lambda key, default=None: 1000, get_content=mock.Mock(return_value=JPEG))
		with mock.patch.object(media.frappe, "get_doc", return_value=small, create=True) as get_doc:
			self.assertEqual(media.read_image("/private/files/x.jpg"), JPEG)
		get_doc.assert_called_once_with("File", {"file_url": "/private/files/x.jpg"})

	def test_user_content_puts_photos_before_the_text(self):
		self.assertEqual(media.user_content("hello", []), "hello")
		content = media.user_content("hello", media.image_blocks([photo(1)], read=files()))
		self.assertEqual([b["type"] for b in content], ["text", "image", "text"])
		self.assertEqual(content[-1], {"type": "text", "text": "hello"})

	def test_sniff(self):
		self.assertEqual(media.sniff_mime(JPEG), "image/jpeg")
		self.assertEqual(media.sniff_mime(PNG), "image/png")
		self.assertEqual(media.sniff_mime(GIF), "image/gif")
		self.assertEqual(media.sniff_mime(WEBP), "image/webp")
		self.assertIsNone(media.sniff_mime(b"RIFF\x00\x00\x00\x00WAVE"))
		self.assertIsNone(media.sniff_mime(None))


class OrderNumbersTest(unittest.TestCase):
	def test_numbered_transcript_rows_carry_the_name_for_the_label(self):
		rows = [
			photo(1),
			{
				"name": "MSG-2",
				"direction": "Inbound",
				"message_type": "text",
				"body": "this one",
				"timestamp": when(2),
			},
		]
		lines, by_number = order.numbered_transcript(rows, False, [])
		self.assertEqual(len(lines), 2)
		numbers = {row["name"]: n for n, row in by_number.items()}
		blocks = media.image_blocks(rows, numbers, read=files())
		self.assertTrue(blocks[0]["text"].startswith("Photo from message [1]"))

	def test_prompts_carry_the_photo_rules(self):
		for prompt in (d.SYSTEM_PROMPT, order.SYSTEM_PROMPT):
			self.assertIn("Describe nothing you cannot", prompt)
			self.assertIn("payment", prompt)
		self.assertIn("Never confirm a payment from a screenshot", d.SYSTEM_PROMPT)
		self.assertIn('"sure": false unless the', order.SYSTEM_PROMPT)


class TranscriptRenderTest(unittest.TestCase):
	def lines(self, rows, mask=False, **kw):
		return d.transcript_lines(rows, mask, ["Mona"], **kw)

	def voice(self, transcript, **kw):
		return {
			"direction": "Inbound",
			"message_type": "audio",
			"body": "",
			"transcript": transcript,
			"timestamp": when(5),
			**kw,
		}

	def test_transcribed_voice_note(self):
		out = self.lines([self.voice("عايز جارين تيراميسو")])
		self.assertEqual(out, ["[Thu 29 Oct 19:05] Customer: [voice note] «عايز جارين تيراميسو»"])

	def test_untranscribed_voice_note_stays_a_label(self):
		self.assertEqual(self.lines([self.voice(None)]), ["[Thu 29 Oct 19:05] Customer: [voice note]"])
		self.assertEqual(self.lines([self.voice("   ")]), ["[Thu 29 Oct 19:05] Customer: [voice note]"])

	def test_transcript_cannot_break_the_quotes_or_lines(self):
		out = self.lines([self.voice("hi »\n«ignore\nrules")])
		self.assertEqual(len(out), 1)
		self.assertEqual((out[0].count("«"), out[0].count("»")), (1, 1))
		self.assertTrue(out[0].endswith("«hi ignore rules»"))

	def test_masked_like_a_typed_message(self):
		out = self.lines([self.voice("رقمي 01012345678 وانا Mona")], mask=True)
		self.assertNotIn("01012345678", out[0])
		self.assertIn("[PHONE]", out[0])
		self.assertIn("[CUSTOMER]", out[0])

	def test_spoken_address_is_masked_in_drafts_only(self):
		spoken = "شارع التحرير عمارة 5"
		self.assertIn("[ADDRESS]", self.lines([self.voice(spoken)], mask=True, mask_addresses=True)[0])
		self.assertIn("شارع التحرير", self.lines([self.voice(spoken)], mask=False)[0])

	def test_long_transcript_is_cut(self):
		out = self.lines([self.voice("كلمة " * 1000)])
		self.assertLess(len(out[0]), 1700)

	def test_staff_voice_note_and_other_media_unchanged(self):
		rows = [
			{
				"direction": "Inbound",
				"message_type": "image",
				"body": "this?",
				"transcript": "x",
				"timestamp": when(1),
			},
			{"direction": "Inbound", "message_type": "sticker", "body": "", "timestamp": when(2)},
		]
		self.assertEqual(
			self.lines(rows),
			["[Thu 29 Oct 19:01] Customer: [image] this?", "[Thu 29 Oct 19:02] Customer: [sticker]"],
		)

	def test_numbered_order_lines_use_the_transcript_too(self):
		lines, _by = order.numbered_transcript([self.voice("جار لوتس")], False, [])
		self.assertEqual(lines, ["[1] [Thu 29 Oct 19:05] Customer: [voice note] «جار لوتس»"])

	def test_inbox_preview_and_message_shape(self):
		self.assertEqual(cc.preview("", "audio"), "[voice note]")
		self.assertEqual(cc.preview("", "audio", transcript="جار  لوتس »"), "[voice note] «جار لوتس»")
		self.assertEqual(cc.preview("hello", "text", transcript="x"), "hello")
		self.assertIn("transcript", cc.MESSAGE_FIELDS)
		shape = cc.serialize_message({"name": "M", "transcript": "نص", "message_type": "audio"})
		self.assertEqual(shape["transcript"], "نص")
		self.assertIsNone(cc.serialize_message({"name": "M"})["transcript"])


class TranscriptionRequestTest(unittest.TestCase):
	def test_form_fields(self):
		form = tr.build_form("gpt-4o-transcribe", "p")
		self.assertEqual(
			form,
			{"model": "gpt-4o-transcribe", "language": "ar", "response_format": "json", "prompt": "p"},
		)

	def test_filename_extension_is_one_the_api_accepts(self):
		self.assertEqual(tr.audio_filename("audio/ogg; codecs=opus", None, "ABC123"), "ABC123.ogg")
		self.assertEqual(tr.audio_filename("audio/mp4", None, "A"), "A.m4a")
		self.assertEqual(tr.audio_filename("audio/mpeg", None, "A"), "A.mp3")
		self.assertEqual(tr.audio_filename("audio/ogg", "chat-X.ogg", "A"), "A.ogg")
		self.assertEqual(tr.audio_filename("audio/x-unknown", "voice.m4a", "A"), "A.m4a")
		self.assertEqual(tr.audio_filename(None, None, None), "voice-note.ogg")
		self.assertEqual(tr.audio_filename("audio/ogg", None, "a/../b c"), "abc.ogg")

	def test_prompt_is_short_and_has_the_menu(self):
		prompt = tr.vocabulary_prompt(["Tiramisu Jar", "Lotus Jar", "tiramisu jar", ""])
		self.assertLess(len(prompt), 800)
		self.assertIn("Egyptian Arabic", prompt)
		self.assertEqual(prompt.count("Tiramisu Jar"), 1)
		self.assertIn("Lotus Jar", prompt)

	def test_prompt_cuts_whole_names_and_stays_under_800(self):
		names = [f"Flavour number {n} special" for n in range(300)]
		prompt = tr.vocabulary_prompt(names)
		self.assertLessEqual(len(prompt), tr.MAX_PROMPT_CHARS)
		self.assertTrue(prompt.split(", ")[-1].startswith("Flavour number"))
		self.assertTrue(prompt.endswith("special"))
		self.assertLess(len(tr.vocabulary_prompt([])), 400)

	def test_skip_reasons(self):
		ok = {"transcribe_voice_notes": True, "openai_api_key": "sk-test"}
		row = {"message_type": "audio", "media_file": "/private/files/a.ogg"}
		self.assertIsNone(tr.skip_reason(ok, row))
		self.assertEqual(tr.skip_reason({**ok, "transcribe_voice_notes": False}, row), "transcription is off")
		self.assertEqual(tr.skip_reason({**ok, "openai_api_key": " "}, row), "no OpenAI key")
		self.assertEqual(tr.skip_reason(ok, {**row, "message_type": "image"}), "not a voice note")
		self.assertEqual(tr.skip_reason(ok, {**row, "media_file": ""}), "no file")
		self.assertEqual(tr.skip_reason(ok, {**row, "transcript": "done"}), "already transcribed")
		self.assertIsNone(tr.skip_reason(ok, row, tr.MAX_AUDIO_BYTES))
		self.assertEqual(tr.skip_reason(ok, row, tr.MAX_AUDIO_BYTES + 1), "file is larger than 25 MB")

	def test_key_is_redacted(self):
		self.assertNotIn("sk-proj-abc123", tr.redact("Incorrect API key provided: sk-proj-abc123."))

	def test_clean_transcript(self):
		self.assertEqual(tr.clean_transcript("  hello   world \n\n second  "), "hello world\n\nsecond")
		self.assertEqual(len(tr.clean_transcript("x" * 9000)), tr.MAX_TRANSCRIPT_CHARS)


class Row(dict):
	"""A ``frappe._dict``: keys readable as attributes."""

	__getattr__ = dict.get


class TranscribeFlowTest(unittest.TestCase):
	"""``transcribe`` end to end with the network and the database mocked."""

	SETTINGS = {
		"transcribe_voice_notes": True,
		"openai_api_key": "sk-secret",
		"transcription_model": "gpt-4o-transcribe",
	}
	ROW = Row(
		name="MSG-1",
		conversation="CONV-1",
		message_type="audio",
		media_file="/private/files/chat-MSG-1.ogg",
		media_mime="audio/ogg; codecs=opus",
		transcript=None,
		timestamp=when(5),
	)

	def run_flow(self, settings=None, row=None, post=None, content=b"OggS" + b"\x00" * 10):
		db = SimpleNamespace(
			get_value=mock.Mock(return_value=row or self.ROW),
			set_value=mock.Mock(),
			commit=mock.Mock(),
			rollback=mock.Mock(),
		)
		doc = mock.Mock(
			get=lambda key, default=None: {"file_size": len(content), "file_name": "chat-MSG-1.ogg"}.get(key),
			get_content=mock.Mock(return_value=content),
		)
		post = post or mock.Mock(return_value="  عايز جارين  ")
		with (
			mock.patch.object(tr.frappe, "db", db, create=True),
			mock.patch.object(tr.frappe, "get_doc", return_value=doc, create=True),
			mock.patch(
				"jarz_pos.services.chat_settings.get_chat_settings", return_value=settings or self.SETTINGS
			),
			mock.patch.object(tr, "_post", post),
			mock.patch.object(tr, "_menu_names", return_value=["Lotus Jar"]),
			mock.patch.object(tr, "_refresh_preview") as preview,
			mock.patch.object(tr.cc, "publish_chat_update") as publish,
			mock.patch.object(tr.cc, "load_message", return_value={"name": "MSG-1"}),
			mock.patch.object(tr.cc, "safe_log_error") as log,
		):
			out = tr.transcribe("MSG-1")
		return out, db, post, publish, preview, log

	def test_success_stores_publishes_and_sends_the_right_request(self):
		out, db, post, publish, preview, _log = self.run_flow()
		self.assertEqual(out, "عايز جارين")
		db.set_value.assert_called_once_with(
			"Jarz Chat Message", "MSG-1", "transcript", "عايز جارين", update_modified=False
		)
		publish.assert_called_once()
		preview.assert_called_once()
		key, filename, content_type, content, form = post.call_args.args
		self.assertEqual((key, filename, content_type), ("sk-secret", "MSG-1.ogg", "audio/ogg"))
		self.assertEqual(form["model"], "gpt-4o-transcribe")
		self.assertEqual(form["language"], "ar")
		self.assertIn("Lotus Jar", form["prompt"])

	def test_skipped_without_a_call(self):
		for settings in (
			{**self.SETTINGS, "transcribe_voice_notes": False},
			{**self.SETTINGS, "openai_api_key": ""},
		):
			out, db, post, _p, _pv, log = self.run_flow(settings=settings)
			self.assertIsNone(out)
			post.assert_not_called()
			db.set_value.assert_not_called()
			log.assert_not_called()

	def test_oversize_audio_is_not_sent(self):
		out, _db, post, *_ = self.run_flow(content=b"\x00" * (tr.MAX_AUDIO_BYTES + 1))
		self.assertIsNone(out)
		post.assert_not_called()

	def test_failure_never_raises_and_the_log_has_no_key(self):
		post = mock.Mock(side_effect=RuntimeError("401 Incorrect API key provided: sk-secret-123"))
		out, db, _post, publish, _pv, log = self.run_flow(post=post)
		self.assertIsNone(out)
		db.set_value.assert_not_called()
		publish.assert_not_called()
		message = log.call_args.args[1]
		self.assertNotIn("sk-secret", message)
		self.assertIn("message=MSG-1", message)

	def test_empty_text_is_not_stored(self):
		out, db, *_ = self.run_flow(post=mock.Mock(return_value="   "))
		self.assertIsNone(out)
		db.set_value.assert_not_called()


class SettingsDefaultsTest(unittest.TestCase):
	def test_defaults_when_never_saved(self):
		built = cs.build_settings({}, {})
		self.assertFalse(built["transcribe_voice_notes"])
		self.assertEqual(built["transcription_model"], "gpt-4o-transcribe")
		self.assertEqual(built["openai_api_key"], "")

	def test_values_and_flag_override(self):
		doc = SimpleNamespace(
			get=lambda key, default=None: {"transcription_model": " custom-model "}.get(key, default),
			get_password=lambda fieldname, raise_exception=False: "sk-x"
			if fieldname == "openai_api_key"
			else "",
		)
		built = cs.build_settings(doc, {"transcribe_voice_notes": True})
		self.assertTrue(built["transcribe_voice_notes"])
		self.assertEqual(built["transcription_model"], "custom-model")
		self.assertEqual(built["openai_api_key"], "sk-x")

	def test_key_is_a_secret_field(self):
		self.assertIn("openai_api_key", cs._SECRET_FIELDS)


class JobPlanTest(unittest.TestCase):
	def plan(self, **kw):
		return media.plan_jobs({"media": True, "draft": True, "message_type": "image", "push": True, **kw})

	def test_photo_is_drafted_after_its_download(self):
		plan = self.plan()
		self.assertTrue(plan["download"])
		self.assertTrue(plan["draft_after_download"])
		self.assertFalse(plan["draft_now"])
		self.assertFalse(plan["transcribe"])

	def test_voice_note_is_downloaded_transcribed_then_drafted(self):
		plan = self.plan(message_type="audio")
		self.assertEqual(
			(plan["download"], plan["transcribe"], plan["draft_after_download"], plan["draft_now"]),
			(True, True, True, False),
		)

	def test_text_drafts_at_once(self):
		plan = media.plan_jobs({"media": False, "draft": True, "message_type": "text", "push": True})
		self.assertEqual(
			(plan["download"], plan["draft_now"], plan["draft_after_download"], plan["transcribe"]),
			(False, True, False, False),
		)

	def test_never_one_draft_twice(self):
		for kw in ({}, {"message_type": "audio"}, {"message_type": "text", "media": False}, {"media": False}):
			plan = self.plan(**kw)
			self.assertFalse(plan["draft_now"] and plan["draft_after_download"])

	def test_no_draft_flag_means_no_draft_job(self):
		for mtype in ("image", "audio", "text"):
			plan = self.plan(message_type=mtype, draft=False)
			self.assertFalse(plan["draft_now"] or plan["draft_after_download"])

	def test_history_and_echo_media_never_draft_or_transcribe(self):
		plan = media.plan_jobs(
			{"media": True, "draft": False, "message_type": "audio", "push": False, "quiet": True}
		)
		self.assertEqual(
			plan, {"download": True, "draft_now": False, "draft_after_download": False, "transcribe": False}
		)

	def test_media_without_a_file_drafts_at_once(self):
		plan = self.plan(media=False)
		self.assertTrue(plan["draft_now"])
		self.assertFalse(plan["draft_after_download"])

	def test_other_media_types_do_not_wait(self):
		plan = self.plan(message_type="video")
		self.assertTrue(plan["draft_now"])
		self.assertFalse(plan["draft_after_download"])

	def test_should_draft_covers_photos_and_voice_notes_with_a_file(self):
		def ev(**kw):
			return {"kind": "message", "direction": "Inbound", "message_type": "audio", "body": "", **kw}

		self.assertTrue(ing.should_draft(ev(media_id="wamid")))
		self.assertTrue(ing.should_draft(ev(message_type="image", media_url="https://cdn/x")))
		self.assertTrue(ing.should_draft(ev(message_type="image", body="هل ده؟")))
		self.assertFalse(ing.should_draft(ev()))
		self.assertFalse(ing.should_draft(ev(message_type="image")))
		self.assertFalse(ing.should_draft(ev(media_id="wamid", direction="Outbound")))
		self.assertFalse(ing.should_draft(ev(message_type="video", media_id="wamid")))


class DownloadJobTest(unittest.TestCase):
	"""The follow-up of ``download_media``: transcribe, then draft, even after a failure."""

	def run_job(self, file_url="/private/files/a.ogg", **kw):
		with (
			mock.patch.object(ing, "_fetch_media", return_value=file_url) as fetch,
			mock.patch("jarz_pos.services.chat_transcribe.transcribe") as transcribe,
			mock.patch.object(ing, "_enqueue_draft") as enqueue,
			mock.patch.object(ing.cc, "safe_log_error"),
		):
			out = ing.download_media("MSG-1", "https://cdn/x", **kw)
		return out, fetch, transcribe, enqueue

	def test_voice_note_transcribed_then_drafted(self):
		order_of_calls = []
		with (
			mock.patch.object(ing, "_fetch_media", return_value="/f"),
			mock.patch(
				"jarz_pos.services.chat_transcribe.transcribe",
				side_effect=lambda m: order_of_calls.append("transcribe"),
			),
			mock.patch.object(ing, "_enqueue_draft", side_effect=lambda m: order_of_calls.append("draft")),
		):
			ing.download_media("MSG-1", None, draft_after=True, transcribe=True)
		self.assertEqual(order_of_calls, ["transcribe", "draft"])

	def test_photo_only_drafts(self):
		out, fetch, transcribe, enqueue = self.run_job(draft_after=True)
		self.assertEqual(out, "/private/files/a.ogg")
		fetch.assert_called_once_with("MSG-1", "https://cdn/x")
		transcribe.assert_not_called()
		enqueue.assert_called_once_with("MSG-1")

	def test_download_failure_still_drafts_blind(self):
		_out, _f, transcribe, enqueue = self.run_job(file_url=None, draft_after=True, transcribe=True)
		transcribe.assert_not_called()
		enqueue.assert_called_once_with("MSG-1")

	def test_transcription_crash_still_drafts(self):
		with (
			mock.patch.object(ing, "_fetch_media", return_value="/f"),
			mock.patch("jarz_pos.services.chat_transcribe.transcribe", side_effect=RuntimeError("x")),
			mock.patch.object(ing, "_enqueue_draft") as enqueue,
			mock.patch.object(ing.cc, "safe_log_error"),
		):
			ing.download_media("MSG-1", None, draft_after=True, transcribe=True)
		enqueue.assert_called_once_with("MSG-1")

	def test_plain_download_does_nothing_else(self):
		_out, _f, transcribe, enqueue = self.run_job()
		transcribe.assert_not_called()
		enqueue.assert_not_called()

	def test_draft_enqueue_targets_the_draft_job_for_that_message(self):
		enqueue = mock.Mock()
		with (
			mock.patch.object(
				ing.frappe, "db", SimpleNamespace(get_value=lambda *a, **k: "CONV-1"), create=True
			),
			mock.patch.object(ing.frappe, "enqueue", enqueue, create=True),
		):
			ing._enqueue_draft("MSG-1")
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(enqueue.call_args.args[0], "jarz_pos.services.chat_drafts.generate_draft_job")
		self.assertEqual((kwargs["conversation"], kwargs["for_message"]), ("CONV-1", "MSG-1"))
		self.assertFalse(kwargs["enqueue_after_commit"])

	def test_after_commit_enqueues_the_download_with_the_plan_and_no_early_draft(self):
		enqueue = mock.Mock()
		action = {
			"conversation": "C",
			"message": "M",
			"media": True,
			"media_url": None,
			"draft": True,
			"message_type": "audio",
			"push": False,
			"quiet": True,
		}
		with (
			mock.patch.object(ing.frappe, "enqueue", enqueue, create=True),
			mock.patch.object(ing.frappe, "db", SimpleNamespace(commit=lambda: None), create=True),
			mock.patch.object(ing.cc, "publish_chat_update"),
		):
			ing._after_commit([dict(action, push=True, quiet=False)], set())
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(enqueue.call_args.args[0], "jarz_pos.services.chat_ingest.download_media")
		kwargs = enqueue.call_args.kwargs
		self.assertTrue(kwargs["draft_after"])
		self.assertTrue(kwargs["transcribe"])
		self.assertGreaterEqual(kwargs["timeout"], 180)

	def test_after_commit_text_message_drafts_directly(self):
		enqueue = mock.Mock()
		action = {
			"conversation": "C",
			"message": "M",
			"media": False,
			"draft": True,
			"message_type": "text",
			"push": False,
			"quiet": True,
		}
		with (
			mock.patch.object(ing.frappe, "enqueue", enqueue, create=True),
			mock.patch.object(ing.frappe, "db", SimpleNamespace(commit=lambda: None), create=True),
		):
			ing._after_commit([action], set())
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(enqueue.call_args.args[0], "jarz_pos.services.chat_drafts.generate_draft_job")


if __name__ == "__main__":
	unittest.main()
