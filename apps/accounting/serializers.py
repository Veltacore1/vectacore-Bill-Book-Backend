from rest_framework import serializers
from django.db import transaction
from apps.accounts.sequences import next_model_document_number
from .models import BankAccount, BankTransaction, Expense, AutomatedBill


def apply_bank_transaction_balance(account, transaction_type, amount, *, reverse=False):
    if transaction_type == "deposit":
        delta = amount
    elif transaction_type == "withdrawal":
        delta = -amount
    else:
        return
    account.current_balance += -delta if reverse else delta
    account.save(update_fields=["current_balance"])

class BankAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = BankAccount
        fields = [
            "id", "account_name", "account_number", "ifsc_code", "bank_name",
            "branch", "opening_balance", "current_balance", "is_active", "created_at"
        ]
        read_only_fields = ["id", "current_balance", "created_at"]

    def create(self, validated_data):
        business = self.context["request"].business
        validated_data["current_balance"] = validated_data.get("opening_balance", 0.0)
        validated_data["business"] = business
        return BankAccount.objects.create(**validated_data)

class BankTransactionSerializer(serializers.ModelSerializer):
    bank_account_name = serializers.CharField(source="bank_account.account_name", read_only=True)

    class Meta:
        model = BankTransaction
        fields = "__all__"
        read_only_fields = ["id", "business", "created_at"]

    def validate(self, attrs):
        request = self.context.get("request")
        business = getattr(request, "business", None)
        bank_account = attrs.get("bank_account") or getattr(self.instance, "bank_account", None)
        amount = attrs.get("amount", getattr(self.instance, "amount", 0))

        if business and bank_account and (bank_account.business_id != business.id or not bank_account.is_active):
            raise serializers.ValidationError({"bank_account": "Choose an active bank account from the active tenant."})
        if amount is not None and amount <= 0:
            raise serializers.ValidationError({"amount": "Transaction amount must be greater than zero."})

        return attrs

    def create(self, validated_data):
        business = self.context["request"].business
        validated_data["business"] = business
        
        with transaction.atomic():
            account = BankAccount.objects.select_for_update().get(
                id=validated_data["bank_account"].id,
                business=business,
                is_active=True,
            )
            validated_data["bank_account"] = account
            tx = BankTransaction.objects.create(**validated_data)
            apply_bank_transaction_balance(account, tx.transaction_type, tx.amount)
            
            return tx

    def update(self, instance, validated_data):
        business = self.context["request"].business

        with transaction.atomic():
            tx = BankTransaction.objects.select_for_update().select_related("bank_account").get(
                id=instance.id,
                business=business,
            )
            old_account = BankAccount.objects.select_for_update().get(
                id=tx.bank_account_id,
                business=business,
            )
            apply_bank_transaction_balance(old_account, tx.transaction_type, tx.amount, reverse=True)

            new_account = validated_data.get("bank_account", tx.bank_account)
            new_account = BankAccount.objects.select_for_update().get(
                id=new_account.id,
                business=business,
                is_active=True,
            )

            for attr, value in validated_data.items():
                if attr in {"business"}:
                    continue
                setattr(tx, attr, value)
            tx.bank_account = new_account
            tx.save()

            apply_bank_transaction_balance(new_account, tx.transaction_type, tx.amount)
            return tx

class ExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = "__all__"
        read_only_fields = ["id", "business", "expense_number", "created_by", "created_at"]

    def create(self, validated_data):
        request = self.context["request"]
        business = request.business
        
        with transaction.atomic():
            validated_data["expense_number"] = next_model_document_number(
                business=business,
                sequence_key="expense:EXP",
                model=Expense,
                field_name="expense_number",
                number_prefix="EXP-",
            )
            validated_data["business"] = business
            validated_data["created_by"] = request.user
            
            return Expense.objects.create(**validated_data)

class AutomatedBillSerializer(serializers.ModelSerializer):
    class Meta:
        model = AutomatedBill
        fields = "__all__"
        read_only_fields = ["id", "business", "created_at"]

    def validate_amount(self, amount):
        if amount <= 0:
            raise serializers.ValidationError("Bill amount must be greater than zero.")
        return amount

    def create(self, validated_data):
        validated_data["business"] = self.context["request"].business
        return AutomatedBill.objects.create(**validated_data)
