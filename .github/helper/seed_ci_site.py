"""Prepare the throwaway CI site so jarz_pos can be installed on it.

Jarz POS Settings ships Link defaults named for the Jarz company (abbreviation
"J"). Installing the app saves that Single with those defaults, so every record
they point at must exist first. This runs only in the Server job, on the
``test_site`` it just created; it never touches a real site.

Usage, from the bench's ``sites`` folder:
    ../env/bin/python seed_ci_site.py <site>
"""

import json
import os
import sys

import frappe
from frappe.utils import getdate, nowdate

COMPANY = "Jarz"
ABBR = "J"

#: Accounts the settings defaults name that the standard chart does not carry.
EXTRA_ACCOUNTS = (
	# (account name, parent group, account type)
	("Cash Over Short", "Indirect Expenses", "Expense Account"),
	("Courier Outstanding", "Current Assets", ""),
	("Mobile Wallet", "Bank Accounts", "Bank"),
)


def setup_company():
	from erpnext.setup.setup_wizard.setup_wizard import setup_complete

	year = getdate(nowdate()).year
	setup_complete(
		frappe._dict(
			company_name=COMPANY,
			company_abbr=ABBR,
			currency="EGP",
			country="Egypt",
			chart_of_accounts="Standard",
			fy_start_date=f"{year}-01-01",
			fy_end_date=f"{year}-12-31",
		)
	)


def add_accounts():
	for name, parent, account_type in EXTRA_ACCOUNTS:
		if frappe.db.exists("Account", f"{name} - {ABBR}"):
			continue
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": name,
				"parent_account": f"{parent} - {ABBR}",
				"company": COMPANY,
				"account_type": account_type,
			}
		).insert(ignore_permissions=True)


def missing_link_defaults():
	"""Every Link default on Jarz POS Settings whose target does not exist."""
	import jarz_pos

	path = os.path.join(
		os.path.dirname(jarz_pos.__file__), "doctype", "jarz_pos_settings", "jarz_pos_settings.json"
	)
	with open(path, encoding="utf-8") as f:
		fields = json.load(f)["fields"]
	return [
		f"{df['fieldname']} -> {df['options']}: {df['default']}"
		for df in fields
		if df.get("fieldtype") == "Link"
		and df.get("default")
		and not frappe.db.exists(df["options"], df["default"])
	]


def main(site):
	frappe.init(site=site, sites_path=".")
	frappe.connect()
	try:
		setup_company()
		add_accounts()
		frappe.db.commit()  # nosemgrep
		missing = missing_link_defaults()
	finally:
		frappe.destroy()

	if missing:
		print("Still missing for Jarz POS Settings defaults:", *missing, sep="\n  ")
		sys.exit(1)
	print("CI site ready: company, chart of accounts and every settings default exist.")


if __name__ == "__main__":
	main(sys.argv[1])
