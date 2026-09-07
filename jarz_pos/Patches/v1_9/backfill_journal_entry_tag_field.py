"""Copy the Jarz dedup tag off ``user_remark`` and into ``custom_jarz_je_tag``.

Every idempotency and reversal guard in ``delivery_handling`` used to match
``user_remark LIKE '%[JARZ-JE:<type>:<key>]%'`` — a company-wide search of free
text, satisfied by ANY submitted Journal Entry whose remark happened to contain
the literal. As of 2026-09-07 they match ``custom_jarz_je_tag`` with ``=``
instead, a field only this app writes.

Without this patch every entry posted before that release carries the tag ONLY
in its remark. The guards would then miss it and post a second one — a second
OFD freight accrual, a second courier-expense entry, a second sales-partner
settlement. The code does keep a legacy remark fallback for entries older than
the Custom Field itself, but that fallback exists so a failed backfill degrades
to the old behaviour instead of double-posting money entries — not so the
backfill can be skipped. Only the field is fast (the fallback is a
leading-wildcard scan of a TEXT column) and only the field is unforgeable.

Extraction is END-ANCHORED on purpose. Every writer composes the remark as
``f"{human} {tag}"`` — the tag is always the last thing in the field — so a tag
found anywhere else is not something this app wrote in the normal way. Those are
logged rather than copied: laundering a remark that was forged before the write
side was sanitised, straight into the field the guards now trust, is the one
mistake this patch could make that would be worse than doing nothing.
"""

import re

import frappe

FIELDNAME = "custom_jarz_je_tag"

#: The tag as the writers leave it: last thing in the remark, no nesting.
_TAG_AT_END = re.compile(r"(\[JARZ-JE:[^\[\]]{1,220}\])\s*$")

#: Anything tag-shaped, used only to tell "tag in the wrong place" from "no tag".
_TAG_ANYWHERE = re.compile(r"\[JARZ-JE:[^\[\]]{1,220}\]")


def execute():
    if not frappe.db.has_column("Journal Entry", FIELDNAME):
        # `utils.cleanup.ensure_journal_entry_tag_field` runs in before_migrate,
        # so this should never happen. Fail loudly rather than silently
        # recording the patch as done and leaving every legacy entry unstamped.
        frappe.throw(
            f"Journal Entry.{FIELDNAME} does not exist. "
            "ensure_journal_entry_tag_field (before_migrate) must run first; "
            "re-run `bench migrate` rather than marking this patch complete."
        )

    rows = frappe.db.get_all(
        "Journal Entry",
        filters={"user_remark": ["like", "%[JARZ-JE:%"], FIELDNAME: ["is", "not set"]},
        fields=["name", "user_remark"],
        limit_page_length=0,
    )

    stamped = 0
    misplaced = []
    for row in rows:
        remark = row.get("user_remark") or ""
        match = _TAG_AT_END.search(remark)
        if not match:
            if _TAG_ANYWHERE.search(remark):
                misplaced.append(row["name"])
            continue
        frappe.db.set_value(
            "Journal Entry", row["name"], FIELDNAME, match.group(1), update_modified=False
        )
        stamped += 1

    frappe.db.commit()

    print(f"[jarz_pos] backfilled {FIELDNAME} on {stamped} of {len(rows)} tagged Journal Entries")
    if misplaced:
        # Not an error, and deliberately not copied. A tag that is not at the
        # end of the remark was either hand-written in Desk or is a leftover of
        # the forgery the write-side sanitisers now block; either way an
        # operator should look at it rather than have it silently promoted into
        # the field the reversal guard trusts.
        print(
            "[jarz_pos] NOT backfilled — tag is not at the end of the remark, "
            f"review by hand: {', '.join(misplaced[:50])}"
            + (f" (+{len(misplaced) - 50} more)" if len(misplaced) > 50 else "")
        )
