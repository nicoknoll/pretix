from decimal import Decimal
import datetime

from django import forms
from django.utils.translation import gettext_lazy as _, gettext_noop
from i18nfield.strings import LazyI18nString
from i18nfield.forms import I18nFormField, I18nTextarea, I18nTextInput

from pretix.base.settings import settings_hierarkey
from pretix.base.forms import SettingsForm, PlaceholderValidator
from pretix.base.email import get_available_placeholders
from pretix.control.forms import SplitDateTimeField, SplitDateTimePickerWidget


COMMON_SETTINGS = {
    'ticketbourse_cancellation_fee': {
        'type': Decimal,
        'form_class': forms.DecimalField,
        'form_kwargs': {
            'label': _('Cancellation fee'),
            'decimal_places': 2,
            'localize': True,
        },
        'default': '0',
    },
    'ticketbourse_payment_term_days': {
        'type': int,
        'form_class': forms.IntegerField,
        'form_kwargs': {
            'label': _('Payment term in days'),
        },
        'default': '2',
    },
    'ticketbourse_banktransfer_refund_reason': {
        'type': str,
        'form_class': forms.CharField,
        'form_kwargs': {
            'label': _('Banktransfer refund reason'),
        },
        'default': 'REFUND TICKET BOURSE',
    },
    'ticketbourse_enable': {
        'type': bool,
        'form_class': forms.BooleanField,
        'form_kwargs': {
            'label': _('Enable automatic approval/cancelation'),
        },
        'default': 'True',
    },
    'ticketbourse_active_from': {
        'type': datetime.datetime,
        'form_class': SplitDateTimeField,
        'form_kwargs': {
            'label': _('Active from'),
            # Prior to this date, no requests can be created, nothing gets approved
            'widget': SplitDateTimePickerWidget(),
        },
        'default': None,
    },
    'ticketbourse_active_until': {
        'type': datetime.datetime,
        'form_class': SplitDateTimeField,
        'form_kwargs': {
            'label': _('Active until'),
            # After this date no requests can be created or get approved
            # Sell requests can still be withdrawn
            # Ongoing transactions are still allowed to complete, so sell requests can still be completed (by caceling the positions, due to an approved order being paid)
            'widget': SplitDateTimePickerWidget(),
        },
        'default': None,
    },
    'ticketbourse_approval_batching_mode': {
        'type': str,
        'form_class': forms.ChoiceField,
        'form_kwargs': {
            'label': _('Batch automatic buy request approvals'),
            'choices': [
                ('DISABLED', _('Disabled')),
                ('HOURLY', _('Hourly')),
                ('DAILY', _('Daily')),
                ('PAYMENT_TERM', _('Aligned to payment term')),
            ],
            'help_text': _('The auto-approval algorithm statistically discriminates buy requests that span mulitple product groups. Delaying and batching approvals somewhat couters that. The batching interval should be as large as possible. However, batching slows down reselling, so there is a tradeoff between resell speed and fairness. At times with many sell requests, it might make sense to choose a short interval (e.g. hourly), otherwise longer intervals are recommended.'),
        },
        'default': 'DISABLED',
    },
    'ticketbourse_allow_edit': {
        'type': bool,
        'form_class': forms.BooleanField,
        'form_kwargs': {
            'label': _('Allow creation/edits of requests'),
        },
        'default': 'True',
    },
#    'ticketbourse_ratelimit': {
#        'type': int,
#        'form_class': forms.IntegerField,
#        'form_kwargs': {
#            'label': _('Auto approve/cancel rate limit'),
#            'help_text': _('Limits the number of approve and cancel actions per hour. Safeguard to slow automatic operation down to be more observable and limit the impact of errors. Negative values disable the ratelimit.'),
#        },
#        'default': '10',
#    },
    'ticketbourse_order_info': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('No request or request failed'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
To buy or sell a ticket on the Ticket Exchange, you need to create a request with one \
of the buttons below.'''))
    },

}


ITEM_GROUP_SUFFIXES = ['', '2', '3']
ITEM_GROUP_SETTINGS = {}
for index, suffix in enumerate(ITEM_GROUP_SUFFIXES):
    ITEM_GROUP_SETTINGS['ticketbourse_buy_items'+suffix] = {
        'type': list,
        'form_class': forms.ModelMultipleChoiceField,
        'form_kwargs': lambda event: {
            'label': _('Items allowed to buy'),
            'widget': forms.CheckboxSelectMultiple(
                attrs={'class': 'scrolling-multiple-choice'},
            ),
            'queryset': event.items.filter(require_approval=True, require_bundling=False).all(),
            'help_text': _('Caution: Removing items will abort all open buy requests with these items!'),
        },
        'clean': lambda objs: [obj.pk for obj in objs],
        'default': '[]',
    }
    ITEM_GROUP_SETTINGS['ticketbourse_sell_items'+suffix] = {
        'type': list,
        'form_class': forms.ModelMultipleChoiceField,
        'form_kwargs': lambda event: {
            'label': _('Items allowed to sell'),
            'widget': forms.CheckboxSelectMultiple(
                attrs={'class': 'scrolling-multiple-choice'},
            ),
            'queryset': event.items.filter(require_bundling=False).all(),
            'help_text': _('Caution: Removing items will abort all open sell requests with these items!'),
        },
        'clean': lambda objs: [obj.pk for obj in objs],
        'default': '[]',
    }
    ITEM_GROUP_SETTINGS['ticketbourse_target_balance'+suffix] = {
        'type': int,
        'form_class': forms.IntegerField,
        'form_kwargs': {
            'label': _('Target sell/buy balance'),
            'help_text': _('Caution: Changes to this value can immediatly cancel or approve many orders! Positive values cause the ticket exchange to sell extra tickets, negative values cause it to buy back tickets.'),
        },
        'default': '0',
    }
    ITEM_GROUP_SETTINGS['ticketbourse_oversell_limit'+suffix] = {
        'type': int,
        'form_class': forms.IntegerField,
        'form_kwargs': {
            'label': _('Oversell limit'),
            'help_text': _('Limits the number of tickets to risk overselling. Safeguard. Negative values disable the limit.'),
        },
        'default': '10',
    }


BUY_SETTINGS = {
    # Checkout step
    'ticketbourse_buy_request_checkout_header': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Text above options'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Please select whether you want to buy a ticket directly from a specific \
person or you want to take part in the automatic ticket resale:'''))
    },
    'ticketbourse_buy_request_checkout_option_direct_label': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextInput,
            'label': _('Option Direct (label)'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('Direct Code'))
    },
    'ticketbourse_buy_request_checkout_option_direct': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Option Direct'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Select this option, if you want to buy a ticket from a specific person.

After placing your order, you well get a direct code. The person you want to \
buy a ticket from has to enter this code to start the transaction.'''))
    },
    'ticketbourse_buy_request_checkout_option_auto_label': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextInput,
            'label': _('Option Auto (label)'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('Random match'))
    },
    'ticketbourse_buy_request_checkout_option_auto': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Option Auto'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Select this option, if you want to buy a ticket from anyone who offers it.

Whenever someone offers to sell a ticket, your order has the chance to be \
selected at random. You will be notified in this case.'''))
    },
    'ticketbourse_buy_request_checkout_footer': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Text below options'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Note that you can change your decision afterwards until you have been offered \
a ticket.'''))
    },

    # Checkout confirmation
    'ticketbourse_buy_request_checkout_confirm_direct': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Direct selected'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
You selected to buy a ticket directly.'''))
    },
    'ticketbourse_buy_request_checkout_confirm_auto': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Auto selected'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
You selected to buy a ticket automatically.'''))
    },

    # Change/create request
    'ticketbourse_buy_request_create_header': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Text above options'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Please select whether you want to buy a ticket directly from a specific \
person or you want to take part in the automatic ticket resale:'''))
    },
    'ticketbourse_buy_request_create_option_direct_label': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextInput,
            'label': _('Option Direct (label)'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('Direct Code'))
    },
    'ticketbourse_buy_request_create_option_direct': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Option Direct'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Select this option, if you want to buy a ticket from a specific person.

You well get a direct code. The person you want to \
buy a ticket from has to enter this code to start the transaction.'''))
    },
    'ticketbourse_buy_request_create_option_auto_label': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextInput,
            'label': _('Option Auto (label)'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('Random match'))
    },
    'ticketbourse_buy_request_create_option_auto': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Option Auto'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Select this option, if you want to buy a ticket from anyone who offers it.

Whenever someone offers to sell a ticket, your order has the chance to be \
selected at random. You will be notified in this case.'''))
    },
    'ticketbourse_buy_request_create_footer': {
         'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Text below options'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Note that you can change your decision afterwards until you have been offered \
a ticket.'''))
    },

    # Order info box
    'ticketbourse_buy_request_order_direct_requested': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Direct selected'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
You selected that you want to buy a ticket directly. Copy the direct code \
below and send it to the person you want to buy the ticket from. The person \
has to create a direct sell request and enter your direct code. You will be \
notified, when your order has been approved and can be payed.'''))
    },
    'ticketbourse_buy_request_order_auto_requested': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Auto selected'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
You selected that you want to buy a ticket automatically. Please wait until \
you receive an offer to buy a ticket. You will be notified, when this is the \
case and your order can be payed.'''))
    },
    'ticketbourse_buy_request_order_approved': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Order approved'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
You received an offer to buy a ticket.

If you don't plan to buy a ticket anymore, please cancel your order so \
someone else can get chance at buying the ticket.'''))
    },
    'ticketbourse_buy_request_order_failed': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Request failed'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Your request to buy a ticket failed.'''))
    },


    # Order approved e-mail
    'ticketbourse_buy_request_approved_mail_subject': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextInput,
            'label': _('Subject'),
        },
        'placeholders': ['event', 'order'],
        'default': LazyI18nString.from_gettext(gettext_noop('Order approved and awaiting payment: {code}')),
    },
    'ticketbourse_buy_request_approved_mail_text': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Text'),
        },
        'placeholders': ['event', 'order'],
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Hello,

we approved your order for {event} and will be happy to welcome you
at our event.

Please continue by paying for your order before {expire_date}.

You can select a payment method and perform the payment here:

{url}

Best regards,
Your {event} team''')),
    },
}


SELL_SETTINGS = {
    # Order info box
    'ticketbourse_sell_request_order_auto_requested': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Auto sell requested'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
You requested to sell your ticket automatically. We will notify you as soon \
as we were able to sell it.'''))
    },
    'ticketbourse_sell_request_order_direct_requested': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Direct sell requested'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
You requested to sell your ticket to a specfic person. The order that matches \
the direct code you have entered is pending payment. We will notify you, when \
it is payed and we have prepared your refund.'''))
    },
    'ticketbourse_sell_request_order_completed': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Sell completed'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Your request to sell your ticket succeeded.'''))
    },
    'ticketbourse_sell_request_order_failed': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Sell failed'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Your request to sell your ticket failed.'''))
    },

    # Change/create request
    'ticketbourse_sell_request_create_header': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Text above options'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Please select whether you want to sell your ticket directly to a specific \
person or you want to take part in the automatic ticket resale:'''))
    },
    'ticketbourse_sell_request_create_option_direct_label': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextInput,
            'label': _('Option Direct (label)'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('Direct Code'))
    },
    'ticketbourse_sell_request_create_option_direct': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Option Direct'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Select this option, if you want to sell your ticket to a specific person.

The person you want to sell your ticket to must create a Ticket Exchange order \
first and select the option for direct resale. The person will get a direct \
code that you need to enter here to start the transaction:'''))
    },
    'ticketbourse_sell_request_create_option_auto_label': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextInput,
            'label': _('Option Auto (label)'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('Random match'))
    },
    'ticketbourse_sell_request_create_option_auto': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Option Auto'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
Select this option, if you want to sell your ticket to anyone who wants it. \
You will be notified when your ticket is sold.'''))
    },
    'ticketbourse_sell_request_create_footer': {
         'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Text below options'),
        },
        'default': '{}'
    },

    # Change/create request confirmation
    'ticketbourse_sell_request_create_confirm_direct': {
         'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Direct selected'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
You selected to sell your ticket directly to a specific person.'''))
    },
    'ticketbourse_sell_request_create_confirm_auto': {
         'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Auto selected'),
        },
        'default': LazyI18nString.from_gettext(gettext_noop('''\
You selected to sell your ticket automatically.'''))
    },

    # Sell completed e-mail
    'ticketbourse_sell_request_completed_mail_subject': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextInput,
            'label': _('Subject'),
        },
        'placeholders': ['event', 'order'],
        'default': LazyI18nString.from_gettext(gettext_noop('Order canceled: {code}')),
    },
    'ticketbourse_sell_request_completed_mail_text': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Text'),
        },
        'placeholders': ['event', 'order'],
        'default': LazyI18nString.from_gettext(gettext_noop('''Hello,

your order {code} for {event} has been canceled.

You can view the details of your order at
{url}

Best regards,
Your {event} team''')),
    },
    # Sell failed e-mail
    'ticketbourse_sell_request_failed_buyer_mail_subject': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextInput,
            'label': _('Subject'),
        },
        'placeholders': ['event', 'order'],
        'default': LazyI18nString.from_gettext(gettext_noop('Ticket Exchange request failed: {code}')),
    },
    'ticketbourse_sell_request_failed_buyer_mail_text': {
        'type': LazyI18nString,
        'form_class': I18nFormField,
        'form_kwargs': {
            'widget': I18nTextarea,
            'label': _('Text'),
        },
        'placeholders': ['event', 'order'],
        'default': LazyI18nString.from_gettext(gettext_noop('''Hello,

your request, to sell your order {code} for {event} failed.
You can create a new request, if you are still interested.

You can view the details of your order at
{url}

Best regards,
Your {event} team''')),
    },
}


for settings_dict in (COMMON_SETTINGS, ITEM_GROUP_SETTINGS, BUY_SETTINGS, SELL_SETTINGS):
    for key, args in settings_dict.items():
        settings_hierarkey.add_default(key, args['default'], args['type'])

settings_hierarkey.add_default('ticketbourse_approval_batching_last_run', '1970-01-01 01:00:00Z', datetime.datetime)

class TicketBourseSettingsForm(SettingsForm):
    COMMON_FIELDS = COMMON_SETTINGS
    ITEM_GROUP_FIELDS = ITEM_GROUP_SETTINGS
    BUY_FIELDS = BUY_SETTINGS
    SELL_FIELDS = SELL_SETTINGS

    def __init__(self, *args, **kwargs):
        self.event = kwargs.get('obj')
        super().__init__(*args, **kwargs)
        for settings_dict in (COMMON_SETTINGS, ITEM_GROUP_SETTINGS, BUY_SETTINGS, SELL_SETTINGS):
            for key, args in settings_dict.items():
                kwargs = args.get('form_kwargs', {})
                if callable(kwargs):
                    kwargs = kwargs(self.event)
                kwargs.setdefault('required', False)
                self.fields[key] = args['form_class'](**kwargs)
                if isinstance(self.fields[key], I18nFormField):
                    self.fields[key].widget.enabled_locales = self.locales
                if 'placeholders' in args:
                    # From src/pretix/control/forms/event.py
                    phs = [
                        '{%s}' % p
                        for p in sorted(get_available_placeholders(self.event, args['placeholders']).keys())
                    ]
                    ht = _('Available placeholders: {list}').format(list=', '.join(phs))
                    if self.fields[key].help_text:
                        self.fields[key].help_text += ' ' + str(ht)
                    else:
                        self.fields[key].help_text = ht
                    self.fields[key].validators.append(
                        PlaceholderValidator(phs)
                    )

    def clean(self):
        data = self.cleaned_data
        for settings_dict in (COMMON_SETTINGS, ITEM_GROUP_SETTINGS, BUY_SETTINGS, SELL_SETTINGS):
            for key, args in settings_dict.items():
                if 'clean' in args:
                    data[key] = args['clean'](data[key])

        # Check for overlapping items
        buy_items = []
        sell_items = []
        for index, suffix in enumerate(ITEM_GROUP_SUFFIXES):
            buy_items += list(data['ticketbourse_buy_items'+suffix])
            sell_items += list(data['ticketbourse_sell_items'+suffix])
        if any(buy_items.count(item) > 1 for item in buy_items):
            for suffix in ITEM_GROUP_SUFFIXES:
                self.add_error('ticketbourse_buy_items'+suffix, _('Products must not overlap between groups.'))
        if any(sell_items.count(item) > 1 for item in sell_items):
            for suffix in ITEM_GROUP_SUFFIXES:
                self.add_error('ticketbourse_sell_items'+suffix, _('Products must not overlap between groups.'))

        return data
