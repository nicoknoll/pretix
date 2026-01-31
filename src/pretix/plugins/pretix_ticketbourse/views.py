import logging
from decimal import Decimal

from django import forms
from django.conf import settings
from django.http import Http404
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _
from django.utils.functional import cached_property
from django.views.generic import View, TemplateView
from django.urls import reverse
from django.db import models, transaction
from django.contrib import messages
from formtools.wizard.views import SessionWizardView
from django.core.exceptions import ValidationError

from pretix.control.permissions import EventPermissionRequiredMixin
from pretix.control.views.event import EventSettingsFormView, EventSettingsViewMixin
from pretix.control.views.orders import OrderView
from pretix.presale.views.event import item_group_by_category
from pretix.presale.views.order import OrderDetailMixin
from pretix.presale.views import EventViewMixin
from pretix.presale.signals import checkout_confirm_messages
from pretix.base.models import Event, Order, OrderPosition, TaxRule, InvoiceAddress
from pretix.base.services.pricing import get_price
from pretix.base.views.mixins import BaseQuestionsViewMixin
from pretix.base.forms.questions import BaseQuestionsForm

from .utils import (
    lock,
    is_selling_active,
    sellable_positions_queryset,
    get_missing_addons,
    get_positions_total,
    get_cancellation_fee,
    count_sell_items_per_group,
    get_buyable_items_for_order,
    validate_position_change,
)
from .models import BuyRequest, SellRequest
from .tasks import run_ticket_bourse, get_ticket_bourse_result
from .settings import TicketBourseSettingsForm, ITEM_GROUP_SUFFIXES

try:
    from refund_banktransfer.payment import RefundBanktransfer
    from refund_banktransfer.forms import RefundForm as RefundBanktransferForm
    HAS_REFUND_HANDLING = True
except ImportError:
    RefundBanktransfer = None
    class RefundBanktransferForm(forms.Form):
        pass
    HAS_REFUND_HANDLING = False


logger = logging.getLogger(__name__)


class BuyModeForm(forms.Form):
    mode = forms.ChoiceField(
        required=True,
        choices=BuyRequest.Modes.choices,
    )


class BuyItemsForm(forms.Form):
    def __init__(self, *args, order=None, items=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.order = order
        self.items = items

    @property
    def order_invoice_address(self):
        try:
            return self.order.invoice_address
        except InvoiceAddress.DoesNotExist:
            return None

    def clean(self):
        positions = []
        for item in self.items:
            count = int((self.data.get(f'item_{item.id}', []) or ['0'])[0])
            for i in range(count):
                position = OrderPosition(id=f'new_{item.id}_{i}', item=item)
                position.is_new = True
                positions.append(position)
                bundled_sum = Decimal('0.00')
                for bundle in item.bundles.all():
                    for j in range(bundle.count):
                        bundled_sum += bundle.designated_price
                        tax_rule = (bundle.bundled_item.tax_rule or TaxRule(rate=Decimal('0.00')))
                        price = tax_rule.tax(
                            bundle.designated_price,
                            base_price_is='gross',
                            invoice_address=self.order_invoice_address,
                        )
                        addon = OrderPosition(
                            id=f'new_bundled_{item.id}_{i}',
                            item=bundle.bundled_item,
                            variation=bundle.bundled_variation,
                            price=price,
                            tax_rule=tax_rule,
                            addon_to=position,
                            addon_to_id=position.id,
                            is_bundled=True,
                        )
                        positions.append(addon)
                position.price = get_price(
                    position.item,
                    variation=position.variation,
                    invoice_address=self.order_invoice_address,
                    bundled_sum=bundled_sum,
                )
                position.tax_rule = (position.item.tax_rule or TaxRule(rate=Decimal('0.00')))

        old_positions = list(self.order.positions.all())
        addon_formset = get_buyable_items_for_order(self.order.event, old_positions + positions)[1]
        if not addon_formset:
            if not positions:
                raise ValidationError(_('You need to select at least one product.'))
            for error in validate_position_change(self.order.event, old_positions, old_positions + positions):
                raise ValidationError(error)

        return {'positions': positions}


class BuyAddonsForm(forms.Form):
    def __init__(self, *args, order=None, addon_formset=None, items_step_positions=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.order = order
        self.addon_formset = addon_formset
        self.items_step_positions = items_step_positions

    @property
    def order_invoice_address(self):
        try:
            return self.order.invoice_address
        except InvoiceAddress.DoesNotExist:
            return None

    def clean(self):
        drafts = []
        for formset in self.addon_formset:
            position = formset['pos']
            for category in formset['categories']:
                for item in category['items']:
                    count = int((self.data.get(f'cp_{position.id}_item_{item.id}', []) or ['0'])[0])
                    for i in range(count):
                        draft = OrderPosition(
                            id=f'new_cp_{position.id}_{item.id}_{i}',
                            item=item,
                            addon_to=position,
                            addon_to_id=position.id,
                        )
                        draft.price = get_price(
                            draft.item,
                            variation=draft.variation,
                            invoice_address=self.order_invoice_address,
                            addon_to=draft.addon_to,
                        )
                        draft.tax_rule = (draft.item.tax_rule or TaxRule(rate=Decimal('0.00')))
                        drafts.append(draft)
        if not drafts and not self.items_step_positions:
            raise ValidationError(_('You need to select at least one product.'))
        old_positions = list(self.order.positions.all())
        new_position_drafts = self.items_step_positions + drafts
        for error in validate_position_change(self.order.event, old_positions, old_positions + new_position_drafts):
            raise ValidationError(error)
        return {'positions': drafts}


class BuyQuestionsForm(forms.Form):
    def __init__(self, *args, **kwargs):
        self.view = kwargs.pop('build_questions_view_func')(data=kwargs['data'], files=kwargs['files'])
        super().__init__(*args, **kwargs)

    def clean(self):
        is_valid = True
        for form in self.view.forms:
            is_valid &= form.is_valid()
        if not is_valid:
            raise ValidationError('Invalid answer')
        return {'view': self.view}


class BuyConfirmForm(forms.Form):
    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        msgs = {}
        responses = checkout_confirm_messages.send(self.event)
        for receiver, response in responses:
            msgs.update(response)
        for key, msg in msgs.items():
            self.fields[f'confirm_{key}'] = forms.BooleanField(label=msg)

    def clean(self):
        cleaned_data = super().clean()
        result = {'confirmed_messages': []}
        for name, field in self.fields.items():
            if cleaned_data.get(name):
                result['confirmed_messages'].append(str(field.label))
        return result


class RequestBuyView(EventViewMixin, OrderDetailMixin, SessionWizardView):
    template_name = 'pretix_ticketbourse/presale/buy/request.html'
    form_list = [
        ('mode', BuyModeForm),
        ('items', BuyItemsForm),
        ('addons', BuyAddonsForm),
        ('questions', BuyQuestionsForm),
        ('confirm', BuyConfirmForm),
    ]

    @cached_property
    def items_step_buyable_items(self):
        return get_buyable_items_for_order(self.order.event, self.order.positions.all())[0]

    @cached_property
    def items_condition(self):
        if self.request_mode != 'extended':
            return False
        return any(item.order_max for item in self.items_step_buyable_items)

    @property
    def items_step_positions(self):
        if hasattr(self, '_cached_items_step_positions'):
            return self._cached_items_step_positions
        if self.items_condition:
            cleaned_data = self.get_cleaned_data_for_step('items')
            if cleaned_data is None:
                return []
            self._cached_items_step_positions = cleaned_data['positions']
            return cleaned_data['positions']
        return []

    @property
    def addons_step_formset(self):
        cache_key = '_cached_addons_step_formset'
        items_step_positions = self.items_step_positions
        if not items_step_positions:
            cache_key += '_without_items_step_positions'
        if hasattr(self, cache_key):
            return getattr(self, cache_key)
        positions = list(self.order.positions.all()) + items_step_positions
        result = get_buyable_items_for_order(self.order.event, positions)[1]
        setattr(self, cache_key, result)
        return result

    @property
    def addons_condition(self):
        if self.request_mode != 'extended':
            return False
        return bool(self.addons_step_formset)

    @property
    def addons_step_positions(self):
        if hasattr(self, '_cached_addons_step_positions'):
            return self._cached_addons_step_positions
        if self.addons_condition:
            cleaned_data = self.get_cleaned_data_for_step('addons')
            if cleaned_data is None:
                return []
            self._cached_addons_step_positions = cleaned_data['positions']
            return cleaned_data['positions']
        return []

    def build_questions_view(self, new_positions=None, data=None, files=None):
        if new_positions is None:
            new_positions = self.items_step_positions + self.addons_step_positions
        for position in new_positions:
            position.item.questions_to_ask = position.item.questions.filter(ask_during_checkin=False, hidden=False).all()
            position.answerlist = []

        addons_to_existing_positions = [position for position in new_positions if position.addon_to and position.addon_to not in new_positions]
        new_positions = addons_to_existing_positions + [position for position in new_positions if position not in addons_to_existing_positions]

        class _QuestionsForm(BaseQuestionsForm):
            required_css_class = 'required'

            def __init__(_self, *args, **kwargs):
                kwargs['request'] = None
                kwargs['data'] = data
                kwargs['files'] = files
                super().__init__(*args, **kwargs)

        class _QuestionsView(BaseQuestionsViewMixin):
            form_class = _QuestionsForm
            request = self.request

            @property
            def _positions_for_questions(_self):
                return new_positions

        return _QuestionsView()

    @property
    def questions_condition(self):
        if self.request_mode != 'extended':
            return False
        cache_key = '_cached_questions_condition'
        items_step_positions = self.items_step_positions
        if items_step_positions:
            cache_key += '_without_items_step_positions'
        addons_step_positions = self.addons_step_positions
        if addons_step_positions:
            cache_key += '_without_addons_step_positions'
        if hasattr(self, cache_key):
            return getattr(self, cache_key)
        result = bool(self.build_questions_view(items_step_positions + addons_step_positions).forms)
        setattr(self, cache_key, result)
        return result

    @property
    def confirm_condition(self):
        return self.items_condition or self.addons_condition

    condition_dict = {
        'items': lambda self: self.items_condition,
        'addons': lambda self: self.addons_condition,
        'questions': lambda self: self.questions_condition,
        'confirm': lambda self: self.confirm_condition,
    }

    def get_request_mode(self):
        if self.buy_request.can_change_mode:
            return 'change'
        elif self.buy_request.can_start:
            return 'full'
        elif self.buy_request.can_start_extended:
            return 'extended'
        else:
            return None

    def dispatch(self, request, *args, **kwargs):
        if not self.order or not self.order.event.settings.ticketbourse_allow_edit or not is_selling_active(self.order.event):
            raise Http404()
        self.buy_request = BuyRequest.get_for_order(self.order)
        self.request_mode = self.get_request_mode()
        if not self.request_mode:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['order'] = self.order
        ctx['prev_url'] = self.get_order_url()
        if self.steps.current == 'items':
            ctx['items_by_category'] = item_group_by_category(self.items_step_buyable_items)
        if self.steps.current == 'addons':
            for form in self.addons_step_formset:
                form['pos'].cache_answers(all=False)
            ctx['addon_formset'] = self.addons_step_formset
        if self.steps.current == 'questions':
            formdict = ctx['form'].view.formdict
            for pos, forms in formdict.items():
                pos.cache_answers(all=False)
            ctx['formdict'] = formdict
        if self.steps.current == 'confirm':
            ctx['mode'] = self.get_cleaned_data_for_step('mode').get('mode')
            new_positions = self.items_step_positions + self.addons_step_positions
            addons_to_existing_positions = [position for position in new_positions if position.addon_to and position.addon_to not in new_positions]
            ctx['new_positions'] = addons_to_existing_positions + [position for position in new_positions if position not in addons_to_existing_positions]
            ctx['total_gross'] = sum(position.price.gross for position in ctx['new_positions'])
            ctx['total_tax'] = sum(position.price.tax for position in ctx['new_positions'])
        return ctx

    def get_form_kwargs(self, step=None):
        if step == 'items':
            return {'order': self.order, 'items': self.items_step_buyable_items}
        if step == 'addons':
            return {'order': self.order, 'addon_formset': self.addons_step_formset, 'items_step_positions': self.items_step_positions}
        if step == 'questions':
            return {'build_questions_view_func': self.build_questions_view}
        if step == 'confirm':
            return {'event': self.order.event}
        return {}

    def done(self, form_list, *args, **kwargs):
        with lock(self.request.event) as lock_acquired:
            if not lock_acquired:
                messages.error(self.request, _('We could not process your request. Please try again later.'))
                logger.error('Could not acquire ticketbourse lock for RequestBuyView')
                return redirect(self.get_order_url())

            with transaction.atomic():
                self.buy_request = BuyRequest.get_for_order(self.order)
                assert self.request_mode == self.get_request_mode()
                mode = self.get_cleaned_data_for_step('mode')['mode']
                if self.request_mode == 'change':
                    self.buy_request.change_mode(mode)
                elif self.request_mode == 'full':
                    self.buy_request.start(mode)
                elif self.request_mode == 'extended':
                    confirmed_messages = self.get_cleaned_data_for_step('confirm')['confirmed_messages']
                    for msg in confirmed_messages:
                        self.order.log_action('pretix.event.order.consent', data={'msg': msg})
                    old_positions = list(self.order.positions.all())
                    new_position_drafts = self.items_step_positions + self.addons_step_positions
                    assert new_position_drafts
                    assert not list(validate_position_change(self.order.event, old_positions, old_positions + new_position_drafts))
                    questions_view = self.get_cleaned_data_for_step('questions')['view'] if self.questions_condition else None
                    self.buy_request.start_extended(mode, new_position_drafts)
                    if questions_view:
                        for form in questions_view.forms:
                            form.pos = form.pos.real_position
                            form.orderpos = form.orderpos.real_position
                        success = questions_view.save()
                        assert success

        run_ticket_bourse.apply_async(kwargs={'event_id': self.order.event.id})
        return redirect(self.get_order_url())


class CancelBuyView(EventViewMixin, OrderDetailMixin, TemplateView):
    template_name = 'pretix_ticketbourse/presale/buy/cancel_request.html'

    def dispatch(self, request, *args, **kwargs):
        if not self.order or not self.order.event.settings.ticketbourse_allow_edit:
            raise Http404()
        self.buy_request = BuyRequest.get_for_order(self.order)
        if not self.buy_request.can_abort:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['order'] = self.order
        ctx['prev_url'] = self.get_order_url()
        return ctx

    def post(self, request, *args, **kwargs):
        self.request = request

        with lock(self.request.event) as lock_acquired:
            if not lock_acquired:
                messages.error(self.request, _('We could not process your request. Please try again later.'))
                logger.error('Could not acquire ticketbourse lock for CancelBuyView')
                return redirect(self.get_order_url())

            self.buy_request.refresh_from_db()
            if not self.buy_request.can_abort:
                raise Http404()

            self.buy_request.abort()

        # A canceled buy does not affect the ticktbourse algorithm -> no need for run_ticket_bourse
        return redirect(self.get_order_url())


class SellModeForm(forms.Form):
    mode = forms.ChoiceField(
        required=True,
        choices=SellRequest.Modes.choices,
    )
    direct_code = forms.CharField(
        required=False,
        label=_('Direct Code'),
    )

    def __init__(self, *args, order=None, **kwargs):
        self.order = order
        super().__init__(*args, **kwargs)

    @cached_property
    def ratelimit_key(self):
        return f'pretix_ticketbourse_directcode_{self.order.id}'

    def check_is_ratelimited(self):
        if not settings.HAS_REDIS:
            return False
        from django_redis import get_redis_connection
        conn = get_redis_connection('redis')
        count = conn.get(self.ratelimit_key)
        return count and int(count) > 3

    def log_ratelimit_event(self):
        if not settings.HAS_REDIS:
            return
        from django_redis import get_redis_connection
        conn = get_redis_connection('redis')
        conn.incr(self.ratelimit_key)
        conn.expire(self.ratelimit_key, 1*60*60)

    def clean(self):
        cleaned_data = super().clean()
        cleaned_data['direct_buy_request'] = None
        mode = cleaned_data.get('mode', 'none')
        if mode == SellRequest.Modes.DIRECT:
            direct_code = cleaned_data.get('direct_code')
            if not direct_code:
                self.add_error('direct_code', _('This field is required.'))

            if self.check_is_ratelimited():
                raise forms.ValidationError(_('Please wait 1 hour before you try again.'), code='rate_limit')

            try:
                order = self.order.event.orders.get(code=direct_code)
            except Order.DoesNotExist:
                self.log_ratelimit_event()
                self.add_error('direct_code', _('The Direct Code is invalid.'))
                return
            buy_request = BuyRequest.get_for_order(order)
            if not buy_request or buy_request.effective_state != BuyRequest.States.REQUESTED:
                self.log_ratelimit_event()
                self.add_error('direct_code', _('The Direct Code is invalid.'))
                return

            sell_counts = count_sell_items_per_group(self.order, sellable_positions_queryset(self.order))
            buy_counts = buy_request.count_items_per_group()
            if any(sell_counts.get(group, 0) < count for group, count in buy_counts.items()):
                raise forms.ValidationError(_('You do not have all tickets required for the entered Direct Code.'))
            cleaned_data['direct_buy_request'] = buy_request
        else:
            cleaned_data['direct_code'] = None

        return cleaned_data


class SellPositionsForm(forms.Form):
    def __init__(self, *args, order=None, direct_buy_request=None, **kwargs):
        self.order = order
        self.direct_buy_request = direct_buy_request
        super().__init__(*args, **kwargs)
        self.fields['positions'] = forms.ModelMultipleChoiceField(queryset=sellable_positions_queryset(self.order), required=False)
        self.required_item_groups = []
        if self.direct_buy_request:
            sellable_items = {}
            for position in sellable_positions_queryset(self.order).all():
                sellable_items[position.item.id] = position.item
            group_items_map = {}
            for index, suffix in enumerate(ITEM_GROUP_SUFFIXES):
                group_items_map[index] = [
                    sellable_items[item_id]
                    for item_id in order.event.settings['ticketbourse_sell_items'+suffix]
                    if item_id in sellable_items
                ]
            for group, count in self.direct_buy_request.count_items_per_group().items():
                self.required_item_groups.append({
                    'items': group_items_map[group],
                    'count': count,
                })

    def clean(self):
        cleaned_data = super().clean()
        positions = list(cleaned_data.get('positions') or [])
        if not positions:
            self.add_error('positions', _('You need to select at least one ticket.'))
        for position, missing_addons in get_missing_addons(positions):
            self.add_error('positions', _('You cannot sell %(product)s without also selling all associated addon products.')%{'product': position.item})
        if self.direct_buy_request:
            if count_sell_items_per_group(self.order, positions) != self.direct_buy_request.count_items_per_group():
                self.add_error('positions', _('You need to select the right amount of tickets for the Direct Code.'))

        old_positions = list(self.order.positions.all())
        new_positions = [
            position for position in old_positions
            if position not in positions and (position.addon_to not in positions or not position.is_bundled)
        ]
        if new_positions:
            for error in validate_position_change(self.order.event, old_positions, new_positions):
                self.add_error('positions', error)

        return cleaned_data


class RequestSellView(EventViewMixin, OrderDetailMixin, SessionWizardView):
    form_list = [
        ('mode', SellModeForm),
        ('positions', SellPositionsForm),
        ('refund', RefundBanktransferForm),
        ('confirm', forms.Form),
    ]
    template_name = 'pretix_ticketbourse/presale/sell/request.html'

    def positions_condition(self):
        return self.order.positions.filter(is_bundled=False).count() > 1

    def refund_condition(self):
        pp = self.request.event.get_payment_providers().get('refund-banktransfer')
        if pp and pp.is_enabled and pp.can_refund(order=self.order):
            return True
        return False

    condition_dict = {
        'positions': positions_condition,
        'refund': refund_condition,
    }

    def dispatch(self, request, *args, **kwargs):
        if not self.order or not self.order.event.settings.ticketbourse_allow_edit or not is_selling_active(self.order.event):
            raise Http404()
        self.sell_request = SellRequest.get_for_order(self.order)
        if not self.sell_request.can_start_or_change:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_cleaned_positions(self):
        if self.positions_condition():
            return self.get_cleaned_data_for_step('positions').get('positions')
        else:
            return self.order.positions.filter(is_bundled=False).all()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['order'] = self.order
        ctx['prev_url'] = self.get_order_url()
        ctx['sell_request'] = self.sell_request
        if self.steps.current == 'positions':
            ctx['all_positions'] = sorted(self.order.positions.all(), key=lambda position: position.sort_key)
        if self.steps.current == 'confirm':
            ctx['selected_mode'] = self.get_cleaned_data_for_step('mode').get('mode')
            if ctx['selected_mode'] == SellRequest.Modes.DIRECT:
                ctx['direct_code'] = self.get_cleaned_data_for_step('mode').get('direct_code')
            if self.storage.get_step_data('refund'):
                ctx['refund_confirm'] = self.get_form(step='refund').render_confirm(
                    data=self.storage.get_step_data('refund')
                )
            positions = self.get_cleaned_positions()
            # Quick hack to display bundled addons, but not pass them to get_positions_total (it accounts for them already)
            # TODO: Refactor everything so that we treat bundled addons like other addons and just ignore them in the algorithm code
            ctx['selected_positions'] = list(positions)
            for position in ctx['selected_positions'].copy():
                ctx['selected_positions'] += list(position.addons.filter(is_bundled=True).all())
            ctx['selected_positions'].sort(key=lambda position: position.sort_key)
            ctx['cancellation_fee'] = - get_cancellation_fee(self.request.event, ctx['selected_positions'])
            ctx['refund_amount'] = get_positions_total(positions) + ctx['cancellation_fee']
        return ctx

    def get_form_kwargs(self, step=None):
        if step in {'mode', 'refund'}:
            return {'order': self.order}
        if step == 'positions':
            return {'order': self.order, 'direct_buy_request': self.get_cleaned_data_for_step('mode').get('direct_buy_request')}
        return {}

    def done(self, form_list, *args, **kwargs):
        with lock(self.request.event) as lock_acquired:
            if not lock_acquired:
                messages.error(self.request, _('We could not process your request. Please try again later.'))
                logger.error('Could not acquire ticketbourse lock for RequestBuyView')
                return redirect(self.get_order_url())

            with transaction.atomic():
                self.sell_request = SellRequest.get_for_order(self.order)
                if not self.sell_request.can_start_or_change:
                    raise Http404()

                positions = self.get_cleaned_positions()

                if HAS_REFUND_HANDLING and self.storage.get_step_data("refund"):
                    # Create/update RefundInfo object that is later used for try_auto_refund
                    RefundBanktransfer(
                        self.request.event
                    ).refund_presale_form_process(
                        order=self.order,
                        data=self.storage.get_step_data("refund"),
                        fee=get_cancellation_fee(self.request.event, positions),
                        prefix='refund',
                        reason=self.request.event.settings.ticketbourse_banktransfer_refund_reason,
                    )

                mode = self.get_cleaned_data_for_step('mode').get('mode')
                direct_code = self.get_cleaned_data_for_step('mode').get('direct_code')
                self.sell_request.start_or_change(mode, positions, code=direct_code)

        run_ticket_bourse.apply_async(kwargs={'event_id': self.order.event.id})
        return redirect(self.get_order_url())


class CancelSellView(EventViewMixin, OrderDetailMixin, TemplateView):
    template_name = 'pretix_ticketbourse/presale/sell/cancel_request.html'

    def dispatch(self, request, *args, **kwargs):
        if not self.order or not self.order.event.settings.ticketbourse_allow_edit:
            raise Http404()
        self.sell_request = SellRequest.get_for_order(self.order)
        if not self.sell_request.can_abort:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['order'] = self.order
        ctx['prev_url'] = self.get_order_url()
        return ctx

    def post(self, request, *args, **kwargs):
        self.request = request

        with lock(self.request.event) as lock_acquired:
            if not lock_acquired:
                messages.error(self.request, _('We could not process your request. Please try again later.'))
                logger.error('Could not acquire ticketbourse lock for RequestBuyView')
                return redirect(self.get_order_url())

            self.sell_request.refresh_from_db()
            if not self.sell_request.can_abort:
                raise Http404()

            self.sell_request.abort()

        # A canceled sell does not affect the ticktbourse algorithm -> no need for run_ticket_bourse
        return redirect(self.get_order_url())


class TicketBourseSettingsView(EventSettingsViewMixin, EventSettingsFormView):
    model = Event
    permission = 'can_change_settings'
    form_class = TicketBourseSettingsForm
    template_name = 'pretix_ticketbourse/control/settings.html'

    def get_success_url(self, **kwargs):
        return reverse(
            'plugins:pretix_ticketbourse:event.settings.ticketbourse',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        )

    def post(self, *args, **kwargs):
        with lock(self.request.event) as lock_acquired:
            if not lock_acquired:
                raise Exception('Cannot acquire ticket bourse lock')
            resp = super().post(*args, **kwargs)
        run_ticket_bourse.apply_async(kwargs={'event_id': self.request.event.id})
        return resp


class TicketBourseStatsView(EventPermissionRequiredMixin, TemplateView):
    permission = 'can_view_orders'
    template_name = 'pretix_ticketbourse/control/stats.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['event'] = self.request.event
        ctx['results'] = get_ticket_bourse_result(self.request.event)

        ctx.update({
            f'buy_{mode}_{state}_count': 0
            for mode in BuyRequest.Modes
            for state in BuyRequest.States
        })
        qs = BuyRequest.objects.filter(
            order__event=self.request.event,
        ).values(
            'mode',
            'state',
        ).annotate(
            count=models.Count('*'),
        )
        for row in qs:
            ctx[f'buy_{row["mode"]}_{row["state"]}_count'] = row['count']

        ctx.update({
            f'sell_{mode}_{state}_count': 0
            for mode in SellRequest.Modes
            for state in SellRequest.States
        })
        qs = SellRequest.objects.filter(
            order__event=self.request.event,
        ).values(
            'mode',
            'state',
        ).annotate(
            count=models.Count('*'),
        )
        for row in qs:
            ctx[f'sell_{row["mode"]}_{row["state"]}_count'] = row['count']
        for mode in SellRequest.Modes:
            ctx[f'sell_{mode}_FAILED_count'] = 0
            for state in SellRequest.FAILED_STATES:
                ctx[f'sell_{mode}_FAILED_count'] += ctx[f'sell_{mode}_{state}_count']
        return ctx


class ManualCancelApprove(View):
    permission = 'can_change_settings'

    def post(self, request, *args, **kwargs):
        manual_params = {'cancel_groups': [], 'approve_groups': []}
        for index in range(len(ITEM_GROUP_SUFFIXES)):
            manual_params['cancel_groups'].append(int(request.POST[f'cancel_{index}']))
            manual_params['approve_groups'].append(int(request.POST[f'approve_{index}']))
        run_ticket_bourse.apply_async(kwargs={'event_id': self.request.event.id, 'manual_params': manual_params})
        return redirect(reverse(
            'plugins:pretix_ticketbourse:event.ticketbourse.stats',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        ))


class BuyApproveOrderView(OrderView):
    permission = 'can_change_orders'
    template_name = 'pretix_ticketbourse/control/buy/approve_order.html'

    def dispatch(self, request, *args, **kwargs):
        self.buy_request = BuyRequest.get_for_order(self.order)
        if not self.buy_request.can_approve_order:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['buy_request'] = self.buy_request
        return ctx

    def post(self, request, *args, **kwargs):
        with lock(self.request.event) as lock_acquired:
            if not lock_acquired:
                raise Exception('Cannot acquire ticket bourse lock')
            self.buy_request.refresh_from_db()
            self.buy_request.approve_order(self.request.user)
        run_ticket_bourse.apply_async(kwargs={'event_id': self.order.event.id})
        return redirect(self.get_order_url())


class BuyDeleteRequestView(OrderView):
    permission = 'can_change_orders'
    template_name = 'pretix_ticketbourse/control/buy/delete_request.html'

    def dispatch(self, request, *args, **kwargs):
        self.buy_request = BuyRequest.get_for_order(self.order)
        if not self.buy_request.can_delete:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['buy_request'] = self.buy_request
        return ctx

    def post(self, request, *args, **kwargs):
        with lock(self.request.event) as lock_acquired:
            if not lock_acquired:
                raise Exception('Cannot acquire ticket bourse lock')
            self.buy_request.refresh_from_db()
            self.buy_request.delete(user=self.request.user)
        return redirect(self.get_order_url())


class BuyBlockRequest(OrderView):
    permission = 'can_change_orders'

    def dispatch(self, request, *args, **kwargs):
        self.buy_request = BuyRequest.get_for_order(self.order)
        if not self.buy_request.can_block:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.buy_request.block(user=self.request.user)
        return redirect(self.get_order_url())


class BuyUnblockRequest(OrderView):
    permission = 'can_change_orders'
    template_name = 'pretix_ticketbourse/control/buy/unblock_request.html'

    def dispatch(self, request, *args, **kwargs):
        self.buy_request = BuyRequest.get_for_order(self.order)
        if not self.buy_request.can_block:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['buy_request'] = self.buy_request
        return ctx

    def post(self, request, *args, **kwargs):
        self.buy_request.unblock(user=self.request.user)
        run_ticket_bourse.apply_async(kwargs={'event_id': self.order.event.id})
        return redirect(self.get_order_url())


class SellCancelOrderView(OrderView):
    permission = 'can_change_orders'
    template_name = 'pretix_ticketbourse/control/sell/cancel_order.html'

    def dispatch(self, request, *args, **kwargs):
        self.sell_request = SellRequest.get_for_order(self.order)
        if not self.sell_request.can_cancel_order:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['sell_request'] = self.sell_request
        return ctx

    def post(self, request, *args, **kwargs):
        with lock(self.request.event) as lock_acquired:
            if not lock_acquired:
                raise Exception('Cannot acquire ticket bourse lock')
            self.sell_request.refresh_from_db()
            self.sell_request.cancel_order(self.request.user)
        run_ticket_bourse.apply_async(kwargs={'event_id': self.order.event.id})
        return redirect(self.get_order_url())


class SellDeleteRequestView(OrderView):
    permission = 'can_change_orders'
    template_name = 'pretix_ticketbourse/control/sell/delete_request.html'

    def dispatch(self, request, *args, **kwargs):
        self.sell_request = SellRequest.get_for_order(self.order)
        if not self.sell_request.can_delete:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['sell_request'] = self.sell_request
        return ctx

    def post(self, request, *args, **kwargs):
        with lock(self.request.event) as lock_acquired:
            if not lock_acquired:
                raise Exception('Cannot acquire ticket bourse lock')
            self.sell_request.refresh_from_db()
            self.sell_request.delete(user=self.request.user)
        return redirect(self.get_order_url())
