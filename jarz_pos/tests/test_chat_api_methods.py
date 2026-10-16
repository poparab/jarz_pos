"""Chat inbox API: the HTTP methods each endpoint accepts.

Opening a thread (``get_conversation``) marks it read and sends WhatsApp read
receipts, so it must refuse GET: a forged GET link would otherwise mark a
customer's thread read. Reads that change nothing stay GET.
"""

import unittest

import frappe

from jarz_pos.api import chat


def _methods(fn):
	return frappe.allowed_http_methods_for_whitelisted_func.get(getattr(fn, "__func__", fn))


class ChatApiMethodsTest(unittest.TestCase):
	def test_get_conversation_is_post_only(self):
		self.assertEqual(_methods(chat.get_conversation), ["POST"])

	def test_list_read_still_accepts_get(self):
		self.assertIn("GET", _methods(chat.get_conversations))
