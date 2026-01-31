#
# This file is part of pretix (Community Edition).
#
# Copyright (C) 2014-2020  Raphael Michel and contributors
# Copyright (C) 2020-today pretix GmbH and contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation in version 3 of the License.
#
# ADDITIONAL TERMS APPLY: Pursuant to Section 7 of the GNU Affero General Public License, additional terms are
# applicable granting you additional permissions and placing additional restrictions on your usage of this software.
# Please refer to the pretix LICENSE file to obtain the full terms applicable to this work. If you did not receive
# this file, see <https://pretix.eu/about/en/license>.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
#

"""
This file contains settings that we need at wheel require time. All settings that we only need at runtime are set
in settings.py.
"""
import configparser

from ._base_settings import *  # NOQA
from .helpers.config import EnvOrParserConfig

ENTROPY = {
    'order_code': 5,
    'customer_identifier': 7,
    'ticket_secret': 32,
    'voucher_code': 16,
    'giftcard_secret': 12,
}

MAIL_FROM_ORGANIZERS = 'invalid@invalid'
FILE_UPLOAD_MAX_SIZE_EMAIL_AUTO_ATTACHMENT = 10
FILE_UPLOAD_MAX_SIZE_EMAIL_ATTACHMENT = 10
FILE_UPLOAD_MAX_SIZE_IMAGE = 10
FILE_UPLOAD_MAX_SIZE_FAVICON = 10
DEFAULT_CURRENCY = 'EUR'
SECRET_KEY = "build-time-secret-key"
HAS_REDIS = False
STATIC_URL = '/static/'
HAS_MEMCACHED = False
HAS_CELERY = False
HAS_GEOIP = False
SENTRY_ENABLED = False

# for production
_config = configparser.RawConfigParser()
config = EnvOrParserConfig(_config)

AWS_ACCESS_KEY_ID = config.get('aws', 'access_key_id', fallback='')
AWS_SECRET_ACCESS_KEY = config.get('aws', 'secret_access_key', fallback='')
AWS_QUERYSTRING_AUTH = False
AWS_DEFAULT_ACL = 'public-read'
AWS_S3_ENDPOINT_URL = config.get('aws', 's3_endpoint_url', fallback='')
AWS_S3_OBJECT_PARAMETERS = {
    'CacheControl': 'max-age=86400'
}

if AWS_S3_ENDPOINT_URL:
    AWS_STATIC_LOCATION = 'static'
    STATIC_URL = f'{AWS_S3_ENDPOINT_URL}/{AWS_STATIC_LOCATION}/'
    COMPRESS_URL = STATIC_URL

    AWS_MEDIA_LOCATION = 'media'
    PUBLIC_MEDIA_LOCATION = 'media'
    MEDIA_URL = f'{AWS_S3_ENDPOINT_URL}/{AWS_MEDIA_LOCATION}/'

    STORAGES = {
        "default": {
            "BACKEND": "pretix.storage_backends.MediaStorage",
        },
        "staticfiles": {
            "BACKEND": "pretix.storage_backends.CachedS3BotoStorage",
        },
        "compressor": {
            "BACKEND": "pretix.storage_backends.CachedS3BotoStorage",
        },
    }
