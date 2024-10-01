# Use an official Python runtime as a parent image
FROM python:3.9

# Set environment variables
ENV PYTHONUNBUFFERED 1
ENV DJANGO_SETTINGS_MODULE pretix.settings
ENV POETRY_VERSION=1.4.2

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    gettext \
    git \
    libffi-dev \
    libjpeg-dev \
    libpq-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    python3-dev \
    zlib1g-dev

# Install Poetry
RUN pip install "poetry==$POETRY_VERSION"

# Copy only pyproject.toml and poetry.lock (if it exists)
COPY pyproject.toml poetry.lock* /app/

# Install project dependencies
RUN poetry config virtualenvs.create false \
  && poetry install --no-interaction --no-ansi

# Copy the current directory contents into the container at /app
COPY . /app/

# Run database migrations
RUN python manage.py migrate

# Collect static files
RUN python manage.py collectstatic --noinput

# Make port 8000 available to the world outside this container
EXPOSE 8000

# Run the application
CMD ["gunicorn", "pretix.wsgi:application", "--bind", "0.0.0.0:8000"]
