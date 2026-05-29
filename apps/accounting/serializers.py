from rest_framework import serializers
from django.db import transaction
from .models import BankAccount, BankTransaction, Expense, AutomatedBill

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

    def create(self, validated_data):
        business = self.context["request"].business
        validated_data["business"] = business
        
        with transaction.atomic():
            tx = BankTransaction.objects.create(**validated_data)
            
            # Apply balance updates to BankAccount
            account = BankAccount.objects.select_for_update().get(id=tx.bank_account.id)
            if tx.transaction_type == "deposit":
                account.current_balance += tx.amount
            elif tx.transaction_type == "withdrawal":
                account.current_balance -= tx.amount
            account.save()
            
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
            # Generate sequential expense number
            last_exp = Expense.objects.filter(business=business).order_by("-created_at").first()
            next_seq = 1
            if last_exp:
                try:
                    last_seq_str = last_exp.expense_number.split("-")[-1]
                    next_seq = int(last_seq_str) + 1
                except (ValueError, IndexError):
                    next_seq = 1
            exp_num = f"EXP-{next_seq:04d}"
            
            validated_data["expense_number"] = exp_num
            validated_data["business"] = business
            validated_data["created_by"] = request.user
            
            return Expense.objects.create(**validated_data)

class AutomatedBillSerializer(serializers.ModelSerializer):
    class Meta:
        model = AutomatedBill
        fields = "__all__"
        read_only_fields = ["id", "business", "created_at"]

    def create(self, validated_data):
        validated_data["business"] = self.context["request"].business
        return AutomatedBill.objects.create(**validated_data)
