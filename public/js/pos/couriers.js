frappe.provide('jarz_pos.couriers');

jarz_pos.couriers.openDialog = function() {
    frappe.call({
        method:'jarz_pos.jarz_pos.page.custom_pos.custom_pos.get_courier_balances',
        freeze:true,
        callback:function(r){
            var list = r.message || [];
            var content = renderCourierCards(list);
            var d = new frappe.ui.Dialog({
                title: __('Courier Balances'),
                fields:[{fieldtype:'HTML', fieldname:'html_area'}],
                size:'large'
            });
            d.fields_dict.html_area.$wrapper.html(content);
            // Store reference globally so we can close later
            jarz_pos.couriers._dialog = d;

            // bind expand click events
            d.$wrapper.find('.courier-card').on('click', function(){
                var cid = $(this).data('courier');
                var c = list.find(function(x){ return x.courier===cid; });
                if(!c){ return; }
                var expanded = $(this).next('.courier-details');
                if(expanded.length){ expanded.toggle(); return; }
                $(this).after(renderDetailsTable(c));
            });

            d.show();
        }
    });

    function renderCourierCards(list){
        if(list.length===0){ return '<p style="color:#888;">No couriers found.</p>'; }

        return list.map(function(c){
            var isNegative = parseFloat(c.balance) < 0;
            var balStr = _fmtCurrency(Math.abs(c.balance));
            // Prefix with + / - to clarify direction
            balStr = (isNegative ? '-' : '+') + balStr;

            var colorClass = isNegative ? 'text-danger' : 'text-success';
            var borderStyle = isNegative ? 'border-left:4px solid #dc3545;' : 'border-left:4px solid #28a745;';

            return `<div class="card courier-card mb-2" data-courier="${c.courier}" style="cursor:pointer; ${borderStyle}">
                       <div class="card-body p-2 d-flex justify-content-between align-items-center" style="font-size:13px;">
                         <strong>${c.courier_name}</strong>
                         <div class="d-flex align-items-center" style="gap:6px;">
                             <span class="${colorClass}">${balStr}</span>
                             <button type="button" class="btn btn-sm btn-outline-primary settle-btn" style="padding:2px 6px;">Pay</button>
                         </div>
                       </div>
                    </div>`;
        }).join('');
    }

    function renderDetailsTable(c){
        var rows = c.details.map(function(d){
            var amt = _fmtCurrency(d.amount);
            var ship = _fmtCurrency(d.shipping);
            return `<tr><td>${d.invoice||''}</td><td>${d.city||''}</td><td style="text-align:right;">${amt}</td><td style="text-align:right;">${ship}</td></tr>`;
        }).join('');
        return `<div class="courier-details mb-3" style="display:block;">
                  <table class="table table-bordered table-sm">
                    <thead><tr><th>Invoice</th><th>City</th><th style="text-align:right;">Amount</th><th style="text-align:right;">Shipping</th></tr></thead>
                    <tbody>${rows}</tbody>
                  </table>
                </div>`;
    }
};

// Add event delegation for settle buttons after dialog open
    frappe.after_ajax && frappe.after_ajax(function(){
        $(document).on('click', '.settle-btn', function(e){
            e.stopPropagation(); // prevent card expand
            var courierId = $(this).closest('.courier-card').data('courier');
            if(!courierId){ return; }
            frappe.confirm(__('Settle all transactions with courier?'), function(){
                frappe.call({
                    method:'jarz_pos.jarz_pos.page.custom_pos.custom_pos.settle_courier',
                    args:{courier:courierId, pos_profile:(window.JarzPOSProfile && JarzPOSProfile.name) || ''},
                    freeze:true,
                    callback:function(r){
                        frappe.msgprint(__('Courier settlement completed'));
                        // Close existing dialog if open
                        if(jarz_pos.couriers._dialog){
                            jarz_pos.couriers._dialog.hide();
                            jarz_pos.couriers._dialog = null;
                        }
                    }
                });
            });
        });
    });

// ---------------------------------------------------------------------------
// 🚚 Courier Selector – touch-friendly card picker with Add New option
// ---------------------------------------------------------------------------

// Usage: jarz_pos.couriers.openSelector(function(courierId){ ... })
jarz_pos.couriers.openSelector = function(onSelect){
    const dlg = new frappe.ui.Dialog({
        title: __('Select Courier'),
        fields:[{fieldtype:'HTML', fieldname:'area'}],
        size:'large'
    });

    const wrapper = dlg.fields_dict.area.$wrapper;

    function loadCouriers(){
        frappe.call({
            method:'frappe.client.get_list',
            args:{
                doctype:'Courier',
                fields:['name','courier_name','courier_phone_number'],
                limit_page_length:0
            },
            callback:function(r){ renderList(r.message || []); }
        });
    }

    function renderList(list){
        // Build simple flex grid of cards plus a "New" card
        let html = '<div class="d-flex flex-wrap gap-3">';
        list.forEach(function(c){
            const phone = c.courier_phone_number ? `<br/><small>${c.courier_phone_number}</small>` : '';
            html += `<div class="courier-pick-card card p-3" data-id="${c.name}" style="cursor:pointer;width:140px;text-align:center;">
                        <strong>${c.courier_name || c.name}</strong>${phone}
                     </div>`;
        });
        html += `<div class="new-courier-card card p-3 text-muted" style="cursor:pointer;width:140px;text-align:center;border:2px dashed #6c757d;">
                    <i class="fa fa-plus"></i><br/>${__('New Courier')}
                 </div>`;
        html += '</div>';
        wrapper.html(html);

        wrapper.find('.courier-pick-card').on('click', function(){
            const cid = $(this).data('id');
            if(onSelect){ onSelect(cid); }
            dlg.hide();
        });

        wrapper.find('.new-courier-card').on('click', function(){ openCreateDialog(); });
    }

    function openCreateDialog(){
        const nd = new frappe.ui.Dialog({
            title: __('Create Courier'),
            fields:[
                {fieldname:'courier_name', label:__('Courier Name'), fieldtype:'Data', reqd:1},
                {fieldname:'courier_phone_number', label:__('Phone Number'), fieldtype:'Data'}
            ],
            primary_action_label: __('Save'),
            primary_action(values){
                if(!values.courier_name){ return; }
                frappe.call({
                    method:'frappe.client.insert',
                    args:{ doc:{doctype:'Courier', courier_name: values.courier_name, courier_phone_number: values.courier_phone_number} },
                    freeze:true,
                    callback:function(){ nd.hide(); loadCouriers(); }
                });
            }
        });
        nd.show();
    }

    dlg.show();
    loadCouriers();
};

// helper
window.openCourierBalances = jarz_pos.couriers.openDialog;

// Safe currency formatter – works even if frappe.format_value undefined
function _fmtCurrency(val){
    if(typeof frappe.format_value==='function'){
        return frappe.format_value(val,{fieldtype:'Currency'});
    }
    if(frappe.utils && typeof frappe.utils.format_number==='function'){
        return frappe.utils.format_number(val);
    }
    return (val||0).toFixed(2);
} 