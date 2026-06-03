from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import Business, User
from apps.business_settings.models import BusinessPreference
from apps.items.models import BarcodeLabel, Godown, Item, ItemCategory, ItemGodownStock, ItemOffer, ItemPartyPrice
from apps.parties.models import Party


class ItemInventoryLifecycleTests(APITestCase):
    def auth_as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def setUp(self):
        self.business = Business.objects.create(name="CSM SILKS", phone="8608633066")
        self.other_business = Business.objects.create(name="Other Textile", phone="9000000000")
        self.user = User.objects.create_user(
            mobile="8608633066",
            business=self.business,
            role="admin",
            first_name="CSM",
            is_active=True,
        )
        self.category = ItemCategory.objects.create(business=self.business, name="PURE SILK")
        self.godown = Godown.objects.create(business=self.business, name="Main Godown", is_default=True)
        self.party = Party.objects.create(business=self.business, name="AARTHI", party_type="customer")

    def test_item_custom_fields_and_online_store_are_persisted_per_tenant(self):
        self.auth_as(self.user)
        response = self.client.post("/api/v1/items/items/", {
            "name": "TEST SAREE",
            "item_code": "TEST-001",
            "hsn_code": "50072010",
            "category": str(self.category.id),
            "godown": str(self.godown.id),
            "unit": "PCS",
            "selling_price": "1200.00",
            "purchase_price": "800.00",
            "mrp": "1500.00",
            "gst_rate": 5,
            "opening_stock": "2.000",
            "low_stock_qty": "1.000",
            "show_online_store": True,
            "secondary_unit": "BOX",
            "serialisation_enabled": True,
            "color": "MAROON",
            "cin_date": "12 May 2026",
            "grn_date": "13 May 2026",
            "bill_no": "BILL-10",
            "description": "Counter stock item",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        item_id = response.data["id"]
        item = Item.objects.get(id=item_id)
        self.assertEqual(item.business, self.business)
        self.assertEqual(item.current_stock, Decimal("2.000"))
        self.assertTrue(item.show_online_store)
        self.assertEqual(item.color, "MAROON")
        self.assertEqual(item.cin_date, "12 May 2026")
        self.assertEqual(item.grn_date, "13 May 2026")
        self.assertEqual(item.bill_no, "BILL-10")

        list_response = self.client.get("/api/v1/items/items/")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data[0]["color"], "MAROON")
        self.assertEqual(list_response.data[0]["secondary_unit"], "BOX")

    def test_category_and_godown_must_belong_to_active_tenant(self):
        foreign_category = ItemCategory.objects.create(business=self.other_business, name="FOREIGN")
        foreign_godown = Godown.objects.create(business=self.other_business, name="Foreign Godown")

        self.auth_as(self.user)
        category_response = self.client.post("/api/v1/items/items/", {
            "name": "FOREIGN CATEGORY ITEM",
            "category": str(foreign_category.id),
            "godown": str(self.godown.id),
            "selling_price": "10.00",
            "purchase_price": "8.00",
            "gst_rate": 5,
        }, format="json")
        self.assertEqual(category_response.status_code, status.HTTP_400_BAD_REQUEST)

        godown_response = self.client.post("/api/v1/items/items/", {
            "name": "FOREIGN GODOWN ITEM",
            "category": str(self.category.id),
            "godown": str(foreign_godown.id),
            "selling_price": "10.00",
            "purchase_price": "8.00",
            "gst_rate": 5,
        }, format="json")
        self.assertEqual(godown_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_item_soft_deletes_inside_active_tenant(self):
        item = Item.objects.create(
            business=self.business,
            name="OLD ITEM",
            category=self.category,
            godown=self.godown,
            selling_price=Decimal("100.00"),
            purchase_price=Decimal("70.00"),
        )

        self.auth_as(self.user)
        delete_response = self.client.delete(f"/api/v1/items/items/{item.id}/")
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)

        item.refresh_from_db()
        self.assertFalse(item.is_active)
        list_response = self.client.get("/api/v1/items/items/")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        ids = {row["id"] for row in list_response.data}
        self.assertNotIn(str(item.id), ids)

    def test_party_wise_prices_are_upserted_and_tenant_scoped(self):
        item = Item.objects.create(
            business=self.business,
            name="PARTY PRICE ITEM",
            category=self.category,
            godown=self.godown,
            selling_price=Decimal("1000.00"),
            purchase_price=Decimal("700.00"),
        )
        other_item = Item.objects.create(
            business=self.other_business,
            name="OTHER ITEM",
            selling_price=Decimal("999.00"),
            purchase_price=Decimal("600.00"),
        )
        other_party = Party.objects.create(business=self.other_business, name="OTHER PARTY", party_type="customer")
        ItemPartyPrice.objects.create(
            business=self.other_business,
            item=other_item,
            party=other_party,
            sales_price=Decimal("777.00"),
        )

        self.auth_as(self.user)
        create_response = self.client.post("/api/v1/items/party-prices/", {
            "item": str(item.id),
            "party": str(self.party.id),
            "sales_price": "900.00",
            "tax_inclusive": True,
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)

        upsert_response = self.client.post("/api/v1/items/party-prices/", {
            "item": str(item.id),
            "party": str(self.party.id),
            "sales_price": "875.00",
            "tax_inclusive": False,
        }, format="json")
        self.assertEqual(upsert_response.status_code, status.HTTP_200_OK)
        self.assertEqual(ItemPartyPrice.objects.filter(business=self.business, item=item, party=self.party).count(), 1)

        list_response = self.client.get(f"/api/v1/items/party-prices/?item={item.id}")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data), 1)
        self.assertEqual(Decimal(list_response.data[0]["sales_price"]), Decimal("875.00"))

        foreign_party_response = self.client.post("/api/v1/items/party-prices/", {
            "item": str(item.id),
            "party": str(other_party.id),
            "sales_price": "500.00",
        }, format="json")
        self.assertEqual(foreign_party_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_item_offers_are_tenant_scoped_and_exposed_on_items(self):
        item = Item.objects.create(
            business=self.business,
            name="OFFER ITEM",
            category=self.category,
            godown=self.godown,
            item_code="OFF-001",
            selling_price=Decimal("1000.00"),
            purchase_price=Decimal("700.00"),
        )
        foreign_item = Item.objects.create(
            business=self.other_business,
            name="FOREIGN OFFER ITEM",
            selling_price=Decimal("999.00"),
            purchase_price=Decimal("600.00"),
        )

        self.auth_as(self.user)
        create_response = self.client.post("/api/v1/items/offers/", {
            "item": str(item.id),
            "title": "Festival Silk Offer",
            "discount_type": "percent",
            "discount_value": "10.00",
            "starts_on": "2026-06-01",
            "ends_on": "2026-06-30",
            "channel": "billing",
            "status": "active",
            "notes": "Counter launch offer",
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED, create_response.data)
        self.assertEqual(ItemOffer.objects.filter(business=self.business, item=item).count(), 1)
        self.assertEqual(Decimal(create_response.data["offer_price"]), Decimal("900.00"))

        list_response = self.client.get("/api/v1/items/offers/?status=active")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data), 1)

        item_response = self.client.get("/api/v1/items/items/")
        self.assertEqual(item_response.status_code, status.HTTP_200_OK)
        offer_row = next(row for row in item_response.data if row["id"] == str(item.id))
        self.assertEqual(offer_row["active_offer"]["title"], "Festival Silk Offer")

        pause_response = self.client.post(f"/api/v1/items/offers/{create_response.data['id']}/pause/")
        self.assertEqual(pause_response.status_code, status.HTTP_200_OK)
        self.assertEqual(pause_response.data["offer"]["status"], "paused")

        foreign_response = self.client.post("/api/v1/items/offers/", {
            "item": str(foreign_item.id),
            "title": "Foreign Offer",
            "discount_type": "flat",
            "discount_value": "20.00",
        }, format="json")
        self.assertEqual(foreign_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_godown_default_summary_and_safe_delete_are_tenant_scoped(self):
        secondary = Godown.objects.create(business=self.business, name="Showroom")
        empty = Godown.objects.create(business=self.business, name="Temporary Store")
        item = Item.objects.create(
            business=self.business,
            name="WAREHOUSE ITEM",
            category=self.category,
            godown=self.godown,
            selling_price=Decimal("150.00"),
            purchase_price=Decimal("100.00"),
            current_stock=Decimal("5.000"),
        )
        ItemGodownStock.objects.create(
            business=self.business,
            item=item,
            godown=self.godown,
            opening_stock=Decimal("5.000"),
            current_stock=Decimal("5.000"),
        )

        self.auth_as(self.user)
        summary_response = self.client.get("/api/v1/items/godowns/summary/")
        self.assertEqual(summary_response.status_code, status.HTTP_200_OK)
        summary = {row["name"]: row for row in summary_response.data["godowns"]}
        self.assertEqual(summary["Main Godown"]["stockQty"], 5.0)
        self.assertEqual(summary["Main Godown"]["stockValue"], 500.0)

        default_delete = self.client.delete(f"/api/v1/items/godowns/{self.godown.id}/")
        self.assertEqual(default_delete.status_code, status.HTTP_400_BAD_REQUEST)

        default_response = self.client.post(f"/api/v1/items/godowns/{secondary.id}/set_default/")
        self.assertEqual(default_response.status_code, status.HTTP_200_OK)
        self.godown.refresh_from_db()
        secondary.refresh_from_db()
        self.assertFalse(self.godown.is_default)
        self.assertTrue(secondary.is_default)

        stocked_delete = self.client.delete(f"/api/v1/items/godowns/{self.godown.id}/")
        self.assertEqual(stocked_delete.status_code, status.HTTP_400_BAD_REQUEST)

        empty_delete = self.client.delete(f"/api/v1/items/godowns/{empty.id}/")
        self.assertEqual(empty_delete.status_code, status.HTTP_204_NO_CONTENT)

    def test_godown_transfer_posts_balances_and_history(self):
        source = self.godown
        destination = Godown.objects.create(business=self.business, name="Branch Store")
        foreign_godown = Godown.objects.create(business=self.other_business, name="Foreign Store")
        item = Item.objects.create(
            business=self.business,
            name="TRANSFER ITEM",
            category=self.category,
            godown=source,
            selling_price=Decimal("250.00"),
            purchase_price=Decimal("150.00"),
            current_stock=Decimal("4.000"),
        )
        ItemGodownStock.objects.create(
            business=self.business,
            item=item,
            godown=source,
            opening_stock=Decimal("4.000"),
            current_stock=Decimal("4.000"),
        )

        self.auth_as(self.user)
        foreign_response = self.client.post(f"/api/v1/items/items/{item.id}/transfer/", {
            "from_godown": str(source.id),
            "to_godown": str(foreign_godown.id),
            "quantity": "1.000",
        }, format="json")
        self.assertEqual(foreign_response.status_code, status.HTTP_400_BAD_REQUEST)

        transfer_response = self.client.post(f"/api/v1/items/items/{item.id}/transfer/", {
            "from_godown": str(source.id),
            "to_godown": str(destination.id),
            "quantity": "2.000",
            "notes": "Counter replenishment",
        }, format="json")
        self.assertEqual(transfer_response.status_code, status.HTTP_200_OK)

        source_stock = ItemGodownStock.objects.get(business=self.business, item=item, godown=source)
        destination_stock = ItemGodownStock.objects.get(business=self.business, item=item, godown=destination)
        item.refresh_from_db()
        self.assertEqual(source_stock.current_stock, Decimal("2.000"))
        self.assertEqual(destination_stock.current_stock, Decimal("2.000"))
        self.assertEqual(item.current_stock, Decimal("4.000"))

        transfers_response = self.client.get(f"/api/v1/items/transfers/?godown={destination.id}")
        self.assertEqual(transfers_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(transfers_response.data), 1)
        self.assertEqual(transfers_response.data[0]["item_name"], "TRANSFER ITEM")

    def test_stock_adjustment_rejects_foreign_godown_without_fallback_mutation(self):
        item = Item.objects.create(
            business=self.business,
            name="ADJUST ITEM",
            category=self.category,
            godown=self.godown,
            selling_price=Decimal("250.00"),
            purchase_price=Decimal("150.00"),
            current_stock=Decimal("4.000"),
        )
        ItemGodownStock.objects.create(
            business=self.business,
            item=item,
            godown=self.godown,
            opening_stock=Decimal("4.000"),
            current_stock=Decimal("4.000"),
        )
        foreign_godown = Godown.objects.create(business=self.other_business, name="Foreign Adjustment Store")

        self.auth_as(self.user)
        foreign_response = self.client.post(f"/api/v1/items/items/{item.id}/stock_adjustment/", {
            "godown": str(foreign_godown.id),
            "movement_type": "adjustment_in",
            "quantity": "2.000",
            "notes": "Should not land in default godown",
        }, format="json")

        self.assertEqual(foreign_response.status_code, status.HTTP_400_BAD_REQUEST)
        item.refresh_from_db()
        default_stock = ItemGodownStock.objects.get(business=self.business, item=item, godown=self.godown)
        self.assertEqual(item.current_stock, Decimal("4.000"))
        self.assertEqual(default_stock.current_stock, Decimal("4.000"))

    def test_stock_adjustment_updates_selected_tenant_godown_and_aggregate_stock(self):
        branch = Godown.objects.create(business=self.business, name="Adjustment Branch")
        item = Item.objects.create(
            business=self.business,
            name="ADJUST BRANCH ITEM",
            category=self.category,
            godown=self.godown,
            selling_price=Decimal("250.00"),
            purchase_price=Decimal("150.00"),
            current_stock=Decimal("4.000"),
        )
        ItemGodownStock.objects.create(
            business=self.business,
            item=item,
            godown=self.godown,
            opening_stock=Decimal("4.000"),
            current_stock=Decimal("4.000"),
        )

        self.auth_as(self.user)
        response = self.client.post(f"/api/v1/items/items/{item.id}/stock_adjustment/", {
            "godown": str(branch.id),
            "movement_type": "adjustment_in",
            "quantity": "3.000",
            "notes": "Branch opening adjustment",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        default_stock = ItemGodownStock.objects.get(business=self.business, item=item, godown=self.godown)
        branch_stock = ItemGodownStock.objects.get(business=self.business, item=item, godown=branch)
        self.assertEqual(default_stock.current_stock, Decimal("4.000"))
        self.assertEqual(branch_stock.current_stock, Decimal("3.000"))
        self.assertEqual(item.current_stock, Decimal("7.000"))

    def test_barcode_labels_are_real_tenant_records_and_persist_item_barcode(self):
        item = Item.objects.create(
            business=self.business,
            name="BARCODE ITEM",
            category=self.category,
            godown=self.godown,
            item_code="BC-ITEM-001",
            selling_price=Decimal("450.00"),
            purchase_price=Decimal("300.00"),
            mrp=Decimal("500.00"),
            current_stock=Decimal("3.000"),
        )
        foreign_item = Item.objects.create(
            business=self.other_business,
            name="FOREIGN BARCODE ITEM",
            selling_price=Decimal("450.00"),
            purchase_price=Decimal("300.00"),
            current_stock=Decimal("3.000"),
        )

        self.auth_as(self.user)
        size_response = self.client.get("/api/v1/items/barcode-labels/label_sizes/")
        self.assertEqual(size_response.status_code, status.HTTP_200_OK)
        self.assertTrue(any(row["id"] == "50x25" for row in size_response.data["sizes"]))

        create_response = self.client.post("/api/v1/items/barcode-labels/", {
            "item": str(item.id),
            "copies": 2,
            "label_size": "50x25",
            "price_source": "mrp",
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED, create_response.data)
        item.refresh_from_db()
        self.assertEqual(item.barcode, "BC-ITEM-001")
        self.assertEqual(BarcodeLabel.objects.filter(business=self.business, item=item).count(), 1)
        self.assertIn("<svg", create_response.data["barcode_svg"])

        print_response = self.client.get(f"/api/v1/items/barcode-labels/print_sheet/?ids={create_response.data['id']}")
        html = print_response.content.decode("utf-8")
        self.assertEqual(print_response.status_code, status.HTTP_200_OK)
        self.assertIn("BARCODE ITEM", html)
        self.assertIn("MRP:", html)

        foreign_response = self.client.post("/api/v1/items/barcode-labels/", {
            "item": str(foreign_item.id),
            "copies": 1,
            "label_size": "50x25",
        }, format="json")
        self.assertEqual(foreign_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_barcode_bulk_rejects_invalid_size_missing_items_and_zero_stock_preference(self):
        BusinessPreference.objects.create(business=self.business, hide_zero_stock_barcodes=True)
        stocked = Item.objects.create(
            business=self.business,
            name="STOCKED BARCODE ITEM",
            category=self.category,
            godown=self.godown,
            selling_price=Decimal("450.00"),
            purchase_price=Decimal("300.00"),
            current_stock=Decimal("3.000"),
        )
        zero_stock = Item.objects.create(
            business=self.business,
            name="ZERO BARCODE ITEM",
            category=self.category,
            godown=self.godown,
            selling_price=Decimal("450.00"),
            purchase_price=Decimal("300.00"),
            current_stock=Decimal("0.000"),
        )

        self.auth_as(self.user)
        invalid_size_response = self.client.post("/api/v1/items/barcode-labels/", {
            "item": str(stocked.id),
            "copies": 1,
            "label_size": "unknown-size",
        }, format="json")
        self.assertEqual(invalid_size_response.status_code, status.HTTP_400_BAD_REQUEST)

        missing_response = self.client.post("/api/v1/items/barcode-labels/bulk_create/", {
            "item_ids": [str(stocked.id), "00000000-0000-0000-0000-000000000000"],
            "copies": 1,
            "label_size": "50x25",
        }, format="json")
        self.assertEqual(missing_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(BarcodeLabel.objects.filter(business=self.business).count(), 0)

        zero_response = self.client.post("/api/v1/items/barcode-labels/bulk_create/", {
            "item_ids": [str(stocked.id), str(zero_stock.id)],
            "copies": 1,
            "label_size": "50x25",
        }, format="json")
        self.assertEqual(zero_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(BarcodeLabel.objects.filter(business=self.business).count(), 0)

        ok_response = self.client.post("/api/v1/items/barcode-labels/bulk_create/", {
            "item_ids": [str(stocked.id)],
            "copies": 150,
            "label_size": "50x25",
        }, format="json")
        self.assertEqual(ok_response.status_code, status.HTTP_201_CREATED, ok_response.data)
        self.assertEqual(ok_response.data["labels"][0]["copies"], 99)
