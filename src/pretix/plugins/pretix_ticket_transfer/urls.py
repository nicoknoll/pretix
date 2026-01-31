from django.urls import re_path
from pretix.multidomain import event_url

from .views import (
    TicketTransferSettingsView,
    TicketTransfer,
    TicketTransferAccept,
    TicketTransferStats
)

event_patterns = [
    event_url(
        r"^order/(?P<order>[^/]+)/(?P<secret>[A-Za-z0-9]+)/ticket_transfer$",
        TicketTransfer.as_view(),
        name="generate",
    ),
    event_url(
        r"^order/(?P<order>[^/]+)/(?P<secret>[A-Za-z0-9]+)/ticket_transfer_accept$",
        TicketTransferAccept.as_view(),
        name="accept",
    ),
]

urlpatterns = [
    re_path(r'^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/ticket_transfer/settings$',
        TicketTransferSettingsView.as_view(), name='settings'),
    re_path(r'^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/ticket_transfer/stats$',
        TicketTransferStats.as_view(), name='stats'),
]
