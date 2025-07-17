frappe.provide('jarz_pos.kanban.board');

jarz_pos.kanban.board.initializeKanbanBoard = function(profile) {
	console.log("Initializing Kanban Board for profile:", profile.name);

	// Load saved configuration or use defaults
	jarz_pos.kanban.columns.loadKanbanConfiguration(profile).then(function(config) {
		window.kanbanConfig = config;
		jarz_pos.kanban.columns.createKanbanColumns(config);
		jarz_pos.kanban.data.loadOrdersData();

		// Start auto-refresh every 30 seconds
		if (window.ordersRefreshInterval) {
			clearInterval(window.ordersRefreshInterval);
		}
		// Fallback polling every 10 minutes – primary updates come via WebSocket
		window.ordersRefreshInterval = setInterval(function() {
			if ($('#orders-view').is(':visible')) {
				jarz_pos.kanban.data.loadOrdersData();
			}
		}, 600000);

		// Attach realtime listener only once
		if (!window._jarzRealtimeAttached) {
			window._jarzRealtimeAttached = true;
			// Prepare simple audio ping (reuse Frappe's default notification sound)
			window.JarzPingAudio = new Audio('/assets/frappe/sounds/notification.mp3');
			frappe.realtime.on('jarz_pos_new_invoice', function(data) {
				console.log('📡 Realtime invoice received:', data);
				if (!data) return;
				// Filter by current POS profile
				if (window.currentPOSProfile && data.pos_profile && data.pos_profile !== window.currentPOSProfile.name) {
					return; // Ignore invoices from other profiles
				}
				jarz_pos.kanban.data.addInvoiceCard(data);
				try { window.JarzPingAudio.currentTime = 0; window.JarzPingAudio.play(); } catch (e) {}
			});

			// Invoice marked paid elsewhere
			frappe.realtime.on('jarz_pos_invoice_paid', function(data){
				if(!data || !data.invoice) return;
				var $card = $('#card-'+data.invoice);
				if($card.length){
					jarz_pos.kanban.cards.applyStatusClass($card,'Paid');
					$card.find('.kanban-card-status-text').text('Paid');
					$card.find('.mark-paid-btn').remove();
				}
			});
		}
	});
}

jarz_pos.kanban.board.initializeKanbanEventHandlers = function(profile) {
	// Add column button
	$('#manage-columns-btn').off('click').on('click', function() { jarz_pos.kanban.columns.manageColumns(); });

	// Save configuration button
	$('#save-config-btn').off('click').on('click', function() {
		jarz_pos.kanban.columns.saveKanbanConfiguration(profile);
	});

	// Refresh orders button
	$('#refresh-orders-btn').off('click').on('click', function() {
		jarz_pos.kanban.data.loadOrdersData();
	});
}
