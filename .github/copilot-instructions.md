# Jarz POS Project - Copilot Instructions

## Project Overview
This workspace contains a complete POS system with two main components:
1. **Backend/API**: ERPNext custom app at `C:\ERPNext\frappe_docker\development\frappe-bench\apps\jarz_pos\jarz_pos`
2. **Frontend**: Flutter mobile app at `C:\ERPNext\jarz_pos_mobile\jarz_pos`

## Backend (ERPNext Custom App) Structure

### API Endpoints Location
**Primary API directory**: `C:\ERPNext\frappe_docker\development\frappe-bench\apps\jarz_pos\jarz_pos\api\`

API modules:
- `pos.py` - POS profiles, bundles, and configuration
- `invoices.py` - Sales invoice creation and payment processing
- `couriers.py` - Courier management and outstanding balances
- `customer.py` - Customer-related operations

### Backend File Organization
```
jarz_pos/
├── api/           # REST API endpoints (ADD NEW APIS HERE)
├── jarz_pos/      # Main app logic and DocTypes
│   └── doctype/   # CRITICAL: Custom DocTypes (database models)
├── page/          # Legacy page controllers
├── hooks.py       # App configuration and event hooks
├── fixtures/      # Data fixtures
├── templates/     # Email/print templates
└── www/           # Web assets (if any)
```

### Custom DocTypes (Database Models) - CRITICAL
**DocTypes Location**: `C:\ERPNext\frappe_docker\development\frappe-bench\apps\jarz_pos\jarz_pos\jarz_pos\doctype\`

**Available Custom DocTypes**:
- `courier/` - Courier management and information
- `courier_transaction/` - Courier transaction records
- `custom_settings/` - Application-specific settings
- `jarz_bundle/` - Product bundle definitions
- `jarz_bundle_item_group/` - Bundle item group configurations
- `city/` - City/location management
- `pos_profile_day_timing/` - POS profile timing settings
- `pos_profile_timetable/` - POS profile schedule management

Each DocType contains:
- `{doctype}.json` - Field definitions and metadata
- `{doctype}.py` - Python controller with business logic
- `{doctype}.js` - Client-side JavaScript (if needed)

### API Development Patterns
- All API methods use `@frappe.whitelist(allow_guest=False)`
- API endpoints follow pattern: `/api/method/jarz_pos.api.{module}.{function}` for modules under `jarz_pos/api/`
- Service endpoints under `jarz_pos/services/` are accessed as `/api/method/jarz_pos.jarz_pos.services.{module}.{function}`
- Use `frappe.get_all()`, `frappe.get_doc()` for database operations
- Return plain Python objects (dicts/lists) - Frappe handles JSON serialization
- Raise errors with `frappe.throw("Error message")`

### When to Add Backend Code
- **New API endpoints**: Add to appropriate file in `api/` directory
- **Business logic**: Add to `jarz_pos/` directory as DocType methods
- **Database schema**: Use Frappe DocTypes in `jarz_pos/doctype/` directory
- **Custom DocType modifications**: Edit `.py` files in `jarz_pos/doctype/{doctype_name}/`
- **Database field changes**: Modify `.json` files in DocType directories
- **Client-side DocType behavior**: Add/edit `.js` files in DocType directories
- **Background jobs**: Add to hooks.py scheduler events
- **Custom fields**: Use fixtures or migrations

## Frontend (Flutter App) Structure

### Main Application Structure
```
lib/
├── main.dart           # App entry point
└── src/
    ├── core/           # Core app infrastructure
    │   ├── app.dart    # Main app widget
    │   ├── router.dart # Navigation routing
    │   ├── network/    # HTTP client and API services
    │   └── session/    # Authentication and session management
    └── features/       # Feature-based organization
        ├── auth/       # Authentication screens and logic
        └── pos/        # POS functionality
```

### Flutter Development Patterns
- Use **Riverpod** for state management
- Environment variables in `.env` file
- Landscape-only orientation (configured in main.dart)
- Feature-based architecture in `lib/src/features/`
- Network layer in `lib/src/core/network/`

### When to Add Frontend Code
- **New screens**: Add to appropriate feature directory under `features/`
- **API integration**: Add service classes in `core/network/`
- **State management**: Add providers in relevant feature directories
- **Navigation**: Update `router.dart`
- **Authentication**: Modify `core/session/`

## Cross-Project Integration

### API Communication
- Flutter app communicates with ERPNext via REST API
- Base URL configured in Flutter `.env` file
- Authentication handled through ERPNext session cookies
- API modules: `/api/method/jarz_pos.api.*`
- Service modules (Python under services/): `/api/method/jarz_pos.jarz_pos.services.*`

### Development Workflow
1. **Backend First**: Implement API endpoints in `jarz_pos/api/`
2. **Test API**: Use Frappe's built-in API explorer or Postman
3. **Frontend Integration**: Create service classes in Flutter `core/network/`
4. **UI Implementation**: Add screens and state management in `features/`

## File Editing Guidelines

### For Backend Changes
- **API modifications**: Edit files in `C:\ERPNext\frappe_docker\development\frappe-bench\apps\jarz_pos\jarz_pos\api\`
- **DocType changes**: Edit files in `C:\ERPNext\frappe_docker\development\frappe-bench\apps\jarz_pos\jarz_pos\jarz_pos\doctype\{doctype_name}\`
- **Configuration**: Edit `hooks.py` for app-level configuration
- **Database model changes**: Modify DocType `.json` files for field definitions
- **Business logic**: Add methods to DocType `.py` controllers

### For Frontend Changes
- **UI/Screens**: Edit files in `C:\ERPNext\jarz_pos_mobile\jarz_pos\lib\src\features\`
- **API services**: Edit files in `C:\ERPNext\jarz_pos_mobile\jarz_pos\lib\src\core\network\`
- **App configuration**: Edit `pubspec.yaml` for dependencies, `.env` for environment variables

### Common Operations
- **Adding new API endpoint**: Create function in appropriate `api/*.py` file
- **Adding new screen**: Create widget in appropriate `features/*/` directory
- **Database changes**: Add/modify DocTypes in backend `jarz_pos/doctype/` directory
- **New DocType creation**: Use `bench make-doctype` command in backend
- **DocType field modifications**: Edit `.json` files in respective DocType directories
- **Authentication changes**: Modify both backend session handling and frontend `core/session/`

## Environment Setup
- **Backend**: Runs in Frappe Docker container with bench commands
- **Frontend**: Standard Flutter development environment
- **Testing**: Backend uses Frappe test framework, Flutter uses Flutter test
- **Flutter Device**: Always run Flutter app on Android device ID `192.168.1.14:5555`
  - Use command: `flutter run -d 192.168.1.14:5555 --dart-define=ENV=staging` to make it run on the staging environment

## Key Dependencies
- **Backend**: Frappe Framework, ERPNext
- **Frontend**: Flutter, Riverpod, flutter_dotenv, http package

Remember: Always implement backend API first, then integrate with Flutter frontend. The backend serves as the single source of truth for business logic and data.

when you want to restart the backend, use: bench restart
when you want to restart the frontend, use: R in the terminal to restart the Flutter app

don't create multiple test files for the same purpose, consolidate them into one. only create specific test files for significantly different functionalities.