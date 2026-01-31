from pretix.base.templatetags.rich_text import rich_text
from pretix.presale.signals import checkout_confirm_messages


def get_confirm_messages(event):
    msgs = {}
    for index, text in enumerate(event.settings.pretix_ticket_transfer_confirm_texts):
        msgs['ticket_transfer_confirm_text_%i' % index] = rich_text(str(text))
    return msgs


def transfer_needs_accept(event):
    return bool(get_confirm_messages(event))
