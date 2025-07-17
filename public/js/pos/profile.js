frappe.provide('jarz_pos.profile');

jarz_pos.profile.loadPOSProfile = function(callback) {
	console.log("Loading POS Profiles for current user...");

	frappe.call({
		method: 'frappe.client.get_list',
		args: {
			doctype: 'POS Profile',
			fields: ['name'],
			filters: {
				disabled: 0
			},
			limit: 50
		},
		callback: function(r) {
			var profiles = r.message || [];
			console.log("All POS Profiles:", profiles);

			if (profiles.length === 0) {
				callback(null);
				return;
			}

			// Load full details for each profile to check user access
			var loadProfilePromises = profiles.map(function(profile) {
				return frappe.call({
					method: 'frappe.client.get',
					args: {
						doctype: 'POS Profile',
						name: profile.name
					}
				}).then(function(result) {
					return result.message;
				}).catch(function(err) {
					console.error("Error loading profile:", profile.name, err);
					return null;
				});
			});

			Promise.all(loadProfilePromises).then(function(fullProfiles) {
				// Filter out null results and check user access
				var accessibleProfiles = fullProfiles.filter(function(profile) {
					if (!profile) return false;

					// A profile is accessible only when the current user is explicitly
					// listed in its "Applicable for Users" child table.
					if (Array.isArray(profile.applicable_for_users) && profile.applicable_for_users.length > 0) {
						return profile.applicable_for_users.some(function(userRow) {
							return userRow.user === frappe.session.user;
						});
					}

					// Profiles without any user specified are NOT accessible.
					return false;
				});

				console.log("Accessible POS Profiles:", accessibleProfiles.map(function(p) { return p.name; }));

				if (accessibleProfiles.length === 0) {
					callback(null);
					return;
				}

				if (accessibleProfiles.length === 1) {
					// Use the single accessible profile
					window.JarzPOSProfile = accessibleProfiles[0];
					callback(accessibleProfiles[0]);
				} else {
					// Show profile selection modal
					jarz_pos.profile.showPOSProfileSelection(accessibleProfiles, (selectedProfile) => {
						window.JarzPOSProfile = selectedProfile;
						callback(selectedProfile);
					});
				}
			}).catch(function(err) {
				console.error("Error loading profile details:", err);
				callback(null);
			});
		},
		error: function(err) {
			console.error("Error loading POS Profiles:", err);
			callback(null);
		}
	});
}

jarz_pos.profile.showPOSProfileSelection = function(profiles, callback) {
	var dialog = new frappe.ui.Dialog({
		title: 'Select POS Profile',
		fields: [
			{
				fieldname: 'profile',
				fieldtype: 'Select',
				label: 'POS Profile',
				options: profiles.map(function(p) { return p.name; }).join('\n'),
				reqd: 1,
				description: 'Select the POS profile to use for this session'
			}
		],
		primary_action_label: 'Select',
		primary_action: function(values) {
			if (values.profile) {
				var selectedProfile = profiles.find(function(p) { return p.name === values.profile; });
				if (selectedProfile) {
					callback(selectedProfile);
					dialog.hide();
				}
			}
		}
	});

	dialog.show();
}
