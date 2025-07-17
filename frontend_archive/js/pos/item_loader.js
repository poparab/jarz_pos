frappe.provide('jarz_pos.item_loader');

jarz_pos.item_loader.loadItemsWithProfile = function($parent, profile) {
	console.log("Loading items with POS Profile:", profile.name);

	// Get item groups from profile
	var itemGroups = profile.item_groups || [];
	var warehouse = profile.warehouse;
	var priceList = profile.selling_price_list || profile.price_list;

	if (itemGroups.length === 0) {
		$parent.html('<p style="color: #888;">No item groups configured in POS Profile. Please configure item groups first.</p>');
		return;
	}

	console.log("Item groups:", itemGroups.map(function(ig) { return ig.item_group; }));
	console.log("Warehouse:", warehouse);
	console.log("Price List:", priceList);

	if (!priceList) {
		console.error("No price list found in POS Profile!");
		console.log("Full profile object:", profile);
		console.log("Available fields:", Object.keys(profile));
	}

	$parent.empty();

	// Load items for each item group
	var loadPromises = itemGroups.map(function(igRow) {
		var itemGroup = igRow.item_group;

		return frappe.call({
			method: 'frappe.client.get_list',
			args: {
				doctype: 'Item',
				fields: ['name', 'item_name', 'item_group', 'standard_rate'],
			  filters: {
					item_group: itemGroup,
				disabled: 0
				},
				limit: 200
			}
		}).then(function(itemResult) {
			return {
				item_group: itemGroup,
				items: itemResult.message || []
			};
		});
	});

	// Wait for all item groups to load
	Promise.all(loadPromises).then(function(results) {
		console.log("All item groups loaded");

		// Get all item codes for batch processing
		var allItems = [];
		results.forEach(function(result) {
			allItems = allItems.concat(result.items);
		});

		if (allItems.length === 0) {
			$parent.html('<p style="color: #888;">No items found in configured item groups.</p>');
			return;
		}

		var itemCodes = allItems.map(function(item) { return item.name; });

		// Load inventory and price data in parallel
		var inventoryPromise = frappe.call({
		method: 'frappe.client.get_list',
			args: {
				doctype: 'Bin',
				fields: ['item_code', 'actual_qty'],
				filters: [
					['item_code', 'in', itemCodes],
					['warehouse', '=', warehouse]
				],
				limit: 500
			}
		});

		var pricePromise = frappe.call({
			method: 'frappe.client.get_list',
			args: {
				doctype: 'Item Price',
				fields: ['item_code', 'price_list_rate'],
				filters: [
					['item_code', 'in', itemCodes],
					['price_list', '=', priceList]
				],
				limit: 500
			}
		}).then(function(result) {
			console.log("Price data loaded:", result.message ? result.message.length : 0, "prices for price list:", priceList);
			if (result.message && result.message.length > 0) {
				console.log("Sample price data:", result.message.slice(0, 3));
			}
			return result;
		});

		Promise.all([inventoryPromise, pricePromise]).then(function(dataResults) {
			var inventoryResult = dataResults[0];
			var priceResult = dataResults[1];

			// Create data lookups
			var inventoryData = {};
			var priceData = {};

			if (inventoryResult.message) {
				inventoryResult.message.forEach(function(bin) {
					inventoryData[bin.item_code] = bin.actual_qty || 0;
				});
			}

			if (priceResult.message) {
				priceResult.message.forEach(function(price) {
					priceData[price.item_code] = price.price_list_rate || 0;
				});
				console.log("Price data processed:", Object.keys(priceData).length, "items with prices");
			} else {
				console.log("No price data received from API");
			}

			// Store combined data in a global object for the cart to use
			window.JarzItemsData = {};
			allItems.forEach(function(item) {
				window.JarzItemsData[item.name] = {
					price: priceData[item.name] || item.standard_rate || 0,
					inventory: inventoryData[item.name] || 0,
					item_name: item.item_name,
					item_group: item.item_group
				};
			});

			// Render item groups
			results.forEach(function(result) {
				if (result.items.length > 0) {
					jarz_pos.item_loader.renderItemGroup(result.item_group, result.items, inventoryData, priceData, $parent);
				}
			});

			console.log("Items rendered with profile successfully");
		}).catch(function(err) {
			console.error("Error loading inventory/price data:", err);

			// Fallback: render without inventory/price data
			results.forEach(function(result) {
				if (result.items.length > 0) {
					jarz_pos.item_loader.renderItemGroup(result.item_group, result.items, {}, {}, $parent);
				}
			});
		});
	}).catch(function(err) {
		console.error("Error loading items:", err);
		$parent.html('<p style="color: red;">Error loading items</p>');
	});
}

jarz_pos.item_loader.renderItemGroup = function(itemGroup, items, inventoryData, priceData, $parent) {
	console.log("Rendering item group:", itemGroup, "with", items.length, "items");

	var groupHtml = `
		<div class="items-group-section">
			<div class="items-group-title">${itemGroup}</div>
			<div class="items-group-grid">
	`;

	items.forEach(function(item) {
		var inventory = inventoryData[item.name] || 0;
		var price = priceData[item.name] || item.standard_rate || 0;

		// Debug: log price for first few items
		if (items.indexOf(item) < 3) {
			console.log("Item:", item.name, "Price from list:", priceData[item.name], "Standard rate:", item.standard_rate, "Final price:", price);
		}

		var inventoryColor = inventory <= 0 ? '#dc3545' : (inventory < 20 ? '#ffc107' : '#28a745');
		var inventoryText = inventory <= 0 ? 'OUT' : inventory.toString();

		groupHtml += `
			<div class="card item-card" data-item-code="${item.name}" style="cursor: pointer; border: 1px solid #ddd; margin-bottom: 8px; position: relative; touch-action: manipulation; -webkit-tap-highlight-color: transparent;">
				<div class="card-body p-2" style="text-align: center;">
					<div style="position: absolute; top: 5px; right: 5px; background: ${inventoryColor}; color: white; border-radius: 10px; padding: 2px 6px; font-size: 10px; font-weight: bold;">
						${inventoryText}
					</div>
										<div style="font-weight: bold; font-size: 13px; margin-top: 10px;">${item.item_name || item.name}</div>
				<div style="color: #666; font-size: 11px;">$${(price || 0).toFixed(2)}</div>
				</div>
			</div>
		`;
	});

	groupHtml += `
			</div>
		</div>
	`;

	var $groupElement = $(groupHtml);
	$parent.append($groupElement);

	// Add click handlers for items in this group
	$groupElement.find('.item-card').on('click', function() {
		var itemCode = $(this).data('item-code');
		var item = items.find(function(i) { return i.name === itemCode; });

		if (!item) {
			console.error("Item not found:", itemCode);
			return;
		}

		var inventory = inventoryData[item.name] || 0;
		if (inventory <= 0) {
			frappe.msgprint('Item is out of stock!');
			return;
		}

		console.log("Item clicked:", item.item_name, "Price:", window.JarzItemsData[item.name].price);
		jarz_pos.cart.addToCart(item);
	});
}

jarz_pos.item_loader.loadItems = function($parent) {
	// Legacy function - kept for backward compatibility
	console.log("Legacy loadItems called - this should not happen with POS Profile");
	$parent.html('<p style="color: red;">POS Profile not loaded. Please refresh the page.</p>');
}

jarz_pos.item_loader.loadBundles = function($parent) {
	console.log("Loading bundles...");

  frappe.call({
	method: 'frappe.client.get_list',
		args: {
			doctype: 'Jarz Bundle',
			fields: ['name', 'bundle_name', 'bundle_price'],
			limit: 10
		},
	callback: function(r) {
			console.log("Bundles loaded:", r.message ? r.message.length : 0);
			var bundles = r.message || [];

			bundles.forEach(function(bundle) {
				var bundleHtml = `
					<div class="card bundle-card" style="cursor: pointer; border: 2px solid #007bff; margin-bottom: 8px; display: inline-block; margin-right: 10px; touch-action: manipulation; -webkit-tap-highlight-color: transparent;">
						<div class="card-body p-2" style="text-align: center; min-width: 150px;">
							<div style="font-weight: bold; color: #007bff; font-size: 14px;">${bundle.bundle_name || bundle.name}</div>
							<div style="color: #666; font-size: 12px;">$${bundle.bundle_price || 0}</div>
						</div>
					</div>
				`;

				var $bundleCard = $(bundleHtml);
				$bundleCard.on('click', function() {
					console.log("Bundle clicked:", bundle.bundle_name);
					jarz_pos.cart.openBundleSelectionModal(bundle);
				});

				$parent.append($bundleCard);
			});

			console.log("Bundles rendered successfully");
		},
		error: function(err) {
			console.error("Error loading bundles:", err);
	}
  });
} 