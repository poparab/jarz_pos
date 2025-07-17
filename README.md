# Jarz POS - Advanced Point of Sale System for ERPNext

A comprehensive, touch-optimized Point of Sale (POS) system built specifically for Jarz company, featuring advanced bundle management, real-time inventory tracking, intelligent delivery management, enhanced cart functionality, and seamless ERPNext integration.

## 🚀 Latest Features & Enhancements

### ✨ **Enhanced Cart Management**
- **Remove Items**: One-click removal of individual items and bundles from cart
- **Edit Bundles**: Modify bundle contents after adding to cart with live pricing updates
- **Confirmation Dialogs**: Prevent accidental deletions with confirmation prompts
- **Real-time Updates**: Cart automatically updates with new totals after changes

### 🎯 **Smart Customer Search**
- **Recent Customers**: Automatically shows last 5 customers when search field is focused
- **Smart Date Display**: Shows when customers were added (Today, Yesterday, X days ago)
- **No Typing Required**: Quick access to recent customers without typing
- **Intelligent Search**: Enhanced search with contact information display

### 💰 **Advanced Delivery Management**
- **Expense Editing**: Modify delivery expenses during checkout with quick dialog
- **Dual Display**: Shows both delivery income (customer charge) and expense (our cost)
- **Clean Interface**: Expense editing doesn't clutter the main POS interface
- **Profit Tracking**: Clear visibility of delivery profit margins

## 🏪 Core POS Functionality

### Advanced Point of Sale Features
- **POS Profile Integration**: Automatic warehouse and price list selection based on user permissions
- **Real-time Inventory**: Live stock levels with color-coded indicators (Green/Yellow/Red)
- **Dynamic Pricing**: Automatic price fetching from configured price lists
- **Item Group Organization**: Items organized by categories for easy navigation
- **Customer Management**: Enhanced search, selection, and creation with delivery address support
- **Full-screen Mode**: Toggle full-screen experience with ESC key support

### Sophisticated Bundle System
- **Complex Bundle Configuration**: Support for multi-group item bundles with quantity requirements
- **Interactive Bundle Selection**: Touch-friendly modal with inventory validation and live editing
- **Bundle Pricing**: Automatic discount calculation showing real savings
- **Hierarchical Cart Display**: Organized bundle presentation with editing capabilities
- **Bundle Editing**: Modify bundle contents after adding to cart with validation
- **ERPNext Integration**: Seamless sales invoice creation with proper item mapping

### Intelligent Delivery Management
- **City-based Delivery**: Configure delivery charges and expenses per city
- **Dynamic Delivery Pricing**: Real-time delivery charge calculation from customer addresses
- **Editable Delivery Expenses**: Modify delivery expenses on-the-fly during sales
- **Address Integration**: Automatic delivery loading from customer address city information
- **Dual Accounting**: Delivery income as tax charges, expenses as invoice discounts

### Touch-Optimized Experience
- **Full-screen POS Interface**: Clean, sidebar-free interface with toggle support
- **Responsive Design**: Optimized for tablets, touch screens, and desktop
- **Touch-friendly Interactions**: Large buttons, intuitive gestures, and quick actions
- **Real-time Updates**: Live inventory, pricing, delivery costs, and cart updates
- **Enhanced Search**: Smart pre-filling and recent customer quick access

## 📦 Installation

### Prerequisites
- ERPNext v13/v14/v15
- Frappe Framework
- Access to ERPNext site with administrator privileges

### Installation Steps

1. **Clone the app**:
   ```bash
   cd /path/to/your/frappe-bench
   bench get-app https://github.com/your-username/jarz_pos.git
   ```

2. **Install the app on your site**:
   ```bash
   bench --site your-site-name install-app jarz_pos
   ```

3. **Migrate database** (for City doctype):
   ```bash
   bench --site your-site-name migrate
   ```

4. **Restart the bench**:
```bash
   bench restart
   ```

## 🚀 Production Deployment

Below is a **copy-paste friendly checklist** for taking Jarz POS live on a fresh server. It assumes you already have SSH access and basic Linux administration rights.

### Prerequisites
• Ubuntu 20.04/22.04 (or Debian 12) with at least **2 vCPU / 4 GB RAM** (8 GB recommended)  
• `bench` ≥ 5, Node 18, Yarn, Redis, MariaDB 10.6, wkhtmltopdf 0.12.6  
• ERPNext / Frappe Framework **v15** codebase (same branch used in development)

### 1 – Prepare the server (one-time)
```bash
# As root or a sudo user
sudo apt update && sudo apt install git python3-pip -y
pip3 install --upgrade frappe-bench

# Create the bench directory and install Frappe
bench init --frappe-branch version-15 ~/frappe-bench
cd ~/frappe-bench
```

### 2 – Create site & install ERPNext
```bash
bench new-site your-site.com \
    --mariadb-root-password <MYSQL_ROOT> \
    --admin-password <ADMIN_PASS>

# (Skip if ERPNext already installed)
bench get-app erpnext --branch version-15
bench --site your-site.com install-app erpnext
```

### 3 – Install **Jarz POS**
```bash
# Pull the app source
bench get-app jarz_pos https://github.com/your-username/jarz_pos.git

# Install on your production site
bench --site your-site.com install-app jarz_pos

# Run database migrations & automatic patches (custom fields etc.)
bench --site your-site.com migrate
```

### 4 – Build assets & switch to production
```bash
# Compile JS/CSS for production (uses Node/Yarn)
bench build --production

# Generate Supervisor + Nginx configs and start services under supervisor
sudo bench setup production frappe
sudo bench restart
```

### 5 – Post-install checklist
1. **POS Profile** – Create at `Setup › Point of Sale › POS Profile`, set warehouse, price list, and assign users.  
2. **Delivery Cities** – Add records under `Jarz POS › City` with income & expense amounts.  
3. **Verify Custom Fields** – `required_delivery_datetime` & `sales_invoice_state` should now appear on *Sales Invoice* (patch runs automatically).  
4. **Test POS** – Browse to `https://your-site.com/app/custom-pos` and complete a test sale.  
5. *(Optional)* Enable HTTPS with Let’s Encrypt: `sudo bench setup lets-encrypt your-site.com`.

### 6 – Upgrading Jarz POS later
```bash
cd ~/frappe-bench/apps/jarz_pos
git pull
bench --site your-site.com migrate
bench build --production && bench restart
```

That’s it—Jarz POS is now running in production. Happy selling! 🚀

## ⚙️ Configuration

### 1. Create POS Profile
Navigate to: `Setup > Point of Sale > POS Profile`

Create a new POS Profile with:
- **Name**: "Jarz POS Profile" (or your preferred name)
- **Warehouse**: Select your main warehouse for inventory tracking
- **Selling Price List**: Select your selling price list (e.g., "Standard Selling")
- **Applicable for Users**: Add users who should have access to this POS
- **Item Groups**: Select the item groups you want to display in the POS
- **Payment Methods**: Configure at least one payment method (required for POS invoices)

### 2. Configure Delivery Cities
Navigate to: `Jarz POS > City`

Create delivery cities with:
- **City Name**: Name of the delivery city
- **Delivery Income**: Amount charged to customer for delivery
- **Delivery Expense**: Actual cost/expense for delivery to this city

Example:
```
City: Downtown Riyadh
Delivery Income: $10.00
Delivery Expense: $3.00
Net Delivery Profit: $7.00
```

### 3. Configure Item Groups
Ensure your items are properly categorized into Item Groups:
- Navigate to: `Stock > Setup > Item Group`
- Create/organize item groups as needed
- Add these groups to your POS Profile

### 4. Set Up Item Prices
Ensure all items have prices in your configured price list:
- Navigate to: `Stock > Item Price`
- Create item prices for your selling price list
- Alternatively, set standard selling rates on items

### 5. Configure Bundles (Optional)
To use the advanced bundle feature:
- Navigate to: `Jarz POS > Jarz Bundle`
- Create bundle configurations with:
  - Bundle name and price
  - Item groups with required quantities
  - **ERPNext Item**: Link to an ERPNext item that represents this bundle in sales invoices
  - Bundle items and pricing

**Important**: Each bundle must have an `erpnext_item` field linking to a valid ERPNext Item. This item will be used when creating sales invoices for bundle purchases.

### 6. Configure Accounts for Delivery (Important)
Ensure your Chart of Accounts has appropriate accounts for delivery:
- **Freight and Forwarding Charges**: For delivery income/expense tracking
- **Miscellaneous Expenses**: Fallback account for delivery expenses
- The system will automatically find and use appropriate accounts

## 🖥️ Usage

### Accessing the POS
1. Navigate to: `/app/custom-pos` in your ERPNext site
2. Select POS Profile (if multiple profiles are available)
3. The POS interface will load with your configured items and settings

### POS Interface Overview
- **Top Bar**: 
  - Current POS Profile info (name, warehouse, price list)
  - Full-screen toggle button
- **Left Panel (75%)**: 
  - Bundles section (if configured)
  - Items organized by item groups with inventory indicators
- **Right Panel (25%)**:
  - Smart customer search with recent customers display
  - Shopping cart with enhanced management features
  - Delivery information with expense editing
  - Checkout button

### Enhanced Customer Management
- **Recent Customer Display**: Last 5 customers shown when field is focused
- **Smart Search**: Type customer name, mobile, or email to search
- **Quick Access**: Select recent customers without typing
- **Smart Pre-filling**: 
  - Numbers only → Pre-fills Mobile Number field
  - Letters (Arabic/English) → Pre-fills Customer Name field
- **Address Integration**: Automatic delivery charge loading from customer addresses
- **Create New**: Use "+ New" button to create customers with delivery address

### Advanced Cart Features
- **Remove Items**: Click "Remove" button on any cart item with confirmation
- **Edit Bundles**: Click "Edit" button on bundles to modify contents after adding
- **Live Updates**: Cart totals update automatically after changes
- **Bundle Editing**: Full bundle reconfiguration with validation and pricing updates
- **Delivery Management**: View and edit delivery expenses directly in cart

### Delivery Management
- **Address-based Delivery**: Delivery charges determined from customer's address city
- **Automatic Calculation**: Delivery costs automatically added when customer selected
- **Expense Editing**: Click "Edit Expense" to modify delivery costs during checkout
- **Dual Display**: Shows both customer charge and our expense
- **Dynamic Loading**: Delivery charges loaded when customer is selected

### Adding Items to Cart
- **Individual Items**: Click on any item card to add to cart
- **Bundles**: Click on bundle card, select required items, then add to cart
- **Inventory Validation**: Out-of-stock items cannot be added (red indicators)
- **Price Display**: Shows prices from configured price list
- **Bundle Inventory**: Real-time inventory checking for bundle items

### Bundle Selection & Editing Process
1. **Initial Selection**: Click on any bundle card
2. **Modal Interface**: Interactive modal with item groups and requirements
3. **Item Selection**: Select required quantity from each group with inventory validation
4. **Visual Feedback**: Blue highlighting, quantity badges, remove buttons
5. **Add to Cart**: Complete selection and add bundle to cart
6. **Edit in Cart**: Click "Edit" button to modify bundle contents
7. **Live Updates**: Bundle pricing and savings update automatically

### Enhanced Checkout Process
1. **Add Items**: Add individual items and/or bundles to cart
2. **Select Customer**: Choose from recent customers or search/create new
3. **Automatic Delivery**: Delivery charges loaded from customer's address city
4. **Edit Delivery**: Modify delivery expenses if needed using "Edit Expense"
5. **Review Cart**: View items, bundles, delivery charges, and totals
6. **Remove/Edit**: Make any last-minute changes to cart contents
7. **Click Checkout**: System creates and submits sales invoice automatically
8. **Invoice Success**: Cart clears, success dialog with print option

### Invoice Structure with Enhanced Delivery
```
Items Total: $50.00
+ Delivery Charge: $10.00 (Tax - Customer pays)
= Subtotal: $60.00
+ Discount Amount: -$3.00 (Delivery Expense - Our cost)
= Grand Total: $57.00 (Net profit: $7.00 delivery)
= Paid Amount: $57.00
```

## 🎨 User Interface Features

### Recent Customer Display
```
[+ Add New Customer]
Create a new customer

📅 Recent Customers
Ahmad Al-Hassan          Today
+966501234567 • ahmad@email.com

Sarah Johnson            Yesterday  
sarah@company.com • +1234567890

Mohamed Ali              3 days ago
+966509876543
```

### Enhanced Cart Display
```
📦 Combo Meal Bundle     [Edit] [Remove]
Bundle Price: $15.00  Save $5.00
├── Main Course: Burger × 1
├── Side: Fries × 1
└── Drink: Coke × 1

Coffee × 2               [Remove]  
$3.50 each = $7.00

🚚 Delivery: Downtown    [Edit Expense]
Delivery Charge: $10.00
Our Expense: $3.00

Total: $32.00
```

## 🛠️ Development

### File Structure
```
jarz_pos/
├── jarz_pos/
│   ├── doctype/
│   │   ├── city/          # City delivery configuration
│   │   └── jarz_bundle/   # Bundle management
│   ├── page/
│   │   └── custom_pos/    # Main POS page
│   ├── public/
│   │   └── js/
│   │       └── custom_pos.js  # Enhanced POS logic
│   └── hooks.py           # App configuration
├── README.md              # This documentation
├── FIXES_APPLIED.md       # Detailed fix documentation
└── requirements.txt       # Python dependencies
```

### Key Functions
- `loadRecentCustomers()` - Smart customer search
- `addCartEventHandlers()` - Cart management
- `editBundleInCart()` - Bundle editing
- `editDeliveryExpense()` - Delivery cost management
- `validateAndUpdateBundle()` - Bundle validation and updates

### Testing
Run comprehensive tests:
```bash
bench run-tests --app jarz_pos
```

## 🔧 Troubleshooting

### Common Issues

1. **City Dropdown Not Showing**
   - Ensure City doctype exists and has records
   - Verify Link field configuration in customer creation

2. **Delivery Charges Not Loading**
   - Check customer has address with valid city
   - Verify city configuration has delivery income/expense

3. **Bundle Issues**
   - Ensure bundles have valid `erpnext_item` field
   - Check item group configurations in bundle setup

4. **Cart Management Not Working**
   - Verify JavaScript console for errors
   - Check cart event handlers are properly attached

## 📈 Performance

- **Optimized Loading**: Parallel API calls for inventory and pricing
- **Smart Caching**: Recent customer caching for faster access
- **Real-time Updates**: Efficient cart rendering with minimal DOM updates
- **Touch Optimization**: Debounced search and touch-friendly interactions

## 🔒 Security

- **User Permissions**: POS Profile controls user access
- **Data Validation**: Server-side validation for all transactions
- **Inventory Checks**: Real-time inventory validation prevents overselling
- **Address Verification**: Proper address linking and validation

## 📞 Support

For issues, feature requests, or support:
- Create issues on GitHub repository
- Check `FIXES_APPLIED.md` for recent fixes and solutions
- Review console logs for debugging information

---

## 🎯 Recent Updates

### v2.1.0 - Enhanced Cart & Customer Management
- ✅ Added remove functionality for cart items and bundles
- ✅ Implemented bundle editing in cart with live updates
- ✅ Added delivery expense editing during checkout
- ✅ Enhanced customer search with recent customers display
- ✅ Improved cart management with confirmation dialogs
- ✅ Added smart date display for recent customers

### v2.0.0 - Delivery System Overhaul  
- ✅ Fixed city dropdown implementation
- ✅ Resolved server errors in address lookup
- ✅ Unified delivery management through customer addresses
- ✅ Enhanced error handling and debugging capabilities

The Jarz POS system provides a complete, enterprise-grade point of sale solution with advanced features tailored for modern retail operations.
\n\n---\n\nFor day-to-day POS operation instructions, refer to **USAGE.md**.
