frappe.provide('jarz_pos.kanban.columns');

// Ensure minimal drop zone area for Out for Delivery sub-lists
(function(){
    if(!document.getElementById('ofd-subbody-style')){
        const style=document.createElement('style');
        style.id='ofd-subbody-style';
        style.innerHTML=`
            .kanban-column-subbody{min-height:60px;padding-bottom:2px;}
        `;
        document.head.appendChild(style);
    }
})();

jarz_pos.kanban.columns.ensureStandardColumns = function(config) {
	// Default operational state columns (uses custom field `sales_invoice_state`)
	var standardColumns = [
		{ id:'received',title:'Received',status:'Received',isCustom:false,order:0,hidden:false },
		{ id:'processing',title:'Processing',status:'Processing',isCustom:false,order:1,hidden:false },
		{ id:'preparing',title:'Preparing',status:'Preparing',isCustom:false,order:2,hidden:false },
		{ id:'out_for_delivery',title:'Out for Delivery',status:'Out for delivery',isCustom:false,order:3,hidden:false },
		{ id:'completed',title:'Completed',status:'Completed',isCustom:false,order:4,hidden:false }
	];

	// Remove any obsolete legacy status columns from previous versions
	var obsoleteIds = ['draft','submitted','paid','overdue','unpaid','return','cancelled','credit_note'];
	config.columns = config.columns.filter(function(c){ return !obsoleteIds.includes(c.id); });

	standardColumns.forEach(function(standardCol) {
		var found = config.columns.find(function(c) { return c.id === standardCol.id; });
		if (!found) {
			config.columns.push(standardCol);
		}
	});

	return config;
}

// ---------------------------------------------------------------------------
// 📦 Helper: unified move handler for Out for Delivery unpaid cards
// ---------------------------------------------------------------------------

jarz_pos.kanban.columns.handleOutForDeliveryMove = function(invoiceId, cardElement, newColumnId, newStatus, oldColumnId){
    var $card = $('#card-' + invoiceId);

    var finalized = false; // becomes true once Cash or Courier flow completes

    function revertCard(){
        if(finalized) return;
        var $origCol = $('#column-' + oldColumnId);
        if($origCol.length){ $origCol.prepend($card); }
    }

    // Safety: if already paid, just update assignment
    if($card.hasClass('status-paid')){
        // ------------------------------------------------------------------
        // NEW FLOW (July 2025): already-paid invoice – handle delivery expense
        // ------------------------------------------------------------------

        var finalizedPaid = false;

        function revertPaid(){
            if(finalizedPaid) return;
            var $origCol = $('#column-' + oldColumnId);
            if($origCol.length){ $origCol.prepend($card); }
        }

        var dlgPaid = new frappe.ui.Dialog({
            title: __('Courier Delivery Expense'),
            fields:[{fieldtype:'HTML', fieldname:'options_html'}],
            size:'small'
        });

        dlgPaid.onhide = revertPaid;

        var paidHtml = `
            <div style="display:flex;gap:24px;justify-content:center;padding:12px;">
                <div class="pay-expense-option" data-choice="cash" style="cursor:pointer;border:2px solid #28a745;border-radius:8px;padding:16px;width:160px;text-align:center;">
                    <i class="fa fa-money-bill" style="font-size:32px;color:#28a745;"></i><br/><strong>`+__('Pay Cash')+`</strong>
                </div>
                <div class="pay-expense-option" data-choice="later" style="cursor:pointer;border:2px solid #6c757d;border-radius:8px;padding:16px;width:160px;text-align:center;">
                    <i class="fa fa-truck" style="font-size:32px;color:#6c757d;"></i><br/><strong>`+__('Pay Later')+`</strong>
                </div>
            </div>`;

        dlgPaid.fields_dict.options_html.$wrapper.html(paidHtml);

        dlgPaid.$wrapper.find('.pay-expense-option').on('click', function(){
            var choice = $(this).data('choice');

            // Mark as finalized BEFORE closing dialog to avoid revertPaid firing
            finalizedPaid = true;

            // Temporarily disable onhide to skip revert logic once action chosen
            dlgPaid.onhide = function(){};

            dlgPaid.hide();

            if(choice==='cash'){
                var profileName = (window.currentPOSProfile ? window.currentPOSProfile.name : '');
                frappe.call({
                    method:'jarz_pos.jarz_pos.page.custom_pos.custom_pos.pay_delivery_expense',
                    args:{invoice_name:invoiceId, pos_profile: profileName},
                    freeze:true,
                    callback:function(r){
                        var amt = r.message ? r.message.amount : 0;
                        var amtLabel = (typeof frappe.format_value === 'function') ? frappe.format_value(amt, { fieldtype: 'Currency' }) : (amt || 0).toFixed(2);
                        frappe.msgprint({
                            title: __('Pay Courier'),
                            message: __('Please pay <strong>{0}</strong> to the courier.', [amtLabel]),
                            indicator:'green'
                        });
                        // Move card and update status (already paid)
                        $('#column-out_for_delivery').prepend($card);
                        jarz_pos.kanban.data.updateInvoiceStatus(invoiceId, newColumnId, newStatus);
                    },
                    error:function(){ revertPaid(); }
                });
            } else if(choice==='later'){
                // Touch-friendly selector using card UI
                jarz_pos.couriers.openSelector(function(courierId){
                    if(!courierId){ finalizedPaid=false; revertPaid(); return; }
                    frappe.call({
                        method:'jarz_pos.jarz_pos.page.custom_pos.custom_pos.courier_delivery_expense_only',
                        args:{invoice_name:invoiceId, courier: courierId},
                        freeze:true,
                        callback:function(){
                            frappe.msgprint(__('Courier transaction recorded.'));
                            $('#column-out_for_delivery').prepend($card);
                            jarz_pos.kanban.data.updateInvoiceStatus(invoiceId, newColumnId, newStatus);
                        },
                        error:function(){ revertPaid(); }
                    });
                });
            } else {
                revertPaid();
            }
        });

        dlgPaid.show();

        return; // Prevent default flow
    }

    // Build selection dialog (Cash or Courier)
    var dlg = new frappe.ui.Dialog({
        title: __('Select Collection Method'),
        fields:[{fieldtype:'HTML', fieldname:'options_html'}],
        size:'small'
    });

    dlg.onhide = revertCard;

    var html = `
        <div style="display:flex;gap:24px;justify-content:center;padding:12px;">
            <div class="pay-card-option" data-choice="Cash" style="cursor:pointer;border:2px solid #28a745;border-radius:8px;padding:16px;width:140px;text-align:center;">
                <i class="fa fa-money-bill" style="font-size:32px;color:#28a745;"></i><br/><strong>Cash</strong>
            </div>
            <div class="pay-card-option" data-choice="Outstanding Courier" style="cursor:pointer;border:2px solid #6c757d;border-radius:8px;padding:16px;width:140px;text-align:center;">
                <i class="fa fa-truck" style="font-size:32px;color:#6c757d;"></i><br/><strong>Courier</strong>
            </div>
        </div>`;

    dlg.fields_dict.options_html.$wrapper.html(html);

    dlg.$wrapper.find('.pay-card-option').on('click', function(){
        var choice = $(this).data('choice');
        dlg.hide();

        if(choice === 'Cash'){
            finalized = true;

            // Helper to format currency consistently (falls back if format_value unavailable)
            function fmtCurrency(val){
                if(typeof frappe.format_value === 'function'){
                    return frappe.format_value(val, { fieldtype: 'Currency' });
                }
                return (val || 0).toFixed(2);
            }

            // ──────────────────────────────────────────────────────────────
            // 1️⃣  Fetch invoice & delivery expense to show confirmation
            // ──────────────────────────────────────────────────────────────
            const fetchShippingExpense = new Promise(function(resolve){
                frappe.call({
                    method: 'frappe.client.get',
                    args: { doctype: 'Sales Invoice', name: invoiceId }
                }).then(function(invRes){
                    const inv = invRes.message || {};
                    const total = parseFloat(inv.grand_total || inv.total || 0);
                    let shipExp = 0;

                    const addressName = inv.shipping_address_name || inv.customer_address;
                    if(!addressName){
                        return resolve({ total, shipExp });
                    }

                    frappe.call({
                        method:'frappe.client.get',
                        args:{ doctype:'Address', name: addressName }
                    }).then(function(addrRes){
                        const cityId = (addrRes.message || {}).city;
                        if(!cityId){ return resolve({ total, shipExp }); }
                        frappe.call({
                            method:'frappe.client.get',
                            args:{ doctype:'City', name: cityId }
                        }).then(function(cityRes){
                            shipExp = parseFloat((cityRes.message || {}).delivery_expense || 0);
                            resolve({ total, shipExp });
                        }).catch(function(){ resolve({ total, shipExp }); });
                    }).catch(function(){ resolve({ total, shipExp }); });
                }).catch(function(){ resolve({ total:0, shipExp:0 }); });
            });

            fetchShippingExpense.then(function(vals){
                const collectAmt = Math.max(vals.total - vals.shipExp, 0);

                const msg = __('You need to collect amount: <strong>{0}</strong><br/><br/>Total Invoice: {1}<br/>Shipping Expense: {2}',
                    [fmtCurrency(collectAmt), fmtCurrency(vals.total), fmtCurrency(vals.shipExp)]);

                frappe.confirm(msg, function(){
                    // User confirmed – proceed with backend call
                    proceedPayInvoice();
                }, function(){
                    // Cancelled – revert card move
                    finalized = false;
                    revertCard();
                    jarz_pos.kanban.data.loadOrdersData();
                });
            });

            // Function to actually call backend and finalize payment
            function proceedPayInvoice(){
                console.log('[DEBUG] Calling pay_invoice', {
                    invoice_name: invoiceId,
                    payment_mode: 'Cash',
                    pos_profile: (window.currentPOSProfile ? window.currentPOSProfile.name : '')
                });

                frappe.call({
                    method:'jarz_pos.jarz_pos.page.custom_pos.custom_pos.pay_invoice',
                    args:{ invoice_name: invoiceId, payment_mode: 'Cash', pos_profile: (window.currentPOSProfile ? window.currentPOSProfile.name : '') },
                    freeze:true,
                    callback:function(r){
                        console.log('[DEBUG] pay_invoice response', r.message);
                        jarz_pos.kanban.cards.applyStatusClass($card,'Paid');
                        $card.find('.kanban-card-status-text').text('Paid');
                        $card.find('.mark-paid-btn').remove();
                        $('#column-out_for_delivery').prepend($card);
                        jarz_pos.kanban.data.updateInvoiceStatus(invoiceId, newColumnId, newStatus);
                    },
                    error:function(err){
                        console.error('[DEBUG] pay_invoice error', err);
                        jarz_pos.kanban.data.loadOrdersData();
                    }
                });
            }

        } else if(choice === 'Outstanding Courier'){
            finalized = true; // hide first dialog, show courierDlg; will reset below
            jarz_pos.couriers.openSelector(function(courierId){
                if(!courierId){ finalized=false; revertCard(); jarz_pos.kanban.data.loadOrdersData(); return; }
                finalized = true;
                frappe.call({
                    method:'jarz_pos.jarz_pos.page.custom_pos.custom_pos.mark_courier_outstanding',
                    args:{invoice_name:invoiceId, courier:courierId},
                    freeze:true,
                    callback:function(){
                        $card.find('.kanban-card-status-text').text('Courier Outstanding');
                        $card.addClass('status-courier');
                        if($card.find('.courier-tag').length===0){
                            $card.find('.kanban-card-status-text').append(' <span class="courier-tag">'+__('Outstanding Courier')+'</span>');
                        }
                        $('#column-out_for_delivery').prepend($card);
                        jarz_pos.kanban.data.updateInvoiceStatus(invoiceId, newColumnId, newStatus);
                    },
                    error:function(){ loadOrdersDataAndRevert(); }
                });

                function loadOrdersDataAndRevert(){
                    revertCard();
                    jarz_pos.kanban.data.loadOrdersData();
                }
            });
        } else {
            revertCard();
            jarz_pos.kanban.data.loadOrdersData();
        }
    });

    dlg.show();
};

jarz_pos.kanban.columns.loadKanbanConfiguration = function(profile) {
	return new Promise(function(resolve) {
		// Try to load from localStorage first as fallback
		var localStorageKey = 'kanban_config_' + profile.name;
		var localConfig = localStorage.getItem(localStorageKey);

		if (localConfig) {
			try {
				var parsedConfig = JSON.parse(localConfig);
				console.log("Loaded kanban config from localStorage:", parsedConfig);
				resolve(jarz_pos.kanban.columns.ensureStandardColumns(parsedConfig));
				return;
			} catch (e) {
				console.error("Error parsing localStorage config:", e);
			}
		}

		// Try to load saved configuration from Custom Settings
		frappe.call({
			method: 'frappe.client.get_list',
			args: {
				doctype: 'Custom Settings',
				fields: ['name', 'value'],
		  filters: {
					doctype_name: 'POS Profile',
					document_name: profile.name,
					setting_name: 'kanban_config'
				},
				limit: 1
			},
			callback: function(r) {
				if (r.message && r.message.length > 0) {
					try {
						var savedConfig = JSON.parse(r.message[0].value);
						console.log("Loaded saved kanban config:", savedConfig);
						// Also save to localStorage for future use
						localStorage.setItem(localStorageKey, JSON.stringify(savedConfig));
						resolve(jarz_pos.kanban.columns.ensureStandardColumns(savedConfig));
					} catch (e) {
						console.error("Error parsing saved config:", e);
						resolve(jarz_pos.kanban.columns.ensureStandardColumns(jarz_pos.kanban.columns.getDefaultKanbanConfig()));
					}
				} else {
					console.log("No saved config found, using defaults");
					resolve(jarz_pos.kanban.columns.ensureStandardColumns(jarz_pos.kanban.columns.getDefaultKanbanConfig()));
				}
			},
			error: function(err) {
				console.error("Error loading kanban config, using defaults:", err);
				resolve(jarz_pos.kanban.columns.ensureStandardColumns(jarz_pos.kanban.columns.getDefaultKanbanConfig()));
			}
		});
	});
}

jarz_pos.kanban.columns.getDefaultKanbanConfig = function() {
	return {
		columns: [
			{ id:'received',title:'Received',status:'Received',isCustom:false,order:0,hidden:false },
			{ id:'processing',title:'Processing',status:'Processing',isCustom:false,order:1,hidden:false },
			{ id:'preparing',title:'Preparing',status:'Preparing',isCustom:false,order:2,hidden:false },
			{ id:'out_for_delivery',title:'Out for Delivery',status:'Out for delivery',isCustom:false,order:3,hidden:false },
			{ id:'completed',title:'Completed',status:'Completed',isCustom:false,order:4,hidden:false }
		]
	};
}

jarz_pos.kanban.columns.createKanbanColumns = function(config) {
	var $board = $('#kanban-board');
	$board.empty();

	// Sort columns by order
	config.columns.sort(function(a, b) {
		return a.order - b.order;
	});

	config.columns.filter(function(col){ return !col.hidden; }).forEach(function(column) {
		var columnHtml = `
			<div class="kanban-column" data-column-id="${column.id}" data-status="${column.status || ''}" data-is-custom="${column.isCustom}">
				<div class="kanban-column-header ${column.isCustom ? 'custom-column' : ''}">
					<span class="column-title">${column.title}</span>
					<div>
						${column.isCustom ? `<button class="btn btn-sm column-delete-btn" style="background: none; border: none; color: white; padding: 2px 4px;"><i class="fa fa-times"></i></button>` : ''}
					</div>
				</div>
				<div class="kanban-column-body" id="column-${column.id}">
					${column.id==='out_for_delivery' ? `
					<!-- Cards will be loaded here -->
					` : '<!-- Cards will be loaded here -->'}
				</div>
			</div>
		`;

		$board.append(columnHtml);
	});

	// Initialize drag and drop for columns
	if (window.Sortable) {
		new Sortable($board[0], {
			animation: 150,
			handle: '.kanban-column-header',
			onEnd: function(evt) {
				jarz_pos.kanban.columns.updateColumnOrder();
			}
		});

		// Initialize drag and drop for cards within columns
		config.columns.forEach(function(column) {
			var columnBody = document.getElementById(`column-${column.id}`);
			if (!columnBody) return;

				new Sortable(columnBody, {
					group: 'kanban-cards',
					animation: 150,
					ghostClass: 'sortable-ghost',
					dragClass: 'sortable-drag',
					onEnd: function(evt) {
						var cardElement = evt.item;
						var invoiceId = cardElement.dataset.invoiceId;
						var newColumnId = evt.to.parentElement.dataset.columnId;
					var oldColumnId = evt.from.parentElement.dataset.columnId;
						var newStatus = evt.to.parentElement.dataset.status;

					if (newColumnId === 'out_for_delivery') {
						// Always route through unified handler – it determines dialog
						// flow depending on whether the invoice is already paid or not.
						jarz_pos.kanban.columns.handleOutForDeliveryMove(invoiceId, cardElement, newColumnId, newStatus, oldColumnId);
						return; // Handler will call updateInvoiceStatus when appropriate
					}

					// For moves to other columns, update status immediately.
						jarz_pos.kanban.data.updateInvoiceStatus(invoiceId, newColumnId, newStatus);
					}
				});
		});
	}
}

jarz_pos.kanban.columns.updateColumnOrder = function() {
	var $columns = $('.kanban-column');
	var newOrder = [];

	$columns.each(function(index) {
		var columnId = $(this).data('column-id');
		var column = window.kanbanConfig.columns.find(function(col) {
			return col.id === columnId;
		});

		if (column) {
			column.order = index;
			newOrder.push(column);
		}
	});

	window.kanbanConfig.columns = newOrder;
	console.log("Column order updated:", newOrder.map(function(col) { return col.title; }));
}

jarz_pos.kanban.columns.manageColumns = function() {
	// Build dynamic HTML for dialog
	var fieldsHtml = '<div style="max-height:400px;overflow-y:auto;" id="column-manager-wrapper">';
	window.kanbanConfig.columns.forEach(function(col){
		if(col.isCustom){
			// Custom columns – editable title + delete button
			fieldsHtml += `
			<div class="column-mgr-row" data-col-id="${col.id}" style="display:flex;align-items:center;margin-bottom:6px;gap:4px;">
				<input type="checkbox" data-col-id="${col.id}" ${col.hidden ? '' : 'checked'} style="margin-right:4px;">
				<input type="text" class="form-control col-title-input" data-col-id="${col.id}" value="${frappe.utils.escape_html(col.title)}" style="flex:1;min-width:120px;">
				<button class="btn btn-danger btn-xs delete-col-btn" data-col-id="${col.id}"><i class="fa fa-trash"></i></button>
			</div>`;
		} else {
			// Standard columns – readonly title
			fieldsHtml += `<div style="margin-bottom:6px;"><label><input type="checkbox" data-col-id="${col.id}" ${col.hidden ? '' : 'checked'}> ${frappe.utils.escape_html(col.title)}</label></div>`;
		}
	});
	fieldsHtml += '</div><hr><div><input type="text" class="form-control" id="new-col-title" placeholder="New custom column title (comma / newline to add multiple)"></div>';

	var dlg = new frappe.ui.Dialog({
		title: 'Manage Columns',
		fields: [{ fieldtype: 'HTML', options: fieldsHtml, fieldname: 'html' }],
		primary_action_label: 'Save',
		primary_action: function() {
			var deletedIds = [];
			// Handle deletions – rows removed from DOM beforehand
			window.kanbanConfig.columns = window.kanbanConfig.columns.filter(function(col){
				return !col.isCustom || !$('#column-manager-wrapper').find(`[data-col-id="${col.id}"]`).length === false;
			});

			// Update visibility & titles
			dlg.$wrapper.find('input[type="checkbox"]').each(function(){
				var colId = $(this).data('col-id');
				var col = window.kanbanConfig.columns.find(function(c){ return c.id === colId; });
				if(col){ col.hidden = !$(this).is(':checked'); }
			});

			dlg.$wrapper.find('.col-title-input').each(function(){
				var colId = $(this).data('col-id');
				var newTitle = $(this).val().trim();
				var col = window.kanbanConfig.columns.find(function(c){ return c.id === colId; });
				if(col && newTitle){ col.title = newTitle; }
			});

			// Add new custom columns if provided
			var raw = dlg.$wrapper.find('#new-col-title').val();
			if(raw){
				raw.split(/[,\n]/).map(function(s){ return s.trim(); }).filter(Boolean).forEach(function(title){
					var newId = 'custom_' + Date.now() + '_' + Math.floor(Math.random()*1000);
					window.kanbanConfig.columns.push({ id: newId, title: title, status: null, isCustom: true, order: window.kanbanConfig.columns.length, hidden: false });
				});
			}

			dlg.hide();
			jarz_pos.kanban.columns.createKanbanColumns(window.kanbanConfig);
			jarz_pos.kanban.columns.saveKanbanConfiguration(window.currentPOSProfile);
		}
	});

	// Attach delete handlers
	dlg.$wrapper.on('click', '.delete-col-btn', function(e){
		e.preventDefault();
		var colId = $(this).data('col-id');
		// Remove row immediately
		$(this).closest('.column-mgr-row').remove();
	});

	dlg.show();
}

jarz_pos.kanban.columns.deleteCustomColumn = function(columnId) {
	var column = window.kanbanConfig.columns.find(function(col) {
		return col.id === columnId;
	});

	if (!column || !column.isCustom) {
		frappe.msgprint('Cannot delete default columns');
		return;
	}

	// Check if column has any cards before allowing deletion
	var $columnBodyCheck = $(`#column-${columnId}`);
	if ($columnBodyCheck.find('.kanban-card').length > 0) {
		frappe.msgprint('Please move or delete all cards from this column before deleting it.');
		return;
	}

	frappe.confirm(
		`Are you sure you want to delete the column "${column.title}"?`,
		function() {
			// Remove column from config
			window.kanbanConfig.columns = window.kanbanConfig.columns.filter(function(col) {
				return col.id !== columnId;
			});

			// Remove column from DOM
			$(`.kanban-column[data-column-id="${columnId}"]`).remove();

			frappe.msgprint(`Column "${column.title}" deleted successfully!`);
		}
	);
}

jarz_pos.kanban.columns.saveKanbanConfiguration = function(profile) {
	var configData = JSON.stringify(window.kanbanConfig);
	var localStorageKey = 'kanban_config_' + profile.name;

	// Save to localStorage immediately
	localStorage.setItem(localStorageKey, configData);
	console.log("Kanban configuration saved to localStorage");

	// Try to save to database as well (optional)
		  frappe.call({
			method: 'frappe.client.insert',
		args: {
			doc: {
				doctype: 'Custom Settings',
				doctype_name: 'POS Profile',
				document_name: profile.name,
				setting_name: 'kanban_config',
				value: configData
			}
		},
		callback: function(r) {
			frappe.msgprint('Kanban configuration saved successfully to database!');
		},
		error: function(err) {
			console.log("Could not save to database, using localStorage only:", err.message);
			frappe.msgprint('Kanban configuration saved locally!');

			// If insert fails, try to update existing record
			frappe.call({
				method: 'frappe.client.get_list',
				args: {
					doctype: 'Custom Settings',
					fields: ['name'],
					filters: {
						doctype_name: 'POS Profile',
						document_name: profile.name,
						setting_name: 'kanban_config'
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
								value: configData
							},
							callback: function(r) {
								frappe.msgprint('Kanban configuration updated in database!');
							}
						});
					}
				},
				error: function(err2) {
					console.log("Database operations failed, localStorage is being used:", err2.message);
		}
	  });
	}
  });
}
