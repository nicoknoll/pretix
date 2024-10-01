FROM python:3.11-bookworm

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
            build-essential \
            gettext \
            git \
            libffi-dev \
            libjpeg-dev \
            libmemcached-dev \
            libpq-dev \
            libssl-dev \
            libxml2-dev \
            libxslt1-dev \
            locales \
            python3-virtualenv \
            python3-dev \
            libmaxminddb0 \
            libmaxminddb-dev \
            zlib1g-dev \
            nodejs  \
            npm && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    dpkg-reconfigure locales &&  \
    locale-gen C.UTF-8 &&  \
    /usr/sbin/update-locale LANG=C.UTF-8 && \
    mkdir /etc/pretix && \
    mkdir /data && \
    useradd -ms /bin/bash -d /pretix -u 15371 pretixuser && \
    mkdir /static

ENV LC_ALL=C.UTF-8 \
    DJANGO_SETTINGS_MODULE=production_settings

# Copy necessary files
COPY deployment/docker/pretix.bash /usr/local/bin/pretix
COPY deployment/docker/production_settings.py /pretix/src/production_settings.py
COPY pyproject.toml /pretix/pyproject.toml
COPY *build /pretix/*build
COPY src /pretix/src

# Install Python dependencies
RUN pip3 install -U \
        pip \
        setuptools \
        wheel && \
    cd /pretix && \
    PRETIX_DOCKER_BUILD=TRUE pip3 install \
        -e ".[memcached]" \
        gunicorn django-extensions ipython && \
    rm -rf ~/.cache/pip

# Set up Pretix
RUN chmod +x /usr/local/bin/pretix && \
    cd /pretix/src && \
    rm -f pretix.cfg &&  \
    mkdir -p data && \
    chown -R pretixuser:pretixuser /pretix /data data &&  \
    su pretixuser -c "cd /pretix/src && make production"

USER pretixuser

EXPOSE 8000

CMD ["gunicorn", "pretix.wsgi", "--bind", "0.0.0.0:8000"]
