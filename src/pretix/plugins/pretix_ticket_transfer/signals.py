import json
from decimal import Decimal
from django.dispatch import receiver
from django.template.loader import get_template
from django.middleware import csrf
from django.urls import resolve, reverse
from django import forms
from django.utils.html import escape
from django.utils.translation import gettext_lazy as _
from django.utils.safestring import mark_safe
from i18nfield.strings import LazyI18nString
from pretix.base.models import Order, ItemVariation
from pretix.base.signals import logentry_display, allow_ticket_download
from pretix.base.settings import settings_hierarkey, LazyI18nStringList
from pretix.base.templatetags.rich_text import rich_text
from pretix.base.templatetags.money import money_filter
from pretix.presale.signals import order_info_top, order_info
from pretix.control.signals import nav_event, nav_event_settings, order_search_forms

from .user_split import user_split_positions, TICKET_TRANSFER_START, TICKET_TRANSFER_DONE, TICKET_TRANSFER_SENT
from .utils import get_confirm_messages


settings_hierarkey.add_default("pretix_ticket_transfer_confirm_texts", '[]', LazyI18nStringList)
settings_hierarkey.add_default("pretix_ticket_transfer_global_confirm_texts", 'True', bool)


@receiver(signal=logentry_display, dispatch_uid="ticket_transfer_logentry_display")
def pretixcontrol_logentry_display(sender, logentry, **kwargs):
  event_type = logentry.action_type
  data = json.loads(logentry.data)

  event = sender

  if event_type == 'pretix_ticket_transfer.changed.split':
     old_item = str(event.items.get(pk=data['old_item']))
     if data['old_variation']:
         old_item += ' - ' + str(ItemVariation.objects.get(pk=data['old_variation']))
     url = reverse('control:event.order', kwargs={
         'event': event.slug,
         'organizer': event.organizer.slug,
         'code': data['new_order']
     })
     text = _('The order has been changed:')
     return mark_safe(escape(text) + ' ' + _('Position #{posid} ({old_item}, {old_price}) split into new order: {order}').format(
         old_item=escape(old_item),
         posid=data.get('positionid', '?'),
         order='<a href="{}">{}</a>'.format(url, data['new_order']),
         old_price=money_filter(Decimal(data['old_price']), event.currency),
     ))



  plains = {
    'pretix.event.order.email.ticket_transfer_recipient': _('Ticket transfer recipient email sent'),
    'pretix.event.order.email.ticket_transfer_sender': _('Ticket transfer sender email sent'),
    'pretix_ticket_transfer.changed.split_from': _('This order has been created by splitting the order {order}').format(order=data.get('original_order'))
  }

  if event_type in plains:
    return plains[event_type]


@receiver(order_info_top, dispatch_uid="ticket_transfer_order_info_target")
def orderinfo_target(sender, order, request, **kwargs):
  ctx = {
    'order': order,
    'event': sender,
    'title': str( sender.settings.get('pretix_ticket_transfer_title', as_type=LazyI18nString )),
    'csrf_token': csrf.get_token( request ) }

  if order.meta_info_data and order.meta_info_data.get('ticket_transfer'):
    if order.meta_info_data.get('ticket_transfer') == TICKET_TRANSFER_START:
      ctx['message'] = str( rich_text( sender.settings.get( 'pretix_ticket_transfer_recipient_message', as_type=LazyI18nString )))
      ctx['confirm_messages'] = get_confirm_messages(sender)
      template = get_template( 'pretix_ticket_transfer/order_info_accept.html' )
      return template.render( ctx )

    elif order.meta_info_data.get('ticket_transfer') == TICKET_TRANSFER_DONE:
      ctx['message'] = str(rich_text( sender.settings.get('pretix_ticket_transfer_recipient_done_message', as_type=LazyI18nString )))
      template = get_template( 'pretix_ticket_transfer/order_info_done.html' )
      if ctx['message']:
        return template.render( ctx )

  elif order.meta_info_data and order.meta_info_data.get('ticket_transfer_sent'):
    ctx['message'] = str(rich_text( sender.settings.get('pretix_ticket_transfer_done_message', as_type=LazyI18nString )))
    template = get_template( 'pretix_ticket_transfer/order_info_done.html' )
    if ctx['message']:
      return template.render( ctx )

  return False

@receiver(order_info, dispatch_uid="ticket_transfer_order_info_source")
def orderinfo_source(sender, order, request, **kwargs):
  if order.status != Order.STATUS_PAID and order.status != Order.STATUS_CANCELED:
    return False

  if order.meta_info_data.get('ticket_transfer') == TICKET_TRANSFER_START:
    return False

  event = order.event
  pos = []
  log = []

  pos = user_split_positions( order )

  logentries = order.all_logentries( )
  for l in logentries:
    if l.action_type == 'pretix.event.order.changed.split':
      data = json.loads( l.data )
      old_item = str( event.items.get(pk=data['old_item']) )
      if data['old_variation']:
        old_item += ' - ' + str( ItemVariation.objects.get(pk=data['old_variation']) )
        log.append( mark_safe( '{old_item}, {old_price}'.format(
            old_item=escape(old_item),
            old_price=money_filter(Decimal(data['old_price']), event.currency) )))

  if not len( pos ) and not len( log ):
    return False

  ctx = {
      'csrf_token': csrf.get_token(request),
      'order': order,
      'pos': pos,
      'log': log,
      'event': sender,
      'title': str( sender.settings.get('pretix_ticket_transfer_title', as_type=LazyI18nString )),
      'message': str(rich_text( sender.settings.get('pretix_ticket_transfer_message', as_type=LazyI18nString ))),
      'url': False }

  template = get_template( 'pretix_ticket_transfer/order_info.html' )
  return template.render( ctx )

@receiver(allow_ticket_download, dispatch_uid="ticket_transfer_allow_ticket_download")
def ticket_transfer_allow_ticket(sender, **kwargs):
    order = kwargs.get('order')
    if order.meta_info_data.get('ticket_transfer') == TICKET_TRANSFER_START:
      return False
    return True

@receiver(nav_event_settings, dispatch_uid='ticket_transfer_nav_settings')
def navbar_settings(sender, request, **kwargs):
  url = resolve(request.path_info)
  return [{
    'label': _('Ticket transfer'),
    'url': reverse('plugins:pretix_ticket_transfer:settings', kwargs={
      'event': request.event.slug,
      'organizer': request.organizer.slug }),
    'active': url.namespace == 'plugins:pretix_ticket_transfer' and url.url_name == 'settings' }]


@receiver(nav_event, dispatch_uid="ticket_transfer_nav_info")
def navbar_info(sender, request, **kwargs):
    url = resolve(request.path_info)
    if not request.user.has_event_permission(
        request.organizer, request.event, "can_change_event_settings"
    ):
        return []
    return [
        {
            "label": _("Ticket Transfer"),
            "icon": "random",
            "url": reverse(
                "plugins:pretix_ticket_transfer:stats",
                kwargs={
                    "event": request.event.slug,
                    "organizer": request.organizer.slug,
                },
            ),
            "active": url.namespace == "plugins:pretix_ticket_transfer"
            and url.url_name == "stats",
        }
    ]

class TransferSearchForm(forms.Form):
    ticket_transfer = forms.ChoiceField(
        required=False,
        label=_("Ticket Transfers"),
        choices=(
            ("", "--------"),
            ("0", _("no transfer")),
            ("1", _("open transfer")),
            ("2", _("finalized transfer")),
        ),
    )
    ticket_transfer_sent = forms.ChoiceField(
        required=False,
        label=_("Ticket Transfers Outgoing"),
        choices=(
            ("", "--------"),
            ("0", _("no transfer")),
            ("23", _("sent transfer")),
        ),
    )

    def __init__(self, *args, event=None, **kwargs):
        self.event = event
        super().__init__(*args, **kwargs)

    def filter_qs(self, queryset):
        print(self.cleaned_data)
        status = self.cleaned_data.get("ticket_transfer")
        if status:
            if status == str(TICKET_TRANSFER_START):
                queryset = queryset.filter(
                  meta_info__contains='"ticket_transfer": 1',
                )
            if status == str(TICKET_TRANSFER_DONE):
                queryset = queryset.filter(
                  meta_info__contains='"ticket_transfer": 2',
                )
        sent = self.cleaned_data.get("ticket_transfer_sent")
        if sent:
            print(f'sent {sent}')
            if sent == str(TICKET_TRANSFER_SENT):
                queryset = queryset.filter(
                  meta_info__contains='"ticket_transfer_sent": 23',
                )

        return queryset

    def filter_to_strings(self):
        status = self.cleaned_data.get("ticket_transfer")
        ticket_transfer_string = {
            "": "",
            "0": _("no Ticket Transfer"),
            "1": _("open Ticket Transfer"),
            "2": _("finalized Ticket Transfer"),
        }[status]

        result = []
        if ticket_transfer_string:
            result.append(ticket_transfer_string)
        return result

@receiver(order_search_forms)
def ticket_transfer_search_forms(request, sender, **kwargs):
    return TransferSearchForm(request.GET, event=sender, prefix="ticket_transfer")

