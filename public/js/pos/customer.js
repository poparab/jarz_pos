frappe.provide('jarz_pos.customer');

jarz_pos.customer.initCustomerSelector = function(parent) {
    let customerHTML = `
        <div id="selected-customer-section" class="d-none">
            <div class="d-flex justify-content-between align-items-center form-control" style="background-color: #f0f0f0; height: auto; padding: 0.375rem 0.75rem;">
                <strong id="selected-customer-name" style="flex-grow: 1;"></strong>
                <button class="btn btn-sm btn-danger change-customer-btn" style="line-height: 1;">&times;</button>
            </div>
        </div>
        <div id="customer-search-section">
            <div class="customer-search-container">
                <input type="text" class="form-control customer-search" placeholder="Search or create customer...">
                <div class="customer-details mt-2"></div>
            </div>
        </div>
    `;
    parent.html(customerHTML);

    jarz_pos.customer.setupCustomerSearch(parent);

    parent.find('.change-customer-btn').on('click', function() {
        jarz_pos.customer.clearCustomerSelection();
    });
};

jarz_pos.customer.setupCustomerSearch = function(parent) {
    let $customerSearch = parent.find('.customer-search');
    let $customerDetails = parent.find('.customer-details');
    
    // Customer search functionality
    $customerSearch.on('keyup', function() {
        let searchTerm = $(this).val();
        if (searchTerm.length > 2) {
            jarz_pos.customer.searchCustomers(searchTerm, $customerDetails);
        } else {
            $customerDetails.empty();
        }
    });
    
    // Show recent customers on focus
    $customerSearch.on('focus', function() {
        if ($(this).val().length === 0) {
            jarz_pos.customer.showRecentCustomers($customerDetails);
        }
    });
};

jarz_pos.customer.searchCustomers = function(searchTerm, $container) {
    frappe.call({
        method: 'frappe.client.get_list',
        args: {
            doctype: 'Customer',
            filters: [
                ['name', 'like', '%' + searchTerm + '%']
            ],
            fields: ['name', 'customer_name', 'mobile_no', 'email_id'],
            limit: 10
        },
        callback: function(r) {
            jarz_pos.customer.displayCustomerSuggestions(r.message || [], $container, searchTerm);
        }
    });
};

jarz_pos.customer.showRecentCustomers = function($container) {
    frappe.call({
        method: 'frappe.client.get_list',
        args: {
            doctype: 'Customer',
            fields: ['name', 'customer_name', 'mobile_no', 'email_id', 'creation'],
            order_by: 'creation desc',
            limit: 5
        },
        callback: function(r) {
            jarz_pos.customer.displayCustomerSuggestions(r.message || [], $container, '', true);
        }
    });
};

jarz_pos.customer.displayCustomerSuggestions = function(customers, $container, searchTerm, isRecent = false) {
    let html = '';
    
    if (searchTerm && !isRecent) {
        html += `
            <div class="customer-suggestion add-new" data-search="${searchTerm}">
                <strong>+ Add New Customer</strong><br>
                <small>Create a new customer with: "${searchTerm}"</small>
            </div>
        `;
    }
    
    if (isRecent && customers.length > 0) {
        html += '<div class="recent-header"><small>📅 Recent Customers</small></div>';
    }
    
    customers.forEach(function(customer) {
        let displayName = customer.customer_name || customer.name;
        let contactInfo = '';
        if (customer.mobile_no) contactInfo += customer.mobile_no;
        if (customer.email_id) contactInfo += (contactInfo ? ' • ' : '') + customer.email_id;
        
        let timeInfo = '';
        if (isRecent && customer.creation) {
            let createdDate = new Date(customer.creation);
            let today = new Date();
            let diffTime = Math.abs(today - createdDate);
            let diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
            
            if (diffDays === 1) timeInfo = 'Today';
            else if (diffDays === 2) timeInfo = 'Yesterday';
            else timeInfo = `${diffDays - 1} days ago`;
        }
        
        html += `
            <div class="customer-suggestion" data-customer="${customer.name}">
                <strong>${displayName}</strong>
                ${timeInfo ? `<span class="float-right text-muted"><small>${timeInfo}</small></span>` : ''}
                ${contactInfo ? `<br><small class="text-muted">${contactInfo}</small>` : ''}
            </div>
        `;
    });
    
    $container.html(html);
    
    // Add click handlers
    $container.find('.customer-suggestion').on('click', function() {
        if ($(this).hasClass('add-new')) {
            jarz_pos.customer.createNewCustomer($(this).data('search'));
        } else {
            jarz_pos.customer.selectCustomer($(this).data('customer'));
        }
    });
};

jarz_pos.customer.selectCustomer = function(customerName) {
    $('.customer-details').empty();
    
    // Load customer details and delivery info
    frappe.call({
        method: 'frappe.client.get',
        args: {
            doctype: 'Customer',
            name: customerName
        },
        callback: function(r) {
            if (r.message) {
                let customer = r.message;
                window.JarzSelectedCustomer = customer;

                // Update the UI to show the selected customer
                $('#customer-search-section').addClass('d-none');
                $('#selected-customer-name').text(customer.customer_name || customer.name);
                $('#selected-customer-section').removeClass('d-none');

                // Load delivery charges for the selected customer
                jarz_pos.customer.loadDeliveryCharges(r.message);
            }
        }
    });
};

jarz_pos.customer.clearCustomerSelection = function() {
    window.JarzSelectedCustomer = null;
    window.JarzDeliveryCharges = null;

    $('#selected-customer-section').addClass('d-none');
    $('#selected-customer-name').text('');
    
    $('#customer-search-section').removeClass('d-none');
    $('.customer-search').val('').focus();

    // Update the cart display to remove any delivery charges
    jarz_pos.cart.updateCartDisplay();
};

jarz_pos.customer.loadDeliveryCharges = function(customer) {
    // Load delivery charges from customer address
    frappe.call({
        method: 'frappe.client.get_list',
        args: {
            doctype: 'Address',
            filters: {
                'address_title': customer.customer_name || customer.name,
                'disabled': 0
            },
            fields: ['name', 'city', 'address_line1', 'address_line2']
        },
        callback: function(r) {
            if (r.message && r.message.length > 0) {
                let address = r.message[0];
                if (address.city) {
                    jarz_pos.customer.getCityDeliveryInfo(address.city);
                }
            }
        }
    });
};

jarz_pos.customer.getCityDeliveryInfo = function(cityId) {
    frappe.call({
        method: 'frappe.client.get',
        args: {
            doctype: 'City',
            name: cityId
        },
        callback: function(r) {
            if (r.message) {
                window.JarzDeliveryCharges = {
                    city: r.message.city_name,
                    income: r.message.delivery_income || 0,
                    expense: r.message.delivery_expense || 0
                };
                jarz_pos.cart.updateCartDisplay();
            }
        }
    });
};

jarz_pos.customer.createNewCustomer = function(searchTerm) {
    let d = new frappe.ui.Dialog({
        title: 'Create New Customer',
        fields: [
            {
                fieldname: 'customer_name',
                fieldtype: 'Data',
                label: 'Customer Name',
                default: searchTerm,
                reqd: 1
            },
            {
                fieldname: 'mobile_no',
                fieldtype: 'Data',
                label: 'Mobile Number'
            },
            {
                fieldname: 'email_id',
                fieldtype: 'Data',
                label: 'Email'
            },
            {
                fieldname: 'address_line1',
                fieldtype: 'Data',
                label: 'Address Line 1'
            },
            {
                fieldname: 'city',
                fieldtype: 'Link',
                label: 'City',
                options: 'City'
            }
        ],
        primary_action: function() {
            let values = d.get_values();
            if (values) {
                jarz_pos.customer.saveNewCustomer(values, d);
            }
        },
        primary_action_label: 'Create Customer'
    });
    
    d.show();
};

jarz_pos.customer.saveNewCustomer = function(values, dialog) {
    frappe.call({
        method: 'frappe.client.insert',
        args: {
            doc: {
                doctype: 'Customer',
                customer_name: values.customer_name,
                mobile_no: values.mobile_no,
                email_id: values.email_id,
                customer_group: 'All Customer Groups',
                territory: 'All Territories'
            }
        },
        callback: function(r) {
            if (r.message) {
                // Create address if provided
                if (values.address_line1 && values.city) {
                    jarz_pos.customer.createCustomerAddress(r.message.name, values, dialog);
                } else {
                    dialog.hide();
                    jarz_pos.customer.selectCustomer(r.message.name);
                    frappe.show_alert('Customer created successfully');
                }
            }
        }
    });
};

jarz_pos.customer.createCustomerAddress = function(customerName, values, dialog) {
    frappe.call({
        method: 'frappe.client.insert',
        args: {
            doc: {
                doctype: 'Address',
                address_title: values.customer_name,
                address_line1: values.address_line1,
                city: values.city,
                links: [{
                    link_doctype: 'Customer',
                    link_name: customerName
                }]
            }
        },
        callback: function(r) {
            dialog.hide();
            jarz_pos.customer.selectCustomer(customerName);
            frappe.show_alert('Customer and address created successfully');
        }
    });
};
 