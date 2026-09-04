"""The M1 financial API surface: one authenticated read endpoint.

There is no write endpoint. FinancialCustomer, FinancialAccount and Wallet are
system-owned resources — a client must not be able to POST ``status=ACTIVE`` or
create wallets — and M1 has no customer-facing action that would justify one.
Provisioning and lifecycle transitions are domain services, exercised by tests
and by future internal callers, not by the mobile app.

Ownership is structural rather than checked: the view resolves the relationship
from ``request.user`` and never accepts an identifier, so there is no id to
tamper with and no lookup that could reach another user's data.
"""

from __future__ import annotations

from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from moneycore.api.serializers import FinancialRelationshipSerializer
from moneycore.services.provisioning import get_financial_relationship


class FinancialAccountView(APIView):
    """``GET /api/financial-account/`` — the caller's own financial relationship.

    Always 200 for an authenticated caller. A user without a financial
    relationship gets ``{"customer": null, "account": null, "wallets": []}``,
    which says truthfully that nothing has been provisioned. It is not a 404,
    because absence here is an expected state rather than a missing resource,
    and it carries no zero balance, because there is no balance to report.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        customer = get_financial_relationship(request.user)
        serializer = FinancialRelationshipSerializer.from_customer(customer)
        return Response(serializer.data)
