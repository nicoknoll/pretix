# Writing Custom Pretix Plugins

---

## Plugin discovery

Pretix finds plugins through two mechanisms:

### Bundled plugins

Hardcoded in `_base_settings.py` → `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    # ...
    'pretix.plugins.banktransfer',
    'pretix.plugins.stripe',
    'pretix.plugins.my_plugin',
]
```

### External plugins

Discovered at startup via entry points in `settings.py`:

```python
PLUGINS = []
for entry_point in metadata.entry_points(group='pretix.plugin'):
    if entry_point.module in PRETIX_PLUGINS_EXCLUDE:
        continue
    PLUGINS.append(entry_point.module)
    INSTALLED_APPS.append(entry_point.module)
```

External plugins declare their entry point in `pyproject.toml`:

```toml
[project.entry-points."pretix.plugin"]
my_plugin = "my_plugin:PluginApp"
```

### Config knobs

In `pretix.cfg`:
- `plugins_default` — comma-separated list of plugins enabled by default for new events
- `plugins_organizer_default` — same but for organizer-level plugins
- `plugins_exclude` — comma-separated list of plugin modules to block from loading

```python
PRETIX_PLUGINS_DEFAULT = config.get('pretix', 'plugins_default',
    fallback='pretix.plugins.sendmail,pretix.plugins.statistics,pretix.plugins.checkinlists')
PRETIX_PLUGINS_EXCLUDE = config.get('pretix', 'plugins_exclude', fallback='').split(',')
```

---

## Plugin structure

```
pretix/plugins/my_plugin/
├── __init__.py          # version string
├── apps.py              # PluginConfig + PretixPluginMeta
├── signals.py           # all signal receivers; imported in apps.ready()
├── urls.py              # URL patterns (auto-discovered)
├── views.py             # Django views
├── forms.py             # SettingsForm subclass
├── models.py            # custom models (if needed)
├── tasks.py             # Celery tasks (if needed)
├── locale/
│   └── de/LC_MESSAGES/django.po
├── static/my_plugin/
│   └── style.css
└── templates/my_plugin/
    └── control_settings.html
```

---

## apps.py

```python
from pretix.base.plugins import PluginConfig
from django.utils.translation import gettext_lazy

class PluginApp(PluginConfig):
    default = True
    name = "pretix.plugins.my_plugin"   # dotted path
    verbose_name = "My Plugin"

    class PretixPluginMeta:
        name = gettext_lazy("My Plugin")
        author = "..."
        description = gettext_lazy("...")
        visible = True
        version = "1.0.0"
        category = "CUSTOMIZATION"   # or PAYMENT, FEATURE, INTEGRATION, …
        compatibility = "pretix>=2.7.0"

    def ready(self):
        from . import signals  # NOQA — registers all receivers
```

`app.label` defaults to the last component of `name` (e.g. `my_plugin`).
This becomes the URL namespace: `plugins:my_plugin`.

---

## URL auto-discovery

`pretix/multidomain/maindomain_urlconf.py` scans every app with `PretixPluginMeta` for a `urls` module.  It registers two kinds of patterns:

| Variable in `urls.py` | Where it lands |
|-----------------------|----------------|
| `urlpatterns` | top-level; use for control-panel routes (`^control/event/…`) |
| `event_patterns` | prefixed with `^<organizer>/<event>/`; use for presale routes |

```python
# urls.py
from django.urls import re_path
from pretix.multidomain import event_url
from .views import MySettingsView, MyPresaleView

event_patterns = [
    event_url(r'^my_plugin/info$', MyPresaleView.as_view(), name='info'),
]

urlpatterns = [
    re_path(
        r'^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/my_plugin/settings$',
        MySettingsView.as_view(),
        name='settings',
    ),
]
```

URL name resolution: `plugins:my_plugin:settings`, `plugins:my_plugin:info`, etc.

---

## Control-panel settings page

Inherit from both mixins — order matters:

```python
# views.py
from django.urls import reverse
from pretix.control.views.event import EventSettingsFormView, EventSettingsViewMixin
from .forms import MySettingsForm

class MySettingsView(EventSettingsViewMixin, EventSettingsFormView):
    form_class = MySettingsForm
    template_name = 'my_plugin/control_settings.html'

    def get_success_url(self):
        return reverse('plugins:my_plugin:settings', kwargs={
            'organizer': self.request.organizer.slug,
            'event': self.request.event.slug,
        })
```

```python
# forms.py
from django import forms
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SettingsForm

class MySettingsForm(SettingsForm):
    my_plugin_foo = forms.CharField(required=False, label=_('Foo'))
```

`SettingsForm` reads/writes to `event.settings` (key-value store backed by an existing DB table). **No migrations needed.**  Field names become the settings keys.

```html
{# templates/my_plugin/control_settings.html #}
{% extends "pretixcontrol/event/settings_base.html" %}
{% load i18n bootstrap3 %}
{% block title %}{% trans "My Plugin" %}{% endblock %}
{% block inside %}
<form action="" method="post" class="form-horizontal">
    {% csrf_token %}
    <fieldset>
        {% bootstrap_form form layout="horizontal" %}
    </fieldset>
    <div class="form-group submit-group">
        <button type="submit" class="btn btn-primary btn-save">{% trans "Save" %}</button>
    </div>
</form>
{% endblock %}
```

---

## Adding a tab to the event-settings sidebar

```python
from pretix.control.signals import nav_event_settings
from django.dispatch import receiver
from django.urls import resolve, reverse
from django.utils.translation import gettext_lazy as _

@receiver(nav_event_settings, dispatch_uid='my_plugin_nav_settings')
def navbar_settings(sender, request, **kwargs):
    url = resolve(request.path_info)
    return [{
        'label': _('My Plugin'),
        'url': reverse('plugins:my_plugin:settings', kwargs={
            'event': request.event.slug,
            'organizer': request.organizer.slug,
        }),
        'active': url.namespace == 'plugins:my_plugin' and url.url_name == 'settings',
    }]
```

---

## Presale signals

`html_head` and `html_footer` are `EventPluginSignal`s — they only fire for events that have the plugin enabled.  `sender` is the `Event` object.

```python
from pretix.presale.signals import html_footer, html_head

@receiver(html_head, dispatch_uid="my_plugin_html_head")
def r_html_head(sender, request=None, **kwargs):
    ...
    return '<link rel="stylesheet" href="...">'

@receiver(html_footer, dispatch_uid="my_plugin_html_footer")
def r_html_footer(sender, request=None, **kwargs):
    ...
    return '<script src="..."></script>'
```

Reading per-event settings in a signal:
```python
value = sender.settings.get('my_plugin_foo', default='')
```

---

## Checkout flow steps

Add custom pages to the checkout process using the `checkout_flow_steps` signal and `TemplateFlowStep`:

```python
from pretix.presale.checkoutflow import CartMixin, TemplateFlowStep
from pretix.presale.signals import checkout_flow_steps

class MyFlowStep(CartMixin, TemplateFlowStep):
    priority = 180                  # controls ordering among steps
    identifier = 'my_plugin_step'
    template_name = 'my_plugin/checkout_step.html'
    icon = 'cog'
    label = _('My Step')

    def is_applicable(self, request):
        """Return True if this step should appear in the checkout."""
        return True

    def is_completed(self, request, warn=False):
        """Return True if the user can proceed past this step."""
        return self.cart_session.get('my_plugin_choice', '') != ''

    def post(self, request):
        """Handle form submission. Store data in cart_session, then redirect."""
        self.request = request
        self.cart_session['my_plugin_choice'] = request.POST.get('choice', '')
        return redirect(self.get_next_url(request))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['selected'] = self.cart_session.get('my_plugin_choice', '')
        return ctx

@receiver(signal=checkout_flow_steps, dispatch_uid='my_plugin_checkout_flow_step')
def checkout_flow_step(sender, **kwargs):
    return MyFlowStep
```

`cart_session` is a dict-like object that stores data across the checkout flow — it persists until the order is placed.

---

## Order lifecycle signals

These are `EventPluginSignal`s defined in `pretix.base.signals`:

| Signal | Fired when |
|--------|-----------|
| `order_placed` | Order is created (args: `order`, `bulk`) |
| `order_paid` | Order is marked as paid (args: `order`) |
| `order_canceled` | Order is canceled (args: `order`) |
| `order_changed` | Order content is modified (args: `order`) |
| `order_expired` | Order expires (args: `order`) |

### Storing data during checkout

Use `order_meta_from_request` to inject data into the order's `meta_info` JSON field during creation:

```python
from pretix.presale.signals import order_meta_from_request

@receiver(order_meta_from_request, dispatch_uid='my_plugin_order_meta')
def order_meta(sender, request, **kwargs):
    cs = cart_session(request)
    choice = cs.get('my_plugin_choice', '')
    if not choice:
        return {}
    return {'my_plugin_choice': choice}
```

Then consume it when the order is placed:

```python
from pretix.base.signals import order_placed

@receiver(order_placed, dispatch_uid='my_plugin_order_placed')
def on_order_placed(sender, order, **kwargs):
    meta = order.meta_info_data
    if not meta or 'my_plugin_choice' not in meta:
        return
    # Do something with meta['my_plugin_choice']
    # Clean up meta_info if the data was transient
    del meta['my_plugin_choice']
    order.meta_info = json.dumps(meta)
    order.save(update_fields=['meta_info'])
```

### Showing content on the checkout confirmation page

Use `checkout_confirm_page_content` to render HTML on the last page before order creation:

```python
from pretix.presale.signals import checkout_confirm_page_content

@receiver(signal=checkout_confirm_page_content, dispatch_uid='my_plugin_checkout_confirm')
def checkout_confirm(sender, request, **kwargs):
    cs = cart_session(request)
    if not cs.get('my_plugin_choice', ''):
        return
    template = get_template('my_plugin/checkout_confirm.html')
    return template.render({
        'request': request,
        'event': request.event,
        'choice': cs.get('my_plugin_choice', ''),
    })
```

---

## Order info display

Show plugin information on order detail pages. There are **two separate signals** — one for the presale frontend and one for the control panel:

### Presale order detail

```python
from pretix.presale.signals import order_info

@receiver(order_info, dispatch_uid='my_plugin_order_info')
def presale_order_info(sender, order, **kwargs):
    data = get_my_plugin_data(order)
    if not data:
        return False
    template = get_template('my_plugin/presale/order_info.html')
    return template.render({
        'order': order,
        'event': sender,
        'data': data,
    })
```

### Control panel order detail

```python
from pretix.control.signals import order_info as order_info_control

@receiver(order_info_control, dispatch_uid='my_plugin_order_info_control')
def control_order_info(sender, order, request, **kwargs):
    data = get_my_plugin_data(order)
    if not data:
        return False
    template = get_template('my_plugin/control/order_info.html')
    return template.render({
        'order': order,
        'event': sender,
        'data': data,
    }, request=request)
```

Note: the control-panel variant receives `request` as an argument and should pass it to `template.render()`.

---

## Content Security Policy (CSP)

Pretix's `SecurityMiddleware` enforces a strict CSP on every response:
- `style-src 'self'` — **inline `<style>` tags are blocked**
- `script-src 'self'` — **inline `<script>` tags are blocked**

### Wrong (blocked by CSP):
```python
return '<style>{}</style>'.format(css)
return '<script>{}</script>'.format(js)
```

### Right — serve content as an external file:

Add presale views that return the stored CSS/JS with the correct `Content-Type`:

```python
from django.http import HttpResponse
from django.views.generic import View

class MyCssView(View):
    def get(self, request, *args, **kwargs):
        css = request.event.settings.get('my_plugin_css', default='')
        return HttpResponse(css or '', content_type='text/css; charset=utf-8')
```

Register via `event_patterns` so the file is served from `'self'`, then inject a `<link>` tag using `eventreverse` (handles custom domains):

```python
from pretix.multidomain.urlreverse import eventreverse

@receiver(html_head, dispatch_uid="my_plugin_html_head")
def r_html_head(sender, request=None, **kwargs):
    css = sender.settings.get('my_plugin_css', default='')
    if css and css.strip():
        url = eventreverse(sender, 'plugins:my_plugin:custom.css')
        return '<link rel="stylesheet" type="text/css" href="{}">'.format(url)
    return ''
```

`eventreverse` is the right tool for presale URLs: it respects per-event custom domains, unlike a plain `reverse()`.

---

## Static files

For assets that don't change per-event, use Django's static files system:

```
my_plugin/
└── static/my_plugin/
    ├── style.css
    └── script.js
```

In templates:
```html
{% load static %}
<link rel="stylesheet" href="{% static 'my_plugin/style.css' %}">
<script src="{% static 'my_plugin/script.js' %}"></script>
```

Pretix uses `ManifestStaticFilesStorage`, which automatically appends content hashes to filenames during `collectstatic` — cache-busting is handled for you.

For **per-event dynamic content** (e.g. CSS/JS from settings), serve it via a view instead (see CSP section above).

---

## Dynamic content with cache-busting

When serving dynamic content from a view (e.g. per-event CSS), the URL stays the same so browsers cache stale content. To fix this, hash the content and append it as a query parameter:

```python
import hashlib

@receiver(html_head, dispatch_uid="my_plugin_html_head")
def r_html_head(sender, request=None, **kwargs):
    css = sender.settings.get('my_plugin_css', default='')
    if css and css.strip():
        url = eventreverse(sender, 'plugins:my_plugin:custom.css')
        content_hash = hashlib.md5(css.encode()).hexdigest()[:8]
        return '<link rel="stylesheet" type="text/css" href="{}?v={}">'.format(url, content_hash)
    return ''
```

In the view, when a `v` parameter is present, serve with long cache headers:

```python
class MyCssView(View):
    def get(self, request, *args, **kwargs):
        css = request.event.settings.get('my_plugin_css', default='')
        resp = HttpResponse(css or '', content_type='text/css; charset=utf-8')
        if request.GET.get('v'):
            resp['Cache-Control'] = 'public, max-age=31536000'
        return resp
```

Content changes → hash changes → URL changes → browser fetches fresh.

---

## Models & migrations

When `event.settings` isn't enough (relations, queries, state machines), add custom models:

```python
# models.py
from django.db import models
from django.utils.translation import gettext_lazy as _

class MyRequest(models.Model):
    order = models.ForeignKey(
        'pretixbase.Order',
        on_delete=models.CASCADE,
        related_name='my_plugin_requests',
    )
    event = models.ForeignKey(
        'pretixbase.Event',
        on_delete=models.CASCADE,
        related_name='my_plugin_requests',
    )

    class States(models.TextChoices):
        PENDING = 'PENDING', _('Pending')
        COMPLETED = 'COMPLETED', _('Completed')

    state = models.CharField(max_length=32, choices=States.choices)
    created = models.DateTimeField(auto_now_add=True)
```

### ForeignKey targets

Use string references to pretix core models:
- `'pretixbase.Event'`
- `'pretixbase.Order'`
- `'pretixbase.OrderPosition'`
- `'pretixbase.Organizer'`

### Migrations

Standard Django migrations in the plugin's own `migrations/` directory:

```bash
python -m django makemigrations my_plugin
```

Migrations live inside the plugin and only run when the plugin is installed.

### django_scopes

Pretix uses `django_scopes` to prevent cross-event data leaks. In signal handlers that run within a request, the scope is already set. In tasks and periodic handlers, you must disable scopes explicitly:

```python
from django_scopes import scopes_disabled

with scopes_disabled():
    MyRequest.objects.filter(event=event, state='PENDING')
```

---

## Celery tasks

For heavy work triggered by signals, offload to Celery tasks:

```python
# tasks.py
from django_scopes import scopes_disabled
from pretix.base.services.tasks import EventTask
from pretix.celery_app import app

@app.task(base=EventTask)
@scopes_disabled()
def process_my_request(event, **kwargs):
    """EventTask injects the Event object from event_id kwarg."""
    # Heavy processing here
    ...
```

Trigger from a signal:

```python
from .tasks import process_my_request

@receiver(order_placed, dispatch_uid='my_plugin_order_placed')
def on_order_placed(sender, order, **kwargs):
    process_my_request.apply_async(kwargs={'event_id': sender.pk})
```

`EventTask` deserializes `event_id` into the `event` argument. The `@scopes_disabled()` decorator is required because tasks run outside a request context.

---

## Plugin lifecycle hooks

`PluginConfig` supports two optional lifecycle methods called when a plugin is enabled/disabled on an event (or organizer):

```python
class PluginApp(PluginConfig):
    # ...

    def installed(self, event):
        """Called when the plugin is enabled. Use for setup (e.g. creating default layouts)."""
        pass

    def uninstalled(self, event):
        """Called when the plugin is disabled. Use for cleanup."""
        pass
```

The argument is the `Event` (or `Organizer`) object. These are called from `event.enable_plugin()` / `event.disable_plugin()`.

### Settings cleanup on deactivation

Hierarkey settings persist in the DB across plugin deactivation/reactivation. If you don't clean them up, stale values override fresh defaults when the plugin is re-enabled. This is especially problematic for `LazyI18nString.from_gettext()` defaults — if settings were saved when translations were broken, the broken values stick around forever.

```python
def uninstalled(self, event):
    from .settings import MY_SETTINGS
    for key in MY_SETTINGS:
        event.settings.delete(key)
```

### Existing examples

- `pretix/plugins/ticketoutputpdf/apps.py` — creates default `TicketLayout` in `installed()`
- `pretix/plugins/badges/apps.py` — creates default `BadgeLayout` in `installed()`
- `pretix/plugins/pretix_ticketbourse/apps.py` — deletes all settings in `uninstalled()`

---

## I18n for plugin settings

### Translatable default values

Use `LazyI18nString.from_gettext()` with `gettext_noop()` to define defaults that resolve translations at runtime:

```python
from django.utils.translation import gettext_noop
from i18nfield.strings import LazyI18nString

'default': LazyI18nString.from_gettext(gettext_noop('Some translatable text'))
```

`gettext_noop()` marks the string for extraction by `makemessages` without translating it at import time. `from_gettext()` looks up translations for each enabled locale when the default is actually used.

**Important:** These defaults only apply when no value is stored in the DB. Once the settings form is saved, the DB values take over. If translations were missing at that point, English-only values get persisted — hence the need for settings cleanup on deactivation (see above).

### Restricting I18n fields to enabled locales

By default, `I18nFormField` shows input fields for all installed languages. To show only the event's enabled locales:

```python
from i18nfield.forms import I18nFormField

class MySettingsForm(SettingsForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # self.locales is set by SettingsForm from event.settings.locales
        for field in self.fields.values():
            if isinstance(field, I18nFormField):
                field.widget.enabled_locales = self.locales
```

### Translation workflow

1. `python -m django makemessages -l de -l de_Informal` — extract strings, update `.po` files
2. Translate the `.po` files (check for fuzzy matches after renames — `makemessages` often guesses wrong)
3. `python -m django compilemessages` — compile `.po` → `.mo`
4. Restart the server — `.mo` files are cached at startup

Formal German (`de`) uses Sie/Ihr, informal (`de_Informal`) uses du/dein.

---

## Organizer-level plugins

Plugins that operate on an organizer (not per-event) need an extra meta attribute:

```python
from pretix.base.plugins import PluginConfig, PLUGIN_LEVEL_ORGANIZER

class PretixPluginMeta:
    level = PLUGIN_LEVEL_ORGANIZER
```

Without this, pretix treats the plugin as per-event. The organizer nav items won't appear
until the plugin is enabled for a specific event — which is confusing for organizer-wide
features like bank account data imports.

### Organizer-level views

Use `OrganizerDetailViewMixin` + `OrganizerPermissionRequiredMixin` instead of the event
equivalents:

```python
from pretix.control.permissions import OrganizerPermissionRequiredMixin
from pretix.control.views.organizer import OrganizerDetailViewMixin

class MyOrganizerView(OrganizerDetailViewMixin, OrganizerPermissionRequiredMixin, FormView):
    permission = 'can_change_organizer_settings'
    template_name = 'my_plugin/organizer_settings.html'

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['obj'] = self.request.organizer  # SettingsForm reads/writes organizer.settings
        return kwargs
```

Template extends the organizer base:
```html
{% extends "pretixcontrol/organizers/base.html" %}
```

### Organizer nav signal

Return a top-level item with `children`. Do **not** use the `parent` key to nest under
another plugin's nav item — users won't find it.

```python
from pretix.control.signals import nav_organizer

@receiver(nav_organizer, dispatch_uid="my_plugin_organav")
def control_nav_orga(sender, request=None, **kwargs):
    url = resolve(request.path_info)
    if not request.user.has_organizer_permission(request.organizer, 'can_change_orders', request=request):
        return []
    return [{
        'label': _('My Plugin'),
        'url': reverse('plugins:my_plugin:status', kwargs={'organizer': request.organizer.slug}),
        'icon': 'university',
        'active': url.namespace == 'plugins:my_plugin',
        'children': [
            {
                'label': _('Overview'),
                'url': reverse('plugins:my_plugin:status', kwargs={'organizer': request.organizer.slug}),
                'active': url.namespace == 'plugins:my_plugin' and url.url_name == 'status',
                'icon': 'download',
            },
            {
                'label': _('Settings'),
                'url': reverse('plugins:my_plugin:settings', kwargs={'organizer': request.organizer.slug}),
                'active': url.namespace == 'plugins:my_plugin' and url.url_name == 'settings',
                'icon': 'wrench',
            },
        ],
    }]
```

### How organizer nav is rendered

`context.py` → `get_organizer_navigation()` → `nav_organizer.send()` + `merge_in()` → `ctx['nav_items']`.

The `merge_in()` function handles `parent` keys: it looks for an existing item in `nav`
whose `url` matches `item['parent']` and nests the child under it. Items with `parent`
are sorted to the end so their parent is already in the list when processed. But this
approach requires the parent item to exist (i.e., the other plugin must also be active).

### Organizer-level URL patterns

Use `urlpatterns` (not `event_patterns`):

```python
urlpatterns = [
    re_path(r'^control/organizer/(?P<organizer>[^/]+)/my_plugin/$',
            views.MyStatusView.as_view(), name='status'),
    re_path(r'^control/organizer/(?P<organizer>[^/]+)/my_plugin/settings/$',
            views.MySettingsView.as_view(), name='settings'),
]
```

---

## Periodic tasks

Use `periodic_task` signal + `minimum_interval` decorator — **not** Celery Beat directly:

```python
from pretix.base.signals import periodic_task
from pretix.helpers.periodic import minimum_interval

@receiver(periodic_task, dispatch_uid='my_plugin_periodic')
@minimum_interval(minutes_after_success=60, minutes_after_error=5)
def periodic_handler(sender, **kwargs):
    from django_scopes import scopes_disabled
    with scopes_disabled():
        ...
        my_task.apply_async(kwargs={...})
```

`minimum_interval` uses cache (Redis) as a distributed lock/rate-limiter.

---

## Reusing the banktransfer pipeline

To create an import job and process it through the existing banktransfer matching logic:

```python
from pretix.plugins.banktransfer.models import BankImportJob
from pretix.plugins.banktransfer.tasks import process_banktransfers

job = BankImportJob.objects.create(organizer=organizer, currency='EUR')
process_banktransfers.apply_async(kwargs={
    'job': job.pk,
    'data': [
        {
            'amount': '49.00',
            'reference': 'MYEVENT-ABC12',
            'payer': 'Max Mustermann',
            'date': '2026-03-14',
            'external_id': 'unique-tx-id-from-bank',
        },
        ...
    ],
})
```

The pipeline handles deduplication (via checksum + `external_id`), order code matching,
and payment confirmation automatically.

---

## External (standalone) plugin packaging

External plugins live outside the pretix source tree and are installed as separate Python packages.

### Key differences from bundled plugins

| | Bundled | External |
|---|---------|----------|
| Location | `pretix/plugins/my_plugin/` | `my_plugin/` (separate repo) |
| Module path | `pretix.plugins.my_plugin` | `my_plugin` |
| Discovery | Hardcoded in `INSTALLED_APPS` | Entry point in `pyproject.toml` |
| Installation | Comes with pretix | `pip install pretix-my-plugin` |

### pyproject.toml

```toml
[project]
name = "pretix-my-plugin"
version = "1.0.0"
dependencies = ["pretix>=2.7.0"]

[project.entry-points."pretix.plugin"]
my_plugin = "my_plugin:PluginApp"
```

### apps.py

The `name` changes to match the standalone module path:

```python
class PluginApp(PluginConfig):
    name = "my_plugin"              # not pretix.plugins.my_plugin
    verbose_name = "My Plugin"
```

---

## Reference plugins

### Event-level, settings + presale
- `pretix/plugins/pretix_ticket_transfer/` — settings view, presale views, nav signal, `event_patterns`
- `pretix/plugins/pretix_custom_css_js/` — dynamic CSS/JS injection, presale signals, CSP-compliant content serving

### Event-level, checkout flow + order lifecycle
- `pretix/plugins/pretix_ticketbourse/` — checkout flow step, `order_meta_from_request`, `order_placed`, order info display, Celery tasks, models with migrations, periodic tasks

### Organizer-level, payment integration
- `pretix/plugins/pretix_gocardless/` — `PLUGIN_LEVEL_ORGANIZER`, organizer views, banktransfer pipeline reuse, periodic task, mock client

### Built-in plugins (good for patterns)
- `pretix/plugins/ticketoutputpdf/` — `installed()` lifecycle hook, ticket rendering
- `pretix/plugins/badges/` — `installed()` lifecycle hook, badge layout creation
- `pretix/plugins/banktransfer/` — import pipeline, task processing, organizer-level features