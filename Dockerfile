# Use an official Python runtime as a parent image
FROM python:3.11-bookworm

# Set environment variables
ENV PYTHONUNBUFFERED 1

ARG PRETIX_DJANGO_SECRET
ARG PRETIX_AWS_ACCESS_KEY_ID
ARG PRETIX_AWS_S3_ENDPOINT_URL
ARG PRETIX_AWS_SECRET_ACCESS_KEY
ARG DEPLOYMENT_TYPE
ARG NUM_THREADS
ARG NUM_WORKERS

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3-pip \
    python3-dev \
    python3-venv \
    libffi-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    libenchant-2-2 \
    gettext \
    git \
    make \
    cron \
    build-essential

# Install Node.js 20.x
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs

# Set work directory
WORKDIR /app

# Copy the current directory contents into the container
COPY . /app/


# Update pip first, BEFORE any pip install commands
RUN pip3 install --upgrade pip wheel setuptools

# Install Python dependencies and Gunicorn
RUN pip3 install -e ".[dev]" gunicorn gevent

# Change to src directory as per documentation
WORKDIR /app/src

# Install JavaScript dependencies
RUN make npminstall

# Compile language files
RUN make localecompile

# compress assets
RUN python manage.py compress --force

# Collect static files (only for main deployment)
RUN if [ "$DEPLOYMENT_TYPE" = "main" ]; then \
        python manage.py collectstatic --noinput --no-post-process; \
    else \
        echo "Skipping collectstatic for non-main deployment"; \
    fi

# Set work directory
WORKDIR /app

# Copy the current directory contents into the container
COPY . /app/
COPY ./run.sh /app/src

# Change to src directory as per documentation
WORKDIR /app/src

# make our entrypoint.sh executable
RUN chmod +x ./run.sh

CMD ["./run.sh"]
