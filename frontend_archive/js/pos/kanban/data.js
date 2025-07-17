frappe.provide('jarz_pos.kanban.data');

jarz_pos.kanban.data.loadOrdersData = function() {
	console.log("Loading orders data...");

	var profile = window.currentPOSProfile;
	if (!profile) {
		console.error("No POS profile available");
		return;
	}

	// Try to load sales invoices for this POS profile
	frappe.call({
		method: 'frappe.client.get_list',
		args: {
			doctype: 'Sales Invoice',
	fields: [
				'name', 'customer', 'customer_name', 'total', 'grand_total',
				'status', 'sales_invoice_state', 'posting_date', 'posting_time', 'is_pos',
				'outstanding_amount', 'paid_amount'
			],
			filters: {
				is_pos: 1,
				pos_profile: profile.name,
				status: ['!=', 'Cancelled'], // Exclude cancelled for performance
				posting_date: ['>=', frappe.datetime.add_days(frappe.datetime.nowdate(), -7)] // Last 7 days
			},
			order_by: 'creation desc',
			limit: 100
		},
		callback: function(r) {
			if (r.message) {
				var invoices = r.message;
				console.log("Loaded", invoices.length, "sales invoices");

				// ------------------------------------------------------------
				// Prefetch *unsettled* Courier Transactions for these invoices
				// ------------------------------------------------------------
				var invIds = invoices.map(function(inv){ return inv.name; });
				frappe.call({
					method:'frappe.client.get_list',
					args:{
						doctype:'Courier Transaction',
						fields:['reference_invoice','amount','shipping_amount'],
						filters:{
							status:['!=','Settled'],
							reference_invoice:['in',invIds]
						},
						limit:1000
					}
				}).then(function(ctRes){
					var balMap={};
					(ctRes.message||[]).forEach(function(ct){
						var ref=ct.reference_invoice;
						var bal=parseFloat(ct.amount||0)-parseFloat(ct.shipping_amount||0);
						balMap[ref]=(balMap[ref]||0)+bal;
					});
					invoices.forEach(function(inv){
						if(balMap.hasOwnProperty(inv.name)){
							inv.courier_outstanding_balance=balMap[inv.name];
						}
					});
					jarz_pos.kanban.data.processOrdersData(invoices);
				}).catch(function(){
					jarz_pos.kanban.data.processOrdersData(invoices);
				});
			} else {
				console.log("No sales invoices found or no access");
				jarz_pos.kanban.data.showNoDataMessage();
			}
		},
		error: function(err) {
			console.error("Error loading orders, showing demo data:", err);
			jarz_pos.kanban.data.showPermissionError();
		}
	});
}

jarz_pos.kanban.data.showPermissionError = function() {
	$('.kanban-column-body').empty();
	$('#column-draft').html(`
		<div style="padding: 20px; text-align: center; color: #666;">
			<i class="fa fa-lock" style="font-size: 32px; margin-bottom: 10px; opacity: 0.5;"></i>
			<h5>Permission Required</h5>
			<p>You need additional permissions to view Sales Invoice data.</p>
			<p>The kanban board will work once you have the proper permissions.</p>
			<small>Contact your administrator to grant Sales Invoice read permissions.</small>
			<br><br>
			<button class="btn btn-sm btn-primary" onclick="jarz_pos.kanban.data.loadDemoData()">
				<i class="fa fa-eye"></i> Preview with Demo Data
			</button>
		</div>
	`);
}

window.loadDemoData = function() { // Keep on window for now as it is called from an onclick attribute
	console.log("Loading demo data for kanban preview");

	var demoInvoices = [
		{
			name: 'DEMO-INV-001',
			customer_name: 'Ahmed Hassan',
			total: 150.00,
			grand_total: 165.00,
			status: 'Draft',
			posting_date: frappe.datetime.nowdate(),
			posting_time: '10:30:00',
			itemCount: 3,
			address: { city: 'Cairo', address_line1: '123 Main St' },
			deliveryExpense: 15.00
		},
		{
			name: 'DEMO-INV-002',
			customer_name: 'Fatima Al-Zahra',
			total: 320.00,
			grand_total: 345.00,
			status: 'Submitted',
			posting_date: frappe.datetime.nowdate(),
			posting_time: '11:15:00',
			itemCount: 5,
			address: { city: 'Alexandria', address_line1: '456 River Road' },
			deliveryExpense: 25.00
		},
		{
			name: 'DEMO-INV-003',
			customer_name: 'Omar Mahmoud',
			total: 89.50,
			grand_total: 89.50,
			status: 'Paid',
			posting_date: frappe.datetime.nowdate(),
			posting_time: '12:45:00',
			itemCount: 2,
			address: { city: 'Giza', address_line1: '789 Desert Ave' },
			deliveryExpense: 0
		},
		{
			name: 'DEMO-INV-004',
			customer_name: 'Layla Abdel Rahman',
			total: 275.00,
			grand_total: 295.00,
			status: 'Submitted',
			posting_date: frappe.datetime.add_days(frappe.datetime.nowdate(), -1),
			posting_time: '14:20:00',
			itemCount: 4,
			address: { city: 'Cairo', address_line1: '321 Garden St' },
			deliveryExpense: 20.00
		}
	];

	// Process demo data through the same pipeline
	jarz_pos.kanban.data.processOrdersData(demoInvoices);

	// Add demo notice
	setTimeout(function() {
		$('.kanban-board').prepend(`
			<div class="alert alert-warning" style="margin-bottom: 15px; font-size: 12px;">
				<i class="fa fa-info-circle"></i> <strong>Demo Mode:</strong> This is sample data to demonstrate the kanban board functionality.
				Real data will appear here once you have the proper permissions.
			</div>
		`);
	}, 500);
}

jarz_pos.kanban.data.showNoDataMessage = function() {
	$('.kanban-column-body').empty();
	$('#column-draft').html(`
		<div style="padding: 20px; text-align: center; color: #666;">
			<i class="fa fa-inbox" style="font-size: 32px; margin-bottom: 10px; opacity: 0.5;"></i>
			<h5>No Recent Orders</h5>
			<p>No POS invoices found for the last 7 days.</p>
			<p>Create some sales through the POS to see them here.</p>
		</div>
	`);
}

jarz_pos.kanban.data.processOrdersData = function(invoices){
  // Clear columns
  $('.kanban-column-body').empty();

  // STEP 1: load column assignments
  jarz_pos.kanban.data.loadCustomColumnAssignments().then(function(customAssignments){

    // STEP 2: Prefetch addresses & city names in BULK so cards can display immediately
    var customerTitles = invoices.map(function(inv){ return inv.customer_name; }).filter(Boolean);
    var uniqueCust = Array.from(new Set(customerTitles));

    frappe.call({
      method:'frappe.client.get_list',
      args:{
        doctype:'Address',
        fields:['address_title','city','address_line1'],
        filters:[['address_title','in',uniqueCust]],
        limit:500
      }
    }).then(function(addrRes){
      var addrMap={};
      (addrRes.message||[]).forEach(function(a){ addrMap[a.address_title]=a; });

      var cityIds = Array.from(new Set((addrRes.message||[]).map(function(a){ return a.city; }).filter(Boolean)));

      if(cityIds.length===0){ return {addrMap:addrMap, cityMap:{}}; }

      return frappe.call({
        method:'frappe.client.get_list',
        args:{ doctype:'City', fields:['name','city_name'], filters:[['name','in',cityIds]], limit:500 }
      }).then(function(cityRes){
        var cityMap={};
        (cityRes.message||[]).forEach(function(c){ cityMap[c.name]=c.city_name||c.name; });
        return {addrMap:addrMap, cityMap:cityMap};
      });
    }).then(function(prefetch){
      prefetch = prefetch || {addrMap:{}, cityMap:{}};

      // STEP 3: create cards with immediate city
    invoices.forEach(function(inv){
      var colId=jarz_pos.kanban.data.getInvoiceColumnId(inv,customAssignments);
      var $body=$('#column-'+colId);

        // attach simple address info if available
        var addr = prefetch.addrMap[inv.customer_name];
        if(addr){
          var cityName = prefetch.cityMap[addr.city] || addr.city || 'N/A';
          inv.address = {city:addr.city||'N/A', city_name:cityName, address_line1:addr.address_line1||''};
        }

      if($body.length){
        var isCourierOutstanding = (colId==='out_for_delivery' && (inv.outstanding_amount>0));
        var cardHtml = jarz_pos.kanban.cards.createInvoiceCard(inv);
        if(isCourierOutstanding){
            $body.prepend(cardHtml);
        } else {
            $body.append(cardHtml);
        }

          // Background detailed load (taxes, item count, etc.)
          jarz_pos.kanban.cards.loadInvoiceDetails(inv).then(function(det){
            var cityLabel = det.address ? (det.address.city_name || det.address.city || 'N/A') : 'N/A';
            var cardSelector = `#card-${det.name} .kanban-card-city`;
            $(cardSelector).text(cityLabel);
          });
      }
    });

      // Finally attach handlers
    jarz_pos.kanban.cards.addKanbanCardEventHandlers();
    });
  });
}

jarz_pos.kanban.data.getInvoiceColumnId = function(invoice, customAssignments) {
	// Preserve original ERPNext status once
	if (!invoice.erp_status) {
		invoice.erp_status = invoice.status; // may be undefined for demo data but fine
	}

	// Custom assignment overrides everything
	if (customAssignments[invoice.name]) {
		return customAssignments[invoice.name];
	}

	// Operational state (custom field) decides standard column; fallback to ERP status
	var stateVal = invoice.sales_invoice_state || invoice.status;

	// For UI display we overwrite invoice.status to operational state, keep ERP status separate
	invoice.status = stateVal;

	var stateMap = {
		'Received': 'received',
		'Processing': 'processing',
		'Preparing': 'preparing',
		'Out for delivery': 'out_for_delivery',
		'Completed': 'completed'
	};

	return stateMap[stateVal] || 'received';
}

jarz_pos.kanban.data.loadCustomColumnAssignments = function() {
	return new Promise(function(resolve) {
		// Try localStorage first
		var localAssignments = localStorage.getItem('kanban_column_assignments');
		if (localAssignments) {
			try {
				var assignments = JSON.parse(localAssignments);
				console.log("Loaded column assignments from localStorage");
				resolve(assignments);
				return;
			} catch (e) {
				console.error("Error parsing localStorage assignments:", e);
			}
		}

		// Fallback to Custom Settings
		frappe.call({
			method: 'frappe.client.get_list',
			args: {
				doctype: 'Custom Settings',
				fields: ['document_name', 'value'],
				filters: {
					doctype_name: 'Sales Invoice',
					setting_name: 'kanban_column'
				},
				limit: 1000
			},
			callback: function(r) {
				var assignments = {};
				if (r.message) {
					r.message.forEach(function(setting) {
						assignments[setting.document_name] = setting.value;
					});
					// Save to localStorage for future use
					localStorage.setItem('kanban_column_assignments', JSON.stringify(assignments));
				}
				resolve(assignments);
			},
			error: function(err) {
				console.error("Error loading custom assignments, using empty:", err);
				resolve({});
			}
		});
	});
}

jarz_pos.kanban.data.updateInvoiceStatus = function(invoiceId, newColumnId, newStatus) {
	console.log("Moving invoice", invoiceId, "to column", newColumnId, "with status", newStatus);

	// Update localStorage immediately
	var assignments = {};
	try {
		var stored = localStorage.getItem('kanban_column_assignments');
		if (stored) {
			assignments = JSON.parse(stored);
		}
	} catch (e) {
		console.error("Error reading localStorage assignments:", e);
	}

	assignments[invoiceId] = newColumnId;
	localStorage.setItem('kanban_column_assignments', JSON.stringify(assignments));
	console.log("Invoice column assignment updated in localStorage");

	// Try to update Custom Settings (optional)
	  frappe.call({
		method: 'frappe.client.insert',
		args: {
			doc: {
				doctype: 'Custom Settings',
				doctype_name: 'Sales Invoice',
				document_name: invoiceId,
				setting_name: 'kanban_column',
				value: newColumnId
			}
		},
		callback: function(r) {
			console.log("Invoice column assignment updated in database");
		},
		error: function(err) {
			console.log("Could not save to database, using localStorage only:", err.message);
			// If insert fails, try to update existing record
			frappe.call({
				method: 'frappe.client.get_list',
				args: {
					doctype: 'Custom Settings',
					fields: ['name'],
					filters: {
						doctype_name: 'Sales Invoice',
						document_name: invoiceId,
						setting_name: 'kanban_column'
					},
					limit: 1
				},
				callback: function(r) {
					if (r.message && r.message.length > 0) {
						frappe.call({
							method: 'frappe.client.set_value',
							args: {
								doctype: 'Custom Settings',
								name: r.message[0].name,
								fieldname: 'value',
								value: newColumnId
							}
						});
					}
				},
				error: function(err2) {
					console.log("Database update failed, localStorage will be used:", err2.message);
				}
			});
		}
	});

	// Update ERPNext *operational state* field when dragged between standard columns
	if (newStatus && newStatus !== '') {
		frappe.call({
			method: 'frappe.client.set_value',
			args: {
				doctype: 'Sales Invoice',
				name: invoiceId,
				fieldname: 'sales_invoice_state',
				value: newStatus
			},
			callback: function(r) {
				console.log("Invoice operational state updated to:", newStatus);
			},
			error: function(err) {
				console.error("Error updating invoice operational state:", err);
			}
		});
	}
} 

// ────────────────────────────────────────────────────────────────
// Realtime helper: add a single invoice card (used by WebSocket)
// ────────────────────────────────────────────────────────────────
jarz_pos.kanban.data.addInvoiceCard = function(inv) {
	// Map to operational state first
	inv.status = inv.sales_invoice_state || inv.status;

	// Ignore cancelled invoices entirely (still rely on ERPNext status field)
	if (inv.status === 'Cancelled') {
		return;
	}
	// Prevent duplicates
	if ($('#card-' + inv.name).length) {
		return;
	}
	var colId = jarz_pos.kanban.data.getInvoiceColumnId(inv, {});
	var $body = $('#column-' + colId);
	if (!$body.length) {
		console.warn('Column not found for realtime invoice', colId);
		return;
	}

	var isCourierOutstanding = (colId==='out_for_delivery' && (inv.outstanding_amount>0));
	var cardHtml = jarz_pos.kanban.cards.createInvoiceCard(inv);
	if(isCourierOutstanding){
		$body.prepend(cardHtml);
	} else {
		$body.append(cardHtml);
	}

	// Load additional details (taxes / address) asynchronously
	jarz_pos.kanban.cards.loadInvoiceDetails(inv).then(function(det) {
		var cityLabel = det.address ? (det.address.city_name || det.address.city || 'N/A') : 'N/A';
		$('#card-' + det.name + ' .kanban-card-city').text(cityLabel);
	});

	jarz_pos.kanban.cards.addKanbanCardEventHandlers();
}; 