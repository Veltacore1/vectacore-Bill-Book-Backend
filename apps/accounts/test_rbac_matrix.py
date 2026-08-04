"""
Exhaustive role x module x action verification for the RBAC matrix declared
in apps/accounts/permissions.py (MODULE_ROLE_ACCESS). Enforcement actually
lives inside TenantJWTAuthentication.authenticate() (apps/accounts/authentication.py),
which runs on every request via DEFAULT_AUTHENTICATION_CLASSES - this test
hits real endpoints as each role and asserts against expectations computed
independently here (not imported from permissions.py), so a drift between
the declared matrix and the actual enforcement will fail loudly instead of
silently agreeing with itself.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import Business, User
from apps.items.models import Godown, Item, ItemGodownStock
from apps.parties.models import Party
from apps.sales.models import SalesInvoice


ROLES = ["admin", "partner", "salesman", "accountant", "stock_manager"]

# Representative GET (list) endpoint per module, and the roles independently
# expected to be able to view it - computed by hand from MODULE_ROLE_ACCESS
# (view OR create OR manage, per how ROLE_MODULES derives "view").
VIEW_MATRIX = {
    "/api/v1/auth/workspace": {"admin", "partner", "salesman", "accountant", "stock_manager"},
    "/api/v1/auth/business/": {"admin", "partner", "accountant"},
    "/api/v1/auth/users/": {"admin"},
    "/api/v1/auth/activity": {"admin", "partner", "accountant"},
    "/api/v1/parties/parties/": {"admin", "partner", "salesman", "accountant"},
    "/api/v1/items/items/": {"admin", "partner", "salesman", "accountant", "stock_manager"},
    "/api/v1/items/godowns/": {"admin", "partner", "salesman", "accountant", "stock_manager"},
    "/api/v1/sales/invoices/": {"admin", "partner", "salesman", "accountant"},
    "/api/v1/purchases/invoices/": {"admin", "partner", "accountant", "stock_manager"},
    "/api/v1/payments/payment-in/": {"admin", "partner", "salesman", "accountant"},
    "/api/v1/accounting/expenses/": {"admin", "partner", "accountant"},
    "/api/v1/staff/directory/": {"admin", "partner", "accountant"},
    "/api/v1/settings/invoice-layout/": {"admin", "partner", "accountant"},
    "/api/v1/business-tools/online-orders/": {"admin", "partner", "salesman", "accountant", "stock_manager"},
    "/api/v1/accounting/reports/": {"admin", "partner", "accountant"},
}


class RoleModuleAccessMatrixTests(APITestCase):
    def auth_as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def setUp(self):
        self.business = Business.objects.create(name="RBAC Matrix Tenant", phone="9000000801", gstin="33AAAAA0000A1Z5")
        self.users = {
            role: User.objects.create_user(
                mobile=f"90000009{i:02d}",
                business=self.business,
                role=role,
                first_name=f"{role.title()} User",
                is_active=True,
            )
            for i, role in enumerate(ROLES, start=1)
        }

    def test_view_access_matches_independently_computed_matrix_for_every_role_and_module(self):
        failures = []
        for path, allowed_roles in VIEW_MATRIX.items():
            for role in ROLES:
                self.auth_as(self.users[role])
                response = self.client.get(path)
                should_be_allowed = role in allowed_roles
                is_allowed = response.status_code != status.HTTP_403_FORBIDDEN
                if is_allowed != should_be_allowed:
                    failures.append(
                        f"{role} -> GET {path}: expected allowed={should_be_allowed}, "
                        f"got status={response.status_code}"
                    )
        self.assertEqual(failures, [], "\n".join(failures))

    def test_accountant_can_view_sales_but_cannot_create_invoices(self):
        # sales.view includes accountant; sales.create does not (only
        # admin/partner/salesman) - a subtle view-vs-create split worth its
        # own explicit test.
        customer = Party.objects.create(business=self.business, name="Matrix Customer", party_type="customer")
        self.auth_as(self.users["accountant"])

        view_response = self.client.get("/api/v1/sales/invoices/")
        self.assertNotEqual(view_response.status_code, status.HTTP_403_FORBIDDEN)

        create_response = self.client.post("/api/v1/sales/invoices/", {
            "party": str(customer.id),
            "subtotal": "100.00",
            "taxable_amount": "100.00",
            "total_amount": "100.00",
            "paid_amount": "0.00",
            "line_items": [],
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_403_FORBIDDEN)

    def test_stock_manager_can_view_purchases_but_cannot_create_them(self):
        # purchases.view includes stock_manager; purchases.create does not
        # (only admin/partner/accountant).
        supplier = Party.objects.create(business=self.business, name="Matrix Supplier", party_type="supplier")
        self.auth_as(self.users["stock_manager"])

        view_response = self.client.get("/api/v1/purchases/invoices/")
        self.assertNotEqual(view_response.status_code, status.HTTP_403_FORBIDDEN)

        create_response = self.client.post("/api/v1/purchases/invoices/", {
            "party": str(supplier.id),
            "subtotal": "100.00",
            "taxable_amount": "100.00",
            "total_amount": "100.00",
            "paid_amount": "0.00",
            "line_items": [],
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_403_FORBIDDEN)

    def test_salesman_can_create_sales_invoice_but_cannot_cancel_it(self):
        # sales.manage is OWNER_ACCESS ({admin, partner}) only - a salesman
        # who can create an invoice must still be blocked from cancelling
        # one, even one they created themselves.
        godown = Godown.objects.create(business=self.business, name="Matrix Godown", is_default=True)
        item = Item.objects.create(
            business=self.business,
            name="Matrix Saree",
            selling_price=Decimal("1000.00"),
            purchase_price=Decimal("700.00"),
            gst_rate=Decimal("5.00"),
            current_stock=Decimal("10.000"),
            godown=godown,
        )
        ItemGodownStock.objects.create(
            business=self.business, item=item, godown=godown,
            opening_stock=Decimal("10.000"), current_stock=Decimal("10.000"),
        )
        customer = Party.objects.create(business=self.business, name="Matrix Customer 2", party_type="customer")

        self.auth_as(self.users["salesman"])
        create_response = self.client.post("/api/v1/sales/invoices/", {
            "party": str(customer.id),
            "subtotal": "1000.00",
            "taxable_amount": "1000.00",
            "cgst_amount": "25.00",
            "sgst_amount": "25.00",
            "total_amount": "1050.00",
            "paid_amount": "1050.00",
            "payment_mode": "cash",
            "line_items": [{
                "item": str(item.id),
                "item_name": item.name,
                "quantity": "1.000",
                "rate": "1000.00",
                "gst_rate": "5.00",
                "taxable_amount": "1000.00",
                "tax_amount": "50.00",
                "amount": "1050.00",
                "sort_order": 0,
            }],
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        invoice_id = create_response.data["id"]

        cancel_response = self.client.post(f"/api/v1/sales/invoices/{invoice_id}/cancel/", {}, format="json")
        self.assertEqual(cancel_response.status_code, status.HTTP_403_FORBIDDEN)

        invoice = SalesInvoice.objects.get(id=invoice_id)
        self.assertEqual(invoice.status, "paid")

    def test_stock_manager_cannot_view_parties_or_access_staff_and_settings(self):
        # Cross-checks the module boundaries stock_manager sits outside of:
        # no parties access at all (unlike every other non-admin role), and
        # no staff/settings access (accountant-and-above only).
        self.auth_as(self.users["stock_manager"])
        self.assertEqual(self.client.get("/api/v1/parties/parties/").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get("/api/v1/staff/directory/").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get("/api/v1/settings/invoice-layout/").status_code, status.HTTP_403_FORBIDDEN)

    def test_inactive_user_is_denied_regardless_of_role(self):
        admin = self.users["admin"]
        admin.is_active = False
        admin.save(update_fields=["is_active"])
        self.auth_as(admin)
        response = self.client.get("/api/v1/auth/workspace")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
