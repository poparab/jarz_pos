frappe.provide('jarz_pos.kanban.cards');

// Inject CSS once for status color coding
(function(){
	if(!document.getElementById('kanban-status-style')){
		const css=`
		.kanban-card.status-paid{border-left:4px solid #28a745;}
		.kanban-card.status-unpaid{border-left:4px solid #ffc107;}
		.kanban-card.status-overdue{border-left:4px solid #ff5722;}
		.kanban-card.status-cancelled{border-left:4px solid #dc3545;opacity:0.6;}
		.kanban-card.status-return{border-left:4px solid #6f42c1;}
		.kanban-card.status-draft{border-left:4px solid #6c757d;}
		`;
		const style=document.createElement('style');
		style.id='kanban-status-style';
		style.innerHTML=css;
		document.head.appendChild(style);
	}
})();

// ---------------------------------------------------------------------------
// 📦 Bundle Parent Item Detection
// ---------------------------------------------------------------------------

// We need to exclude *parent* bundle items (container rows) from the item-count
// that appears in the expanded panel. All parent bundle items are referenced in
// the `Jarz Bundle` DocType via the `erpnext_item` field. We fetch that list
// ONCE and cache it for the browser session.

if (!jarz_pos.kanban.bundleParentCodesPromise) {
	jarz_pos.kanban.bundleParentCodes = [];
	jarz_pos.kanban.bundleParentCodesPromise = frappe.call({
		method: 'frappe.client.get_list',
		args: {
			doctype: 'Jarz Bundle',
			fields: ['erpnext_item'],
			limit_page_length: 1000
		}
	}).then(function(r) {
		var codes = (r.message || []).map(function(row) { return row.erpnext_item; }).filter(Boolean);
		jarz_pos.kanban.bundleParentCodes = codes;
		return codes;
	}).catch(function() {
		// Silent fail – leave array empty
		return [];
	});
}

// Map ERPNext invoice status to CSS class
jarz_pos.kanban.cards.getStatusClass=function(erpStatus){
	var map={
		'Paid':'status-paid',
		'Unpaid':'status-unpaid',
		'Submitted':'status-unpaid',
		'Overdue':'status-overdue',
		'Cancelled':'status-cancelled',
		'Return':'status-return',
		'Draft':'status-draft'
	};
	return map[erpStatus]||'';
};

jarz_pos.kanban.cards.applyStatusClass=function($card, erpStatus){
	var all=['status-paid','status-unpaid','status-overdue','status-cancelled','status-return','status-draft'];
	$card.removeClass(all.join(' '));
	var cls=jarz_pos.kanban.cards.getStatusClass(erpStatus);
	if(cls){$card.addClass(cls);}
};

jarz_pos.kanban.cards.loadInvoiceDetails = function(invoice) {
	return new Promise(function(resolve) {
		// Ensure bundle parent codes are ready first
		jarz_pos.kanban.bundleParentCodesPromise.then(function(parentCodes) {
			// Load full Sales Invoice document (includes child tables)
			return frappe.call({
				method: 'frappe.client.get',
				args: {
					doctype: 'Sales Invoice',
					name: invoice.name
				}
			}).then(function(res) {
				// res handling will use parentCodes below
				return { res: res, parentCodes: parentCodes };
			});
		}).then(function(payload){
			var res = payload.res;
			var parentCodes = payload.parentCodes || [];
			var doc = res.message || {};
			var items = doc.items || [];
			var taxes = doc.taxes || [];

			// Preserve original ERP status and prefer operational state for column grouping
			invoice.erp_status = doc.status || invoice.erp_status || '';
			invoice.status = doc.sales_invoice_state || doc.status || invoice.status;
			if (!invoice.status) {
				if (doc.docstatus === 0) invoice.status = 'Draft';
				else if (doc.docstatus === 1) {
					invoice.status = (doc.outstanding_amount === 0) ? 'Paid' : 'Submitted';
				} else if (doc.docstatus === 2) {
					invoice.status = 'Cancelled';
				} else {
					invoice.status = 'Unknown';
				}
			}
			invoice.posting_date = doc.posting_date || invoice.posting_date;
			invoice.posting_time = doc.posting_time || invoice.posting_time;

			// Count items excluding delivery/freight **and parent bundle items**
			var actualItems = items.filter(function(it) {
				if (!it.item_name) return false;
				var lowerName = it.item_name.toLowerCase();
				var isDelivery = lowerName.includes('delivery') || lowerName.includes('freight');
				var isParentBundle = parentCodes.includes(it.item_code);
				return !isDelivery && !isParentBundle;
			});
			// Sum quantities so multiple-qty rows are counted properly
			var totalQty = 0;
			actualItems.forEach(function(it){
				var q = parseFloat(it.qty || 0);
				if(!isNaN(q)) totalQty += q;
			});
			invoice.itemCount = totalQty;

			// Delivery expense from taxes table (negative amount)
			var deliveryExpense = 0;
			taxes.forEach(function(tx) {
				if (tx.description && tx.description.toLowerCase().includes('delivery') && tx.tax_amount < 0) {
					deliveryExpense += Math.abs(tx.tax_amount);
				}
			});
			invoice.deliveryExpense = deliveryExpense;

			// Get customer address (attempt, but not critical)
			frappe.call({
				method: 'frappe.client.get_list',
				args: {
					doctype: 'Address',
					fields: ['address_line1', 'city'],
					filters: {
						address_title: invoice.customer_name
					},
					limit: 1
				}
			}).then(function(addrRes) {
				var addresses = addrRes.message || [];
				if (addresses.length) {
					invoice.address = addresses[0];
					var cityId = invoice.address.city;
					if (cityId) {
						frappe.call({
							method: 'frappe.client.get',
							args: { doctype: 'City', name: cityId }
						}).then(function(cityRes) {
							if (cityRes.message) {
								invoice.address.city_name = cityRes.message.city_name || cityRes.message.name;
							} else {
								invoice.address.city_name = cityId;
							}
							// Update status color
							var $cardEl = $('#card-' + invoice.name);
							jarz_pos.kanban.cards.applyStatusClass($cardEl, invoice.erp_status || invoice.status);
							resolve(invoice);
						}).catch(function() {
							invoice.address.city_name = cityId;
							resolve(invoice);
						});
					} else {
						invoice.address.city_name = 'N/A';
						resolve(invoice);
					}
				} else {
					invoice.address = { city: 'N/A', city_name: 'N/A', address_line1: '' };
					resolve(invoice);
				}
			}).catch(function() {
				invoice.address = { city: 'N/A', city_name: 'N/A', address_line1: '' };
				resolve(invoice);
			});
		}).catch(function(err) {
			console.error('Error fetching invoice document:', err);
			// Fallback minimal info
			invoice.itemCount = 0;
			invoice.deliveryExpense = 0;
			invoice.address = { city: 'N/A', address_line1: '' };
			resolve(invoice);
		});
	});
}

jarz_pos.kanban.cards.createInvoiceCard = function(invoice) {
	var address = invoice.address;
	var cityName = address ? (address.city_name || address.city || 'N/A') : 'N/A';
	var addressLine = address ? (address.address_line1 || '') : '';

	// Determine display status replacing 'Overdue' with 'Unpaid' for user-facing text
	var erpStatusForDisplay = invoice.erp_status || invoice.status || '';
	var displayStatus = (erpStatusForDisplay === 'Overdue') ? 'Unpaid' : erpStatusForDisplay;

	var cardId = `card-${invoice.name}`;
	var isDemoData = invoice.name.startsWith('DEMO-');

	// Determine css class for status colouring
	var erpStatus = invoice.erp_status || invoice.status || '';
	var statusCls = jarz_pos.kanban.cards.getStatusClass(erpStatus);

	return `
		<div class="kanban-card ${statusCls}" data-invoice-id="${invoice.name}" data-expanded="false" id="${cardId}">
			<div class="kanban-card-header">
				<div>
					<div class="kanban-card-title">${invoice.customer_name}${isDemoData ? ' <small>(Demo)</small>' : ''}</div>
					<div class="kanban-card-info kanban-card-city">${cityName}</div>
					<div class="kanban-card-info kanban-card-status-text"><small>${displayStatus}</small></div>
				</div>
				<div class="kanban-card-amount">$${(invoice.grand_total || invoice.total || 0).toFixed(2)}</div>
			</div>

			<div class="kanban-card-expanded" style="display: none;">
				<div class="kanban-card-info"><strong>Invoice:</strong> ${invoice.name}</div>
				<div class="kanban-card-info"><strong>Date:</strong> ${frappe.datetime.str_to_user(invoice.posting_date)} ${invoice.posting_time || ''}</div>
				<div class="kanban-card-info"><strong>Address:</strong> ${addressLine}</div>
				<div class="kanban-card-info"><strong>Items:</strong> ${invoice.itemCount || 0}</div>
				<!-- Status omitted in expanded view (column already represents state) -->
				${invoice.deliveryExpense > 0 ? `<div class="kanban-card-info"><strong>Delivery Expense:</strong> $${invoice.deliveryExpense.toFixed(2)}</div>` : ''}

				<div class="kanban-card-actions">
					${isDemoData ?
						`<small class="text-muted" style="font-style: italic;">Print and View disabled for demo data</small>` :
						`<button class="btn btn-sm btn-primary print-invoice-btn" data-invoice="${invoice.name}">
							<i class="fa fa-print"></i> Print
						</button>
						<button class="btn btn-sm btn-outline-secondary view-invoice-btn" data-invoice="${invoice.name}">
							<i class="fa fa-eye"></i> View
						</button>
						${ (['Received','Preparing'].includes(invoice.status) && ['Paid'].indexOf(erpStatusForDisplay)===-1 ) ?
							`<button class="btn btn-sm btn-success mark-paid-btn" data-invoice="${invoice.name}">
								<i class="fa fa-check"></i> Mark Paid
							</button>` : '' }
						`
					}
				</div>
			</div>
		</div>
	`;
}

jarz_pos.kanban.cards.addKanbanCardEventHandlers = function() {
	// Toggle card expansion
	$('.kanban-card').off('click').on('click', function(e) {
		if ($(e.target).closest('button').length > 0) {
			return;
		}

		var $card = $(this);
		var invoiceId = $card.data('invoice-id');
		var $expanded = $card.find('.kanban-card-expanded');
		var isExpanded = $card.data('expanded') === 'true';

		// Lazy-load details once
		if (!isExpanded && !$card.data('detailsLoaded')) {
			jarz_pos.kanban.cards.loadInvoiceDetails({ name: invoiceId }).then(function(det) {
				// Update collapsed city line
				var addr = det.address || { city: 'N/A', city_name:'N/A', address_line1: '' };
				$card.find('.kanban-card-city').text(addr.city_name || addr.city || 'N/A');
				// Update collapsed status text with Overdue → Unpaid logic
				var _st = det.erp_status || det.status || '';
				$card.find('.kanban-card-status-text').text((_st==='Overdue')?'Unpaid':_st);

				// Build expanded HTML with full data
				var expHtml = `
					<div class="kanban-card-info"><strong>Invoice:</strong> ${det.name}</div>
					<div class="kanban-card-info"><strong>Date:</strong> ${frappe.datetime.str_to_user(det.posting_date)} ${det.posting_time || ''}</div>
					<div class="kanban-card-info"><strong>Address:</strong> ${addr.address_line1}</div>
					<div class="kanban-card-info"><strong>Items:</strong> ${det.itemCount || 0}</div>
					<!-- Status omitted in expanded view (column already represents state) -->
					${(det.deliveryExpense > 0) ? `<div class="kanban-card-info"><strong>Delivery Expense:</strong> $${det.deliveryExpense.toFixed(2)}</div>` : ''}
					<div class="kanban-card-actions">
						<button class="btn btn-sm btn-primary print-invoice-btn" data-invoice="${det.name}"><i class="fa fa-print"></i> Print</button>
						<button class="btn btn-sm btn-outline-secondary view-invoice-btn" data-invoice="${det.name}"><i class="fa fa-eye"></i> View</button>
						${ (['Received','Preparing'].includes(det.status) && det.erp_status !== 'Paid') ?
							`<button class="btn btn-sm btn-success mark-paid-btn" data-invoice="${det.name}"><i class="fa fa-check"></i> Mark Paid</button>` : '' }
					</div>`;

				$expanded.html(expHtml);

				// Re-attach print / view buttons within this expanded block
				$expanded.find('.print-invoice-btn').on('click', function(ev) { ev.stopPropagation(); jarz_pos.kanban.cards.printInvoice(det.name); });
				$expanded.find('.view-invoice-btn').on('click', function(ev) { ev.stopPropagation(); frappe.set_route('Form', 'Sales Invoice', det.name); });
				$expanded.find('.mark-paid-btn').on('click', function(ev){ ev.stopPropagation();
					frappe.prompt({
						fieldname:'payment_mode', fieldtype:'Select', label:'Payment Mode', reqd:1,
						options:['Instapay','Payment Gateway','Mobile Wallet']
					}, function(vals){
						frappe.call({
							method:'jarz_pos.jarz_pos.page.custom_pos.custom_pos.pay_invoice',
							args:{invoice_name:det.name, payment_mode:vals.payment_mode},
							freeze:true,
							callback:function(){
								frappe.show_alert({message: __('Payment recorded'), indicator:'green'});
								var $c=$('#card-'+det.name);
								jarz_pos.kanban.cards.applyStatusClass($c,'Paid');
								$c.find('.kanban-card-status-text').text('Paid');
								$c.find('.mark-paid-btn').remove();
							}
						});
					}, __('Mark Invoice Paid'), __('Confirm'));
				});

				$card.data('detailsLoaded', true);
			});
		}

		// Toggle expansion
		if (isExpanded) {
			$expanded.slideUp(200);
			$card.data('expanded', 'false');
		} else {
			$expanded.slideDown(200);
			$card.data('expanded', 'true');
		}
	});

	// Print invoice button
	$('.print-invoice-btn').off('click').on('click', function(e) {
		e.stopPropagation();
		var invoiceName = $(this).data('invoice');
		jarz_pos.kanban.cards.printInvoice(invoiceName);
	});

	// View invoice button
	$('.view-invoice-btn').off('click').on('click', function(e) {
		e.stopPropagation();
		var invoiceName = $(this).data('invoice');
		frappe.set_route('Form', 'Sales Invoice', invoiceName);
	});

	// Mark Paid button
	$('.mark-paid-btn').off('click').on('click', function(e){
		e.stopPropagation();
		var invoiceName = $(this).data('invoice');

		frappe.prompt({
			fieldname: 'payment_mode',
			fieldtype: 'Select',
			label: 'Payment Mode',
			reqd: 1,
			options: ['Instapay','Payment Gateway','Mobile Wallet']
		}, function(values){
			frappe.call({
				method: 'jarz_pos.jarz_pos.page.custom_pos.custom_pos.pay_invoice',
				args: { invoice_name: invoiceName, payment_mode: values.payment_mode },
				freeze: true,
				callback: function(r){
					frappe.show_alert({message: __('Payment recorded'), indicator:'green'});
					// Update card visuals instantly
					var $card = $('#card-'+invoiceName);
					jarz_pos.kanban.cards.applyStatusClass($card,'Paid');
					$card.find('.kanban-card-status-text').text('Paid');
					$card.find('.mark-paid-btn').remove();
				}
			});
		}, __('Mark Invoice Paid'), __('Confirm'));
	});

	// Column delete button
	$('.column-delete-btn').off('click').on('click', function(e) {
		e.stopPropagation();
		var $column = $(this).closest('.kanban-column');
		var columnId = $column.data('column-id');
		jarz_pos.kanban.columns.deleteCustomColumn(columnId);
	});
}

jarz_pos.kanban.cards.printInvoice = function(invoiceName) {
	console.log("Printing invoice:", invoiceName);

	// Use ERPNext's print functionality
	frappe.call({
		method: 'frappe.utils.print_format.download_pdf',
		args: {
			doctype: 'Sales Invoice',
			name: invoiceName,
			format: 'Standard',
			no_letterhead: 0
		},
		callback: function(r) {
			if (r.message) {
				// Open print dialog
				var printWindow = window.open(r.message.pdf_link, '_blank');
				if (printWindow) {
					printWindow.onload = function() {
						printWindow.print();
					};
				}
			}
		},
		error: function(err) {
			console.error("Error printing invoice:", err);
			// Fallback: open the invoice form for manual printing
			frappe.set_route('Form', 'Sales Invoice', invoiceName);
			setTimeout(function() {
				if (window.cur_frm) {
					window.cur_frm.print_doc();
				}
			}, 1000);
		}
	});
}
