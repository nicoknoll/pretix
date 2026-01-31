import logging
from contextlib import contextmanager
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _, ngettext_lazy

from pretix.base.decimal import round_decimal
from pretix.base.models import CartPosition
from pretix.base.services.cart import CartError
from pretix.base.services.orders import OrderChangeManager
from pretix.base.signals import validate_cart
from pretix.base.timemachine import time_machine_now
from pretix.presale.views.cart import get_or_create_cart_id
from pretix.presale.views.event import get_grouped_items

from .settings import ITEM_GROUP_SUFFIXES

logger = logging.getLogger(__name__)


@contextmanager
def lock(event, **kwargs):
    # Pretix's event locks don't support nesting and many pretix service functions already acquire an event lock
    has_redis = settings.HAS_REDIS
    lock_acquired = False
    if has_redis:
        from django_redis import get_redis_connection
        from redis.lock import Lock
        lock = Lock(redis=get_redis_connection('redis'), name='pretix_ticketbourse_event_%s' % event.id, timeout=300, blocking=True, blocking_timeout=10)
        lock_acquired = lock.acquire(**kwargs)
        if not lock_acquired:
            logger.info('Could not acquire ticketbourse lock')
    else:
        lock_acquired = True
    try:
        yield lock_acquired
    finally:
        if has_redis and lock_acquired:
            from redis.exceptions import LockNotOwnedError
            try:
                lock.release()
            except LockNotOwnedError:
                logger.warning('Ticketbourse lock expired prematurely')


def is_selling_active(event):
    now = time_machine_now()
    if event.settings.ticketbourse_active_from and event.settings.ticketbourse_active_from > now:
        return False
    if event.settings.ticketbourse_active_until and event.settings.ticketbourse_active_until < now:
        return False
    return True


def positions_can_buy(positions, event):
    positions = [position for position in positions if not (position.addon_to and position.is_bundled)]
    if not positions:
        return False

    buy_items = []
    for suffix in ITEM_GROUP_SUFFIXES:
        buy_items += event.settings['ticketbourse_buy_items'+suffix]
    for position in positions:
        if position.item_id not in buy_items:
            return False
        if not position.item.require_approval:
            return False
    return True


def cart_can_buy(request):
    positions = CartPosition.objects.filter(cart_id=get_or_create_cart_id(request)).all()
    return positions_can_buy(positions, request.event)


def sellable_positions_queryset(order):
    sell_items = []
    for suffix in ITEM_GROUP_SUFFIXES:
        sell_items += order.event.settings['ticketbourse_sell_items'+suffix]
    return order.positions.filter(
        is_bundled=False,
        item_id__in=sell_items
    ).exclude(
        models.Exists(order.positions.filter(
            ~models.Q(item_id__in=sell_items),
            addon_to__id=models.OuterRef('pk'),
            is_bundled=False,
        )),
    ).exclude(
        models.Exists(order.positions.filter(
            addon_to__id=models.OuterRef('pk'),
            all_checkins__successful=True,
        )),
    ).exclude(
        all_checkins__successful=True,
    )


def count_sell_items_per_group(order, positions):
    item_map = {}
    for index, suffix in enumerate(ITEM_GROUP_SUFFIXES):
        for item_id in order.event.settings['ticketbourse_sell_items'+suffix]:
            item_map[item_id] = index
    group_counts = {}
    for position in positions:
        group = item_map[position.item_id]
        group_counts.setdefault(group, 0)
        group_counts[group] += 1
    return group_counts


def count_buy_items_per_group(order, positions):
    item_map = {}
    for index, suffix in enumerate(ITEM_GROUP_SUFFIXES):
        for item_id in order.event.settings['ticketbourse_buy_items'+suffix]:
            item_map[item_id] = index
    group_counts = {}
    for position in positions:
        group = item_map[position.item_id]
        group_counts.setdefault(group, 0)
        group_counts[group] += 1
    return group_counts


def get_missing_addons(positions):
    position_ids = [position.id for position in positions]
    results = []
    for position in positions:
        missing_addons = position.addons.filter(
            canceled=False,
            is_bundled=False,
        ).exclude(
            id__in=position_ids,
        ).all()
        if missing_addons:
            results.append((position, missing_addons))
    return results


def get_positions_total(positions):
    total = Decimal('0.00')
    for position in positions:
        total += position.price
        for addon in position.addons.filter(is_bundled=True, canceled=False).all():
            total += addon.price
    return total


def get_cancellation_fee(event, positions):
    fee = event.settings.ticketbourse_cancellation_fee
    if 'pretix_kukocustom' in event.plugins.split(','):
        for position in positions:
            key = 'pretix_kukocustom_cancel_allow_user_paid_keep_item_%d' % position.item.id
            fee += event.settings.get(key, default='0.00', as_type=Decimal)
    return round_decimal(min(fee, get_positions_total(positions)), event.currency)


class CustomizedOrderChangeManager(OrderChangeManager):
    def _check_complete_cancel(self):
        # Unmodified OrderChangeManager does not allow cancelling all
        # positions in an order. We should use cancel_order() instead.
        # This works in the absence of cancellation fees (or fees in
        # general) as cancel_order() will simply set Order.status to
        # STATUS_CANCELED. It is, however, not an option with fees:
        # cancel_order() cancels all existing fees. Even with the keep_fees
        # option it substracts existing fees from the passed fee value.
        # It also enforces that the passed fee value is lower than the
        # remaining order value.
        pass


def get_buyable_items_for_order(event, positions):
    event = event
    buy_items = []
    for suffix in ITEM_GROUP_SUFFIXES:
        buy_items += event.settings['ticketbourse_buy_items'+suffix]

    toplevel_positions = []
    toplevel_positions_by_item = {}
    positions_by_item = {}
    addons_by_parent_by_item = {}
    addons_by_parent_by_category = {}
    excluded_items = set()
    for position in positions:
        positions_by_item.setdefault(position.item, []).append(position)
        if not position.addon_to:
            toplevel_positions.append(position)
            toplevel_positions_by_item.setdefault(position.item, []).append(position)
        if position.addon_to and not position.is_bundled:
            addons_by_parent_by_item.setdefault(position.addon_to, {}).setdefault(position.item, []).append(position)
            addons_by_parent_by_category.setdefault(position.addon_to, {}).setdefault(position.item.category_id, []).append(position)

        if 'pretix_kukocustom' in event.plugins.split(','):
            for item in event.items.exclude(id=position.item_id).all():
                items_mutually_exclusive = event.settings.get(
                    'pretix_kukocustom_cart_items_mutually_exclusive_%d_%d' % tuple(sorted((position.item_id, item.id))),
                    default='False',
                    as_type=bool
                )
                if items_mutually_exclusive:
                    excluded_items.add(item.id)

    toplevel_items, display_add_to_cart = get_grouped_items(
        event,
        channel=position.order.sales_channel,
        base_qs=event.items.filter(id__in=buy_items).exclude(id__in=excluded_items),
    )
    for item in toplevel_items:
        order_max = max(0, min(
            (item.max_per_order or int(event.settings.max_items_per_order)) - len(positions_by_item.get(item, [])),
            int(event.settings.max_items_per_order) - len(toplevel_positions),
        ))
        if not item.has_variations:
            item.order_max = min(item.order_max, order_max)
        else:
            for var in item.available_variations:
                var.order_max = min(var.order_max, order_max)

    addon_formset = []
    for position in toplevel_positions:
        formsetentry = {
            'pos': position,
            'item': position.item,
            'variation': position.variation,
            'categories': []
        }
        for iao in position.item.addons.all():
            items, _btn = get_grouped_items(
                event,
                channel=position.order.sales_channel,
                base_qs=iao.addon_category.items.filter(id__in=buy_items).exclude(id__in=excluded_items),
                allow_addons=True,
            )
            addons_by_category = addons_by_parent_by_category.get(position, {}).get(iao.addon_category_id, [])
            min_count = max(0, iao.min_count - len(addons_by_category))
            max_count = max(0, iao.max_count - len(addons_by_category))
            for item in items:
                order_max = max(0, min(
                    (item.max_per_order or int(event.settings.max_items_per_order)) - len(positions_by_item.get(item, [])),
                    max_count,
                ))
                addons_by_item = addons_by_parent_by_item.get(position, {}).get(item, [])
                if addons_by_item and not iao.multi_allowed:
                    order_max = 0
                item.allow_waitinglist = False
                if item.has_variations:
                    for var in item.available_variations:
                        var.initial = 0
                        var.initial_price = var.suggested_price
                        var.order_max = min(var.order_max, order_max)
                    item.expand = False
                else:
                    item.initial = 0
                    item.initial_price = item.suggested_price
                    item.order_max = min(item.order_max, order_max)
            items = [item for item in items if item.order_max]
            if items:
                formsetentry['categories'].append({
                    'category': iao.addon_category,
                    'price_included': iao.price_included,
                    'multi_allowed': iao.multi_allowed,
                    'min_count': min_count,
                    'max_count': max_count,
                    'iao': iao,
                    'items': items
                })
        if formsetentry['categories']:
            addon_formset.append(formsetentry)
    return toplevel_items, addon_formset


# This implements a subset of Pretix's cart validity checks. Pretix only
# applies these checks during checkout and settings for that can change over
# time. So existing orders might fail these checks. Rather than refusing any
# changes on such orders via the ticket bourse, we only refuse changes that
# make things worse.
def validate_position_change(event, old_positions, new_positions):
    old_toplevel_positions = []
    old_positions_by_item = {}
    old_addons_by_parent_by_item = {}
    old_addons_by_parent_by_category = {}
    for position in old_positions:
        old_positions_by_item.setdefault(position.item, []).append(position)
        if not position.addon_to:
            old_toplevel_positions.append(position)
        if position.addon_to and not position.is_bundled:
            old_addons_by_parent_by_item.setdefault(position.addon_to, {}).setdefault(position.item, []).append(position)
            old_addons_by_parent_by_category.setdefault(position.addon_to, {}).setdefault(position.item.category_id, []).append(position)

    new_toplevel_positions = []
    new_positions_by_item = {}
    new_addons_by_parent_by_item = {}
    new_addons_by_parent_by_category = {}
    for position in new_positions:
        new_positions_by_item.setdefault(position.item, []).append(position)
        if not position.addon_to:
            new_toplevel_positions.append(position)
        if position.addon_to and not position.is_bundled:
            new_addons_by_parent_by_item.setdefault(position.addon_to, {}).setdefault(position.item, []).append(position)
            new_addons_by_parent_by_category.setdefault(position.addon_to, {}).setdefault(position.item.category_id, []).append(position)

    if len(new_toplevel_positions) > int(event.settings.max_items_per_order) and len(new_toplevel_positions) > len(old_toplevel_positions):
        yield ngettext_lazy(
            'You cannot have more than %s item in your order.',
            'You cannot have more than %s items in your order.'
        ) % event.settings.max_items_per_order
    for item, new_item_positions in new_positions_by_item.items():
        old_item_positions = old_positions_by_item.get(item, [])
        if item.min_per_order and len(new_item_positions) < item.min_per_order and len(new_item_positions) < len(old_item_positions):
            yield _(
                'You need to have at least %(min)s items (or none) of the product %(product)s in your order.'
            ) % {'min': item.min_per_order, 'product': item.name}
        if item.max_per_order and len(new_item_positions) > item.max_per_order and len(new_item_positions) > len(old_item_positions):
            yield ngettext_lazy(
                'You cannot have more than %(max)s item of the product %(product)s in your order.',
                'You cannot have more than %(max)s items of the product %(product)s in your order.',
                'max'
            ) % {'max': item.max_per_order, 'product': item.name}
    for position in new_toplevel_positions:
        for iao in position.item.addons.all():
            new_addons = new_addons_by_parent_by_category.get(position, {}).get(iao.addon_category_id, [])
            old_addons = old_addons_by_parent_by_category.get(position, {}).get(iao.addon_category_id, [])
            if len(new_addons) < iao.min_count and (position not in old_positions or len(new_addons) < len(old_addons)):
                yield ngettext_lazy(
                    'You need to have at least %(min)s add-on from the category %(cat)s for the product %(base)s.',
                    'You need to have at least %(min)s add-ons from the category %(cat)s for the product %(base)s.',
                    'min'
                ) % {'base': position.item.name, 'min': iao.min_count, 'cat': iao.addon_category.name}
            if len(new_addons) > iao.max_count and len(new_addons) > len(old_addons):
                yield ngettext_lazy(
                    'You can have at most %(max)s add-on from the category %(cat)s for the product %(base)s.',
                    'You can have at most %(max)s add-ons from the category %(cat)s for the product %(base)s.',
                    'max'
                ) % {'base': position.item.name, 'max': iao.max_count, 'cat': iao.addon_category.name}
            new_addons_by_item = new_addons_by_parent_by_item.get(position, {})
            old_addons_by_item = new_addons_by_parent_by_item.get(position, {})
            has_new_multi_addons = False
            for item, new_addons in new_addons_by_item.items():
                old_addons = old_addons_by_item.get(item, [])
                if len(new_addons) > 1 and len(new_addons) > len(old_addons):
                    has_new_multi_addons = True
            if not iao.multi_allowed and has_new_multi_addons:
                yield _(
                    'You can have every add-ons from the category %(cat)s for the product %(base)s at most once.'
                ) % {'base': position.item.name, 'cat': iao.addon_category.name}

    # We cannot determine if the change makes anything worse or not here
    try:
        validate_cart.send(sender=event, positions=new_positions)
    except CartError as e:
        yield str(e)
