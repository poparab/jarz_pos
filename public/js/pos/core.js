frappe.provide('jarz_pos.core');

jarz_pos.core.initializePOSWithProfile = function($layoutMain, profile) {
	window.JarzPOSProfile = profile; // Make profile globally available

	// Create the main layout structure with profile info
	var mainLayout = `
		<div class="container-fluid" id="pos-container" style="height: calc(100vh - 120px); padding: 0;">
			<!-- Header with Profile Info and Toggle -->
			<div class="alert alert-info" style="margin-bottom: 15px; padding: 8px; font-size: 12px; display: flex; justify-content: space-between; align-items: center;">
				<div>
					<strong>POS Profile:</strong> ${profile.name} |
					<strong>Warehouse:</strong> ${profile.warehouse} |
					<strong>Price List:</strong> ${profile.selling_price_list || profile.price_list || 'Not Set'}
				</div>
				<div>
					<button id="view-toggle" class="btn btn-sm btn-primary" style="padding: 4px 12px; font-size: 11px; margin-right: 8px;">
						<i class="fa fa-th-large"></i> Orders View
					</button>
					<button id="courier-btn" class="btn btn-sm btn-outline-primary" style="padding: 4px 8px; font-size: 11px; margin-right:8px;">
						<i class="fa fa-truck"></i> Couriers
					</button>
					<button id="fullscreen-toggle" class="btn btn-sm btn-outline-primary" style="padding: 4px 8px; font-size: 11px;">
						<i class="fa fa-expand"></i> Full Screen
					</button>
				</div>
			</div>

			<!-- POS View -->
			<div id="pos-view" class="row h-100 no-gutters">
				<div class="col-12 col-lg-9 d-flex flex-column" style="border: 1px solid #ddd; padding: 15px;">
					<div id="bundles-section" style="margin-bottom: 15px; flex-shrink: 0;">
						<h5 style="color: #007bff; margin-bottom: 10px;">Bundles</h5>
						<div id="bundles-column" class="bundles-section">
							<p>Loading bundles...</p>
						</div>
					</div>
					<div id="items-section" style="flex: 1; display: flex; flex-direction: column; min-height: 0;">
						<h5 style="margin-bottom: 10px; flex-shrink: 0;">Items</h5>
						<div id="items-columns-container" style="flex: 1; overflow-y: auto;">
							<p>Loading items...</p>
						</div>
					</div>
				</div>
				<div class="col-12 col-lg-3 d-flex flex-column" style="border: 1px solid #ddd; border-left: none; padding: 15px;">
					<h4 style="flex-shrink: 0;">Cart</h4>
					<div id="cart-column" class="cart-section" style="flex: 1; display: flex; flex-direction: column;">
						<div id="cart-items" style="flex: 1; overflow-y: auto; margin-bottom: 15px;">
							<!-- Cart items will be rendered here by cart.js -->
						</div>
						<div class="cart-totals" style="padding: 10px 0; border-top: 1px solid #ddd; margin-bottom: 10px;">
							<!-- Totals will be rendered here -->
						</div>
						<div style="flex-shrink: 0;">
							<button class="btn btn-primary btn-block checkout-btn" style="min-height: 48px; font-size: 16px; font-weight: 500; touch-action: manipulation; -webkit-tap-highlight-color: transparent;">Checkout</button>
						</div>
					</div>
				</div>
			</div>

			<!-- Orders Kanban View -->
			<div id="orders-view" style="display: none; height: calc(100vh - 180px); padding: 10px; overflow: hidden;">
				<div class="orders-header" style="margin-bottom: 15px; display: flex; justify-content: space-between; align-items: center;">
					<h4 style="margin: 0; color: #007bff;">Sales Invoice Orders</h4>
					<div>
						<button id="manage-columns-btn" class="btn btn-sm btn-outline-success" style="margin-right: 8px;">
							<i class="fa fa-columns"></i> Manage Columns
						</button>
						<button id="save-config-btn" class="btn btn-sm btn-outline-primary" style="margin-right: 8px;">
							<i class="fa fa-save"></i> Save Layout
						</button>
						<button id="refresh-orders-btn" class="btn btn-sm btn-outline-secondary">
							<i class="fa fa-refresh"></i> Refresh
						</button>
					</div>
				</div>
				<div id="kanban-board" class="kanban-board" style="display: flex; gap: 15px; height: calc(100% - 60px); overflow-x: auto; overflow-y: hidden; padding-bottom: 10px;">
					<!-- Columns will be dynamically loaded -->
				</div>
			</div>
		</div>

		<style>
			/* Hide sidebar toggle for clean POS experience */
			.toggle-sidebar {
				display: none !important;
			}

			.navbar .toggle-sidebar,
			.navbar-toggle,
			.sidebar-toggle,
			.navbar .sidebar-toggle {
				display: none !important;
			}

			/* Adjust layout for removed sidebar */
			.desk-body {
				margin-left: 0 !important;
			}

			.page-container {
				margin-left: 0 !important;
			}

			/* Full screen mode styles */
			.pos-fullscreen {
				position: fixed !important;
				top: 0 !important;
				left: 0 !important;
				width: 100vw !important;
				height: 100vh !important;
				z-index: 9999 !important;
				background: white !important;
				padding: 10px !important;
			}

			.pos-fullscreen #pos-container {
				height: calc(100vh - 20px) !important;
			}

			.pos-fullscreen .navbar,
			.pos-fullscreen .page-head {
				display: none !important;
			}

			.items-group-section {
				margin-bottom: 20px;
			}

			.items-group-title {
				background: #f8f9fa;
				padding: 8px 12px;
				margin-bottom: 10px;
				border-left: 4px solid #007bff;
				font-weight: 600;
				color: #495057;
			}

			.items-group-grid {
				display: grid;
				grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
				gap: 8px;
				align-content: start;
			}

			.bundle-item-card.out-of-stock {
				opacity: 0.6 !important;
				cursor: not-allowed !important;
				background: #f5f5f5 !important;
			}

			.bundle-item-card.out-of-stock:hover {
				background: #f5f5f5 !important;
				transform: none !important;
			}

			@media (max-width: 991px) {
				.items-group-grid {
					grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)) !important;
				}
			}
			@media (max-width: 767px) {
				.items-group-grid {
					grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)) !important;
				}
				.container-fluid {
					height: calc(100vh - 100px) !important;
				}
			}
			@media (max-width: 575px) {
				.items-group-grid {
					grid-template-columns: repeat(auto-fill, minmax(90px, 1fr)) !important;
				}
			}

			/* Kanban Board Styles */
			.kanban-column {
				min-width: 280px;
				max-width: 300px;
				background: #f8f9fa;
				border-radius: 8px;
				border: 1px solid #dee2e6;
				display: flex;
				flex-direction: column;
				height: 100%;
			}

			.kanban-column-header {
				padding: 12px 15px;
				background: #007bff;
				color: white;
				border-radius: 8px 8px 0 0;
				font-weight: 600;
				font-size: 14px;
				display: flex;
				justify-content: space-between;
				align-items: center;
				cursor: grab;
			}

			.kanban-column-header:active {
				cursor: grabbing;
			}

			.kanban-column-body {
				flex: 1;
				padding: 10px;
				overflow-y: auto;
				min-height: 200px;
			}

			.kanban-card {
				background: white;
				border: 1px solid #dee2e6;
				border-radius: 6px;
				padding: 12px;
				margin-bottom: 8px;
				cursor: pointer;
				transition: all 0.2s ease;
				box-shadow: 0 1px 3px rgba(0,0,0,0.1);
			}

			.kanban-card:hover {
				box-shadow: 0 2px 8px rgba(0,0,0,0.15);
				border-color: #007bff;
			}

			.kanban-card.dragging {
				opacity: 0.5;
				transform: rotate(5deg);
			}

			.kanban-card-header {
				display: flex;
				justify-content: space-between;
				align-items: flex-start;
				margin-bottom: 8px;
			}

			.kanban-card-title {
				font-weight: 600;
				font-size: 13px;
				color: #333;
				line-height: 1.3;
			}

			.kanban-card-amount {
				font-weight: bold;
				color: #28a745;
				font-size: 14px;
			}

			.kanban-card-info {
				font-size: 12px;
				color: #666;
				margin-bottom: 4px;
			}

			.kanban-card-expanded {
				border-top: 1px solid #e9ecef;
				margin-top: 8px;
				padding-top: 8px;
				font-size: 11px;
			}

			.kanban-card-actions {
				display: flex;
				gap: 6px;
				margin-top: 8px;
			}

			.custom-column {
				background: #17a2b8 !important;
			}

			.column-delete-btn {
				opacity: 0;
				transition: opacity 0.2s;
			}

			.kanban-column-header:hover .column-delete-btn {
				opacity: 1;
			}

			/* Drag and drop feedback */
			.sortable-ghost {
				opacity: 0.3;
			}

			.sortable-drag {
				transform: rotate(5deg);
				box-shadow: 0 5px 15px rgba(0,0,0,0.3);
			}
		</style>
	`;

	$layoutMain.append(mainLayout);
	console.log("Layout added to page");

	// Initialize full screen toggle and view toggle
	jarz_pos.core.initializeFullscreenToggle($layoutMain);
	jarz_pos.core.initializeViewToggle($layoutMain, profile);

	// Use another setTimeout to ensure elements are in DOM
	setTimeout(function() {
		// Search inside the main section we just populated first
		var $itemsContainer = $layoutMain.find('#items-columns-container');
		var $cartCol = $layoutMain.find('#cart-column');
		var $bundlesCol = $layoutMain.find('#bundles-column');

		console.log("Items container found:", $itemsContainer.length);
		console.log("Cart column found:", $cartCol.length);
		console.log("Bundles column found:", $bundlesCol.length);

		if ($itemsContainer.length === 0 || $cartCol.length === 0) {
			console.error("Columns not found! Trying alternative approach...");

			// Alternative: search from body
			$itemsContainer = page.body.find('#items-columns-container');
			$cartCol = page.body.find('#cart-column');

			console.log("Alternative search - Items:", $itemsContainer.length, "Cart:", $cartCol.length);

			if ($itemsContainer.length === 0 || $cartCol.length === 0) {
				console.error("Still not found! Searching in document...");
				$itemsContainer = $('#items-columns-container');
				$cartCol = $('#cart-column');
				console.log("Document search - Items:", $itemsContainer.length, "Cart:", $cartCol.length);
			}
		}

		// Bind courier button
		$layoutMain.find('#courier-btn').on('click', function(){
			if(window.jarz_pos && jarz_pos.couriers && typeof jarz_pos.couriers.openDialog==='function'){
				jarz_pos.couriers.openDialog();
			}
		});

		// Initialize cart
		window.JarzCart = [];
		window.JarzDeliveryCharges = {
			city: null,
			income: 0,
			expense: 0
		};

		// Initialize cart and customer selector
		if ($cartCol.length > 0) {
			jarz_pos.cart.initCartView($cartCol);
			
			let $customerContainer = $cartCol.find('.customer-section-container');
			if ($customerContainer.length > 0) {
				jarz_pos.customer.initCustomerSelector($customerContainer);
			} else {
				console.error("Customer container not found in cart template!");
			}
		}

		// Load bundles first, then items
		if ($bundlesCol.length > 0) {
			jarz_pos.item_loader.loadBundles($bundlesCol);
		}
		if ($itemsContainer.length > 0) {
			jarz_pos.item_loader.loadItemsWithProfile($itemsContainer, profile);
		}

		// Load courier balances sidebar
		if(window.jarz_pos && jarz_pos.couriers && typeof jarz_pos.couriers.loadBalances==='function'){
			jarz_pos.couriers.loadBalances();
		}

		console.log("=== POS INITIALIZATION COMPLETE ===");

	}, 100); // Small delay for DOM elements to be available
}

jarz_pos.core.initializeFullscreenToggle = function($parent) {
	var isFullscreen = false;
	var $toggleBtn = $parent.find('#fullscreen-toggle');
	var $posContainer = $parent.find('#pos-container').parent(); // Get the parent container

	$toggleBtn.on('click', function() {
		if (!isFullscreen) {
			// Enter fullscreen
			$posContainer.addClass('pos-fullscreen');
			$toggleBtn.html('<i class="fa fa-compress"></i> Exit Full Screen');
			isFullscreen = true;

			// Hide navbar and other UI elements
			$('.navbar, .page-head, .page-title').hide();

		} else {
			// Exit fullscreen
			$posContainer.removeClass('pos-fullscreen');
			$toggleBtn.html('<i class="fa fa-expand"></i> Full Screen');
			isFullscreen = false;

			// Show navbar and other UI elements
			$('.navbar, .page-head, .page-title').show();
		}
	});

	// Handle ESC key to exit fullscreen
	$(document).on('keydown', function(e) {
		if (e.key === 'Escape' && isFullscreen) {
			$toggleBtn.click();
		}
	});
}

jarz_pos.core.initializeViewToggle = function($parent, profile) {
	var $viewToggle = $parent.find('#view-toggle');
	var $posView = $parent.find('#pos-view');
	var $ordersView = $parent.find('#orders-view');
	var isOrdersView = false;

	$viewToggle.on('click', function() {
		if (isOrdersView) {
			// Switch to POS View
			$posView.show();
			$ordersView.hide();
			$viewToggle.html('<i class="fa fa-th-large"></i> Orders View');
			isOrdersView = false;
		} else {
			// Switch to Orders View
			$posView.hide();
			$ordersView.show();
			$viewToggle.html('<i class="fa fa-shopping-cart"></i> POS View');
			isOrdersView = true;

			// Initialize kanban board if not already done
			if (!window.kanbanInitialized) {
				jarz_pos.kanban.board.initializeKanbanBoard(profile);
				window.kanbanInitialized = true;
			} else {
				// Refresh orders data
				jarz_pos.kanban.data.loadOrdersData();
			}
		}
	});

	// Initialize kanban board event handlers
	jarz_pos.kanban.board.initializeKanbanEventHandlers(profile);
} 