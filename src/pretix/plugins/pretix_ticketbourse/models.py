import logging
import datetime
from inspect import signature

import pytz
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _, pgettext_lazy
from django.utils.timezone import now

from pretix.base.models import Order, Voucher, OrderFee
from pretix.base.i18n import language
from pretix.base.email import get_email_context
from pretix.base.services.orders import approve_order, _cancel_order, OrderChangeManager, _try_auto_refund, OrderError
from pretix.base.services.mail import SendMailException

from .utils import (
    positions_can_buy,
    get_buyable_items_for_order,
    sellable_positions_queryset,
    count_sell_items_per_group,
    count_buy_items_per_group,
    get_missing_addons,
    get_cancellation_fee,
    CustomizedOrderChangeManager,
)

logger = logging.getLogger(__name__)


class BuyRequest(models.Model):
    IS_BUY_REQUEST = True

    order = models.ForeignKey('pretixbase.Order', on_delete=models.CASCADE, related_name='ticketbourse_buy_requests')
    positions = models.ManyToManyField('pretixbase.OrderPosition', related_name='ticketbourse_buy_requests')

    class Modes(models.TextChoices):
        AUTO = 'AUTO', _('Auto')
        DIRECT = 'DIRECT', _('Direct')

    mode = models.CharField(max_length=32, choices=Modes.choices, verbose_name=_('Mode'))

    class States(models.TextChoices):
        REQUESTED = 'REQUESTED', pgettext_lazy('ticketbourse_state', 'Requested')
        APPROVED = 'APPROVED', pgettext_lazy('ticketbourse_state', 'Approved') # Order was approved by the request
        PAID = 'PAID', pgettext_lazy('ticketbourse_state', 'Paid') # Only used for DIRECT
        COMPLETED = 'COMPLETED', pgettext_lazy('ticketbourse_state', 'Completed') # Order was paid after it was approved by the request
        ABORTED = 'ABORTED', pgettext_lazy('ticketbourse_state', 'Aborted') # Request was aborted by user
        FAILED = 'FAILED', pgettext_lazy('ticketbourse_state', 'Failed') # Order expired or was canceled while in REQUESTED or APPROVED state

    ACTIVE_STATES = (
        States.REQUESTED,
        States.APPROVED,
        States.PAID,
    )

    state = models.CharField(max_length=32, choices=States.choices, verbose_name=_('State'))

    # If extends_order is True:
    # - order.status was PAID before the request was started
    # - positions were added to the order by this request
    # If extends_order is False:
    # - order.status was WAITING FOR APPROVAL before the request was started
    # - positions were already part of the order before the request was started
    # - positions is the exact set of postitions the order had before the request (minus bundled addons)
    extends_order = models.BooleanField(default=False)

    request_date = models.DateTimeField(null=True)
    approve_date = models.DateTimeField(null=True)
    pay_date = models.DateTimeField(null=True)
    complete_date = models.DateTimeField(null=True)
    abort_date = models.DateTimeField(null=True)
    fail_date = models.DateTimeField(null=True)

    blocked = models.BooleanField(default=False)

    def __bool__(self):
        return bool(self.id)

    @classmethod
    def get_for_order(cls, order):
        # Returns active request or a fresh request that can be created by calling start()/start_extended()
        buy_request = cls.objects.filter(order=order).order_by('request_date').last()
        if buy_request and buy_request.state in cls.ACTIVE_STATES:
            return buy_request
        return cls(order=order)

    def count_items_per_group(self):
        return count_buy_items_per_group(self.order, self.positions.all())

    @property
    def effective_state(self):
        if not self:
            return None
        if self.state in self.ACTIVE_STATES:
            active_sell_requests = SellRequest.objects.filter(order=self.order, state__in=SellRequest.ACTIVE_STATES).count()
            active_buy_requests = BuyRequest.objects.filter(order=self.order, state__in=BuyRequest.ACTIVE_STATES).count()
            if active_sell_requests + active_buy_requests > 1:
                return self.States.FAILED

        if self.state == self.States.REQUESTED and self.order.status == self.order.STATUS_PENDING and self.order.require_approval:
            return self.States.REQUESTED
        elif self.state == self.States.APPROVED and self.order.status == self.order.STATUS_PENDING and not self.order.require_approval:
            return self.States.APPROVED
        elif self.state == self.States.APPROVED and self.order.status == self.order.STATUS_PAID:
            if self.mode == self.Modes.DIRECT:
                return self.States.PAID
            else:
                return self.States.COMPLETED
        elif self.state == self.States.PAID and self.mode == self.Modes.DIRECT:
            return self.States.PAID
        elif self.state == self.States.COMPLETED:
            return self.States.COMPLETED
        elif self.state == self.States.ABORTED:
            return self.States.ABORTED
        else:
            return self.States.FAILED

    @transaction.atomic()
    def update_state(self):
        old_state = self.state
        new_state = self.effective_state
        if new_state == old_state:
            return

        self.state = new_state
        if new_state == self.States.PAID:
            self.order.log_action('pretix_ticketbourse.buy_request.paid')
            self.pay_date = now()
        if new_state == self.States.COMPLETED:
            self.order.log_action('pretix_ticketbourse.buy_request.completed')
            self.complete_date = now()
        if new_state == self.States.FAILED:
            self.order.log_action('pretix_ticketbourse.buy_request.failed')
            self.fail_date = now()
        self.save()

        if new_state == self.States.FAILED and self.extends_order:
            # This triggers an order_changed signal, which would call
            # update_state again, if we had not already saved the new state.
            self.__unextend_order()

        if self.mode == self.Modes.DIRECT:
            for sell_request in SellRequest.objects.filter(direct_buy_request=self, state=SellRequest.States.BUY_REQUEST_APPROVED):
                sell_request.update_state()

    @property
    def can_start(self):
        if self:
            return False
        if self.order.status != Order.STATUS_PENDING or not self.order.require_approval:
            return False
        if self.order.ticketbourse_buy_requests.filter(state__in=BuyRequest.ACTIVE_STATES).exists():
            return False
        if self.order.ticketbourse_sell_requests.filter(state__in=SellRequest.ACTIVE_STATES).exists():
            return False
        positions = self.order.positions.filter(is_bundled=False)
        return positions and positions_can_buy(positions, self.order.event)

    @transaction.atomic()
    def start(self, mode):
        assert mode in self.Modes
        assert self.can_start
        self.order.log_action(
            'pretix_ticketbourse.buy_request.started',
            data={'mode': mode},
        )
        self.extends_order = False
        self.mode = mode
        self.state = self.States.REQUESTED
        self.request_date = now()
        self.approve_date = None
        self.complete_date = None
        self.fail_date = None
        self.save()
        self.positions.set(self.order.positions.filter(is_bundled=False))

    @property
    def can_start_extended(self):
        if self:
            return False
        if self.order.status != Order.STATUS_PAID or self.order.require_approval:
            return False
        if not self.order.positions.exists():
            return False # Canceled with fee is still canceled
        if self.order.ticketbourse_buy_requests.filter(state__in=BuyRequest.ACTIVE_STATES).exists():
            return False
        if self.order.ticketbourse_sell_requests.filter(state__in=SellRequest.ACTIVE_STATES).exists():
            return False
        toplevel_items, addon_formset = get_buyable_items_for_order(self.order.event, self.order.positions.all())
        return any(item.order_max for item in toplevel_items) or addon_formset

    @transaction.atomic()
    def start_extended(self, mode, new_position_drafts):
        assert mode in self.Modes
        assert self.can_start_extended
        self.order.log_action(
            'pretix_ticketbourse.buy_request.started',
            data={'mode': mode},
        )
        self.__extend_order(new_position_drafts)
        positions = [draft.real_position for draft in new_position_drafts if not draft.is_bundled]
        assert positions and positions_can_buy(positions, self.order.event)
        self.extends_order = True
        self.mode = mode
        self.state = self.States.REQUESTED
        self.request_date = now()
        self.approve_date = None
        self.complete_date = None
        self.fail_date = None
        self.save()
        self.positions.set(positions)

    def __extend_order(self, new_position_drafts):
        for position in self.order.positions.exclude(is_bundled=True).all():
            data = position.meta_info_data
            data['pretix_ticketbourse_was_paid_before_change'] = True
            position.meta_info_data = data
            position.save(update_fields=['meta_info'])
        self.order.status = self.order.STATUS_PENDING
        self.order.require_approval = True
        self.order.save(update_fields=['status', 'require_approval'])
        self.order.create_transactions()

        def build_key(position):
            return position.addon_to, position.item, position.variation

        existing_positions = set(self.order.positions.all())

        # First pass: Top-level positions
        ocm = OrderChangeManager(
            self.order,
            notify=False,
            reissue_invoice=self.order.invoices.exists() or self.order.event.settings.get('invoice_generate') == 'True',
        )
        item_draft_map = {}
        for draft in new_position_drafts:
            if draft.addon_to is not None:
                continue
            ocm.add_position(draft.item, draft.variation, draft.price)
            item_draft_map.setdefault(build_key(draft), []).append(draft)
        ocm.commit()
        for position in self.order.positions.all():
            if position in existing_positions:
                continue
            draft = item_draft_map.get(build_key(position), []).pop(0)
            draft.real_position = position
            existing_positions.add(position)
        assert not {key: values for key, values in item_draft_map.items() if values}

        # Second pass: Add-ons
        ocm = OrderChangeManager(
            self.order,
            notify=False,
            reissue_invoice=self.order.invoices.exists() or self.order.event.settings.get('invoice_generate') == 'True',
        )
        item_draft_map = {}
        for draft in new_position_drafts:
            if draft.addon_to is None:
                continue
            if hasattr(draft.addon_to, 'real_position'):
                draft.addon_to = draft.addon_to.real_position
            assert draft.addon_to in existing_positions
            ocm.add_position(draft.item, draft.variation, draft.price, addon_to=draft.addon_to)
            item_draft_map.setdefault(build_key(draft), []).append(draft)
        ocm.commit()
        for position in self.order.positions.all():
            if position in existing_positions:
                continue
            assert position.addon_to is not None
            draft = item_draft_map.get(build_key(position), []).pop(0)
            draft.real_position = position
        assert not {key: values for key, values in item_draft_map.items() if values}

    def __unextend_order(self):
        if not self.positions.filter(canceled=False).count():
            return

        positions = list(self.positions.all())
        for position in self.order.positions.filter(is_bundled=False).all():
            assert position in positions or position.meta_info_data.get('pretix_ticketbourse_was_paid_before_change')

        self.order.require_approval = False
        self.order.save(update_fields=['require_approval'])
        self.order.create_transactions()

        ocm = CustomizedOrderChangeManager(
            self.order,
            notify=False,
            reissue_invoice=self.order.invoices.exists() or self.order.event.settings.get('invoice_generate') == 'True',
        )
        for position in positions:
            # Canceling a position also cancels all its addons
            if position.addon_to not in positions:
                ocm.cancel(position)
        ocm.commit()

    @property
    def can_change_mode(self):
        return self and self.effective_state == self.States.REQUESTED

    def change_mode(self, mode):
        assert self.can_change_mode
        assert mode in self.Modes
        self.order.log_action(
            'pretix_ticketbourse.buy_request.started',
            data={'mode': mode},
        )
        self.mode = mode
        self.save()

    @property
    def can_abort(self):
        return self.effective_state == self.States.REQUESTED or self.effective_state == self.States.APPROVED and self.extends_order

    @transaction.atomic()
    def abort(self):
        assert self.can_abort
        self.state = self.States.ABORTED
        self.abort_date = now()
        self.save()
        self.order.log_action('pretix_ticketbourse.buy_request.deleted')
        if self.extends_order:
            self.__unextend_order()

    @property
    def can_approve_order(self):
        return self.effective_state == self.States.REQUESTED

    @transaction.atomic()
    def approve_order(self, user=None, auth=None):
        assert self.can_approve_order
        positions = list(self.positions.all())
        for position in self.order.positions.filter(is_bundled=False).all():
            assert position in positions or position.meta_info_data.get('pretix_ticketbourse_was_paid_before_change')

        self.state = self.States.APPROVED
        self.approve_date = now()
        self.save()
        self.order.log_action('pretix_ticketbourse.buy_request.approved', user=user, auth=auth)
        approve_order(self.order, send_mail=False)
        event = self.order.event
        if event.settings.ticketbourse_payment_term_days:
            tz = pytz.timezone(event.settings.timezone)
            self.order.expires = (
                now().astimezone(tz)
                + datetime.timedelta(days=event.settings.ticketbourse_payment_term_days)
                # While we round expires to 23:59:59, in reality orders will be marked as
                # expired a few minutes after 00:00:00 of the next day. Now if we approve
                # other orders because of the expired one, these orders get a payment term
                # much closer to "payment_term_days + 1". Actually most automatic mode buy
                # requests will be approved right after midnight (all with daily/term-based
                # batching enabled). To fix that we give us an hour slack here.
                - datetime.timedelta(hours=1)
            ).replace(hour=23, minute=59, second=59, microsecond=0)
            self.order.save()
        with language(self.order.locale, event.settings.region):
            email_subject = event.settings.ticketbourse_buy_request_approved_mail_subject
            email_template = event.settings.ticketbourse_buy_request_approved_mail_text
            email_context = get_email_context(event=event, order=self.order)
            try:
                self.order.send_mail(
                    email_subject, email_template, email_context,
                    'pretix_ticketbourse.buy_request.approved.order.email'
                )
            except SendMailException:
                logger.exception('Ticketbourse order approved email could not be sent')

    @property
    def can_block(self):
        return (
            self.effective_state == self.States.REQUESTED
            or (self.effective_state in (self.States.APPROVED, self.States.PAID) and self.mode == self.Modes.DIRECT)
        )

    def block(self, user=None, auth=None):
        if self.blocked:
            return
        self.blocked = True
        self.save()
        self.order.log_action('pretix_ticketbourse.buy_request.blocked', user=user, auth=auth)

    def unblock(self, user=None, auth=None):
        if not self.blocked:
            return
        self.blocked = False
        self.save()
        self.order.log_action('pretix_ticketbourse.buy_request.unblocked', user=user, auth=auth)


class SellRequest(models.Model):
    IS_SELL_REQUEST = True

    # We only allow a single active SellRequest for an order at any time,
    # but we need to preserve history (especially COMPLETED) to not skew
    # sold item counts.
    order = models.ForeignKey('pretixbase.Order', on_delete=models.CASCADE, related_name='ticketbourse_sell_requests')
    positions = models.ManyToManyField('pretixbase.OrderPosition', related_name='ticketbourse_sell_requests')

    class Modes(models.TextChoices):
        AUTO = 'AUTO', _('Auto')
        DIRECT = 'DIRECT', _('Direct')

    mode = models.CharField(max_length=32, choices=Modes.choices, verbose_name=_('Mode'))

    direct_buy_request = models.ForeignKey('pretix_ticketbourse.BuyRequest', on_delete=models.SET_NULL, null=True, related_name='direct_sell_requests')

    class States(models.TextChoices):
        REQUESTED = 'REQUESTED', pgettext_lazy('ticketbourse_state', 'Requested')
        BUY_REQUEST_APPROVED = 'BUY_REQUEST_APPROVED', pgettext_lazy('ticketbourse_state', 'Buy request approved') # BuyRequest (and order) was approved because of this request (only for DIRECT)
        COMPLETED = 'COMPLETED', pgettext_lazy('ticketbourse_state', 'Completed') # Order (or positions) was canceled by the request
        ABORTED = 'ABORTED', pgettext_lazy('ticketbourse_state', 'Aborted') # Request was aborted by user
        FAILED_UNKNOWN = 'FAILED_UNKNOWN', pgettext_lazy('ticketbourse_state', 'Failed')
        FAILED_CANCELED = 'FAILED_CANCELED', pgettext_lazy('ticketbourse_state', 'Failed because order was canceled')
        FAILED_CHECKED_IN = 'FAILED_CHECKED_IN', pgettext_lazy('ticketbourse_state', 'Failed because positions were checked-in')
        FAILED_BUY_REQUEST_FAILED = 'FAILED_BUY_REQUEST_FAILED', pgettext_lazy('ticketbourse_state', 'Failed because buy request failed')

    ACTIVE_STATES = (
        States.REQUESTED,
        States.BUY_REQUEST_APPROVED,
    )
    FAILED_STATES = (
        States.FAILED_UNKNOWN,
        States.FAILED_CANCELED,
        States.FAILED_CHECKED_IN,
        States.FAILED_BUY_REQUEST_FAILED,
    )

    state = models.CharField(max_length=32, choices=States.choices, verbose_name=_('State'))
    # Automatic sell requests are selected in the order they are requested in.
    # To not punish sellers from trying to sell their ticket directly,
    # request_date is not reset when the seller switches between direct and
    # automatic sell requests.
    request_date = models.DateTimeField(null=True)
    # pay_request_approve_date is missing, because DIRECT requests did not use the
    # REQUESTED state initially, so request_date could be used instead. No, unless
    # there is an error, the transition REQUESTED->BUY_REQUEST_APPROVED should be
    # pretty much instantaneous, so it is still not really needed.
    complete_date = models.DateTimeField(null=True)
    abort_date = models.DateTimeField(null=True)
    fail_date = models.DateTimeField(null=True)

    def __bool__(self):
        return bool(self.id)

    @classmethod
    def get_for_order(cls, order):
        # Returns active request or a fresh request that can be created by calling start()
        sell_request = cls.objects.filter(order=order).order_by('request_date').last()
        if sell_request and sell_request.state in cls.ACTIVE_STATES:
            return sell_request
        return cls(order=order)

    def count_items_per_group(self):
        return count_sell_items_per_group(self.order, self.positions.all())

    @property
    def effective_state(self):
        if not self:
            return None

        # Common checks
        if self.state in self.ACTIVE_STATES:
            if self.order.status == Order.STATUS_CANCELED or not self.order.count_positions:
                return self.States.FAILED_CANCELED
            if self.order.status != Order.STATUS_PAID:
                return self.States.FAILED_UNKNOWN
            active_sell_requests = SellRequest.objects.filter(order=self.order, state__in=SellRequest.ACTIVE_STATES).count()
            active_buy_requests = BuyRequest.objects.filter(order=self.order, state__in=BuyRequest.ACTIVE_STATES).count()
            if active_sell_requests + active_buy_requests > 1:
                return self.States.FAILED_UNKNOWN
            positions = set(self.positions.all())
            if not positions:
                return self.States.FAILED_UNKNOWN
            if not positions.issubset(sellable_positions_queryset(self.order).all()):
                return self.States.FAILED_UNKNOWN
            if get_missing_addons(positions):
                return self.States.FAILED_UNKNOWN
        if self.state in self.ACTIVE_STATES and self.mode == self.Modes.DIRECT:
            if not self.direct_buy_request:
                return self.States.FAILED_UNKNOWN
            if self.count_items_per_group() != self.direct_buy_request.count_items_per_group():
                return self.States.FAILED_UNKNOWN

        if self.state == self.States.REQUESTED:
            if self.mode == self.Modes.AUTO:
                return self.States.REQUESTED
            if self.mode == self.Modes.DIRECT and self.direct_buy_request.state == BuyRequest.States.REQUESTED:
                return self.States.REQUESTED
        if self.state == self.States.BUY_REQUEST_APPROVED and self.mode == self.Modes.DIRECT:
            if self.direct_buy_request.state in (BuyRequest.States.APPROVED, BuyRequest.States.PAID):
                return self.States.BUY_REQUEST_APPROVED
            return self.States.FAILED_BUY_REQUEST_FAILED
        if self.state == self.States.COMPLETED:
            return self.States.COMPLETED
        if self.state == self.States.ABORTED:
            return self.States.ABORTED
        if self.state in self.FAILED_STATES:
            return self.state
        return self.States.FAILED_UNKNOWN

    def update_state(self):
        old_state = self.state
        new_state = self.effective_state
        if new_state == old_state:
            return

        self.state = new_state
        if new_state in self.FAILED_STATES:
            self.order.log_action(
                'pretix_ticketbourse.sell_request.failed',
                data={'state': new_state}
            )
            self.fail_date = now()
        self.save()
        if new_state == self.States.FAILED_BUY_REQUEST_FAILED:
            event = self.order.event
            with language(self.order.locale, event.settings.region):
                email_subject = event.settings.ticketbourse_sell_request_failed_buyer_mail_subject
                email_template = event.settings.ticketbourse_sell_request_failed_buyer_mail_text
                email_context = get_email_context(event=event, order=self.order)
                try:
                    self.order.send_mail(
                        email_subject, email_template, email_context,
                        'pretix_ticketbourse.sell_request.failed_buyer.order.email'
                    )
                except SendMailException:
                    logger.exception('Ticketbourse sell failed email could not be sent')

    @property
    def can_start(self):
        if self:
            return False
        if self.order.ticketbourse_buy_requests.filter(state__in=BuyRequest.ACTIVE_STATES).exists():
            return False
        if self.order.ticketbourse_sell_requests.filter(state__in=SellRequest.ACTIVE_STATES).exists():
            return False
        return self.order.status == Order.STATUS_PAID and sellable_positions_queryset(self.order).exists()

    @property
    def can_change(self):
        return self.effective_state in (self.States.REQUESTED,)

    @property
    def can_start_or_change(self):
        return self.can_start or self.can_change

    @transaction.atomic()
    def start_or_change(self, mode, positions, code=None):
        assert mode in self.Modes
        assert positions and not get_missing_addons(positions)
        assert self.can_start or self.can_change

        if mode == self.Modes.DIRECT:
            order = self.order.event.orders.get(code=code)
            buy_request = BuyRequest.get_for_order(order)
            assert buy_request and buy_request.effective_state == BuyRequest.States.REQUESTED
            assert count_sell_items_per_group(self.order, positions) == buy_request.count_items_per_group()
        else:
            assert code is None

        if not self.request_date:
            self.request_date = now()
        self.mode = mode
        if mode == self.Modes.DIRECT:
            if buy_request.mode == BuyRequest.Modes.AUTO:
                buy_request.order.log_action('pretix_ticketbourse.buy_request.direct_forced')
                buy_request.mode = BuyRequest.Modes.DIRECT
                buy_request.save()
            self.direct_buy_request = buy_request
        else:
            self.direct_buy_request = None
        self.state = self.States.REQUESTED
        self.complete_date = None
        self.fail_date = None
        self.save()
        self.positions.set(positions)
        self.order.log_action(
            'pretix_ticketbourse.sell_request.started',
            data={'mode': mode, 'code': code},
        )

    @property
    def can_abort(self):
        return self.effective_state in (self.States.REQUESTED,)

    def abort(self):
        assert self.can_abort
        self.state = self.States.ABORTED
        self.abort_date = now()
        self.save()
        self.order.log_action('pretix_ticketbourse.sell_request.deleted')

    @property
    def can_cancel_order(self):
        return self.effective_state in (self.States.REQUESTED, self.States.BUY_REQUEST_APPROVED)

    # TODO: Maybe rename to "complete()"?
    @transaction.atomic()
    def cancel_order(self, user=None):
        assert self.can_cancel_order
        self.state = self.States.COMPLETED
        self.complete_date = now()
        self.save()
        self.order.log_action('pretix_ticketbourse.sell_request.completed', user=user)

        event = self.order.event

        new_cancellation_fee = get_cancellation_fee(event, self.positions.all())
        existing_cancellation_fees = list(self.order.fees.filter(fee_type=OrderFee.FEE_TYPE_CANCELLATION).all())
        positions = self.positions.all()
        # cancel_order() cancels existing cancellation fees, so we use OCM in most cases
        if set(positions) == set(self.order.positions.filter(is_bundled=False).all()) and not existing_cancellation_fees:
            _cancel_order(
                self.order,
                send_mail=False,
                cancellation_fee=new_cancellation_fee,
                user=user,
            )
        else:
            ocm = CustomizedOrderChangeManager(
                self.order,
                notify=False,
                reissue_invoice=self.order.invoices.exists() or event.settings.get('invoice_generate') == 'True',
                user=user,
            )
            for position in positions:
                # Canceling a position also cancels all its addons
                if position.addon_to not in positions:
                    ocm.cancel(position)
            if new_cancellation_fee:
                if not existing_cancellation_fees:
                    ocm.add_fee(OrderFee(
                        fee_type=OrderFee.FEE_TYPE_CANCELLATION,
                        value=new_cancellation_fee,
                        tax_rule=event.settings.tax_rate_default,
                        order=self.order,
                    ))
                else:
                    fee = existing_cancellation_fees[-1]
                    ocm.change_fee(fee, fee.value + new_cancellation_fee)
            ocm.commit()

        try:
            if 'cancellation_fee' in signature(_try_auto_refund).parameters:
                # cancellation_fee is a custom parameter in our pretix fork
                _try_auto_refund(self.order, cancellation_fee=new_cancellation_fee)
            else:
                _try_auto_refund(self.order)
        except OrderError:
            pass # Payment plugin should log pretix.event.order.refund.failed with details

        # Normally, cancelling an order revalidates vouchers used for the order.
        # However, we approved another order in place of the canceled one.
        # Depending on the voucher type, revalidating the vouchers would allow
        # creating extra tickets.
        # We never want that, so we undo that cancel_order decreases the
        # voucher's redeemed count.
        for position in positions:
            if position.voucher:
                Voucher.objects.filter(pk=position.voucher.pk).update(redeemed=models.F('redeemed') + 1)
        with language(self.order.locale, event.settings.region):
            email_subject = event.settings.ticketbourse_sell_request_completed_mail_subject
            email_template = event.settings.ticketbourse_sell_request_completed_mail_text
            email_context = get_email_context(event=event, order=self.order)
            try:
                self.order.send_mail(
                    email_subject, email_template, email_context,
                    'pretix_ticketbourse.sell_request.completed.order.email'
                )
            except SendMailException:
                logger.exception('Ticketbourse order approved email could not be sent')
