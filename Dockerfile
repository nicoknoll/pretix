# Use an official Python runtime as a parent image
FROM python:3.9

# Set environment variables
ENV PYTHONUNBUFFERED 1
ENV DJANGO_SETTINGS_MODULE pretix.settings

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
    make

# Install Node.js 20.x
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs

# Set work directory
WORKDIR /app

# Copy the current directory contents into the container
COPY . /app/

# Install Python dependencies and Gunicorn
RUN pip3 install -e ".[dev]" gunicorn

# Change to src directory as per documentation
WORKDIR /app/src

# Install JavaScript dependencies
RUN make npminstall

# Compile language files
RUN make localecompile

# compress assets
RUN python manage.py compress --force

# Collect static files
RUN python manage.py collectstatic --noinput --no-post-process

# Set work directory
WORKDIR /app

# Copy the current directory contents into the container
COPY . /app/

# Change to src directory as per documentation
WORKDIR /app/src

# Start the application with database migration and Gunicorn
CMD ["sh", "-c", "python manage.py migrate && gunicorn pretix.wsgi"]
