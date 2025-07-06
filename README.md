# Jarz POS - Custom Point of Sale System for ERPNext

A comprehensive Point of Sale (POS) system built specifically for Jarz company, featuring advanced bundle management, real-time inventory tracking, and seamless ERPNext integration.

## 🚀 Features

### Core POS Functionality
- **POS Profile Integration**: Automatic warehouse and price list selection based on user permissions
- **Real-time Inventory**: Live stock levels with color-coded indicators (Green/Yellow/Red)
- **Dynamic Pricing**: Automatic price fetching from configured price lists
- **Item Group Organization**: Items organized by categories for easy navigation
- **Customer Management**: Search, select, and create customers with address support

### Advanced Bundle System
- **Complex Bundle Configuration**: Support for multi-group item bundles with quantity requirements
- **Interactive Bundle Selection**: Touch-friendly modal with inventory validation
- **Bundle Pricing**: Automatic discount calculation showing savings
- **Hierarchical Cart Display**: Organized bundle presentation in cart

### Touch-Optimized Interface
- **Full-screen POS Experience**: Clean, sidebar-free interface
- **Responsive Design**: Works on tablets, touch screens, and desktop
- **Touch-friendly Interactions**: Large buttons and intuitive gestures
- **Real-time Updates**: Live inventory and pricing updates

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

3. **Restart the bench**:
   ```bash
   bench restart
   ```

## ⚙️ Configuration

### 1. Create POS Profile
Navigate to: `Setup > Point of Sale > POS Profile`

Create a new POS Profile with:
- **Name**: "Jarz POS Profile" (or your preferred name)
- **Warehouse**: Select your main warehouse for inventory tracking
- **Selling Price List**: Select your selling price list (e.g., "Standard Selling")
- **Applicable for Users**: Add users who should have access to this POS
- **Item Groups**: Select the item groups you want to display in the POS

### 2. Configure Item Groups
Ensure your items are properly categorized into Item Groups:
- Navigate to: `Stock > Setup > Item Group`
- Create/organize item groups as needed
- Add these groups to your POS Profile

### 3. Set Up Item Prices
Ensure all items have prices in your configured price list:
- Navigate to: `Stock > Item Price`
- Create item prices for your selling price list
- Alternatively, set standard selling rates on items

### 4. Configure Bundles (Optional)
To use the advanced bundle feature:
- Navigate to: `Jarz POS > Jarz Bundle`
- Create bundle configurations with:
  - Bundle name and price
  - Item groups with required quantities
  - **ERPNext Item**: Link to an ERPNext item that represents this bundle in sales invoices
  - Bundle items and pricing

**Important**: Each bundle must have an `erpnext_item` field linking to a valid ERPNext Item. This item will be used when creating sales invoices for bundle purchases.

## 🖥️ Usage

### Accessing the POS
1. Navigate to: `/app/custom-pos` in your ERPNext site
2. Select POS Profile (if multiple profiles are available)
3. The POS interface will load with your configured items and settings

### POS Interface Overview
- **Top Bar**: Shows current POS Profile, warehouse, and price list
- **Left Panel (75%)**: 
  - Bundles section (if configured)
  - Items organized by item groups
- **Right Panel (25%)**:
  - Customer search and selection
  - Shopping cart with hierarchical bundle display
  - Checkout button

### Adding Items to Cart
- **Individual Items**: Click on any item card to add to cart
- **Bundles**: Click on bundle card, select required items from each group, then add bundle to cart
- **Inventory Validation**: Out-of-stock items cannot be added
- **Price Display**: Shows prices from configured price list

### Customer Management
- **Search**: Type customer name, mobile, or email to search
- **Select**: Click on customer from dropdown to select
- **Create New**: Use "+ New" button to create customers with address details
- **Clear**: Remove selected customer to start fresh

### Bundle Selection Process
1. Click on any bundle card
2. Modal opens showing item groups
3. Select required quantity from each group
4. Items show real-time inventory and pricing
5. Complete selection and add bundle to cart
6. Bundle appears in cart with hierarchical structure and savings display

### Checkout Process
1. **Add Items**: Add individual items and/or bundles to cart
2. **Select Customer**: Choose existing customer or create new one
3. **Click Checkout**: System automatically creates and submits sales invoice
4. **Invoice Creation**: 
   - Regular items are added directly to invoice
   - Bundles use the configured `erpnext_item` for invoice line items
   - Proper pricing, taxes, and inventory updates are applied
5. **Success**: Cart is cleared, success dialog shown with invoice details
6. **Print**: Option to print invoice or start new sale

## 🔧 Development

### File Structure
```
jarz_pos/
├── jarz_pos/
│   ├── jarz_pos/
│   │   ├── jarz_pos/
│   │   │   ├── doctype/
│   │   │   │   ├── jarz_bundle/           # Bundle configuration
│   │   │   │   └── jarz_bundle_item_group/ # Bundle item groups
│   │   │   └── page/
│   │   │       └── custom_pos/            # POS page definition
│   │   ├── public/js/
│   │   │   └── custom_pos.js              # Main POS JavaScript logic
│   │   ├── fixtures/                      # DocType fixtures
│   │   └── hooks.py                       # App hooks and configuration
│   ├── setup.py
│   ├── requirements.txt
│   └── README.md
```

### Key Components
- **custom_pos.js**: Main POS interface logic with inventory, pricing, and bundle management
- **Jarz Bundle DocType**: Configuration for bundle products
- **POS Profile Integration**: Automatic configuration based on ERPNext POS Profiles

### Contributing
1. Fork the repository
2. Create a feature branch
3. Make changes and test thoroughly
4. Submit a pull request

### Pre-commit Setup
```bash
cd apps/jarz_pos
pre-commit install
```

## 📋 Requirements

### ERPNext Configuration
- POS Profile with proper warehouse and price list configuration
- Item Price records for your selling price list
- Customer records (or ability to create new ones)
- Item Groups properly configured
- Inventory records (Bin doctype) for warehouse

### Browser Compatibility
- Modern browsers with JavaScript ES6+ support
- Touch screen support for optimal experience
- Minimum screen resolution: 1024x768

## 🐛 Troubleshooting

### Common Issues

**1. "No POS Profile available" Error**
- Ensure you have created a POS Profile
- Check that current user is added to "Applicable for Users"
- Verify the profile is enabled

**2. Items showing $0.00 prices**
- Check Item Price records exist for your price list
- Verify price list name matches POS Profile configuration
- Ensure items have standard selling rates as fallback

**3. Inventory not showing correctly**
- Verify warehouse in POS Profile matches actual inventory location
- Check Bin records exist for items in the specified warehouse
- Ensure stock transactions are properly posted

**4. Bundle modal not working**
- Verify Jarz Bundle doctype is properly installed
- Check bundle configuration has item groups and quantities
- Ensure items exist in the configured item groups

## 📄 License

MIT License - see LICENSE file for details

## 🤝 Support

For support and questions:
- Create an issue on GitHub
- Check ERPNext community forums
- Review the troubleshooting section above

---

**Built with ❤️ for Jarz Company using ERPNext and Frappe Framework**
