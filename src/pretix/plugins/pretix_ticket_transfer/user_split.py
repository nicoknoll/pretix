import json
from decimal import Decimal
from django.db import transaction
from django.utils.timezone import now

from pretix.base.signals import order_split, order_changed
from pretix.base.secrets import assign_ticket_secret
from pretix.base.models.orders import Order, OrderPosition, OrderFee, OrderRefund, OrderPayment, generate_secret
from pretix.base.services.orders import OrderChangeManager
from pretix.base.models.tax import TaxRule
from pretix.base.i18n import language
from pretix.base.email import get_email_context
from django.utils.translation import gettext as _

from i18nfield.strings import LazyI18nString
from pretix.base.services.mail import SendMailException
from pretix.helpers import OF_SELF

from .utils import transfer_needs_accept

TICKET_TRANSFER_START = 1
TICKET_TRANSFER_DONE = 2
TICKET_TRANSFER_SENT = 23

class TicketTransferChangeManager(OrderChangeManager):
    """
    dont complete_cancel check
    no notify
    """
    def commit(self, check_quotas=True):
        if self._committed:
            # an order change can only be committed once
            raise OrderError(error_messages['internal'])
        self._committed = True

        if not self._operations:
            # Do nothing
            return

        # Clear prefetched objects cache of order. We're going to modify the positions and fees and we have no guarantee
        # that every operation tuple points to a position/fee instance that has been fetched from the same object cache,
        # so it's dangerous to keep the cache around.
        self.order._prefetched_objects_cache = {}

        # finally, incorporate difference in payment fees
        self._payment_fee_diff()
        self._check_order_size()

        with transaction.atomic():
            locked_instance = Order.objects.select_for_update(of=OF_SELF).get(pk=self.order.pk)
            if locked_instance.last_modified != self.order.last_modified:
                raise OrderError(error_messages['race_condition'])

            if self.order.status in (Order.STATUS_PENDING, Order.STATUS_PAID):
                if check_quotas:
                    self._check_quotas()
                self._check_seats()
            self._create_locks()
            #self._check_complete_cancel()
            self._check_and_lock_memberships()
            try:
                self._perform_operations()
            except TaxRule.SaleNotAllowed:
                raise OrderError(self.error_messages['tax_rule_country_blocked'])
            self._recalculate_total_and_payment_fee()
            self._check_paid_price_change()
            self._check_paid_to_free()
            if self.order.status in (Order.STATUS_PENDING, Order.STATUS_PAID):
                self._reissue_invoice()
            self._clear_tickets_cache()
            self.order.touch()
            self.order.create_transactions()
            if self.split_order:
                self.split_order.create_transactions()

        #if self.notify:
        #    notify_user_changed_order(
        #        self.order, self.user, self.auth,
        #        self._invoices if self.event.settings.invoice_email_attachment else []
        #    )
        #    if self.split_order:
        #        notify_user_changed_order(
        #            self.split_order, self.user, self.auth,
        #            list(self.split_order.invoices.all()) if self.event.settings.invoice_email_attachment else []
        #        )

        order_changed.send(self.order.event, order=self.order)

    """
    no invoice copy
    clear answers
    """
    def _create_split_order(self, split_positions):
        split_order = Order.objects.get(pk=self.order.pk)
        split_order.pk = None
        split_order.code = None
        split_order.datetime = now()
        split_order.secret = generate_secret()
        split_order.require_approval = self.order.require_approval and any(p.requires_approval(invoice_address=self._invoice_address) for p in split_positions)
        split_order.save()
        split_order.log_action('pretix_ticket_transfer.changed.split_from', user=self.user, auth=self.auth, data={
            'original_order': self.order.code
        })

        for op in split_positions:
            self.order.log_action('pretix_ticket_transfer.changed.split', user=self.user, auth=self.auth, data={
                'position': op.pk,
                'positionid': op.positionid,
                'old_item': op.item.pk,
                'old_variation': op.variation.pk if op.variation else None,
                'old_price': op.price,
                'new_order': split_order.code,
            })
            op.order = split_order
            assign_ticket_secret(
                self.event, position=op, force_invalidate=True,
            )
            op.save()

        ## clear answers
            op.answers.clear()

        #try:
        #    ia = modelcopy(self.order.invoice_address)
        #    ia.pk = None
        #    ia.order = split_order
        #    ia.save()
        #except InvoiceAddress.DoesNotExist:
        #    pass

        split_order.total = sum([p.price for p in split_positions if not p.canceled])

        for fee in self.order.fees.exclude(fee_type=OrderFee.FEE_TYPE_PAYMENT):
            new_fee = modelcopy(fee)
            new_fee.pk = None
            new_fee.order = split_order
            split_order.total += new_fee.value
            new_fee.save()

        if split_order.total != Decimal('0.00') and self.order.status != Order.STATUS_PAID:
            pp = self._get_payment_provider()
            if pp:
                payment_fee = pp.calculate_fee(split_order.total)
            else:
                payment_fee = Decimal('0.00')
            fee = split_order.fees.get_or_create(fee_type=OrderFee.FEE_TYPE_PAYMENT, defaults={'value': 0})[0]
            fee.value = payment_fee
            fee._calculate_tax()
            if payment_fee != 0:
                fee.save()
            elif fee.pk:
                fee.delete()
            split_order.total += fee.value

        remaining_total = sum([p.price for p in self.order.positions.all()]) + sum([f.value for f in self.order.fees.all()])
        offset_amount = min(max(0, self.completed_payment_sum - remaining_total), split_order.total)
        if offset_amount >= split_order.total:
            split_order.status = Order.STATUS_PAID
        else:
            split_order.status = Order.STATUS_PENDING
            #if self.order.status == Order.STATUS_PAID:
            #    split_order.set_expires(
            #        now(),
            #        list(set(p.subevent_id for p in split_positions))
            #    )
        split_order.save()

        if offset_amount > Decimal('0.00'):
            split_order.payments.create(
                state=OrderPayment.PAYMENT_STATE_CONFIRMED,
                amount=offset_amount,
                payment_date=now(),
                provider='offsetting',
                info=json.dumps({'orders': [self.order.code]})
            )
            self.order.refunds.create(
                state=OrderRefund.REFUND_STATE_DONE,
                amount=offset_amount,
                execution_date=now(),
                provider='offsetting',
                info=json.dumps({'orders': [split_order.code]})
            )

        #if split_order.total != Decimal('0.00') and self.order.invoices.filter(is_cancellation=False).last():
        #    generate_invoice(split_order)

        order_split.send(sender=self.order.event, original=self.order, split_order=split_order)
        return split_order

def notify_user_split_order_source(order, user=None, auth=None, invoices=[]):
    with language(order.locale, order.event.settings.region):
        email_template = order.event.settings.get('pretix_ticket_transfer_sender_mailtext', as_type=LazyI18nString)
        email_subject = str(order.event.settings.get('pretix_ticket_transfer_sender_subject', as_type=LazyI18nString)).format(code=order.code)
        email_context = get_email_context(event=order.event, order=order)
        try:
          order.send_mail(
            email_subject, email_template, email_context,
            'pretix.event.order.email.ticket_transfer_sender', user, auth=auth, invoices=invoices, attach_tickets=True)
        except SendMailException:
          logger.exception('Tickettransfer sender email could not be sent')

def notify_user_split_order_target(order, user=None, auth=None, invoices=[]):
    with language(order.locale, order.event.settings.region):
        email_template = order.event.settings.get('pretix_ticket_transfer_recipient_mailtext', as_type=LazyI18nString)
        email_subject = str(order.event.settings.get('pretix_ticket_transfer_recipient_subject', as_type=LazyI18nString)).format(code=order.code)
        email_context = get_email_context(event=order.event, order=order)
        try:
          order.send_mail(
            email_subject, email_template, email_context,
            'pretix.event.order.email.ticket_transfer_recipient', user, auth=auth, invoices=invoices, attach_tickets=True)
        except SendMailException:
            logger.exception('Tickettransfer recipient email could not be sent')

def user_split_positions( order, pids=None ):

  pos = []
  positions = order.positions.select_related('item')
  if pids:
    positions = positions.filter(pk__in=pids)
  for p in positions:
    if not p.item.admission or p.addon_to:
      continue
    if p.all_checkins.exists():
      continue
    if order.event.settings.get( 'pretix_ticket_transfer_items_all' ) == None:
      continue   # default to false
    elif order.event.settings.get( 'pretix_ticket_transfer_items_all' ) == True:
      pos.append( p )
    elif order.event.settings.get( 'pretix_ticket_transfer_items_all' ) == False:
      if p.item.id in json.loads( order.event.settings.get( 'pretix_ticket_transfer_items' )):
        pos.append( p )
  for p in pos:
    p.price_with_addons = p.price
    for addon in p.addons.all():
        if not addon.canceled:
            p.price_with_addons += addon.price
  return pos

def user_split( order, pids, data ):
  with transaction.atomic():
    event = order.event
    positions = OrderPosition.objects.filter(pk__in=pids).select_for_update(nowait=True).all()
    ocm = TicketTransferChangeManager(
        order,
        notify=False,
        reissue_invoice=False )

    pos = user_split_positions( order, pids )
    success = 0
    for p in pos:
      p.attendee_name_parts = {}
      ocm.split(p)
      success+= 1

      if p.meta_info_data and p.meta_info_data.get('vouchergen_voucher_code'):
        from pretix_vouchergen.utils import cancel_voucher
        cancel_voucher( p.meta_info_data.get('vouchergen_voucher_code'))

        meta = p.meta_info_data
        del meta['vouchergen_voucher_code']
        p.meta_info_data = meta
        p.save()

    if success == len(pos):

      ocm.commit(check_quotas=False)

      split_order = ocm.split_order
      split_order.email_known_to_work = False

      if data.get('email'):
        split_order.email = data.get('email')

      meta = split_order.meta_info_data
      meta['doistep'] = {}
      meta['contact_form_data'] = {}
      meta['confirm_messages'] = []
      meta['ticket_transfer'] = TICKET_TRANSFER_START if transfer_needs_accept(event) else TICKET_TRANSFER_DONE
      split_order.meta_info = json.dumps(meta)
      split_order.save()

      meta = order.meta_info_data
      meta['ticket_transfer_sent'] = TICKET_TRANSFER_SENT
      order.meta_info = json.dumps(meta)
      order.save()

      notify_user_split_order_source(
          order, ocm.user, ocm.auth,
          ocm._invoices if ocm.event.settings.invoice_email_attachment else [] )
      notify_user_split_order_target(
          split_order, ocm.user, ocm.auth,
          list(split_order.invoices.all()) if ocm.event.settings.invoice_email_attachment else [] )

      return True
    return False


