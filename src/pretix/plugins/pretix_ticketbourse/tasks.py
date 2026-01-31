import datetime
import pytz

from django.db import models, transaction
from django.utils.timezone import now
from django_scopes import scopes_disabled

from pretix.base.models import Event, OrderPosition
from pretix.base.services.tasks import EventTask
from pretix.celery_app import app

from .settings import ITEM_GROUP_SUFFIXES
from .utils import lock, is_selling_active
from .models import SellRequest, BuyRequest


def get_ticket_bourse_result(event, manual_params=None):
    result = []
    for index, suffix in enumerate(ITEM_GROUP_SUFFIXES):
        target_balance = event.settings['ticketbourse_target_balance'+suffix]
        oversell_limit = event.settings['ticketbourse_oversell_limit'+suffix]
        buy_items = event.settings['ticketbourse_buy_items'+suffix]
        sell_items = event.settings['ticketbourse_sell_items'+suffix]

        data = {
            'target_balance': target_balance,
            'buy_items': buy_items,
            'sell_items': sell_items,
            'to_be_approved_manual': max(0, manual_params['approve_groups'][index]) if manual_params else 0,
            'to_be_canceled_manual': max(0, manual_params['cancel_groups'][index]) if manual_params else 0,
            'tickets_sold': OrderPosition.all.filter(order__event=event, ticketbourse_buy_requests__state=BuyRequest.States.COMPLETED, item_id__in=buy_items).count(),
            'tickets_canceled': OrderPosition.all.filter(order__event=event, ticketbourse_sell_requests__state=SellRequest.States.COMPLETED, item_id__in=sell_items).count(),
            'pending_auto_buy_requests': OrderPosition.all.filter(models.Exists(BuyRequest.objects.filter(positions__id=models.OuterRef('pk'), mode=BuyRequest.Modes.AUTO, state=BuyRequest.States.APPROVED)), item_id__in=buy_items, order__event=event).count(),
            'available_auto_buy_requests': OrderPosition.all.filter(models.Exists(BuyRequest.objects.filter(positions__id=models.OuterRef('pk'), mode=BuyRequest.Modes.AUTO, state=BuyRequest.States.REQUESTED)), item_id__in=buy_items, order__event=event).count(),
            'available_auto_sell_requests': OrderPosition.all.filter(models.Exists(SellRequest.objects.filter(positions__id=models.OuterRef('pk'), mode=SellRequest.Modes.AUTO, state=SellRequest.States.REQUESTED)), item_id__in=sell_items, order__event=event).count(),
            'oversell_limit': oversell_limit,
        }
        result.append(_get_ticket_bourse_result(**data))
    return result


def _get_ticket_bourse_result(**data):
    class AttrDict(dict):
        def __getattr__(self, key):
            return self[key]

        def __setattr__(self, key, value):
            self[key] = value

    d = AttrDict(data)

    # Balance
    d.balance = d.tickets_sold - d.tickets_canceled
    d.to_be_sold_for_balance = max(0, d.target_balance - d.balance)
    d.to_be_canceled_for_balance = max(0, -(d.target_balance - d.balance))

    # How many auto buy requests to approve
    d.to_be_sold_for_auto = max(0, d.available_auto_sell_requests - d.to_be_canceled_for_balance)
    d.to_be_sold_total = d.to_be_sold_for_balance + d.to_be_sold_for_auto
    d.to_be_approved_total = max(0, d.to_be_sold_total - d.pending_auto_buy_requests)
    if d.oversell_limit < 0:
        d.auto_approve_oversell_limit_left = -1
        d.to_be_approved_total_oversell_limited = d.to_be_approved_total
    else:
        d.auto_approve_oversell_limit_left = max(0, d.oversell_limit + d.to_be_sold_for_balance - d.pending_auto_buy_requests - d.to_be_canceled_for_balance)
        d.to_be_approved_total_oversell_limited = min(d.auto_approve_oversell_limit_left, d.to_be_approved_total)
    d.to_be_approved_available = min(d.available_auto_buy_requests, d.to_be_approved_total_oversell_limited + d.to_be_approved_manual)

    # How many auto sell requests to complete (i.e. tickets to cancel)
    d.to_be_canceled_available = min(d.available_auto_sell_requests, d.to_be_canceled_for_balance + d.to_be_canceled_manual)

    # Results
    d.to_be_approved = d.to_be_approved_available
    d.to_be_canceled = d.to_be_canceled_available
    return d


@app.task(base=EventTask)
# TODO: We don't need @scopes_disabled(), but it does less harm than @transaction.atomic()
# The only reason it is there is that without an extra decorator apply_async causes a TypeError:
#   TypeError: run_ticket_bourse() got an unexpected keyword argument 'event_id'
@scopes_disabled()
def run_ticket_bourse(event: Event, after_periodic_task_handler=False, manual_params=None):
    if not event.settings.ticketbourse_enable and not manual_params:
        return
    with lock(event) as lock_acquired:
        if not lock_acquired:
            return # We run this periodically anyway

        is_selling_active_cached = is_selling_active(event)

        if is_selling_active_cached:
            # DIRECT SellRequest REQUESTED -> BUY_REQUEST_APPROVED
            # DIRECT BuyRequest REQUESTED -> APPROVED
            sell_requests = SellRequest.objects.filter(
                order__event=event,
                mode=SellRequest.Modes.DIRECT,
                state=SellRequest.States.REQUESTED,
                direct_buy_request__state=BuyRequest.States.REQUESTED,
                direct_buy_request__blocked=False,
            )
            for sell_request in sell_requests:
                with transaction.atomic():
                    if sell_request.effective_state != SellRequest.States.REQUESTED:
                        continue
                    buy_request = sell_request.direct_buy_request
                    if not buy_request or buy_request.effective_state != BuyRequest.States.REQUESTED:
                        continue
                    buy_request.approve_order()
                    sell_request.order.log_action('pretix_ticketbourse.sell_request.buy_request_approved')
                    sell_request.state = SellRequest.States.BUY_REQUEST_APPROVED
                    sell_request.save()

        # DIRECT SellRequest BUY_REQUEST_APPROVED -> COMPLETED
        # DIRECT BuyRequest PAID -> COMPLETED
        sell_requests = SellRequest.objects.filter(
            order__event=event,
            mode=SellRequest.Modes.DIRECT,
            state=SellRequest.States.BUY_REQUEST_APPROVED,
            direct_buy_request__state=BuyRequest.States.PAID,
            direct_buy_request__blocked=False,
        )
        for sell_request in sell_requests:
            with transaction.atomic():
                if sell_request.effective_state != SellRequest.States.BUY_REQUEST_APPROVED:
                    continue
                buy_request = sell_request.direct_buy_request
                if not buy_request or buy_request.effective_state != BuyRequest.States.PAID:
                    continue
                sell_request.cancel_order()
                buy_request.state = BuyRequest.States.COMPLETED
                buy_request.complete_date = now()
                buy_request.save()
                buy_request.order.log_action('pretix_ticketbourse.buy_request.completed')

        result = get_ticket_bourse_result(event, manual_params=manual_params)
        buy_item_map = {}
        sell_item_map = {}
        for d in result:
            d.approved = 0
            d.canceled = 0
            for item_id in d.buy_items:
                buy_item_map[item_id] = d
            for item_id in d.sell_items:
                sell_item_map[item_id] = d

        # Buy Requests:
        # Similar to raffle, select BuyRequests at random, check feasability and approve
        query_limit = 50
        do_auto_approvals = False
        if not is_selling_active_cached:
            do_auto_approvals = False
        elif event.settings.ticketbourse_approval_batching_mode == 'DISABLED' or manual_params:
            do_auto_approvals = True
        elif after_periodic_task_handler:
            tz = pytz.timezone(event.settings.timezone)
            last_run = event.settings.ticketbourse_approval_batching_last_run.astimezone(tz)
            if event.settings.ticketbourse_approval_batching_mode == 'HOURLY':
                next_run = (last_run + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                query_limit = 500
            elif event.settings.ticketbourse_approval_batching_mode == 'DAILY':
                next_run = (last_run + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                query_limit = 50000
            elif event.settings.ticketbourse_approval_batching_mode == 'PAYMENT_TERM':
                next_run = (
                    last_run
                    + datetime.timedelta(days=event.settings.ticketbourse_payment_term_days)
                ).replace(hour=0, minute=0, second=0, microsecond=0)
                query_limit = 50000
            else:
                raise Exception('Invalid ticketbourse batching mode')
            if next_run <= now().astimezone(tz):
                event.settings.ticketbourse_approval_batching_last_run = now().astimezone(tz)
                do_auto_approvals = True

        to_be_approved = []
        if do_auto_approvals:
            buy_requests = BuyRequest.objects.filter(order__event=event, mode=BuyRequest.Modes.AUTO, state=BuyRequest.States.REQUESTED, blocked=False)
            for buy_request in buy_requests.order_by('?')[:query_limit]:
                done = True
                for d in result:
                    if d.to_be_approved - d.approved > 0:
                        done = False
                if done:
                    break

                if buy_request.effective_state != BuyRequest.States.REQUESTED:
                    continue

                skip_request = False
                for position in buy_request.positions.all():
                    d = buy_item_map[position.item_id]
                    if d.to_be_approved - d.approved <= 0:
                        skip_request = True
                if skip_request:
                    continue

                for position in buy_request.positions.all():
                    d = buy_item_map[position.item_id]
                    d.approved += 1

                to_be_approved.append(buy_request)

        # Sell Requests:
        # Check requests ordered by request_date, either abort at first unfeasable request or "reserve" parts of the available cancels and continue
        to_be_canceled = []
        sell_requests = SellRequest.objects.filter(order__event=event, mode=SellRequest.Modes.AUTO, state=SellRequest.States.REQUESTED)
        for sell_request in sell_requests.order_by('request_date')[:query_limit]:
            skip_request = False
            for position in sell_request.positions.all():
                d = sell_item_map[position.item_id]
                if d.to_be_canceled - d.canceled > 0:
                    d.canceled += 1
                else:
                    skip_request = True
            if not skip_request:
                to_be_canceled.append(sell_request)

        if not to_be_approved and not to_be_canceled:
            return

        event.log_action(
            'pretix_ticketbourse.result',
            data={
                'to_be_approved': len(to_be_approved),
                'to_be_canceled': len(to_be_canceled),
                'item_groups': result,
            }
        )
        for buy_request in to_be_approved:
            buy_request.approve_order()
        for sell_request in to_be_canceled:
            sell_request.cancel_order()
