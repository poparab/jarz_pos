// Copyright (c) 2025, Abdelrahman Mamdouh and contributors
// For license information, please see license.txt

// Live weekly slot preview. Every edit on this form — slot length, custom last
// slot, opening/closing hours — re-renders the exact slots the POS will offer,
// before the document is saved. The server side reuses the same generator the
// delivery-slots API uses, so the preview cannot drift from reality.

const PREVIEW_METHOD = "jarz_pos.api.delivery_slots.preview_timetable_slots";

function render_slot_preview(frm) {
	const wrapper = frm.fields_dict.slot_preview
		&& frm.fields_dict.slot_preview.$wrapper;
	if (!wrapper) return;

	const slot_minutes = cint(frm.doc.slot_hours) * 60 + cint(frm.doc.slot_minutes);
	if (!slot_minutes) {
		wrapper.html(
			`<div class="text-muted">Set a slot length to preview the weekly slots.</div>`
		);
		return;
	}

	const config = {
		slot_hours: cint(frm.doc.slot_hours),
		slot_minutes: cint(frm.doc.slot_minutes),
		has_custom_last_slot: cint(frm.doc.has_custom_last_slot),
		last_slot_hours: cint(frm.doc.last_slot_hours),
		last_slot_minutes: cint(frm.doc.last_slot_minutes),
		anchor_last_slot_to_closing: cint(frm.doc.anchor_last_slot_to_closing),
		timetable: (frm.doc.timetable || []).map((row) => ({
			day: row.day,
			opening_time: row.opening_time,
			closing_time: row.closing_time,
			same_day: row.same_day,
		})),
	};

	frappe
		.call({ method: PREVIEW_METHOD, args: { config: JSON.stringify(config) } })
		.then((r) => {
			if (!r || !r.message) return;
			wrapper.html(build_preview_html(r.message));
		})
		.catch(() => {
			wrapper.html(
				`<div class="text-danger">Could not build the slot preview.</div>`
			);
		});
}

function format_duration(minutes) {
	const h = Math.floor(minutes / 60);
	const m = minutes % 60;
	if (h && m) return `${h}h ${m}m`;
	if (h) return `${h}h`;
	return `${m}m`;
}

function build_preview_html(data) {
	const header_bits = [
		`Slot length: <b>${format_duration(data.slot_duration_minutes)}</b>`,
	];
	if (data.last_slot_duration_minutes) {
		header_bits.push(
			`Custom last slot: <b>${format_duration(data.last_slot_duration_minutes)}</b>`
		);
	}
	header_bits.push(`Slots per week: <b>${data.total_slots}</b>`);

	const rows = (data.days || [])
		.map((day) => {
			const hours = day.configured && day.opening_time
				? `${day.opening_time.slice(0, 5)} – ${day.closing_time.slice(0, 5)}` +
					(day.same_day === "Next Day" ? " <span class='text-muted'>(+1d)</span>" : "")
				: `<span class="text-muted">—</span>`;

			const chips = (day.slots || [])
				.map((slot) => {
					const crosses = slot.end <= slot.start;
					return `<span class="jarz-slot-chip">${slot.start}–${slot.end}${
						crosses ? "<sup>+1</sup>" : ""
					}</span>`;
				})
				.join("");

			const note = day.note
				? `<div class="${day.slot_count ? "text-warning" : "text-muted"}" style="margin-top:4px">${frappe.utils.escape_html(day.note)}</div>`
				: "";

			return `
				<tr>
					<td style="white-space:nowrap"><b>${day.day}</b></td>
					<td style="white-space:nowrap">${hours}</td>
					<td style="text-align:center">${day.slot_count}</td>
					<td>${chips || `<span class="text-muted">No slots</span>`}${note}</td>
				</tr>`;
		})
		.join("");

	return `
		<style>
			.jarz-slot-chip {
				display:inline-block; margin:2px 4px 2px 0; padding:2px 8px;
				border:1px solid var(--border-color); border-radius:10px;
				font-size:11px; white-space:nowrap; background:var(--control-bg);
			}
			.jarz-slot-preview td { vertical-align:top; padding:6px 8px; }
		</style>
		<div style="margin-bottom:8px">${header_bits.join(" &nbsp;•&nbsp; ")}</div>
		<table class="table table-bordered jarz-slot-preview" style="margin-bottom:0">
			<thead>
				<tr>
					<th>Day</th><th>Hours</th><th style="text-align:center">Slots</th><th>Delivery slots</th>
				</tr>
			</thead>
			<tbody>${rows}</tbody>
		</table>
		<div class="text-muted" style="margin-top:6px; font-size:11px">
			Preview of the full day. The POS hides slots already in the past and
			applies a 30-minute preparation buffer for today.
		</div>`;
}

const refresh_preview = frappe.utils.debounce
	? frappe.utils.debounce(render_slot_preview, 300)
	: render_slot_preview;

frappe.ui.form.on("POS Profile Timetable", {
	refresh: render_slot_preview,
	slot_hours: refresh_preview,
	slot_minutes: refresh_preview,
	has_custom_last_slot: refresh_preview,
	last_slot_hours: refresh_preview,
	last_slot_minutes: refresh_preview,
	anchor_last_slot_to_closing: refresh_preview,
});

frappe.ui.form.on("POS Profile Day Timing", {
	day: refresh_preview,
	opening_time: refresh_preview,
	closing_time: refresh_preview,
	same_day: refresh_preview,
	timetable_add: refresh_preview,
	timetable_remove: refresh_preview,
});
