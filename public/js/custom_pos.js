// Frappe-compatible POS Version
frappe.pages['custom-pos'].on_page_load = function(wrapper) {
	frappe.provide('jarz_pos');
	console.log("=== CUSTOM POS LOADING ===");
	
	// Build the page skeleton
	var page = frappe.ui.make_app_page({
	  parent: wrapper,
	  title: 'Jarz POS',
	  single_column: false
	});
	
	console.log("Page created successfully");
	
	// Hide Frappe sidebar toggle for this page
	setTimeout(function(){
		$('.toggle-sidebar, .navbar .toggle-sidebar, .sidebar-toggle, .navbar-toggle').hide();
		$('body').addClass('pos-no-sidebar collapsed-sidebar'); // collapsed-sidebar removes left nav spacing
		$('.layout-side-section, .sidebar, #sidebar-menu').hide();
	},10);
	
	// Add SortableJS for drag and drop functionality
	if (!window.Sortable) {
		$('head').append('<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>');
	}

	// Debug functions remain available from the browser console. The on-page
	// debug panel was removed for production cleanliness.
		loadDebugFunctions();
	
	// IMPORTANT: Use setTimeout to ensure DOM is ready in Frappe
	setTimeout(function() {
		console.log("Initializing POS after DOM ready...");
		
		// Clear and add content to the main layout SECTION that Frappe actually renders
		var $layoutMain = $(wrapper).find('.layout-main-section');
		if(!$layoutMain.length) {
			// Fallback if selector changes in future versions
			$layoutMain = page.body; // use whole page body
		}
		$layoutMain.empty();
		
		function waitForModules(callback) {
			if (window.jarz_pos && jarz_pos.profile && typeof jarz_pos.profile.loadPOSProfile === 'function' &&
				jarz_pos.core && typeof jarz_pos.core.initializePOSWithProfile === 'function') {
				callback();
			} else {
				// Retry after short delay until modules are available
				setTimeout(function() { waitForModules(callback); }, 50);
			}
		}

		waitForModules(function() {
			// Load POS Profile first before continuing
			jarz_pos.profile.loadPOSProfile(function(profile) {
				if (!profile) {
					$layoutMain.html('<div class="alert alert-danger">No POS Profile available for your user. Please contact administrator.</div>');
					return;
				}
				
				window.currentPOSProfile = profile;
				console.log("POS Profile loaded:", profile);
				
				// Continue with regular initialization
				jarz_pos.core.initializePOSWithProfile($layoutMain, profile);
			});
		});
		
	}, 100); // Initial delay for Frappe page setup
};

// 🧪 DEBUG FUNCTIONS FOR BUNDLE PRICING
function loadDebugFunctions() {
	console.log("🎯 LOADING BUNDLE PRICING DEBUG TOOLS...");
	
	// Make debug functions available globally
	window.testBundlePricingDebug = testBundlePricingDebug;
	window.testMultipleBundles = testMultipleBundles;
	window.debugSystemState = debugSystemState;
	window.testBundlePricingWithCart = testBundlePricingWithCart;
	
	console.log(`
🎯 BUNDLE PRICING DEBUG TOOLS LOADED
====================================

The Custom POS page now has comprehensive debug tools!

Available functions:
1. testBundlePricingDebug()     - Run basic bundle test
2. testMultipleBundles()        - Show multiple test configurations  
3. debugSystemState()           - Check system state
4. testBundlePricingWithCart(cart, name) - Test with custom cart

Usage:
- Use the debug panel buttons (top-right corner), OR
- Open browser console (F12) and call functions directly
- Check server logs for backend debug output
- All parameters and calculations will be logged

Ready to test bundle pricing! 🚀
	`);
}

// Debug function to test bundle pricing
function testBundlePricingDebug() {
	console.log("🧪 FRONTEND DEBUG: Starting Bundle Pricing Test");
	console.log("📍 Current URL:", window.location.href);
	console.log("📍 Timestamp:", new Date().toISOString());
	
	// Test cart data - adjust item codes to match your system
	const testCartData = [
		{
			is_bundle: true,
			item_code: "BUNDLE_PARENT",  // Change this to an existing item
			bundle_name: "Debug Test Bundle",
			price: 150.00,  // Target bundle price
			items: [
				{ item_code: "SKU006", qty: 1 },  // Coffee Mug
				{ item_code: "SKU007", qty: 1 }   // Television
			]
		}
	];
	
	console.log("🛒 Test Cart Data:");
	console.log(JSON.stringify(testCartData, null, 2));
	
	// Test parameters
	const profileName = window.currentPOSProfile?.name || frappe.defaults.get_default("pos_profile") || "";
	if(!profileName) {
		console.warn("⚠️  No POS Profile detected for this user. The backend will attempt a fallback.");
	}
	const testParams = {
		cart_json: JSON.stringify(testCartData),
		customer_name: "Walk-In Customer",  // Adjust if needed
		pos_profile_name: profileName
	};
	
	console.log("📋 API Parameters:");
	console.log("   - Method: jarz_pos.jarz_pos.page.custom_pos.custom_pos.create_sales_invoice");
	console.log("   - cart_json:", testParams.cart_json);
	console.log("   - customer_name:", testParams.customer_name);
	console.log("   - pos_profile_name:", testParams.pos_profile_name);
	
	console.log("\n🚀 Making API call...");
	console.log("⚠️  Check the server console/logs for detailed backend debug output!");
	
	// Make the API call
	frappe.call({
		method: "jarz_pos.jarz_pos.page.custom_pos.custom_pos.create_sales_invoice",
		args: testParams,
		callback: function(response) {
			console.log("\n✅ API Response received:");
			console.log("   - Success:", !!response.message);
			
			if (response.message) {
				console.log("   - Invoice Name:", response.message.name);
				console.log("   - Grand Total:", response.message.grand_total);
				console.log("   - Items Count:", response.message.items?.length || 'N/A');
				
				console.log("\n📋 Invoice Items:");
				if (response.message.items) {
					response.message.items.forEach((item, index) => {
						console.log(`   Item ${index + 1}:`);
						console.log(`      - item_code: ${item.item_code}`);
						console.log(`      - qty: ${item.qty}`);
						console.log(`      - rate: ${item.rate}`);
						console.log(`      - discount_amount: ${item.discount_amount || 0}`);
						console.log(`      - amount: ${item.amount}`);
						console.log(`      - description: ${(item.description || '').substring(0, 50)}...`);
					});
				}
				
				// Show success message
				frappe.msgprint({
					title: "Bundle Test Successful! 🎉",
					message: `
						<div style="font-family: monospace;">
							<strong>Invoice:</strong> ${response.message.name}<br>
							<strong>Total:</strong> £${response.message.grand_total}<br>
							<strong>Items:</strong> ${response.message.items?.length || 0}<br><br>
							<em>Check browser console and server logs for detailed debug output!</em>
						</div>
					`,
					indicator: "green"
				});
				
				// Optionally open the invoice
				setTimeout(() => {
					if (confirm("Open the created invoice?")) {
						frappe.set_route("Form", "Sales Invoice", response.message.name);
					}
				}, 1000);
			}
		},
		error: function(error) {
			console.error("\n❌ API Error:");
			console.error("   - Error:", error);
			console.error("   - Message:", error.message);
			console.error("   - Exception:", error.exception);
			
			frappe.msgprint({
				title: "Bundle Test Failed ❌",
				message: `
					<div style="font-family: monospace; color: red;">
						<strong>Error:</strong> ${error.message || 'Unknown error'}<br><br>
						<em>Check browser console and server logs for detailed error information!</em>
					</div>
				`,
				indicator: "red"
			});
		}
	});
}

// Test function with different bundle configurations
function testMultipleBundles() {
	console.log("🧪 FRONTEND DEBUG: Testing Multiple Bundle Configurations");
	
	const testConfigs = [
		{
			name: "Small Bundle",
			cart: [{
				is_bundle: true,
				item_code: "BUNDLE_PARENT",
				bundle_name: "Small Test Bundle",
				price: 50.00,
				items: [{ item_code: "SKU006", qty: 1 }]
			}]
		},
		{
			name: "Mixed Cart",
			cart: [
				{
					is_bundle: true,
					item_code: "BUNDLE_PARENT",
					bundle_name: "Mixed Bundle",
					price: 100.00,
					items: [
						{ item_code: "SKU006", qty: 1 },
						{ item_code: "SKU007", qty: 1 }
					]
				},
				{
					is_bundle: false,
					item_code: "SKU006",
					qty: 1,
					price: 25.00
				}
			]
		}
	];
	
	testConfigs.forEach((config, index) => {
		console.log(`\n🧪 Test ${index + 1}: ${config.name}`);
		console.log("Cart:", JSON.stringify(config.cart, null, 2));
	});
	
	console.log("\n💡 To run a specific test, call:");
	console.log("   testBundlePricingWithCart(testConfigs[0].cart, 'Test Name')");
	
	// Show in UI as well
	frappe.msgprint({
		title: "Multiple Bundle Test Configurations",
		message: `
			<div style="font-family: monospace;">
				<strong>Available Test Configurations:</strong><br><br>
				1. <strong>Small Bundle</strong> - Single item bundle<br>
				2. <strong>Mixed Cart</strong> - Bundle + regular items<br><br>
				<em>Check browser console for detailed configurations!</em><br><br>
				<strong>Usage:</strong><br>
				Call testBundlePricingWithCart(config, 'Test Name') in console
			</div>
		`,
		indicator: "blue"
	});
}

// Helper function to test with specific cart data
function testBundlePricingWithCart(cartData, testName = "Custom Test") {
	console.log(`\n🧪 Running: ${testName}`);
	
	const profileName = window.currentPOSProfile?.name || frappe.defaults.get_default("pos_profile") || "";
	if(!profileName) {
		console.warn("⚠️  No POS Profile detected for this user. The backend will attempt a fallback.");
	}
	frappe.call({
		method: "jarz_pos.jarz_pos.page.custom_pos.custom_pos.create_sales_invoice",
		args: {
			cart_json: JSON.stringify(cartData),
			customer_name: "Test Customer",
			pos_profile_name: profileName
		},
		callback: function(response) {
			console.log(`✅ ${testName} completed:`, response.message?.name);
			frappe.msgprint(`✅ ${testName} completed: ${response.message?.name}`);
		},
		error: function(error) {
			console.error(`❌ ${testName} failed:`, error.message);
			frappe.msgprint(`❌ ${testName} failed: ${error.message}`);
		}
	});
}

// Function to check current system state
function debugSystemState() {
	console.log("🔍 SYSTEM STATE DEBUG");
	console.log("📍 Current user:", frappe.session.user);
	console.log("📍 Current site:", frappe.boot.sitename);
	console.log("📍 Frappe version:", frappe.boot.versions?.frappe);
	console.log("📍 ERPNext version:", frappe.boot.versions?.erpnext);
	
	// Check if we're on the right page
	console.log("📍 Current route:", frappe.get_route());
	console.log("📍 Is custom POS?", window.location.href.includes('custom-pos'));
	
	// Check for required objects
	console.log("📍 Frappe object available:", typeof frappe !== 'undefined');
	console.log("📍 jQuery available:", typeof $ !== 'undefined');
	
	// Test basic API connectivity
	frappe.call({
		method: "frappe.auth.get_logged_user",
		callback: function(r) {
			console.log("📍 API connectivity test:", r.message ? "✅ Working" : "❌ Failed");
			
			// Show system state in UI
			frappe.msgprint({
				title: "System State Debug",
				message: `
					<div style="font-family: monospace;">
						<strong>User:</strong> ${frappe.session.user}<br>
						<strong>Site:</strong> ${frappe.boot.sitename}<br>
						<strong>Frappe:</strong> ${frappe.boot.versions?.frappe || 'N/A'}<br>
						<strong>ERPNext:</strong> ${frappe.boot.versions?.erpnext || 'N/A'}<br>
						<strong>Page:</strong> ${frappe.get_route().join('/')}<br>
						<strong>API:</strong> ${r.message ? '✅ Working' : '❌ Failed'}<br><br>
						<em>Check browser console for detailed output!</em>
					</div>
				`,
				indicator: "blue"
			});
		}
	});
}
  