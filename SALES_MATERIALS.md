# Sales materials on WhatsApp

How a rep puts the price list in a prospect's hands without attaching five
files to a chat one at a time — and how anyone maintaining it should think
about the pieces.

## What happens when a rep taps "Send price list"

1. The app asks for the library and the message template in one call
   (`api.materials.get_sales_materials`).
2. The rep picks who and what, edits the Arabic message, taps send.
3. `api.materials.create_material_share` inserts a `Jarz Material Share`,
   mints a 96-bit token, substitutes `{name}` and `{link}`, writes a journey
   note on the lead, and returns a `wa.me` deep link.
4. The app opens WhatsApp with the chat and message ready. **The rep still
   presses send** — no platform lets an app send from a personal number, and
   this is deliberately not the WhatsApp Business API.
5. The prospect opens `https://erp.orderjarz.com/m/<token>`, a page built for
   one job: reading a price list on a phone.
6. The first open writes a second journey note and stamps the share. That
   signal — "did they look?" — is the reason this sends a link rather than
   attachments.

## Adding a price list (manager, Desk)

**Jarz Desk → JARZ POS → CRM & Leads → Sales Material → New.**

| Field | What to put in it |
|---|---|
| Title | English name. Shown to reps in the picker. |
| Arabic Title | What the *customer* sees. Leave blank to fall back to English. |
| Type | Price List / Product Photos / Catalog / Certificate / Other. |
| File | The PDF or image. Untick **Private** — or just save; the controller publishes it either way, because the reader has no login. |
| Selected by Default | Pre-ticked in the rep's send sheet. Tick this on the current price list. |
| Sort Order | Lowest first, in the picker and on the customer's page. |
| Enabled | Untick to retire it. It disappears from links **already sent**, without breaking them. |

Save, and a background job rasterises it. **Render Status** goes
`Pending → Ready`. If it says `Failed`, read **Render Error**; if it says
`Download Only`, the file is not a PDF or an image (a `.docx`, say) and the
customer gets a download card instead of a viewer.

### Replacing a price list

Edit the same record and attach the new file. The content hash changes, the
old renders are swept, and every link minted afterwards serves the new pixels.
Do **not** create a second record unless you want both sendable at once.

## Generated price list

The owner's Canva price list, rebuilt from live prices every time it is sent.
A material whose **Source** is *Generated Price List* has no file: when a rep
sends it, the backend prices the sheet for **that recipient**, prints it to a
PDF, rasterises it into the same three JPEG tiers as any other material, and
stores the result as a **snapshot** on the share. The customer's page is
unchanged (same payload, same viewer); the Download button gets the PDF.

Feature: `b2b-price-sheet`. Code: `services/price_sheet.py`,
`api/price_sheet.py`, `templates/price_sheet/price_sheet.html`,
`setup/price_sheet_setup.py`.

### What a manager edits

Open the material in Desk (or the app's template editor):

| Field | Meaning |
|---|---|
| Heading | The title under the Jarz logotype ("Price List"). |
| Price List | Blank prices each customer from their own B2B list; a lead or an unknown customer gets `B2B Selling`. |
| Sizes | Comma list of **Item Group** names, in column order (`Medium,Large`). |
| Show Customer Deals | Print a running deal's price (see below). |
| Flavours (rows) | `flavour` is the item-name stem: `Strawberry` prices the items `Strawberry Medium` and `Strawberry Large`. `label` is what the customer reads; blank prints the flavour. Row order is print order; untick *Enabled* to hide a row. |
| Terms | One per line, Arabic, printed right to left. `Label: text` (first colon) prints the label highlighted in coral. |
| Footer, Logo | Text beside the logo; a custom logo replaces the bundled one. |

A new migrate seeds **Jarz B2B Price List** (8 flavours, Medium/Large, the seven
terms) once. The seeder does nothing if *any* generated material exists, so a
rename or a delete is never undone.

### How a cell is priced

Every cell calls `invoice_creation._resolve_item_rate_with_provenance`, the
same resolver an invoice uses, so the sheet cannot promise a number the order
will not book. There is no second resolver.

- Item missing or disabled, or the resolver found nothing (provenance
  `client`): the cell is a dash. A row whose cells are all dashes is dropped.
- Provenance `deal` (the customer has a running **Jarz Customer Deal**): the
  cell shows the deal price in coral with the normal price struck through, and
  one line under the table says "Special prices valid until <date>". With
  several deals the date is the **earliest** end date, so the line is true of
  every deal price above it.
- A Lead or Opportunity that has converted prices as its Customer; one that has
  not has no deal and gets the base list.

### Snapshots: the link keeps the prices it was minted with

`create_material_share` builds the sheet synchronously (before the share row
exists) and stores its digest on the share item (`Jarz Material Share Item.
snapshot`). `get_public_share` serves those pages, never today's prices, so a
link forwarded to a partner next week still shows what the rep sent. If the
build fails, `create_material_share` throws "Could not build the price sheet"
and no link is minted.

The digest is `sha256` over everything the page prints plus a render version
and the logo's content hash; identical sheets share one directory. Files live
in `public/files/jarz_price_sheets/<digest>/` (`price-list.pdf`, `p0-*.jpg`,
`manifest.json`), a **separate root** from `jarz_materials` because
`_sweep_stale_digests` there deletes everything but a material's newest digest,
which would break links already sent. Nothing sweeps this root; a sheet is
~300 KB. The digest includes the template file's content hash, so editing the
HTML re-renders; bump `RENDER_VERSION` for Python-side changes (print options,
fonts, rasteriser).

### PDF engine gotchas

- `frappe.utils.pdf.get_pdf` always passes `--disable-local-file-access`, so
  fonts and the logo are embedded as **base64 data URIs** in the HTML. A
  `file://` reference silently prints in DejaVu with no logo. Fonts live in
  `public/fonts` (Inter, Tajawal, DM Serif Display); the servers have only
  DejaVu installed.
- wkhtmltopdf 0.12.6 is Qt 4.8 WebKit: no flexbox, no grid, no `gap`. The
  template is tables and inline-blocks. Print a real PDF after touching it.

### API (staff)

`api.materials.get_sales_materials` marks generated items (`generated`,
`source_type`, `ready: true`, `download_url: null`) and returns
`can_edit_sheets`. `api.price_sheet.preview_price_sheet` (B2B access),
`get_price_sheet_template` (pricing read), `save_price_sheet_template`
(**full manager only**, POST; `rows` is a full replacement).

## Why the page serves images, not the PDF

Mobile browsers treat an inline PDF as somebody else's problem: Android Chrome
downloads it, iOS Safari opens a viewer whose zoom fights the page. So every
source is rasterised once into three JPEG tiers — 480px for the list, 1600px
for the viewer, 3200px swapped in when the reader zooms — and the viewer works
on a plain `<img>` it can transform on the GPU.

The ladder stops at 3200 because a decoded JPEG costs `w*h*4` bytes of RAM:
3200x2400 is ~30MB, and 6400px would be ~120MB, which is an out-of-memory kill
on the phones this audience carries. Sharper than 3200 comes from "download
the original", not from a bigger tier.

Details, including why `will-change` is dropped after each gesture, are in the
header comment of `jarz_pos/www/m.html`. Read it before changing that file.

## Operational notes

- **Derivatives are public static files** under
  `public/files/jarz_materials/<material>/<digest>/`, served by nginx without
  touching Python. The digest makes them unguessable; the *set* — which lead
  was sent what — is what the token gates.
- **New runtime dependency:** `pypdfium2` (in both `pyproject.toml` and
  `requirements.txt`). Without it, PDFs degrade to download-only cards rather
  than failing.
- **Re-render one material:** `api.materials.rebuild_material(name)`, or just
  save the record again.
- **Links never expire** unless `expires_on` is set. An expired token and an
  unknown token return the same "not found", on purpose.
- **Views are throttled** to one per viewer per 30 minutes, so a prospect who
  scrolls back to the tab does not read as three separate openings.

## What this is not

It is not the WhatsApp Business Cloud API. Nothing here sends a message on its
own, nothing costs per conversation, and no number is consumed by an API. If
that becomes worth it — automatic sends from one company number, with a shared
inbox for the replies — it is a separate piece of work that keeps this one as
its fallback.
