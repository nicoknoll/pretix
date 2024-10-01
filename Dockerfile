# Use an official Python 3.9 runtime as a parent image
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
    git

# Install Node.js 17.x
RUN curl -sL https://deb.nodesource.com/setup_17.x | bash - \
    && apt-get install -y nodejs

# Set work directory
WORKDIR /app

# Copy the current directory contents into the container
COPY . /app/

# Create and activate virtual environment
RUN python3 -m venv env
ENV PATH="/app/env/bin:$PATH"

# Upgrade pip and setuptools
RUN pip3 install -U pip setuptools

# Install Python dependencies
RUN pip3 install -e ".[dev]"

# Install Gunicorn
RUN pip3 install gunicorn

# Install npm dependencies
RUN cd src/ && npm install

# Collect static files
RUN cd src/ && python manage.py collectstatic --noinput

# Compile language files
RUN cd src/ && make localecompile

# Run database migrations
RUN cd src/ && python manage.py migrate

# Expose port 8000 for Gunicorn
EXPOSE 8000

# Set the working directory to /app/src where manage.py is located
WORKDIR /app/src

# Start Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "pretix.wsgi:application"]
