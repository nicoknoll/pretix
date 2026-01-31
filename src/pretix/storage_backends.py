from django.core.files.storage import storages
from storages.backends.s3boto3 import S3Boto3Storage


class StaticStorage(S3Boto3Storage):
    bucket_name = 'static'
    location = ''
    default_acl = 'public-read'


class CachedS3BotoStorage(S3Boto3Storage):
    bucket_name = 'static'
    location = ''
    default_acl = 'public-read'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.local_storage = storages.create_storage({
            "BACKEND": "compressor.storage.CompressorFileStorage"
        })

    def save(self, name, content):
        self.local_storage.save(name, content)
        super().save(name, self.local_storage._open(name))
        return name


class MediaStorage(S3Boto3Storage):
    bucket_name = 'media'
