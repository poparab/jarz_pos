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
