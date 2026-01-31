from django.utils.translation import gettext_lazy
from . import __version__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    default = True
    name = "pretix.plugins.pretix_ticketbourse"
    verbose_name = "Ticket Bourse"

    class PretixPluginMeta:
        name = gettext_lazy("Ticket Bourse")
        author = "Julian Rother"
        description = gettext_lazy("Allow customers to resell their tickets")
        visible = True
        version = __version__
        category = "FEATURE"
        compatibility = "pretix>=2.7.0"

    def ready(self):
        from . import signals  # NOQA
