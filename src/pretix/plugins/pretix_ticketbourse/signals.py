import json
from typing import Union
import inspect

from django import forms
from django.db.models import Exists, OuterRef
from django.http import HttpRequest
from django.shortcuts import redirect
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _
from django.utils.timezone import now
from django.urls import resolve, reverse
from django.template.loader import get_template

from django_scopes import scopes_disabled
from pretix.base.models import Event, Order
from pretix.base.services.orders import mark_order_expired
from pretix.base.services.tickets import invalidate_cache as invalidate_ticket_cache
from pretix.base.signals import order_placed, order_approved, order_canceled, order_changed, order_denied, order_expired, order_paid, periodic_task, logentry_display
from pretix.presale.checkoutflow import TemplateFlowStep
from pretix.presale.views import CartMixin
from pretix.presale.views.cart import cart_session
from pretix.presale.signals import checkout_flow_steps, checkout_confirm_page_content, order_info, order_meta_from_request
from pretix.control.signals import nav_event, nav_event_settings, order_search_forms, order_info as order_info_control

from .utils import cart_can_buy, lock, is_selling_active
from .models import BuyRequest, SellRequest
from .tasks import run_ticket_bourse


class TicketBourseFlowStep(CartMixin, TemplateFlowStep):
    priority = 180
    identifier = 'ticketbourse'
    template_name = 'pretix_ticketbourse/presale/buy/checkout.html'
    icon = 'handshake-o'
    label = _('Ticket:Bourse')

    def post(self, request):
        self.request = request
        self.cart_session['ticketbourse_mode'] = request.POST.get('ticketbourse_mode', '')
        return redirect(self.get_next_url(request))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['cart'] = self.get_cart()
        ctx['cart_session'] = self.cart_session
        ctx['selected'] = self.cart_session.get('ticketbourse_mode', '')
        return ctx

    def is_applicable(self, request):
        return cart_can_buy(request)

    def is_completed(self, request, warn=False):
        self.request = request
        return self.cart_session.get('ticketbourse_mode', '') in BuyRequest.Modes


@receiver(signal=checkout_flow_steps, dispatch_uid='ticketbourse_checkout_flow_step_buyer')
def checkout_flow_step_buyer(sender, **kwargs):
    return TicketBourseFlowStep


@receiver(signal=checkout_confirm_page_content, dispatch_uid='ticketbourse_checkout_confirm_buyer')
def checkout_confirm_buyer(sender, request, **kwargs):
    cs = cart_session(request)
    if not cs.get('ticketbourse_mode', ''):
        return
    template = get_template('pretix_ticketbourse/presale/buy/checkout_confirm.html')
    ctx = {}
    ctx['request'] = request
    ctx['event'] = request.event
    ctx['mode'] = cs.get('ticketbourse_mode', '')
    return template.render(ctx)


@receiver(order_meta_from_request, dispatch_uid='ticketbourse_order_meta_buyer')
def order_meta_buyer(sender: Event, request: HttpRequest, **kwargs):
    cs = cart_session(request)
    if not cs.get('ticketbourse_mode', ''):
        return {}
    return {'ticketbourse_mode': cs.get('ticketbourse_mode')}


@receiver(order_placed, dispatch_uid='ticketbourse_order_placed_buyer')
def order_placed_buyer(sender: Event, order: Order, **kwargs):
    meta_info = order.meta_info_data
    if not meta_info or 'ticketbourse_mode' not in meta_info:
        return
    # No locking here, because we cannot fail
    BuyRequest.get_for_order(order).start(meta_info['ticketbourse_mode'])
    del meta_info['ticketbourse_mode']
    order.meta_info = json.dumps(meta_info)
    order.save(update_fields=['meta_info'])
    run_ticket_bourse.apply_async(kwargs={'event_id': order.event.id})


@receiver(order_approved, dispatch_uid='ticketbourse_order_signal')
@receiver(order_canceled, dispatch_uid='ticketbourse_order_signal')
@receiver(order_changed, dispatch_uid='ticketbourse_order_signal')
@receiver(order_denied, dispatch_uid='ticketbourse_order_signal')
@receiver(order_expired, dispatch_uid='ticketbourse_order_signal')
@receiver(order_paid, dispatch_uid='ticketbourse_order_signal')
def order_signal(sender: Event, order: Order, **kwargs):
    # Make sure CachedCombinedTicket is invalidated on approved/paid, after we extended a paid order
    invalidate_ticket_cache.apply_async(kwargs={'event': sender.pk, 'order': order.pk})

    changed = False
    buy_request = BuyRequest.get_for_order(order)
    if buy_request and buy_request.state != buy_request.effective_state:
        with lock(sender) as lock_acquired:
            if not lock_acquired:
                return # periodic fix_request_states will fix it
            buy_request.update_state()
            changed = True
    sell_request = SellRequest.get_for_order(order)
    if sell_request and sell_request.state != sell_request.effective_state:
        with lock(sender) as lock_acquired:
            if not lock_acquired:
                return # periodic fix_request_states will fix it
            sell_request.update_state()
            changed = True
    if changed:
        run_ticket_bourse.apply_async(kwargs={'event_id': order.event.id})


@receiver(periodic_task, dispatch_uid='ticketbourse_periodic_task_handler')
@scopes_disabled()
def periodic_task_handler(sender, **kwargs):
    for event in Event.objects.filter(plugins__contains='pretix_ticketbourse'):
        if event.settings.get('payment_term_expire_automatically', as_type=bool):
            # pretix.base.services.expire_orders does not expire orders with
            # a cancellation fee. This (kind of) makes sense if a customer
            # owes us the cancellation fee and we would give up on the claim if
            # the order would expire (maybe for free orders where cancelling
            # incurs a fee?).
            # In our case orders can have a history of multiple partial ticket
            # bourse sells (each with it's own fee) and buys, so pending/expiring
            # orders with a (paid) cancellation fee is a common occurence. We
            # collect the cancellation fee by deducting them from refunds, so
            # giving up on the claims is not really a problem.
            # Expiring orders on our own also ensures that expiring happens
            # before we do batched auto approvels.
            # Note: pretix.base.services.expire_orders still likely runs first!
            expired_orders = Order.objects.filter(
                event=event,
                expires__lt=now(),
                status=Order.STATUS_PENDING,
                valid_if_pending=False,
                require_approval=False,
                ticketbourse_buy_requests__state=BuyRequest.States.APPROVED,
            )
            for order in expired_orders:
                if now() >= order.payment_term_expire_date:
                    mark_order_expired(order)

        buy_requests = BuyRequest.objects.filter(
            order__event=event,
            state__in=(BuyRequest.States.REQUESTED, BuyRequest.States.APPROVED)
        ).prefetch_related('order')
        for buy_request in buy_requests:
            if buy_request.state != buy_request.effective_state:
                with lock(event) as lock_acquired:
                    if not lock_acquired:
                        continue # next run will fix it
                    # Loop can take a while, make sure we don't update_state() with stale or inconsistent data
                    buy_request = BuyRequest.objects.get(id=buy_request.id)
                    buy_request.update_state()
        sell_requests = SellRequest.objects.filter(
            order__event=event,
            state__in=(SellRequest.States.REQUESTED, SellRequest.States.BUY_REQUEST_APPROVED)
        ).prefetch_related('order')
        for sell_request in sell_requests:
            if sell_request.state != sell_request.effective_state:
                with lock(event) as lock_acquired:
                    if not lock_acquired:
                        continue # next run will fix it
                    # Loop can take a while, make sure we don't update_state() with stale or inconsistent data
                    sell_request = SellRequest.objects.get(id=sell_request.id)
                    sell_request.update_state()

        run_ticket_bourse.apply_async(kwargs={'event_id': event.id, 'after_periodic_task_handler': True})


@receiver(order_info, dispatch_uid='ticketbourse_order_info')
def order_info_receiver(sender: Event, order: Order, **kwargs):
    buy_request = BuyRequest.get_for_order(order)
    latest_buy_request = BuyRequest.objects.filter(order=order).order_by('request_date').last()
    sell_request = SellRequest.get_for_order(order)
    latest_sell_request = SellRequest.objects.filter(order=order).order_by('request_date').last()
    if latest_buy_request and latest_sell_request:
        if latest_buy_request.request_date > latest_sell_request.request_date:
            latest_sell_request = None
        else:
            latest_buy_request = None
    if buy_request or sell_request or buy_request.can_start or buy_request.can_start_extended or sell_request.can_start:
        template = get_template('pretix_ticketbourse/presale/order_info.html')
        ctx = {
            'order': order,
            'event': sender,
            'buy_request': buy_request,
            'latest_buy_request': latest_buy_request,
            'sell_request': sell_request,
            'latest_sell_request': latest_sell_request,
            'is_selling_active': is_selling_active(order.event),
        }
        return template.render(ctx)
    return False


@receiver(logentry_display, dispatch_uid='ticketbourse_logentry_display')
def logentry_display_receiver(sender, logentry, *args, **kwargs):
    if logentry.action_type == 'pretix_ticketbourse.buy_request.started':
        if logentry.parsed_data.get('mode') == BuyRequest.Modes.DIRECT:
            return _('Direct buy request started.')
        if logentry.parsed_data.get('mode') == BuyRequest.Modes.AUTO:
            return _('Auto buy request started.')
    if logentry.action_type == 'pretix_ticketbourse.buy_request.direct_forced':
        return _('Buy request mode forced to "direct" by seller starting a sell request with this order\'s direct code.')
    if logentry.action_type == 'pretix_ticketbourse.buy_request.deleted':
        return _('Buy request deleted.')
    if logentry.action_type == 'pretix_ticketbourse.buy_request.approved':
        return _('Order approved by buy request.')
    if logentry.action_type == 'pretix_ticketbourse.buy_request.approved.order.email':
        return _('An email has been sent to notify the user that the order was approved by the ticket bourse.')
    if logentry.action_type == 'pretix_ticketbourse.buy_request.paid':
        return _('Order paid, buy request waiting for completion of sell request.')
    if logentry.action_type == 'pretix_ticketbourse.buy_request.completed':
        return _('Buy request completed.')
    if logentry.action_type == 'pretix_ticketbourse.buy_request.failed':
        return _('Buy request failed.')
    if logentry.action_type == 'pretix_ticketbourse.buy_request.blocked':
        return _('Buy request was blocked.')
    if logentry.action_type == 'pretix_ticketbourse.buy_request.unblocked':
        return _('Buy request was unblocked.')

    if logentry.action_type == 'pretix_ticketbourse.sell_request.started':
        if logentry.parsed_data.get('mode') == BuyRequest.Modes.DIRECT:
            code = logentry.parsed_data.get('code', '???')
            return _('Direct sell request started with target order %(code)s.')%{'code': code}
        if logentry.parsed_data.get('mode') == BuyRequest.Modes.AUTO:
            return _('Auto sell request started.')
    if logentry.action_type == 'pretix_ticketbourse.sell_request.deleted':
        return _('Sell request canceled.')
    if logentry.action_type == 'pretix_ticketbourse.sell_request.buy_request_approved':
        return _('Sell request target order approved.')
    if logentry.action_type == 'pretix_ticketbourse.sell_request.completed':
        return _('Sell request completed.')
    if logentry.action_type == 'pretix_ticketbourse.sell_request.completed.order.email':
        return _('An email has been sent to notify the user that the ticket bourse request was completed and the order was canceled.')
    if logentry.action_type == 'pretix_ticketbourse.sell_request.failed':
        if logentry.parsed_data.get('state') == SellRequest.States.FAILED_CANCELED:
            return _('Sell request failed: Order was canceled.')
        if logentry.parsed_data.get('state') == SellRequest.States.FAILED_CHECKED_IN:
            return _('Sell request failed: Order was checked in.')
        if logentry.parsed_data.get('state') == SellRequest.States.FAILED_BUY_REQUEST_FAILED:
            return _('Direct sell request failed: Target order expired or was canceled.')
        return _('Direct sell request failed')
    if logentry.action_type == 'pretix_ticketbourse.sell_request.failed_buyer.order.email':
        return _('An email has been sent to notify the user that the ticket bourse request failed because the buyer did not pay in time or canceled his order.')

    if logentry.action_type == 'pretix_ticketbourse.result':
        return _('Ticketbourse result: %(to_be_approved)d to be approved, %(to_be_canceled)d to be canceled.')%logentry.parsed_data


@receiver(nav_event_settings, dispatch_uid='ticketbourse_nav_event_settings')
def nav_event_settings_receiver(sender, request, **kwargs):
    url = resolve(request.path_info)
    return [
        {
            'label': _('Ticket Bourse'),
            'url': reverse(
                'plugins:pretix_ticketbourse:event.settings.ticketbourse',
                kwargs={
                    'event': request.event.slug,
                    'organizer': request.organizer.slug,
                },
            ),
            'active': url.namespace == 'plugins:pretix_ticketbourse' and url.url_name == 'event.settings.ticketbourse',
        },
    ]


class OrderSearchForm(forms.Form):
    buy_request_state = forms.ChoiceField(
        required=False,
        label=_('Ticket bourse buy state'),
        choices=[
            ('', '--------'),
            ('none', _('No request')),
            ('any', _('Any state (request exists)')),
        ] + BuyRequest.States.choices,
    )
    buy_request_mode = forms.ChoiceField(
        required=False,
        label=_('Ticket bourse buy mode'),
        choices=[
            ('', '--------'),
        ] + BuyRequest.Modes.choices,
    )
    sell_request_state = forms.ChoiceField(
        required=False,
        label=_('Ticket bourse sell state'),
        choices=[
            ('', '--------'),
            ('none', _('No request')),
            ('any', _('Any state (request exists)')),
            ('any_failed', _('Any failed state')),
        ] + SellRequest.States.choices,
    )
    sell_request_mode = forms.ChoiceField(
        required=False,
        label=_('Ticket bourse sell mode'),
        choices=[
            ('', '--------'),
        ] + SellRequest.Modes.choices,
    )
    has_blocked_buy_request = forms.ChoiceField(
        required=False,
        label=_('Has blocked buy request'),
        choices=[
            ('', '--------'),
            ('yes', _('Yes')),
            ('no', _('No')),
        ],
    )

    def __init__(self, *args, event=None, **kwargs):
        self.event = event
        super().__init__(*args, **kwargs)

    def filter_qs(self, qs):
        buy_request_state = self.cleaned_data.get('buy_request_state')
        if buy_request_state == 'none':
            qs = qs.exclude(
                Exists(BuyRequest.objects.filter(order_id=OuterRef('pk')))
            )
        elif buy_request_state == 'any':
            qs = qs.filter(
                Exists(BuyRequest.objects.filter(order_id=OuterRef('pk')))
            )
        elif buy_request_state in BuyRequest.States:
            qs = qs.filter(
                Exists(BuyRequest.objects.filter(
                    order_id=OuterRef('pk'),
                    state=BuyRequest.States[buy_request_state]
                ))
            )

        buy_request_mode = self.cleaned_data.get('buy_request_mode')
        if buy_request_mode in BuyRequest.Modes:
            qs = qs.filter(
                Exists(BuyRequest.objects.filter(
                    order_id=OuterRef('pk'),
                    mode=BuyRequest.Modes[buy_request_mode]
                ))
            )

        sell_request_state = self.cleaned_data.get('sell_request_state')
        if sell_request_state == 'none':
            qs = qs.exclude(
                Exists(SellRequest.objects.filter(order_id=OuterRef('pk')))
            )
        elif sell_request_state == 'any':
            qs = qs.filter(
                Exists(SellRequest.objects.filter(order_id=OuterRef('pk')))
            )
        elif sell_request_state == 'any_failed':
            qs = qs.filter(
                Exists(SellRequest.objects.filter(
                    order_id=OuterRef('pk'),
                    state__in=SellRequest.FAILED_STATES
                ))
            )
        elif sell_request_state in SellRequest.States:
            qs = qs.filter(
                Exists(SellRequest.objects.filter(
                    order_id=OuterRef('pk'),
                    state=SellRequest.States[sell_request_state]
                ))
            )

        sell_request_mode = self.cleaned_data.get('sell_request_mode')
        if sell_request_mode in SellRequest.Modes:
            qs = qs.filter(
                Exists(SellRequest.objects.filter(
                    order_id=OuterRef('pk'),
                    mode=SellRequest.Modes[sell_request_mode]
                ))
            )

        has_blocked_buy_request = self.cleaned_data.get('has_blocked_buy_request')
        if has_blocked_buy_request == 'yes':
            qs = qs.filter(
                Exists(BuyRequest.objects.filter(
                    order_id=OuterRef('pk'),
                    blocked=True,
                ))
            )
        elif has_blocked_buy_request == 'no':
            qs = qs.exclude(
                Exists(BuyRequest.objects.filter(
                    order_id=OuterRef('pk'),
                    blocked=True,
                ))
            )

        return qs

    def filter_to_strings(self):
        results = []
        buy_request_state = self.cleaned_data.get('buy_request_state')
        if buy_request_state == 'none':
            results.append(_('Has no ticket bourse buy request'))
        elif buy_request_state == 'any':
            results.append(_('Has ticket bourse buy request (any state)'))
        elif buy_request_state in BuyRequest.States:
            results.append(_('Has ticket bourse buy request with state "%(state)s"')%{'state': BuyRequest.States[buy_request_state].label})
        buy_request_mode = self.cleaned_data.get('buy_request_mode')
        if buy_request_mode in BuyRequest.Modes:
            results.append(_('Has ticket bourse buy request with mode "%(mode)s"')%{'mode': BuyRequest.Modes[buy_request_mode].label})

        sell_request_state = self.cleaned_data.get('sell_request_state')
        if sell_request_state == 'none':
            results.append(_('Has no ticket bourse sell request'))
        elif sell_request_state == 'any':
            results.append(_('Has ticket bourse sell request (any state)'))
        elif sell_request_state == 'any_failed':
            results.append(_('Has ticket bourse sell request (any failed state)'))
        elif sell_request_state in SellRequest.States:
            results.append(_('Has ticket bourse sell request with state "%(state)s"')%{'state': SellRequest.States[sell_request_state].label})
        sell_request_mode = self.cleaned_data.get('sell_request_mode')
        if sell_request_mode in SellRequest.Modes:
            results.append(_('Has ticket bourse sell request with mode "%(mode)s"')%{'mode': SellRequest.Modes[sell_request_mode].label})

        return results


@receiver(order_search_forms, dispatch_uid='ticketbourse_order_search_forms')
def order_search_forms_receiver(sender, request, **kwargs):
    return OrderSearchForm(request.GET, event=sender, prefix='ticketbourse')


@receiver(order_info_control, dispatch_uid='ticketbourse_order_info_control')
def order_info_control_receiver(sender: Event, order: Order, request, **kwargs):
    requests = []
    requests += list(SellRequest.objects.filter(order=order).all())
    requests += list(BuyRequest.objects.filter(order=order).all())
    requests.sort(key=lambda request: request.request_date, reverse=True)
    if requests:
        template = get_template('pretix_ticketbourse/control/order_info.html')
        ctx = {
            'order': order,
            'event': sender,
            'requests': requests,
        }
        return template.render(ctx, request=request)
    return False


@receiver(nav_event, dispatch_uid='ticketbourse_control_nav')
def control_nav_item(sender, request, **kwargs):
    url = resolve(request.path_info)
    if not request.user.has_event_permission(
        request.organizer, request.event,'can_view_orders'
    ):
        return []
    return [
        {
            'label': _('Ticket Bourse'),
            'icon': 'handshake-o',
            'url': reverse(
                'plugins:pretix_ticketbourse:event.ticketbourse.stats',
                kwargs={
                    'event': request.event.slug,
                    'organizer': request.organizer.slug,
                },
            ),
            'active': url.namespace == 'plugins:pretix_ticketbourse'
            and url.url_name == 'event.ticketbourse.stats',
        }
    ]


# Pretix allows users to pay for expired orders if there is still quota left.
# We don't use quotas and rely on STATUS_EXPIRED being final.

old__is_still_available = Order._is_still_available

def _is_still_available(self, now_dt=None, count_waitinglist=True, lock=False, force=False,
                        check_voucher_usage=False, check_memberships=False) -> Union[bool, str]:
    result = old__is_still_available(self,
        now_dt=now_dt,
        count_waitinglist=count_waitinglist,
        force=force,
        check_voucher_usage=check_voucher_usage,
        check_memberships=check_memberships
    )
    if 'pretix_ticketbourse' not in self.event.plugins.split(',') or result is not True or force:
        return result

    if self.status == Order.STATUS_EXPIRED:
        buy_request = BuyRequest.get_for_order(self)
        if buy_request and buy_request.approve_date and buy_request.effective_state == BuyRequest.States.FAILED:
            return str(_('Ticketbourse buy request expired.'))
    return result

def check_monkey_patch_signature():
    sig = inspect.signature(old__is_still_available)
    params = sig.parameters.copy()
    # Type annotation of `now_dt` is DeferredAttribute instance, because
    # Order.datetime shadows the top-level datetime.datetime import
    params['now_dt'] = params['now_dt'].replace(annotation=inspect.Parameter.empty)
    sig = sig.replace(parameters=params.values())
    return sig == inspect.signature(_is_still_available)

assert check_monkey_patch_signature()
Order._is_still_available = _is_still_available
