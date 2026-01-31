import json
import ast
import operator
from django import forms
from django.http import Http404
from django.utils.http import urlencode
from django.utils.functional import cached_property
from django.views.generic import TemplateView
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect
from django_scopes import scope
from django.middleware import csrf
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.contrib import messages
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from i18nfield.strings import LazyI18nString
from pretix.base.models import Event, Order, Item, OrderPosition
from pretix.base.forms import SettingsForm
from pretix.base.settings import LazyI18nStringList
from pretix.control.permissions import EventPermissionRequiredMixin
from pretix.control.views.event import EventSettingsFormView, EventSettingsViewMixin
from pretix.control.forms.event import ConfirmTextFormset
from pretix.presale.views import EventViewMixin
from pretix.presale.views.order import OrderDetailMixin
from pretix.multidomain.urlreverse import eventreverse
from pretix.base.templatetags.rich_text import rich_text
from i18nfield.forms import I18nFormField, I18nTextarea

from .user_split import user_split, user_split_positions, TICKET_TRANSFER_START, TICKET_TRANSFER_DONE, TICKET_TRANSFER_SENT
from .utils import get_confirm_messages

class TicketTransferSettingsForm(SettingsForm):
    pretix_ticket_transfer_title = I18nFormField(
        label=_("Order info title"),
        required=False,
        widget=I18nTextarea,
        help_text=_("Panel title"),
        widget_kwargs={'attrs': { 'rows': '1' }} )

    pretix_ticket_transfer_message = I18nFormField(
        label=_("Sender - orderinfo message"),
        required=False,
        widget=I18nTextarea,
        help_text=_("Order info for sender"),
        widget_kwargs={'attrs': { 'rows': '8' }} )
    pretix_ticket_transfer_step2_message = I18nFormField(
        label=_("Sender - step2 message"),
        required=False,
        widget=I18nTextarea,
        help_text=_("Order info for sender select email step"),
        widget_kwargs={'attrs': { 'rows': '8' }} )
    pretix_ticket_transfer_step3_message = I18nFormField(
        label=_("Sender - step3 message"),
        required=False,
        widget=I18nTextarea,
        help_text=_("Order info for sender confirm step"),
        widget_kwargs={'attrs': { 'rows': '8' }} )
    pretix_ticket_transfer_done_message = I18nFormField(
        label=_("Sender - done message"),
        required=False,
        widget=I18nTextarea,
        help_text=_("Order info for sender after all tickets are sold"),
        widget_kwargs={'attrs': { 'rows': '8' }} )

    pretix_ticket_transfer_recipient_message = I18nFormField(
        label=_("Recipient - orderinfo message"),
        required=False,
        widget=I18nTextarea,
        help_text=_("Order info for recipient"),
        widget_kwargs={'attrs': { 'rows': '8' }} )
    pretix_ticket_transfer_recipient_done_message = I18nFormField(
        label=_("Recipient - done message"),
        required=False,
        widget=I18nTextarea,
        help_text=_("Order info for recipient after accepting"),
        widget_kwargs={'attrs': { 'rows': '8' }} )


    pretix_ticket_transfer_sender_subject = I18nFormField(
        label=_("Sender - email subject"),
        required=False,
        widget=I18nTextarea,
        help_text=_("Subject for sender email"),
        widget_kwargs={'attrs': { 'rows': '1' }} )
    pretix_ticket_transfer_sender_mailtext = I18nFormField(
        label=_("Sender - email text"),
        required=False,
        widget=I18nTextarea,
        help_text=_('placeholders: {list}'.format(list = ', '.join(['{code}', '{event}', '{event_slug}', '{name}', '{total}', '{total_with_currency}', '{url}']))),
        widget_kwargs={'attrs': { 'rows': '8' }} )

    pretix_ticket_transfer_recipient_subject = I18nFormField(
        label=_("Recipient - email subject"),
        required=False,
        widget=I18nTextarea,
        help_text=_("Subject for recipient email"),
        widget_kwargs={'attrs': { 'rows': '1' }} )
    pretix_ticket_transfer_recipient_mailtext = I18nFormField(
        label=_("Recipient - email text"),
        required=False,
        widget=I18nTextarea,
        help_text=_('placeholders: {list}'.format(list = ', '.join(['{code}', '{event}', '{event_slug}', '{name}', '{total}', '{total_with_currency}', '{url}']))),
        widget_kwargs={'attrs': { 'rows': '8' }} )

    def __init__(self, *args, **kwargs):
       event = self.event = kwargs.pop('event')
       super().__init__(*args, **kwargs)

       self.fields['pretix_ticket_transfer_items_all'] = forms.BooleanField(
           label=_("All products (including newly created ones)"),
           required=False )

       if self.initial.get( 'pretix_ticket_transfer_items_all') == None:
         self.initial['pretix_ticket_transfer_items_all'] = True

       if self.initial.get( 'pretix_ticket_transfer_items') != None:
         self.initial['pretix_ticket_transfer_items'] = json.loads( self.initial['pretix_ticket_transfer_items'] )

       self.fields['pretix_ticket_transfer_items'] = forms.ModelMultipleChoiceField(
         widget=forms.CheckboxSelectMultiple(
             attrs={
               'data-inverse-dependency': '<[name$=pretix_ticket_transfer_items_all]',
               'class': 'scrolling-multiple-choice' }),
         label=_('Items'),
         required=False,
         queryset=event.items.all() )

       self.fields['pretix_ticket_transfer_global_confirm_texts'] = forms.BooleanField(label=_("Show general confirmation texts"), required=False)

    def clean(self):
        d = super().clean()
        if d['pretix_ticket_transfer_items_all']:
          d['pretix_ticket_transfer_items'] = None
        else:
          d['pretix_ticket_transfer_items'] = json.dumps([ i.id for i in d['pretix_ticket_transfer_items'] ])
        return d

class TicketTransferSettingsView(EventSettingsViewMixin, EventSettingsFormView):
    model = Event
    permission = 'can_change_settings'
    form_class = TicketTransferSettingsForm
    template_name = 'pretix_ticket_transfer/settings.html'

    def get_context_data(self, *args, **kwargs):
        ctx = super().get_context_data(*args, **kwargs)
        ctx['confirm_texts_formset'] = self.confirm_texts_formset
        return ctx

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        if not self.confirm_texts_formset.is_valid():
            messages.error(self.request, _('We could not save your changes. See below for details.'))
            return self.render_to_response(self.get_context_data(form=self.get_form()))
        self.save_confirm_texts_formset()
        return super().post(request, *args, **kwargs)

    @cached_property
    def confirm_texts_formset(self):
        initial = [
            {"text": text, "ORDER": order}
            for order, text in enumerate(self.request.event.settings.pretix_ticket_transfer_confirm_texts)
        ]
        return ConfirmTextFormset(
            self.request.POST if self.request.method == "POST" else None,
            event=self.request.event,
            prefix="confirm-texts",
            initial=initial
        )

    def save_confirm_texts_formset(self):
        self.request.event.settings.pretix_ticket_transfer_confirm_texts = LazyI18nStringList(
            form_data['text'].data
            for form_data in sorted((d for d in self.confirm_texts_formset.cleaned_data if d), key=operator.itemgetter("ORDER"))
            if form_data and not form_data.get("DELETE", False)
        )

    def get_success_url(self, **kwargs):
        return reverse('plugins:pretix_ticket_transfer:settings', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['event'] = self.request.event
        return kwargs

class TicketTransfer(EventViewMixin, OrderDetailMixin, TemplateView):
    template_name = "pretix_ticket_transfer/transfer.html"

    def get_context_data(self, *args, **kwargs):
        ctx = super().get_context_data(*args, **kwargs)
        ctx['order'] = self.order
        ctx['orderpositions'] = user_split_positions( self.order )

        ctx['title'] = self.order.event.settings.get('pretix_ticket_transfer_title', as_type=LazyI18nString )
        ctx['message'] = str(rich_text( self.order.event.settings.get('pretix_ticket_transfer_step2_message', as_type=LazyI18nString )))
        return ctx

    def post(self, request, *args, **kwargs):
        if self.order.status != Order.STATUS_PAID:
          raise Http404()

        error = False
        pos = []
        pids = request.POST.getlist('pos[]')
        email = request.POST.get('email')
        email_repeat = request.POST.get('email_repeat')
        confirm = request.POST.get('confirm')
        step2 = request.POST.get('step2')

        ctx = self.get_context_data(*args, **kwargs)
        ctx['csrf_token'] = csrf.get_token(request)

        if pids:
          pos = user_split_positions( self.order, pids )
          if not len( pids ) == len( pos ):
            error = _("Invalid ticket selection")

        if not step2 and email:
          try:
            validate_email(email)
          except ValidationError:
            error = _("Please enter a valid email address")
          if email != email_repeat:
            error = _("The email addresses do not match")
          if not len( pids ):
            error = _("Please select ticket(s) for transfer")

          if error:
            messages.warning( self.request, error),
          elif not confirm:
            ctx['confirm'] = True
            ctx['message'] = str(rich_text( self.order.event.settings.get('pretix_ticket_transfer_step3_message', as_type=LazyI18nString )))
          else:
            user_split(self.order,pids,data={'email': email})
            messages.success( self.request, _('Ticket(s) transfered') ),
            return redirect(
                eventreverse(
                    self.request.event,
                    "presale:event.order",
                    kwargs={"order": self.order.code, "secret": self.order.secret} ))

        totalprice = 0
        for position in pos:
          totalprice+= position.price_with_addons

        ctx['pos'] = pos
        ctx['totalprice'] = totalprice
        ctx['email'] = email or ""
        ctx['email_repeat'] = email_repeat or ""

        return self.render_to_response(ctx)

class TicketTransferAccept(EventViewMixin, OrderDetailMixin, TemplateView):
    def post(self, request, *args, **kwargs):
        positions = self.order.positions.select_related('item')

        msgs = get_confirm_messages(self.request.event)
        for key, msg in msgs.items():
            if request.POST.get('confirm_{}'.format(key)) != 'yes':
                msg = str(_('You need to check all checkboxes to confirm the ticket transfer.'))
                messages.error(self.request, msg)
                return redirect(eventreverse(
                    self.request.event,
                    "presale:event.order",
                    kwargs={"order": self.order.code, "secret": self.order.secret}
                ))

        meta = self.order.meta_info_data
        meta['ticket_transfer'] = TICKET_TRANSFER_DONE
        meta.setdefault('confirm_messages', [])
        meta['confirm_messages'] += [str(msg) for msg in msgs.values()]
        self.order.meta_info = json.dumps(meta)
        self.order.save()

        for msg in msgs.values():
            self.order.log_action('pretix.event.order.consent', data={'msg': msg})
        messages.success(self.request, _('Ticket transfer completed'))

        return redirect(
            eventreverse(
                self.request.event,
                "presale:event.order",
                kwargs={"order": self.order.code, "secret": self.order.secret} ))

class TicketTransferStats(EventPermissionRequiredMixin, TemplateView):
    permission = "can_change_event_settings"
    template_name = "pretix_ticket_transfer/control/stats.html"

    def get_context_data(self, *args, **kwargs):
        ctx = super().get_context_data(*args, **kwargs)
        ctx['rows'] = []

        map = {
            TICKET_TRANSFER_START: 'start',
            TICKET_TRANSFER_DONE: 'done',
            TICKET_TRANSFER_SENT: 'sent',
        } 
        counter = {'all':0, 'start':0, 'done':0, 'sent':0}
        def count(*i):
          counter['all']+= 1
          for k in i:
            k = map.get(k,k)
            print(f'k {k}')
            counter[k] = counter.get(k,0) + 1


        orders = Order.objects.filter(
                event=self.request.event,
                meta_info__contains='"ticket_transfer":')
        for o in orders:
          count(o.meta_info_data.get('ticket_transfer'), f'{o.status}')
          #count(o.meta_info_data.get('ticket_transfer'))


        sent = Order.objects.filter(
                event=self.request.event,
                meta_info__contains='"ticket_transfer_sent": 23')
        for o in sent:
          count(o.meta_info_data.get('ticket_transfer_sent'), f'sent_{o.status}')

        print(counter)
        ctx['counter'] = counter

        return ctx


        #orders = Order.objects.raw("select id,code,meta_info from pretixbase_order where meta_info like '%%ticket_transfer%%'")

        #from django.db import connection
        #with connection.cursor() as cursor:
        #  cursor.execute(
        #      """
        #        select meta_info::json->'ticket_transfer' #>> '{}' as ticket_transfer from pretixbase_order where meta_info like '%ticket_transfer%'
        #      """)

        #orders = Order.objects.filter(order__meta_info__ticket_transfer)
        #orders = Order.objects.raw("select id,code,meta_info from pretixbase_order where meta_info like '%%ticket_transfer%%'")
