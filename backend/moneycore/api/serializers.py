"""Response representation for the financial relationship.

Read-only. These serializers render what the services decided; they never
orchestrate a financial state change. That separation is the pattern M2–M8 will
follow.

Note what is absent, deliberately:

* **no balance of any kind** — the ledger (M2) owns what money exists, and a
  zero here would be a fabricated financial figure;
* **no account number, provider name, or provider identifier** — provider
  integration is M6, and a placeholder account number would put a fake bank
  account in front of a customer.
"""

from __future__ import annotations

from rest_framework import serializers

from moneycore.models import FinancialAccount, FinancialCustomer, Wallet


class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = ['currency', 'status', 'created_at']
        read_only_fields = fields


class FinancialAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinancialAccount
        fields = ['status', 'activated_at', 'closed_at', 'created_at']
        read_only_fields = fields


class FinancialCustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinancialCustomer
        fields = ['status', 'created_at']
        read_only_fields = fields


class FinancialRelationshipSerializer(serializers.Serializer):
    """The authenticated user's whole financial relationship, or its absence.

    A user with no relationship serialises to nulls and an empty wallet list
    rather than a 404: being Tier 0 is a normal state the Home experience needs
    to render an activation path for, not a failure.
    """

    customer = FinancialCustomerSerializer(allow_null=True)
    account = FinancialAccountSerializer(allow_null=True)
    wallets = WalletSerializer(many=True)

    @classmethod
    def from_customer(cls, customer: FinancialCustomer | None) -> 'FinancialRelationshipSerializer':
        if customer is None:
            return cls({'customer': None, 'account': None, 'wallets': []})

        account = getattr(customer, 'financial_account', None)
        wallets = list(account.wallets.all()) if account is not None else []
        return cls({'customer': customer, 'account': account, 'wallets': wallets})
