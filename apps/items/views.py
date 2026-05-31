from html import escape
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from django.db import transaction
from django.db.models import F, Q
from .models import (
    ItemCategory, Godown, Item, ItemGodownStock, StockMovement,
    GodownTransfer, PriceHistory, ItemPartyPrice, BarcodeLabel, BARCODE_LABEL_SIZES,
    apply_stock_movement, sync_item_current_stock, make_item_barcode_value,
    generate_barcode_svg, get_barcode_label_size
)
from .serializers import (
    ItemCategorySerializer, GodownSerializer, ItemSerializer,
    ItemGodownStockSerializer, StockMovementSerializer, GodownTransferSerializer,
    ItemPartyPriceSerializer, BarcodeLabelSerializer
)
from decimal import Decimal
from django.http import HttpResponse
from apps.business_settings.models import BusinessPreference

class ItemCategoryViewSet(viewsets.ModelViewSet):
    serializer_class = ItemCategorySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return ItemCategory.objects.none()
        return ItemCategory.objects.filter(business=self.request.business)

class GodownViewSet(viewsets.ModelViewSet):
    serializer_class = GodownSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return Godown.objects.none()
        return Godown.objects.filter(business=self.request.business)

    def perform_create(self, serializer):
        godown = serializer.save()
        if godown.is_default:
            Godown.objects.filter(business=self.request.business, is_default=True).exclude(id=godown.id).update(is_default=False)

    def perform_update(self, serializer):
        godown = serializer.save()
        if godown.is_default:
            Godown.objects.filter(business=self.request.business, is_default=True).exclude(id=godown.id).update(is_default=False)

    def destroy(self, request, *args, **kwargs):
        godown = self.get_object()
        if godown.is_default:
            return Response(
                {"success": False, "message": "Default godown cannot be deleted. Make another godown default first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        has_stock = ItemGodownStock.objects.filter(
            business=request.business,
            godown=godown,
            current_stock__gt=0,
        ).exists()
        has_history = StockMovement.objects.filter(business=request.business, godown=godown).exists()
        has_transfers = GodownTransfer.objects.filter(
            Q(from_godown=godown) | Q(to_godown=godown),
            business=request.business,
        ).exists()
        if has_stock or has_history or has_transfers:
            return Response(
                {"success": False, "message": "Godown has stock or ledger history and cannot be deleted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    def set_default(self, request, pk=None):
        godown = self.get_object()
        Godown.objects.filter(business=request.business, is_default=True).exclude(id=godown.id).update(is_default=False)
        godown.is_default = True
        godown.save(update_fields=["is_default"])
        return Response({"success": True, "godown": self.get_serializer(godown).data})

    @action(detail=False, methods=["get"])
    def summary(self, request):
        if not request.business:
            return Response({"success": False, "message": "No active tenant business"}, status=status.HTTP_404_NOT_FOUND)

        rows = []
        for godown in self.get_queryset().order_by("-is_default", "name"):
            stocks = ItemGodownStock.objects.filter(
                business=request.business,
                godown=godown,
                item__is_active=True,
            ).select_related("item")
            quantity = sum(stock.current_stock for stock in stocks)
            value = sum(stock.current_stock * stock.item.purchase_price for stock in stocks)
            rows.append({
                "id": str(godown.id),
                "name": godown.name,
                "location": godown.address or "",
                "isDefault": godown.is_default,
                "stockQty": float(quantity),
                "stockValue": float(value),
                "itemCount": stocks.exclude(current_stock=0).count(),
            })

        return Response({"success": True, "godowns": rows})

    @action(detail=True, methods=["get"])
    def stocks(self, request, pk=None):
        godown = self.get_object()
        queryset = ItemGodownStock.objects.filter(
            business=request.business,
            godown=godown,
        ).select_related("item", "godown").order_by("item__name")
        serializer = ItemGodownStockSerializer(queryset, many=True)
        return Response({"success": True, "stocks": serializer.data})

class ItemGodownStockViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ItemGodownStockSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return ItemGodownStock.objects.none()
        queryset = ItemGodownStock.objects.filter(
            business=self.request.business,
        ).select_related("item", "godown")
        item_id = self.request.query_params.get("item")
        godown_id = self.request.query_params.get("godown")
        if item_id:
            queryset = queryset.filter(item_id=item_id)
        if godown_id:
            queryset = queryset.filter(godown_id=godown_id)
        return queryset.order_by("item__name", "godown__name")

class ItemViewSet(viewsets.ModelViewSet):
    serializer_class = ItemSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return Item.objects.none()
        
        queryset = (
            Item.objects.filter(business=self.request.business, is_active=True)
            .select_related("category", "godown")
            .prefetch_related("godown_stocks__godown", "party_prices__party")
        )
        
        # Category filtering
        category = self.request.query_params.get("category")
        if category:
            queryset = queryset.filter(category_id=category)
            
        # Low stock check
        low_stock = self.request.query_params.get("low_stock")
        if low_stock == "true":
            queryset = queryset.filter(current_stock__lte=F("low_stock_qty"))
            
        return queryset

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])

    @action(detail=False, methods=["get"])
    def inventory_stats(self, request):
        """Calculates global stock value and total low stock items count."""
        if not request.business:
            return Response({"success": False, "message": "No active business"})
            
        items = Item.objects.filter(business=request.business, is_active=True)
        
        # Calculate Stock Value = Sum(current_stock * purchase_price)
        # In SQLite/Postgres we can compute it inside Python or using Django F expressions
        stock_value = sum(item.current_stock * item.purchase_price for item in items)
        
        # Low stock count
        low_stock_count = items.filter(current_stock__lte=F("low_stock_qty")).count()
        
        return Response({
            "success": True,
            "stock_value": float(stock_value),
            "low_stock_count": low_stock_count
        })

    @action(detail=True, methods=["post"])
    def stock_adjustment(self, request, pk=None):
        """Manually adjusts stock level in or out."""
        item = self.get_object()
        qty = Decimal(str(request.data.get("quantity", 0.0)))
        adj_type = request.data.get("movement_type")  # adjustment_in or adjustment_out
        notes = request.data.get("notes", "Manual stock adjustment")
        godown_id = request.data.get("godown")
        
        if adj_type not in ["adjustment_in", "adjustment_out"]:
            return Response(
                {"success": False, "message": "Invalid adjustment type. Choose 'adjustment_in' or 'adjustment_out'"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        if qty <= 0:
            return Response(
                {"success": False, "message": "Quantity must be positive"},
                status=status.HTTP_400_BAD_REQUEST
            )

        godown = None
        if godown_id:
            godown = Godown.objects.filter(id=godown_id, business=request.business).first()
            if not godown:
                return Response(
                    {"success": False, "message": "Choose a valid godown from the active tenant."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # adjustment_out represents stock REDUCTION, so movement is negative
        actual_qty = qty if adj_type == "adjustment_in" else -qty
        
        with transaction.atomic():
            locked_item = Item.objects.select_for_update().get(id=item.id, business=request.business, is_active=True)
            stock, movement = apply_stock_movement(
                business=request.business,
                item=locked_item,
                godown=godown or locked_item.godown,
                movement_type=adj_type,
                reference_type="manual_adjustment",
                reference_id=locked_item.id,
                quantity=actual_qty,
                rate=locked_item.purchase_price,
                created_by=request.user,
                notes=notes,
                allow_negative=False,
            )
            new_stock = sync_item_current_stock(stock.item)
            
        return Response({
            "success": True,
            "message": "Stock adjusted successfully",
            "current_stock": float(new_stock),
            "godown_stock": float(stock.current_stock)
        })

    @action(detail=True, methods=["post"])
    def transfer(self, request, pk=None):
        """Transfers stock between godowns."""
        item = self.get_object()
        from_godown_id = request.data.get("from_godown")
        to_godown_id = request.data.get("to_godown")
        qty = Decimal(str(request.data.get("quantity", 0.0)))
        notes = request.data.get("notes", "Godown transfer")
        
        if not from_godown_id or not to_godown_id:
            return Response(
                {"success": False, "message": "Source and destination godowns are required"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        if qty <= 0:
            return Response(
                {"success": False, "message": "Quantity must be greater than zero"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        from_godown = Godown.objects.filter(id=from_godown_id, business=request.business).first()
        to_godown = Godown.objects.filter(id=to_godown_id, business=request.business).first()
        
        if not from_godown or not to_godown:
            return Response(
                {"success": False, "message": "Invalid godowns specified"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if from_godown.id == to_godown.id:
            return Response(
                {"success": False, "message": "Source and destination godowns must be different"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        with transaction.atomic():
            locked_item = Item.objects.select_for_update().get(id=item.id, business=request.business, is_active=True)
            source_stock = ItemGodownStock.objects.select_for_update().filter(
                business=request.business,
                item=locked_item,
                godown=from_godown,
            ).first()
            available_qty = source_stock.current_stock if source_stock else Decimal("0.000")
            if available_qty < qty:
                return Response(
                    {"success": False, "message": f"Only {available_qty:g} available in {from_godown.name}"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            transfer_record = GodownTransfer.objects.create(
                business=request.business,
                item=locked_item,
                from_godown=from_godown,
                to_godown=to_godown,
                quantity=qty,
                notes=notes
            )
            
            from_stock, _ = apply_stock_movement(
                business=request.business,
                item=locked_item,
                godown=from_godown,
                movement_type="transfer",
                reference_type="godown_transfer",
                reference_id=transfer_record.id,
                quantity=-qty,
                rate=locked_item.purchase_price,
                created_by=request.user,
                notes=f"Stock Transfer OUT to {to_godown.name}"
            )
            
            to_stock, _ = apply_stock_movement(
                business=request.business,
                item=locked_item,
                godown=to_godown,
                movement_type="transfer",
                reference_type="godown_transfer",
                reference_id=transfer_record.id,
                quantity=qty,
                rate=locked_item.purchase_price,
                created_by=request.user,
                notes=f"Stock Transfer IN from {from_godown.name}"
            )
            aggregate_stock = sync_item_current_stock(locked_item)
            
        return Response({
            "success": True,
            "message": "Stock transferred successfully between godowns",
            "transfer": GodownTransferSerializer(transfer_record).data,
            "from_godown_stock": float(from_stock.current_stock),
            "to_godown_stock": float(to_stock.current_stock),
            "current_stock": float(aggregate_stock),
        })


class ItemPartyPriceViewSet(viewsets.ModelViewSet):
    serializer_class = ItemPartyPriceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return ItemPartyPrice.objects.none()
        queryset = ItemPartyPrice.objects.filter(
            business=self.request.business,
            item__is_active=True,
            party__is_active=True,
        ).select_related("item", "party")
        item_id = self.request.query_params.get("item")
        party_id = self.request.query_params.get("party")
        if item_id:
            queryset = queryset.filter(item_id=item_id)
        if party_id:
            queryset = queryset.filter(party_id=party_id)
        return queryset.order_by("party__name")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        price, created = ItemPartyPrice.objects.update_or_create(
            business=request.business,
            item=data["item"],
            party=data["party"],
            defaults={
                "sales_price": data["sales_price"],
                "tax_inclusive": data.get("tax_inclusive", True),
            },
        )
        return Response(
            self.get_serializer(price).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

class GodownTransferViewSet(viewsets.ModelViewSet):
    serializer_class = GodownTransferSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        if not self.request.business:
            return GodownTransfer.objects.none()
        queryset = GodownTransfer.objects.filter(
            business=self.request.business,
        ).select_related("item", "from_godown", "to_godown")
        item_id = self.request.query_params.get("item")
        godown_id = self.request.query_params.get("godown")
        if item_id:
            queryset = queryset.filter(item_id=item_id)
        if godown_id:
            queryset = queryset.filter(Q(from_godown_id=godown_id) | Q(to_godown_id=godown_id))
        return queryset.order_by("-created_at")

    def create(self, request, *args, **kwargs):
        item = Item.objects.filter(id=request.data.get("item"), business=request.business, is_active=True).first()
        from_godown = Godown.objects.filter(id=request.data.get("from_godown"), business=request.business).first()
        to_godown = Godown.objects.filter(id=request.data.get("to_godown"), business=request.business).first()
        qty = Decimal(str(request.data.get("quantity", 0.0)))
        notes = request.data.get("notes", "Godown transfer")

        if not item or not from_godown or not to_godown:
            return Response(
                {"success": False, "message": "Valid item, source godown, and destination godown are required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        if from_godown.id == to_godown.id:
            return Response(
                {"success": False, "message": "Source and destination godowns must be different"},
                status=status.HTTP_400_BAD_REQUEST
            )
        if qty <= 0:
            return Response(
                {"success": False, "message": "Quantity must be greater than zero"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            with transaction.atomic():
                locked_item = Item.objects.select_for_update().get(id=item.id, business=request.business)
                transfer_record = GodownTransfer.objects.create(
                    business=request.business,
                    item=locked_item,
                    from_godown=from_godown,
                    to_godown=to_godown,
                    quantity=qty,
                    notes=notes
                )
                apply_stock_movement(
                    business=request.business,
                    item=locked_item,
                    godown=from_godown,
                    movement_type="transfer",
                    reference_type="godown_transfer",
                    reference_id=transfer_record.id,
                    quantity=-qty,
                    rate=locked_item.purchase_price,
                    created_by=request.user,
                    notes=f"Stock Transfer OUT to {to_godown.name}",
                    allow_negative=False,
                )
                apply_stock_movement(
                    business=request.business,
                    item=locked_item,
                    godown=to_godown,
                    movement_type="transfer",
                    reference_type="godown_transfer",
                    reference_id=transfer_record.id,
                    quantity=qty,
                    rate=locked_item.purchase_price,
                    created_by=request.user,
                    notes=f"Stock Transfer IN from {from_godown.name}",
                )
        except ValueError as exc:
            return Response(
                {"success": False, "message": str(exc)},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = self.get_serializer(transfer_record)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

class StockMovementViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = StockMovementSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return StockMovement.objects.none()
        
        queryset = StockMovement.objects.filter(business=self.request.business)
        item_id = self.request.query_params.get("item")
        if item_id:
            queryset = queryset.filter(item_id=item_id)
        godown_id = self.request.query_params.get("godown")
        if godown_id:
            queryset = queryset.filter(godown_id=godown_id)
        movement_type = self.request.query_params.get("movement_type")
        if movement_type:
            queryset = queryset.filter(movement_type=movement_type)
            
        return queryset.order_by("-created_at")


def _label_price(label):
    if label.price_source == "none":
        return ""
    if label.price_source == "mrp":
        return f"MRP: {label.item.mrp}"
    return f"Price: {label.item.selling_price}"


def _render_barcode_print_sheet(business, labels):
    labels = list(labels)
    label_size = get_barcode_label_size(labels[0].label_size if labels else "50x25")
    width = label_size["width_mm"]
    height = label_size["height_mm"]
    columns = label_size["columns"]
    gap = label_size["gap_mm"]
    cards = []

    for label in labels:
        price = _label_price(label)
        for _ in range(max(1, label.copies)):
            cards.append(f"""
                <section class="barcode-label">
                    {f'<strong class="business">{escape(business.name)}</strong>' if label.include_business_name else ''}
                    {f'<span class="item-name">{escape(label.item.name)}</span>' if label.include_item_name else ''}
                    <div class="barcode-svg">{generate_barcode_svg(label.barcode_value)}</div>
                    <span class="code">{escape(label.barcode_value)}</span>
                    {f'<span class="price">{escape(price)}</span>' if label.include_price and price else ''}
                    {f'<span class="mrp">MRP: {escape(str(label.item.mrp))}</span>' if label.include_mrp and label.item.mrp else ''}
                </section>
            """)

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{escape(business.name)} Barcode Labels</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Arial, sans-serif; color: #151922; background: #fff; }}
    .toolbar {{ display: flex; justify-content: space-between; align-items: center; padding: 14px 18px; border-bottom: 1px solid #dde2ee; }}
    .toolbar strong {{ font-size: 16px; }}
    .toolbar button {{ border: 1px solid #cfd6e4; background: #fff; border-radius: 6px; padding: 8px 12px; cursor: pointer; }}
    .sheet {{ display: grid; grid-template-columns: repeat({columns}, {width}mm); gap: {gap}mm; padding: {gap}mm; align-items: start; }}
    .barcode-label {{ width: {width}mm; min-height: {height}mm; padding: 2.5mm; border: 1px dashed #aeb7c9; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; break-inside: avoid; overflow: hidden; }}
    .business {{ font-size: 8pt; line-height: 1.1; }}
    .item-name {{ font-size: 7pt; line-height: 1.1; max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .barcode-svg {{ width: 100%; height: 9mm; display: flex; align-items: center; justify-content: center; overflow: hidden; }}
    .barcode-svg svg {{ max-width: 100%; height: 100%; }}
    .code {{ font-size: 6.5pt; letter-spacing: 0; }}
    .price, .mrp {{ font-size: 7pt; font-weight: 700; line-height: 1.1; }}
    @media print {{
      @page {{ margin: 4mm; }}
      .toolbar {{ display: none; }}
      .sheet {{ padding: 0; }}
      .barcode-label {{ border-color: transparent; }}
    }}
  </style>
</head>
<body>
  <div class="toolbar">
    <strong>{len(cards)} barcode label(s)</strong>
    <button onclick="window.print()">Print</button>
  </div>
  <main class="sheet">
    {''.join(cards) or '<p>No labels selected.</p>'}
  </main>
</body>
</html>"""


class BarcodeLabelViewSet(viewsets.ModelViewSet):
    serializer_class = BarcodeLabelSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        if not self.request.business:
            return BarcodeLabel.objects.none()
        return BarcodeLabel.objects.filter(
            business=self.request.business
        ).select_related("item").order_by("-created_at")

    def _preferences(self, request):
        preferences, _ = BusinessPreference.objects.get_or_create(business=request.business)
        return preferences

    def _clean_copies(self, raw_value):
        try:
            copies = int(raw_value or 1)
        except (TypeError, ValueError):
            raise ValidationError({"copies": "Copies must be a number."})
        return max(1, min(99, copies))

    def _assert_printable_item(self, request, item):
        preferences = self._preferences(request)
        if preferences.hide_zero_stock_barcodes and item.current_stock <= 0:
            raise ValidationError({
                "item": f"{item.name} has zero stock and barcode printing is hidden by print settings."
            })

    def _build_payload(self, request, item):
        payload = request.data.copy()
        self._assert_printable_item(request, item)
        barcode_value = (
            payload.get("barcode_value")
            or item.barcode
            or item.item_code
            or make_item_barcode_value(request.business, item)
        )
        if not item.barcode:
            item.barcode = barcode_value
            item.save(update_fields=["barcode", "updated_at"])

        payload["item"] = str(item.id)
        payload["barcode_value"] = barcode_value
        payload["label_size"] = payload.get("label_size") or "50x25"
        payload["copies"] = self._clean_copies(payload.get("copies"))
        return payload

    def create(self, request, *args, **kwargs):
        item = Item.objects.filter(id=request.data.get("item"), business=request.business, is_active=True).first()
        if not item:
            return Response(
                {"success": False, "message": "A valid tenant item is required for barcode printing."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = self.get_serializer(data=self._build_payload(request, item))
        serializer.is_valid(raise_exception=True)
        label = serializer.save(business=request.business, created_by=request.user)
        return Response(self.get_serializer(label).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def label_sizes(self, request):
        return Response({"success": True, "sizes": list(BARCODE_LABEL_SIZES.values())})

    @action(detail=False, methods=["post"])
    def bulk_create(self, request):
        item_ids = request.data.get("item_ids") or []
        if not isinstance(item_ids, list) or not item_ids:
            return Response(
                {"success": False, "message": "Choose at least one item for barcode labels."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        labels = []
        with transaction.atomic():
            items_by_id = {
                str(item.id): item
                for item in Item.objects.select_for_update().filter(
                    id__in=item_ids,
                    business=request.business,
                    is_active=True,
                )
            }
            missing_ids = [str(item_id) for item_id in item_ids if str(item_id) not in items_by_id]
            if missing_ids:
                return Response(
                    {"success": False, "message": "Some selected items are not available in this tenant.", "missing_item_ids": missing_ids},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            items = [items_by_id[str(item_id)] for item_id in item_ids]
            for item in items:
                serializer = self.get_serializer(data=self._build_payload(request, item))
                serializer.is_valid(raise_exception=True)
                labels.append(serializer.save(business=request.business, created_by=request.user))

        return Response({"success": True, "labels": self.get_serializer(labels, many=True).data}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def print_sheet(self, request):
        ids = [value for value in request.query_params.get("ids", "").split(",") if value]
        queryset = self.get_queryset()
        if ids:
            queryset = queryset.filter(id__in=ids)
        labels = list(queryset[:100])
        return HttpResponse(_render_barcode_print_sheet(request.business, labels), content_type="text/html")
