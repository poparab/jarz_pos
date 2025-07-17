frappe.provide('jarz_pos.delivery_slots');

/**
 * Get human day name (Monday ... Sunday) for yyyy-mm-dd string.
 */
jarz_pos.delivery_slots.getDayName = function(dateStr){
    const d = new Date(dateStr + 'T00:00:00');
    return d.toLocaleDateString('en-US', { weekday: 'long' });
};

/**
 * Fetch timetable document for given POS Profile.
 * Returns { slotHours: Number, timetable: Array<childRow> }
 */
jarz_pos.delivery_slots.fetchTimetable = function(profileName){
    return new Promise(function(resolve){
        frappe.call({
            method:'frappe.client.get_list',
            args:{
                doctype:'POS Profile Timetable',
                fields:['name','slot_hours'],
                filters:{ pos_profile: profileName },
                limit:1
            },
            callback:function(r){
                if(r.message && r.message.length){
                    const ttName=r.message[0].name;
                    const slotHours=r.message[0].slot_hours||1;
                    frappe.call({
                        method:'frappe.client.get',
                        args:{doctype:'POS Profile Timetable', name:ttName}
                    }).then(function(res){
                        resolve({ slotHours:Number(slotHours)||1, timetable: res.message.timetable||[] });
                    });
                }else{
                    resolve({ slotHours:1, timetable: [] });
                }
            }
        });
    });
};

/**
 * Build available slot objects for a given date string yyyy-mm-dd using timetable info.
 * Each slot: { value: ISODate string of start, label: "HH:MM - HH:MM" }
 */
jarz_pos.delivery_slots.buildSlots = function(dateStr, timetableInfo){
    const slots=[];
    const now=new Date();
    const dateObj=new Date(dateStr+'T00:00:00');
    const dayName=jarz_pos.delivery_slots.getDayName(dateStr);
    let row = (timetableInfo.timetable||[]).find(r=>r.day===dayName);
    // defaults
    let openTime='09:00:00', closeTime='22:00:00';
    if(row){
        openTime=row.opening_time||openTime;
        closeTime=row.closing_time||closeTime;
    }
    const slotH = Number(timetableInfo.slotHours)||1;
    const [openH,openM] = openTime.split(':').map(Number);
    const [closeH,closeM] = closeTime.split(':').map(Number);
    const start=new Date(dateObj); start.setHours(openH,openM||0,0,0);
    let end=new Date(dateObj); end.setHours(closeH,closeM||0,0,0);
    if(end<=start){ end.setDate(end.getDate()+1); } // cross midnight
    for(let cursor=new Date(start); (cursor.getTime()+slotH*3600*1000)<=end.getTime(); cursor=new Date(cursor.getTime()+slotH*3600*1000)){
        if(cursor>now){
            const slotEnd=new Date(cursor.getTime()+slotH*3600*1000);
            // Format time using moment.js (12-hour h:mm A format)
            const formatTime = dt => moment(dt).format('h:mm A');
            const label = `${formatTime(cursor)} - ${formatTime(slotEnd)}`;
            // Use naive datetime string without timezone for backend compatibility
            const value = moment(cursor).format('YYYY-MM-DD HH:mm:ss');
            slots.push({ value, label });
        }
    }
    return slots;
};

/**
 * Show dialog, store window.SelectedDeliverySlot, then call cb.
 */
jarz_pos.delivery_slots.prompt = function(profileName, cb){
    jarz_pos.delivery_slots.fetchTimetable(profileName).then(function(tt){
        // Build set of allowed weekdays from timetable rows (e.g. ['Monday','Tuesday'])
        const allowedDays = (tt.timetable || []).map(r => r.day);
        // Helper: get next allowed date >= given date (string yyyy-mm-dd)
        function nextAllowedDate(startDateStr){
            // Returns next date (YYYY-MM-DD) matching allowedDays starting from given system date string
            let d = new Date(startDateStr+'T00:00:00');
            if(isNaN(d)){ d = new Date(); }
            for(let i=0;i<60;i++){
                const dayName = d.toLocaleDateString('en-US',{weekday:'long'});
                if(!allowedDays.length || allowedDays.includes(dayName)){
                    return moment(d).format('YYYY-MM-DD');
                }
                d.setDate(d.getDate()+1);
            }
            return moment(d).format('YYYY-MM-DD');
        }

        const today = moment().format('YYYY-MM-DD');

        // Build list of next 5 allowed dates with available slots
        const dateOptions = [];
        let probeDate = today;
        while (dateOptions.length < 5) {
            probeDate = nextAllowedDate(probeDate);
            const s = jarz_pos.delivery_slots.buildSlots(probeDate, tt);
            if (s.length) {
                const label = moment(probeDate).format('ddd, DD MMM YYYY');
                dateOptions.push({value: probeDate, label});
            }
            // Move probeDate +1 day to avoid infinite loop
            probeDate = moment(probeDate).add(1, 'day').format('YYYY-MM-DD');
            if (dateOptions.length >= 10) break; // safety
        }

        const initial = dateOptions.length ? dateOptions[0].value : today;
        let slots = jarz_pos.delivery_slots.buildSlots(initial, tt);

        // Helper to build Frappe Select options (label|value per line)
        function opts(arr){ return arr.map(o=>`${o.label}|${o.value}`).join('\n'); }
        function slotOpts(arr){ return arr.map(s=>`${s.label}|${s.value}`).join('\n'); }

        const firstOpt = dateOptions.length ? `${dateOptions[0].label}|${dateOptions[0].value}` : initial;

        const dlg = new frappe.ui.Dialog({
            title: 'Select Delivery Slot',
            fields: [
                { fieldname:'date', fieldtype:'Select', label:'Delivery Date', reqd:1, options: opts(dateOptions), default: firstOpt },
                { fieldname:'slot', fieldtype:'Select', label:'Time Slot', reqd:1, options: slotOpts(slots) }
            ],
            primary_action_label:'Confirm',
            primary_action:function(values){
                window.SelectedDeliverySlot=values.slot;
                dlg.hide();
                if(typeof cb==='function') cb();
            }
        });
        dlg.fields_dict.date.df.on_change = function(){
            let raw = dlg.get_value('date') || '';
            const ds = raw.split('|').pop(); // extract yyyy-mm-dd
            const dn = jarz_pos.delivery_slots.getDayName(ds);
            if (!ds) return;
            if (allowedDays.length && !allowedDays.includes(dn)) {
                frappe.msgprint(`Selected date (${dn}) is not available for delivery.`);
                return;
            }
            const sl = jarz_pos.delivery_slots.buildSlots(ds, tt);
            dlg.set_df_property('slot','options', slotOpts(sl));
            dlg.set_value('slot', sl.length ? sl[0].label+'|'+sl[0].value : '');
        };

        dlg.show();
    });
}; 