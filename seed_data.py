import os
import sys
from datetime import date
from decimal import Decimal

import django

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.accounting.models import AutomatedBill, BankAccount, BankTransaction, Expense
from apps.accounts.models import Business, User
from apps.business_settings.models import BusinessPreference, InvoiceSettings, Reminder, ReminderPreference
from apps.items.models import Godown, Item, ItemCategory, ItemGodownStock, StockMovement
from apps.parties.models import Party, PartyCategory
from apps.payments.models import PaymentIn, PaymentOut
from apps.purchases.models import PurchaseInvoice, PurchaseInvoiceItem, PurchaseOrder, PurchaseOrderItem
from apps.sales.models import (
    CreditNote,
    DeliveryChallan,
    DeliveryChallanItem,
    ProformaInvoice,
    ProformaInvoiceItem,
    Quotation,
    QuotationItem,
    SalesInvoice,
    SalesInvoiceItem,
)
from apps.staff.models import Attendance, Payroll, Staff


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


def qty(value):
    return Decimal(str(value)).quantize(Decimal("0.001"))


def set_date(model, pk, field_name, value):
    model.objects.filter(pk=pk).update(**{field_name: value})


def create_user(mobile, business, first_name, role="admin", password="admin123"):
    user, created = User.objects.update_or_create(
        mobile=mobile,
        defaults={
            "username": mobile,
            "business": business,
            "first_name": first_name,
            "role": role,
            "is_active": True,
            "is_staff": role == "admin",
            "is_superuser": role == "admin",
        },
    )
    if created or not user.has_usable_password():
        user.set_password(password)
        user.save(update_fields=["password"])
    return user


def seed_business():
    business, _ = Business.objects.update_or_create(
        phone="8608633066",
        defaults={
            "name": "CSM SILKS",
            "gstin": "33CSUPM1165N1Z4",
            "state": "Tamil Nadu",
            "address": "Kanchipuram silk saree showroom",
            "city": "Kanchipuram",
            "pincode": "631501",
            "email": "accounts@csmsilks.in",
            "invoice_prefix": "CSM",
            "terms_conditions": (
                "Soft pure silk sarees are comfortable to wear and flexible. "
                "Only return/refund policy: no refunds are available for used or damaged products."
            ),
            "upi_id": "csmsilks@upi",
        },
    )
    create_user("8608633066", business, "CSM SILKS")
    create_user("9790001122", business, "Sales Counter", "salesman")
    create_user("9790001133", business, "Accounts", "accountant")
    return business


def seed_second_tenant():
    """Create a clean tenant shell used to verify isolation.

    Production onboarding must start at zero data. This seed tenant intentionally
    has no parties, items, vouchers, godowns, payments, or staff records.
    """
    business, _ = Business.objects.update_or_create(
        phone="9000000001",
        defaults={
            "name": "KANCHI TEXTILES DEMO",
            "gstin": "33AAAAA0000A1Z5",
            "state": "Tamil Nadu",
            "address": "Isolated tenant data",
            "city": "Kanchipuram",
            "pincode": "631502",
            "email": "demo@kanchitextiles.in",
            "invoice_prefix": "KTD",
        },
    )
    create_user("9000000001", business, "Kanchi Admin")


def seed_inventory(business):
    godown_main, _ = Godown.objects.update_or_create(
        business=business,
        name="Main Store",
        defaults={"address": "Main showroom stock room", "is_default": True},
    )
    godown_a, _ = Godown.objects.update_or_create(
        business=business,
        name="Godown-A (Loom Site)",
        defaults={"address": "Kanchipuram Loom Site", "is_default": False},
    )
    godown_b, _ = Godown.objects.update_or_create(
        business=business,
        name="Godown-B (Main Showroom)",
        defaults={"address": "Main Showroom Ground", "is_default": False},
    )

    categories = {}
    for name in ["PURE WEDDING SAREES", "KHADI COTTON", "SOFT SILK SAREES", "ART WEDDING SILKS", "FANCY SILKS"]:
        categories[name], _ = ItemCategory.objects.get_or_create(business=business, name=name)

    rows = [
        ("BAS|C-GFC6-9ATM00", "210191761827", "50072010", "PURE WEDDING SAREES", 17255, 11385, 7245, 1, "Soft pure silks sarees are comfortable to wear with floral zari border."),
        ("GHANTH P|C-B6-1SS5", "207735041989", "50072010", "KHADI COTTON", 1999, 985, 615, 0, "Khadi cotton saree."),
        ("GHANTH P|C-B6G-1SS5", "634109820807", "50072010", "KHADI COTTON", 1999, 985, 615, 0, "Khadi cotton saree."),
        ("GHANTH P|C-G6Y-1SS5", "790582327101", "50072010", "KHADI COTTON", 1999, 985, 615, 0, "Khadi cotton saree."),
        ("GHANTH P|C-M6A-1SS5", "835036501318", "50072010", "KHADI COTTON", 1999, 985, 615, 0, "Khadi cotton saree."),
        ("GHANTH P|C-P6-1SS5", "422171994128", "50072010", "KHADI COTTON", 1999, 985, 615, 0, "Khadi cotton saree."),
        ("GHANTH P|C-P6U-1SS5", "780009370528", "50072010", "KHADI COTTON", 1999, 985, 585.71, 0, "Khadi cotton saree."),
        ("KDI CTN |C-8V-1DH5", "931373864561", "52081190", "KHADI COTTON", 1645, 1345, 795, 0, "Cotton saree."),
        ("SFT SILK |C-RD-7P11", "430977120044", "50072010", "SOFT SILK SAREES", 8995, 6495, 4100, 14, "Soft silk running showroom stock."),
        ("WED SILK |Z-MRN-9X21", "430977120051", "50072010", "ART WEDDING SILKS", 24995, 18995, 12750, 6, "Wedding silk saree with zari work."),
        ("FNC SILK |BLU-4Q82", "430977120068", "50072010", "FANCY SILKS", 5995, 4295, 2750, 18, "Fancy silk saree."),
    ]

    items = {}
    for index, row in enumerate(rows):
        name, code, hsn, category, mrp, sale, purchase, stock, description = row
        item, _ = Item.objects.update_or_create(
            business=business,
            item_code=code,
            defaults={
                "name": name,
                "barcode": code,
                "hsn_code": hsn,
                "category": categories[category],
                "unit": "PCS",
                "selling_price": money(sale),
                "purchase_price": money(purchase),
                "mrp": money(mrp),
                "gst_rate": money(5),
                "tax_inclusive": True,
                "opening_stock": qty(stock),
                "current_stock": qty(stock),
                "low_stock_qty": None,
                "godown": godown_main if index < 8 else godown_b,
                "description": description,
                "is_active": True,
            },
        )
        items[code] = item
        ItemGodownStock.objects.update_or_create(
            business=business,
            item=item,
            godown=item.godown,
            defaults={"opening_stock": item.opening_stock, "current_stock": item.current_stock},
        )
        if stock:
            StockMovement.objects.get_or_create(
                business=business,
                item=item,
                movement_type="opening",
                reference_type="seed",
                reference_id=item.id,
                defaults={
                    "godown": item.godown,
                    "quantity": qty(stock),
                    "rate": item.purchase_price,
                    "balance_after": item.current_stock,
                    "notes": "Seeded opening stock",
                },
            )

    return {"main": godown_main, "a": godown_a, "b": godown_b}, categories, items


def seed_parties(business):
    categories = {}
    for name in ["Retail Customers", "ART WEDDING SILKS", "FANCY SILKS", "Suppliers"]:
        categories[name], _ = PartyCategory.objects.get_or_create(business=business, name=name)

    rows = [
        ("PRAVEEN", "-", "customer", "Retail Customers", 1095, "debit"),
        ("SANGEETHA", "-", "customer", "Retail Customers", 895, "debit"),
        ("SEKAR", "-", "customer", "Retail Customers", 2468, "debit"),
        ("REKHA", "-", "customer", "Retail Customers", 2521, "debit"),
        ("SENTHILKUMAR", "-", "customer", "Retail Customers", 495, "debit"),
        ("9600430083", "9600430083", "customer", "Retail Customers", 0, "debit"),
        ("AARATHI", "-", "customer", "Retail Customers", 800, "debit"),
        ("AARTHI", "-", "customer", "Retail Customers", 2716, "debit"),
        ("ABBORVA", "-", "customer", "Retail Customers", 8100, "debit"),
        ("ABBU", "-", "customer", "Retail Customers", 0, "debit"),
        ("ABDUL", "-", "customer", "Retail Customers", 1269, "debit"),
        ("ABI", "9443249611", "customer", "Retail Customers", 2000, "debit"),
        ("ABILASH", "8838392591", "customer", "Retail Customers", 0, "debit"),
        ("ABIRAMI", "-", "customer", "Retail Customers", 1600, "debit"),
        ("ABIROOPA", "-", "customer", "Retail Customers", 945, "debit"),
        ("ABHISHEK SAREES", "-", "supplier", "ART WEDDING SILKS", 0, "credit"),
        ("AJMERA FASHION PRIVATE LIMITED", "-", "supplier", "FANCY SILKS", 14969, "credit"),
        ("MOORTHY", "-", "supplier", "Suppliers", 10522, "credit"),
        ("PAPPAYE TEXTILES", "-", "supplier", "Suppliers", 18773, "credit"),
        ("SUMANGALI SILK CREATION", "-", "supplier", "Suppliers", 0, "credit"),
    ]

    parties = {}
    for name, mobile, party_type, category, balance, balance_type in rows:
        party, _ = Party.objects.update_or_create(
            business=business,
            name=name,
            defaults={
                "mobile": None if mobile == "-" else mobile,
                "party_type": party_type,
                "category": categories[category],
                "state": "Tamil Nadu",
                "opening_balance": money(balance),
                "opening_balance_type": balance_type,
                "is_active": True,
            },
        )
        parties[name] = party
    return parties


def seed_sales(business, parties, items):
    invoice_rows = [
        ("CSM/26-27/1625", "SENTHILKUMAR", "430977120068", 1, 471.43, 495, 495, "paid", date(2026, 5, 17)),
        ("CSM/26-27/1626", "REKHA", "780009370528", 3, 2401.00, 2521, 0, "unpaid", date(2026, 5, 17)),
        ("CSM/26-27/1627", "SEKAR", "207735041989", 3, 2350.48, 2468, 0, "unpaid", date(2026, 5, 17)),
        ("CSM/26-27/1628", "SANGEETHA", "207735041989", 1, 852.38, 895, 0, "unpaid", date(2026, 5, 17)),
        ("CSM/26-27/1629", "PRAVEEN", "210191761827", 1, 1042.86, 1095, 0, "unpaid", date(2026, 5, 17)),
        ("CSM/26-27/1630", "AARTHI", "430977120051", 1, 18090.48, 18995, 10000, "partial", date(2026, 5, 20)),
        ("CSM/26-27/1631", "ABBORVA", "430977120044", 1, 6185.71, 6495, 0, "unpaid", date(2026, 5, 20)),
    ]

    invoices = {}
    for number, party_name, item_code, count, taxable, total, paid, status, invoice_date in invoice_rows:
        item = items[item_code]
        invoice, _ = SalesInvoice.objects.update_or_create(
            business=business,
            invoice_number=number,
            defaults={
                "party": parties[party_name],
                "subtotal": money(taxable),
                "taxable_amount": money(taxable),
                "cgst_amount": money(float(taxable) * 0.025),
                "sgst_amount": money(float(taxable) * 0.025),
                "igst_amount": money(0),
                "cess_amount": money(0),
                "total_amount": money(total),
                "paid_amount": money(paid),
                "status": status,
                "place_of_supply": "Tamil Nadu",
                "notes": "Seeded from CSM SILKS MyBillBook reference data",
                "terms": business.terms_conditions,
            },
        )
        set_date(SalesInvoice, invoice.id, "invoice_date", invoice_date)
        SalesInvoiceItem.objects.update_or_create(
            invoice=invoice,
            sort_order=0,
            defaults={
                "item": item,
                "item_name": item.name,
                "item_code": item.item_code,
                "hsn_code": item.hsn_code,
                "unit": "PCS",
                "quantity": qty(count),
                "free_quantity": qty(0),
                "mrp": item.mrp,
                "rate": money(total / count),
                "discount_pct": money(0),
                "discount_amount": money(0),
                "gst_rate": money(5),
                "taxable_amount": money(taxable),
                "tax_amount": money(total - taxable),
                "amount": money(total),
            },
        )
        invoices[number] = invoice

    quotation, _ = Quotation.objects.update_or_create(
        business=business,
        quotation_number="38",
        defaults={
            "party": parties["AARATHI"],
            "subtotal": money(13770),
            "total_amount": money(13770),
            "status": "open",
            "notes": "Wedding saree quotation",
        },
    )
    set_date(Quotation, quotation.id, "quotation_date", date(2026, 5, 20))
    QuotationItem.objects.update_or_create(
        quotation=quotation,
        sort_order=0,
        defaults={
            "item": items["430977120044"],
            "item_name": items["430977120044"].name,
            "quantity": qty(2),
            "rate": money(6495),
            "gst_rate": money(5),
            "amount": money(13770),
        },
    )

    challan, _ = DeliveryChallan.objects.update_or_create(
        business=business,
        challan_number="12",
        defaults={
            "party": parties["PRAVEEN"],
            "total_amount": money(11385),
            "status": "open",
            "notes": "Delivery challan for saree dispatch",
        },
    )
    DeliveryChallanItem.objects.update_or_create(
        challan=challan,
        sort_order=0,
        defaults={
            "item": items["210191761827"],
            "item_name": items["210191761827"].name,
            "quantity": qty(1),
            "rate": money(11385),
            "gst_rate": money(5),
            "amount": money(11385),
        },
    )

    proforma, _ = ProformaInvoice.objects.update_or_create(
        business=business,
        proforma_number="9",
        defaults={
            "party": parties["ABIRAMI"],
            "total_amount": money(4295),
            "status": "open",
        },
    )
    ProformaInvoiceItem.objects.update_or_create(
        proforma=proforma,
        sort_order=0,
        defaults={
            "item": items["430977120068"],
            "item_name": items["430977120068"].name,
            "quantity": qty(1),
            "rate": money(4295),
            "gst_rate": money(5),
            "amount": money(4295),
        },
    )

    CreditNote.objects.update_or_create(
        business=business,
        credit_note_number="4",
        defaults={
            "party": parties["SANGEETHA"],
            "original_invoice": invoices["CSM/26-27/1628"],
            "total_amount": money(895),
            "status": "unpaid",
            "reason": "Customer exchange pending",
        },
    )

    return invoices


def seed_purchases(business, parties, items):
    purchase_rows = [
        ("PUR/26-27/0001", "MOORTHY", "SUP-99", "207735041989", 10, 615, 6017, 6017, "paid", date(2026, 5, 12)),
        ("PUR/26-27/0002", "PAPPAYE TEXTILES", "PAP-184", "430977120044", 3, 4100, 12915, 0, "unpaid", date(2026, 5, 19)),
        ("PUR/26-27/0003", "AJMERA FASHION PRIVATE LIMITED", "AJM-77", "430977120068", 8, 2750, 23000, 8021, "partial", date(2026, 5, 19)),
    ]

    purchases = {}
    for number, party_name, supplier_number, item_code, count, rate, total, paid, status, invoice_date in purchase_rows:
        item = items[item_code]
        taxable = money(float(total) / 1.05)
        invoice, _ = PurchaseInvoice.objects.update_or_create(
            business=business,
            invoice_number=number,
            defaults={
                "supplier_invoice_number": supplier_number,
                "party": parties[party_name],
                "subtotal": taxable,
                "taxable_amount": taxable,
                "cgst_amount": money(float(taxable) * 0.025),
                "sgst_amount": money(float(taxable) * 0.025),
                "igst_amount": money(0),
                "cess_amount": money(0),
                "total_amount": money(total),
                "paid_amount": money(paid),
                "status": status,
                "notes": "Seeded purchase register entry",
            },
        )
        set_date(PurchaseInvoice, invoice.id, "invoice_date", invoice_date)
        PurchaseInvoiceItem.objects.update_or_create(
            invoice=invoice,
            sort_order=0,
            defaults={
                "item": item,
                "item_name": item.name,
                "quantity": qty(count),
                "rate": money(rate),
                "discount_pct": money(0),
                "gst_rate": money(5),
                "taxable_amount": taxable,
                "amount": money(total),
            },
        )
        purchases[number] = invoice

    order, _ = PurchaseOrder.objects.update_or_create(
        business=business,
        order_number="22",
        defaults={
            "party": parties["PAPPAYE TEXTILES"],
            "total_amount": money(42150),
            "status": "open",
            "notes": "Pending silk stock purchase order",
        },
    )
    PurchaseOrderItem.objects.update_or_create(
        order=order,
        sort_order=0,
        defaults={
            "item": items["430977120051"],
            "item_name": items["430977120051"].name,
            "quantity": qty(3),
            "rate": money(12750),
            "gst_rate": money(5),
            "amount": money(42150),
        },
    )
    return purchases


def seed_payments_and_accounts(business, parties):
    cash, _ = BankAccount.objects.update_or_create(
        business=business,
        account_name="Cash in hand",
        defaults={
            "account_number": "CASH",
            "ifsc_code": "CASH",
            "bank_name": "Cash",
            "branch": "Showroom",
            "opening_balance": money(324360.21),
            "current_balance": money(320089.21),
            "is_active": True,
        },
    )
    axis, _ = BankAccount.objects.update_or_create(
        business=business,
        account_name="AXIS BANK",
        defaults={
            "account_number": "910020001122334",
            "ifsc_code": "UTIB0001234",
            "bank_name": "Axis Bank",
            "branch": "Kanchipuram",
            "opening_balance": money(-898172.57),
            "current_balance": money(-908172.57),
            "is_active": True,
        },
    )

    BankTransaction.objects.get_or_create(
        business=business,
        bank_account=cash,
        reference_number="PMTIN-0001",
        defaults={
            "transaction_type": "deposit",
            "amount": money(1746),
            "description": "Payment in from retail invoices",
        },
    )
    BankTransaction.objects.get_or_create(
        business=business,
        bank_account=cash,
        reference_number="PMTOUT-0001",
        defaults={
            "transaction_type": "withdrawal",
            "amount": money(6017),
            "description": "Payment out to MOORTHY",
        },
    )
    BankTransaction.objects.get_or_create(
        business=business,
        bank_account=axis,
        reference_number="NEFT-PAP-001",
        defaults={
            "transaction_type": "withdrawal",
            "amount": money(10000),
            "description": "Supplier payment to PAPPAYE TEXTILES",
        },
    )

    payment_in, _ = PaymentIn.objects.update_or_create(
        business=business,
        payment_number="PMTIN-0001",
        defaults={
            "party": parties["SENTHILKUMAR"],
            "amount_received": money(495),
            "payment_mode": "cash",
            "reference_number": "COUNTER-CASH-01",
            "notes": "Retail payment in",
        },
    )
    set_date(PaymentIn, payment_in.id, "payment_date", date(2026, 5, 17))

    payment_out, _ = PaymentOut.objects.update_or_create(
        business=business,
        payment_number="PMTOUT-0001",
        defaults={
            "party": parties["MOORTHY"],
            "amount_paid": money(6017),
            "payment_mode": "cash",
            "reference_number": "CASH-MOORTHY-01",
            "notes": "Supplier payment out",
        },
    )
    set_date(PaymentOut, payment_out.id, "payment_date", date(2026, 5, 20))

    Expense.objects.update_or_create(
        business=business,
        expense_number="EXP-0001",
        defaults={
            "expense_category": "Packing Saree boxes",
            "total_amount": money(3750),
            "paid_amount": money(3750),
            "payment_mode": "cash",
            "reference_number": "EXP-PACK-01",
            "notes": "Packing material expense",
        },
    )
    AutomatedBill.objects.update_or_create(
        business=business,
        bill_name="Showroom electricity",
        defaults={
            "amount": money(9800),
            "frequency": "monthly",
            "next_due_date": date(2026, 6, 5),
            "is_active": True,
        },
    )


def seed_staff_and_settings(business, parties):
    staff_rows = [
        ("K. Rameshan", "Master Weaver", 28000),
        ("M. Selvi", "Loom Helper", 16000),
        ("V. Shankar", "Sales Executive", 22000),
    ]
    for name, designation, salary in staff_rows:
        member, _ = Staff.objects.update_or_create(
            business=business,
            name=name,
            defaults={
                "designation": designation,
                "monthly_salary": money(salary),
                "is_active": True,
            },
        )
        Attendance.objects.update_or_create(
            business=business,
            staff=member,
            date=date(2026, 5, 19),
            defaults={"status": "absent" if name == "V. Shankar" else "present"},
        )
        Payroll.objects.update_or_create(
            business=business,
            staff=member,
            month=5,
            year=2026,
            defaults={
                "basic_salary": money(salary),
                "deductions": money(0),
                "allowances": money(0),
                "net_salary": money(salary),
                "status": "unpaid",
            },
        )

    InvoiceSettings.objects.update_or_create(
        business=business,
        defaults={
            "theme": "advanced_gst",
            "theme_color": "#5B48F5",
            "show_mrp": True,
            "show_hsn": True,
            "show_discount": True,
            "show_color": True,
            "show_cin_date": True,
            "show_grn_date": True,
            "invoice_prefix": "CSM",
            "custom_fields": [
                {"label": "COLOR", "type": "text"},
                {"label": "CIN / DATE", "type": "text"},
                {"label": "GRN / DATE", "type": "text"},
                {"label": "BILL NO", "type": "text"},
            ],
        },
    )

    BusinessPreference.objects.update_or_create(
        business=business,
        defaults={
            "business_category": "Silk Sarees & Textiles",
            "show_in_online_store": True,
            "enable_gst_billing": True,
            "show_logo_on_invoice": True,
            "branch_billing": False,
            "show_upi_on_invoice": True,
            "print_preview": True,
            "hide_zero_stock_barcodes": False,
            "print_original_duplicate": True,
            "auto_print_after_sale": False,
            "ca_reports_enabled": False,
            "plan_name": "Platinum Plan",
            "plan_valid_till": date(2026, 5, 20),
            "referral_code": "CSM2026",
            "support_email": "support@mybillbook.in",
            "support_phone": "8608633066",
        },
    )

    ReminderPreference.objects.update_or_create(
        business=business,
        defaults={
            "payment_due": True,
            "sale_invoice": True,
            "low_stock": False,
            "customer_occasions": False,
            "daily_summary": True,
        },
    )

    Reminder.objects.update_or_create(
        business=business,
        party=parties["PRAVEEN"],
        voucher_type="sales_invoice",
        message="Payment reminder for CSM SILKS invoice.",
        defaults={
            "channel": "sms",
            "scheduled_at": None,
            "status": "pending",
        },
    )


def seed_database():
    print("--- CSM SILKS Postgres seed started ---")
    business = seed_business()
    seed_second_tenant()
    _, _, items = seed_inventory(business)
    parties = seed_parties(business)
    seed_sales(business, parties, items)
    seed_purchases(business, parties, items)
    seed_payments_and_accounts(business, parties)
    seed_staff_and_settings(business, parties)
    print("--- CSM SILKS Postgres seed completed ---")
    print("Tenant login mobile: 8608633066")
    print("Demo session API: /api/v1/auth/demo-session")


if __name__ == "__main__":
    seed_database()
