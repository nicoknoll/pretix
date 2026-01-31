from django.utils.translation import gettext_lazy
from . import __version__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    name = "pretix.plugins.pretix_ticket_transfer"
    verbose_name = "Ticket transfer"

    class PretixPluginMeta:
        name = gettext_lazy("Ticket transfer")
        author = "alice"
        description = gettext_lazy("Allow ticket transfer to a new order")
        visible = True
        version = __version__
        category = "FEATURE"
        compatibility = "pretix>=2.7.0"

    def ready(self):
        from . import signals  # NOQA
