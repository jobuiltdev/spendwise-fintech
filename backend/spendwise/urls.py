from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

# Expenses app imports (only expense-related views)
from expenses.views import (
    PaymentMethodViewSet, ExpenseViewSet
)

# Recurring app imports
from recurring.views import (
    RecurringExpenseViewSet, RecurringExpenseLogViewSet
)

# Other imports
from budgets.views import BudgetViewSet, BudgetAlertViewSet
from groups.views import (
    GroupViewSet, GroupExpenseViewSet, GroupMembershipViewSet, GroupSettlementViewSet
)
from categories.views import CategoryViewSet
from accounts.views import (
    UserRegistrationView, UserViewSet, ProfileViewSet, UserPreferenceViewSet,
    MobileTokenObtainPairView, GoogleLoginView, LogoutView, LogoutAllView,
)
# Analytics
from analytics.views import (
    AnalyticsViewSet, SpendingPatternViewSet, 
    AnomalyRuleViewSet, FinancialHealthMetricViewSet
)
#  Reports
from reports.views import ReportViewSet, ScheduledReportViewSet

# Money core (M1): the authenticated read view for the caller's own financial
# relationship. Deliberately not a router registration — these are system-owned
# resources with no client-writable CRUD surface.
from moneycore.api.views import FinancialAccountView


router = DefaultRouter()

# Accounts
router.register(r'users', UserViewSet, basename='user')
router.register(r'profiles', ProfileViewSet, basename='profile')
router.register(r'preferences', UserPreferenceViewSet, basename='preference')

# Categories
router.register(r'categories', CategoryViewSet, basename='category')

# Expenses
router.register(r'payment-methods', PaymentMethodViewSet, basename='paymentmethod')
router.register(r'expenses', ExpenseViewSet, basename='expense')

# Recurring
router.register(r'recurring-expenses', RecurringExpenseViewSet, basename='recurringexpense')
router.register(r'recurring-logs', RecurringExpenseLogViewSet, basename='recurringexpenselog')

# Budgets
router.register(r'budgets', BudgetViewSet, basename='budget')
router.register(r'budget-alerts', BudgetAlertViewSet, basename='budgetalert')

# Groups
router.register(r'groups', GroupViewSet, basename='group')
router.register(r'group-expenses', GroupExpenseViewSet, basename='groupexpense')
router.register(r'group-memberships', GroupMembershipViewSet, basename='groupmembership')
router.register(r'group-settlements', GroupSettlementViewSet, basename='groupsettlement')

# Analytics
router.register(r'analytics/dashboard', AnalyticsViewSet, basename='analytics-dashboard')
router.register(r'analytics/spending-patterns', SpendingPatternViewSet, basename='spendingpattern')
router.register(r'analytics/anomaly-rules', AnomalyRuleViewSet, basename='anomalyrule')
router.register(r'analytics/health-metrics', FinancialHealthMetricViewSet, basename='healthmetric')

# Reports
router.register(r'reports', ReportViewSet, basename='report')
router.register(r'scheduled-reports', ScheduledReportViewSet, basename='scheduledreport')

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include(router.urls)),
    path('api/register/', UserRegistrationView.as_view(), name='user-register'),
    path('api/auth/', include('rest_framework.urls')),
    path('api/token/', MobileTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/google/', GoogleLoginView.as_view(), name='token_google'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/token/logout/', LogoutView.as_view(), name='token_logout'),
    path('api/token/logout-all/', LogoutAllView.as_view(), name='token_logout_all'),
    path('api/financial-account/', FinancialAccountView.as_view(), name='financial-account'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)