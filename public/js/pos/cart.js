frappe.provide('jarz_pos.cart');

jarz_pos.cart.initCartView = function(parent) {
    // This function sets up the initial structure of the cart column.
    parent.empty(); // Clear any existing content to prevent duplication.

    let cartHTML = `
        <div class="cart-section">
            <div class="customer-header d-flex justify-content-between align-items-center mb-2">
                <h5>Customer</h5>
                <button class="btn btn-sm btn-primary new-customer-btn">+ New</button>
            </div>
            <div class="customer-section-container mb-3">
                <!-- Customer selector will be injected here -->
            </div>
            
            <div class="cart-display">
                <h5>Cart</h5>
                <div class="cart-items"></div>
                <div class="cart-totals mt-3"></div>
                <button class="btn btn-primary btn-block checkout-btn mt-3" disabled>Checkout</button>
            </div>
        </div>
    `;
    
    parent.html(cartHTML);
    
    // Initialize cart display
    window.JarzCart = []; // Ensure cart is initialized
    jarz_pos.cart.updateCartDisplay();
    
    // Add event handlers
    jarz_pos.cart.addCartEventHandlers(parent);

    // Event handler for the new customer button
    parent.find('.new-customer-btn').on('click', function() {
        // Use the existing createNewCustomer function, but without a search term
        jarz_pos.customer.createNewCustomer(''); 
    });
};

jarz_pos.cart.updateCartDisplay = function() {
    let $cartItems = $('.cart-items');
    let $cartTotals = $('.cart-totals');
    let $checkoutBtn = $('.checkout-btn');
    
    if (!window.JarzCart || window.JarzCart.length === 0) {
        $cartItems.html('<p class="text-muted">No items in cart</p>');
        $cartTotals.empty();
        $checkoutBtn.prop('disabled', true);
        return;
    }
    
    // Render cart items
    let itemsHTML = '';
    let total = 0;
    
    window.JarzCart.forEach((item, index) => {
        if (item.is_bundle) {
            total += item.price || 0;
            let subItemsHTML = (item.items || []).map(subItem => `<li>${subItem.item_name}</li>`).join('');

            itemsHTML += `
                <div class="cart-item bundle-item" data-index="${index}">
                    <strong>${item.bundle_name}</strong>
                    <strong class="float-right">$${(item.price || 0).toFixed(2)}</strong>
                    <ul style="font-size: 0.9em; margin-left: 20px;">${subItemsHTML}</ul>
                    <button class="btn btn-sm btn-danger remove-item" data-index="${index}">Remove</button>
                </div>
                <hr>
            `;
        } else {
            let itemPrice = item.price || 0;
            let itemQty = item.qty || 1;
            let itemAmount = itemPrice * itemQty;
            total += itemAmount;
            
            itemsHTML += `
                <div class="cart-item" data-index="${index}" data-item-code="${item.item_code}">
                    <div class="d-flex justify-content-between align-items-center">
                        <div>
                            <strong>${item.item_name || item.name}</strong>
                        </div>
                        <div>
                            <strong>$${itemAmount.toFixed(2)}</strong>
                        </div>
                    </div>
                    <div class="d-flex justify-content-between align-items-center mt-1">
                        <div class="d-flex align-items-center">
                            <button class="btn btn-sm btn-secondary qty-change" data-action="decrease">-</button>
                            <span class="mx-2">${itemQty}</span>
                            <button class="btn btn-sm btn-secondary qty-change" data-action="increase">+</button>
                        </div>
                        <button class="btn btn-sm btn-danger remove-item" data-index="${index}">Remove</button>
                    </div>
                </div>
                <hr>
            `;
        }
    });
    
    $cartItems.html(itemsHTML);
    
    // Add delivery charges if any
    let deliveryHTML = '';
    if (window.JarzDeliveryCharges && window.JarzDeliveryCharges.income > 0) {
        let deliveryIncome = window.JarzDeliveryCharges.income;
        let deliveryExpense = window.JarzDeliveryCharges.expense || 0;
        let cityName = window.JarzDeliveryCharges.city || 'N/A';
        total += deliveryIncome;
        
        deliveryHTML = `
            <div class="delivery-section mt-3">
                <div class="d-flex justify-content-between">
                    <div>
                        <strong>🚚 Delivery</strong>
                        <br><small>City: ${cityName} | Our Expense: $${deliveryExpense.toFixed(2)}</small>
                    </div>
                    <div>
                        <strong>$${deliveryIncome.toFixed(2)}</strong>
                    </div>
                </div>
                <hr>
            </div>
        `;
    }
    
    // Show totals
    $cartTotals.html(`
        ${deliveryHTML}
        <div class="d-flex justify-content-between">
            <h4>Total:</h4>
            <h4>$${total.toFixed(2)}</h4>
        </div>
    `);
    
    $checkoutBtn.prop('disabled', total === 0);
};

jarz_pos.cart.addCartEventHandlers = function(parent) {
    // Event delegation for dynamically created buttons
    parent.on('click', '.remove-item', function() {
        let index = $(this).data('index');
        window.JarzCart.splice(index, 1);
        jarz_pos.cart.updateCartDisplay();
    });

    parent.on('click', '.qty-change', function() {
        let itemCode = $(this).closest('.cart-item').data('item-code');
        let action = $(this).data('action');
        jarz_pos.cart.updateQuantity(itemCode, action);
    });
    
    // Checkout button
    parent.on('click', '.checkout-btn', function() {
        jarz_pos.cart.checkout();
    });
};

jarz_pos.cart.checkout = function() {
    // Prevent double submission
    if (window.JarzCheckoutInProgress) {
        console.warn('Checkout already in progress – ignoring duplicate click');
        return;
    }

    // Mark as in-progress and disable checkout button
    window.JarzCheckoutInProgress = true;
    const $checkoutBtnGlobal = $('.checkout-btn');
    $checkoutBtnGlobal.prop('disabled', true).addClass('disabled');
    const originalBtnText = $checkoutBtnGlobal.text();
    $checkoutBtnGlobal.text('Processing…');

    // Helper to proceed with invoice creation once we have a slot
    function proceedCheckout() {
        const delivery_charges_json = window.JarzDeliveryCharges ? JSON.stringify(window.JarzDeliveryCharges) : null;

        frappe.call({
            method: 'jarz_pos.jarz_pos.page.custom_pos.custom_pos.create_sales_invoice',
            args: {
                cart_json: JSON.stringify(window.JarzCart),
                customer_name: window.JarzSelectedCustomer.name,
                pos_profile_name: window.JarzPOSProfile.name,
                delivery_charges_json: delivery_charges_json,
                required_delivery_datetime: (window.SelectedDeliverySlot ? window.SelectedDeliverySlot.split('|').pop() : null) // send pure ISO
            },
            callback: function(r) {
                // Reset in-progress flag and button regardless of result
                window.JarzCheckoutInProgress = false;
                $checkoutBtnGlobal.text(originalBtnText);

                if (r.message) {
                    // Build a detailed confirmation message
                    const invoice = r.message;
                    const cityName = (window.JarzDeliveryCharges && window.JarzDeliveryCharges.city) ? window.JarzDeliveryCharges.city : 'N/A';
                    const deliveryExpense = (window.JarzDeliveryCharges && window.JarzDeliveryCharges.expense) ? window.JarzDeliveryCharges.expense : 0;

                    // Prepare items table (item_code × qty)
                    let itemsHtml = '<table style="width:100%;border-collapse:collapse;margin-top:8px;font-size:12px;">';
                    itemsHtml += '<tr style="background:#f8f9fa;"><th style="text-align:left;padding:4px;border:1px solid #dee2e6;">Item</th><th style="text-align:right;padding:4px;border:1px solid #dee2e6;">Qty</th></tr>';
                    (invoice.items || []).forEach(function(it){
                        itemsHtml += `<tr><td style="padding:4px;border:1px solid #dee2e6;">${it.item_code}</td><td style="text-align:right;padding:4px;border:1px solid #dee2e6;">${it.qty}</td></tr>`;
                    });
                    itemsHtml += '</table>';

                    frappe.msgprint({
                        title: `Sales Invoice ${invoice.name} Created ✅`,
                        message: `
                            <div style="font-family:monospace;font-size:13px;">
                                <strong>Total:</strong> $${(invoice.grand_total || 0).toFixed(2)}<br>
                                <strong>City:</strong> ${cityName}<br>
                                <strong>Delivery Expense:</strong> $${deliveryExpense.toFixed(2)}<br>
                                <strong>Delivery Slot:</strong> ${window.SelectedDeliverySlot}<br>
                                <hr style="margin:6px 0;">
                                <strong>Items:</strong><br>
                                ${itemsHtml}
                            </div>
                        `,
                        indicator: 'green'
                    });

                    // Reset cart & state for next sale
                    window.JarzCart = [];
                    jarz_pos.cart.updateCartDisplay();
                    if (jarz_pos?.customer?.clearCustomerSelection) {
                        jarz_pos.customer.clearCustomerSelection();
                    }
                    window.SelectedDeliverySlot = null;

                    frappe.utils.print_doc(r.message);
                }
            },
            error: function(err) {
                window.JarzCheckoutInProgress = false;
                $checkoutBtnGlobal.text(originalBtnText);
                $checkoutBtnGlobal.prop('disabled', false).removeClass('disabled');
                frappe.msgprint('There was an error creating the Sales Invoice. Please check the console for details.');
                console.error(err);
            }
        });
    }

    if (!window.JarzCart || window.JarzCart.length === 0) {
        frappe.msgprint('Cart is empty');
        return;
    }
    
    if (!window.JarzSelectedCustomer) {
        frappe.msgprint('Please select a customer');
        return;
    }

    // Ensure delivery slot selected
    if (!window.SelectedDeliverySlot) {
        jarz_pos.cart.promptDeliverySlot(function(){ proceedCheckout(); });
        return;
    }

    proceedCheckout();
};

// Prompt user to select delivery date/time slot (delegates to delivery_slots module)
jarz_pos.cart.promptDeliverySlot = function(onConfirm){
    const profileName = window.JarzPOSProfile ? window.JarzPOSProfile.name : null;
    if(!profileName){ frappe.msgprint('POS Profile not loaded.'); return; }
    jarz_pos.delivery_slots.prompt(profileName, onConfirm);
};

jarz_pos.cart.updateQuantity = function(itemCode, action) {
    let itemInCart = window.JarzCart.find(i => i.item_code === itemCode);
    if (itemInCart) {
        if (action === 'increase') {
            itemInCart.qty += 1;
        } else if (action === 'decrease') {
            itemInCart.qty -= 1;
            if (itemInCart.qty <= 0) {
                // If quantity is zero or less, remove the item
                let itemIndex = window.JarzCart.findIndex(i => i.item_code === itemCode);
                window.JarzCart.splice(itemIndex, 1);
            }
        }
        jarz_pos.cart.updateCartDisplay();
    }
};

jarz_pos.cart.addToCart = function(item) {
    if (!window.JarzCart) {
        window.JarzCart = [];
    }

    const itemCode = item.name; // 'name' holds the item_code from the API
    
    // Check if item already exists in cart
    let existingItem = window.JarzCart.find(cartItem => cartItem.item_code === itemCode);
    
    if (existingItem) {
        existingItem.qty += 1;
    } else {
        // IMPORTANT: Fetch the price from the loaded item data which respects the Price List
        let price = window.JarzItemsData[itemCode]?.price || 0;

        window.JarzCart.push({
            item_code: itemCode,
            item_name: item.item_name,
            qty: 1,
            price: price, // Use the correct price
        });
    }
    
    jarz_pos.cart.updateCartDisplay();
};

jarz_pos.cart.openBundleSelectionModal = function(bundle) {
    frappe.call({
        method: 'frappe.client.get',
        args: {
            doctype: 'Jarz Bundle',
            name: bundle.name
        },
        callback: function(r) {
            if (r.message) {
                const bundleDetails = r.message;
                let selectedItems = {};

                const dialog = new frappe.ui.Dialog({
                    title: `Configure '${bundleDetails.bundle_name}'`,
                    fields: [
                        { fieldname: 'bundle_summary_html', fieldtype: 'HTML' },
                        { fieldname: 'items_html', fieldtype: 'HTML' }
                    ],
                    primary_action_label: 'Add to Cart',
                    primary_action: function() {
                        const totalSelected = Object.values(selectedItems).reduce((sum, arr) => sum + arr.length, 0);
                        const totalRequired = bundleDetails.items.reduce((sum, ig) => sum + ig.quantity, 0);

                        if (totalSelected !== totalRequired) {
                            frappe.msgprint(`Please select exactly ${totalRequired} items for the bundle. You have selected ${totalSelected}.`);
                            return;
                        }
                        
                        jarz_pos.cart.addBundleToCart(bundleDetails, selectedItems);
                        dialog.hide();
                    }
                });
                
                dialog.get_field('bundle_summary_html').$wrapper.css({ 'padding': '15px', 'background-color': '#f8f9fa', 'border-radius': '6px', 'margin-bottom': '15px' });

                const $wrapper = dialog.get_field('items_html').$wrapper;
                $wrapper.html('<p>Loading items...</p>');
                dialog.show();
                
                let itemGroupNames = bundleDetails.items.map(ig => ig.item_group);
                frappe.call({
                    method: 'frappe.client.get_list',
                    args: {
                        doctype: 'Item',
                        filters: [['item_group', 'in', itemGroupNames]],
                        fields: ['name', 'item_name', 'item_group']
                    },
                    callback: function(itemResult) {
                        const itemsByGroup = {};
                        (itemResult.message || []).forEach(item => {
                            if (!itemsByGroup[item.item_group]) itemsByGroup[item.item_group] = [];
                            itemsByGroup[item.item_group].push(item);
                        });

                        let html = '<div class="bundle-items-container">';
                        bundleDetails.items.forEach(config => {
                            selectedItems[config.item_group] = [];
                            html += `
                                <h5>
                                    Select ${config.quantity} from ${config.item_group}
                                    <span class="selected-count" data-group="${config.item_group}">(0/${config.quantity})</span>
                                </h5>
                                <div class="selected-items-list" data-group="${config.item_group}"></div>
                                <div class="item-group-grid">
                            `;
                            (itemsByGroup[config.item_group] || []).forEach(item => {
                                const itemData = window.JarzItemsData[item.name] || {};
                                const price = itemData.price || 0;
                                const inventory = itemData.inventory || 0;
                                const disabled = inventory <= 0 ? 'disabled' : '';
                                html += `
                                    <div class="card item-card ${disabled}" data-item-code="${item.name}" data-group="${config.item_group}">
                                        <div class="item-name">${item.item_name}</div>
                                        <div class="item-price">$${price.toFixed(2)}</div>
                                        <div class="item-stock" style="color: ${inventory > 0 ? 'green' : 'red'};">
                                            ${inventory > 0 ? `${inventory} in stock` : 'Out of Stock'}
                                        </div>
                                    </div>
                                `;
                            });
                            html += '</div><hr>';
                        });
                        html += '</div>';
                        $wrapper.html(html);

                        $wrapper.on('click', '.item-card:not(.disabled)', function() {
                            const $card = $(this);
                            const itemCode = $card.data('item-code');
                            const group = $card.data('group');
                            const limit = bundleDetails.items.find(ig => ig.item_group === group).quantity;

                            if (selectedItems[group].length < limit) {
                                selectedItems[group].push(itemCode);
                                renderSelectionsAndSummary();
                            } else {
                                frappe.msgprint(`You can only select ${limit} items from ${group}.`);
                            }
                        });
                        
                        $wrapper.on('click', '.remove-selected-item', function() {
                            const itemCode = $(this).data('item-code');
                            const group = $(this).data('group');
                            const indexToRemove = selectedItems[group].indexOf(itemCode);
                            if (indexToRemove > -1) {
                                selectedItems[group].splice(indexToRemove, 1);
                                renderSelectionsAndSummary();
                            }
                        });

                        function renderSelectionsAndSummary() {
                            let individualTotal = 0;
                            bundleDetails.items.forEach(config => {
                                const group = config.item_group;
                                const container = $wrapper.find(`.selected-items-list[data-group="${group}"]`);
                                const limit = config.quantity;
                                container.empty();
                                
                                selectedItems[group].forEach(itemCode => {
                                    const itemData = window.JarzItemsData[itemCode] || {};
                                    const itemName = itemData.item_name || itemCode;
                                    individualTotal += itemData.price || 0;
                                    container.append(`
                                        <span class="selected-item-tag">
                                            ${itemName}
                                            <button class="remove-selected-item" data-item-code="${itemCode}" data-group="${group}">&times;</button>
                                        </span>
                                    `);
                                });
                                $wrapper.find(`.selected-count[data-group="${group}"]`).text(`(${selectedItems[group].length}/${limit})`);
                            });
                            
                            const savings = individualTotal - bundleDetails.bundle_price;
                            dialog.get_field('bundle_summary_html').$wrapper.html(`
                                <div style="display: flex; justify-content: space-between; font-size: 1.1em;">
                                    <span>Individual Price: <strike>$${individualTotal.toFixed(2)}</strike></span>
                                    <strong>Bundle Price: $${bundleDetails.bundle_price.toFixed(2)}</strong>
                                </div>
                                <div style="text-align: center; margin-top: 10px; color: green; font-weight: bold;">
                                    You Save: $${savings.toFixed(2)}
                                </div>
                            `);
                        }
                        
                        renderSelectionsAndSummary(); // Initial render
                        
                        // Styling
                        $wrapper.find('.item-group-grid').css({ display: 'grid', 'grid-template-columns': 'repeat(auto-fill, minmax(120px, 1fr))', gap: '10px', 'margin-top': '10px' });
                        $wrapper.find('.item-card').css({ padding: '10px', border: '1px solid #ddd', 'border-radius': '4px', 'text-align': 'center', 'cursor': 'pointer' });
                        $wrapper.find('.item-card .item-name').css({ 'font-weight': 'bold', 'margin-bottom': '5px' });
                        $wrapper.find('.item-card .item-price').css({ 'color': '#6c757d', 'margin-bottom': '5px' });
                        $wrapper.find('.item-card.disabled').css({ 'background-color': '#f8f9fa', color: '#6c757d', cursor: 'not-allowed', 'border-color': '#e9ecef' });
                        $wrapper.find('.selected-items-list').css({ display: 'flex', 'flex-wrap': 'wrap', gap: '5px', 'margin-bottom': '10px', 'margin-top': '5px' });
                        $wrapper.find('.selected-item-tag').css({ padding: '5px 8px', 'background-color': '#d4edda', 'border-radius': '4px', display: 'flex', 'align-items': 'center' });
                        $wrapper.find('.remove-selected-item').css({ 'background': 'none', 'border': 'none', 'font-size': '16px', 'margin-left': '8px', 'cursor': 'pointer'});
                    }
                });
            }
        }
    });
};

jarz_pos.cart.addBundleToCart = function(bundleDetails, selectedItems) {
    if (!window.JarzCart) {
        window.JarzCart = [];
    }

    let bundleItem = {
        is_bundle: true,
        bundle_name: bundleDetails.bundle_name,
        price: bundleDetails.bundle_price,
        items: [],
        qty: 1,
        item_code: bundleDetails.erpnext_item // This is the parent item for the invoice
    };

    for (const group in selectedItems) {
        selectedItems[group].forEach(itemCode => {
            bundleItem.items.push({
                item_code: itemCode,
                item_name: window.JarzItemsData[itemCode]?.item_name || itemCode
            });
        });
    }

    window.JarzCart.push(bundleItem);
    jarz_pos.cart.updateCartDisplay();
};
 