#!/bin/bash

NUM_WORKERS_DEFAULT=$((2 * 1))   # $(nproc) = number of vcpus
export NUM_WORKERS=${NUM_WORKERS:-$NUM_WORKERS_DEFAULT}


# Check deployment type and run appropriate command
if [ "${DEPLOYMENT_TYPE}" = "main" ]; then

    # Start cron service
    service cron start
    
    # Create cron job if it doesn't exist
    if [ ! -f /etc/cron.d/pretix-cron ]; then
        printenv | grep -Ev 'BASHOPTS|BASH_VERSINFO|EUID|PPID|SHELLOPTS|UID|LANG|PWD|GPG_KEY|_=' >> /etc/environment
        echo "15,45 * * * * cd /app/src && /usr/local/bin/python -m pretix runperiodic >> /var/log/cron.log 2>&1" > /etc/cron.d/pretix-cron
        chmod 0644 /etc/cron.d/pretix-cron
        crontab /etc/cron.d/pretix-cron
    fi
    
    # Ensure cron log file exists and is writable
    touch /var/log/cron.log
    chmod 0644 /var/log/cron.log
    
    # Run migrations
    python manage.py migrate

  if [ "${NUM_THREADS:-0}" -gt 1 ]; then
      exec gunicorn pretix.wsgi \
          --name pretix \
          --workers $NUM_WORKERS \
          --max-requests 1200 \
          --max-requests-jitter 50 \
          --timeout 120 \
          --worker-class=gthread \
          --threads=$NUM_THREADS
  else
      exec gunicorn pretix.wsgi \
          --name pretix \
          --workers $NUM_WORKERS \
          --max-requests 1200 \
          --max-requests-jitter 50 \
          --timeout 120
  fi

elif [ "${DEPLOYMENT_TYPE}" = "worker" ]; then

    # For I/O-bound tasks like adding to cart and sending emails
    # Use appropriate concurrency settings
    CELERY_CONCURRENCY=${CELERY_CONCURRENCY:-10}
    CELERY_POOL=${CELERY_POOL:-gevent}  # threads

    if [ "${CELERY_POOL}" = "gevent" ]; then
        # For gevent, we can have much higher concurrency
        CELERY_CONCURRENCY=${CELERY_CONCURRENCY:-50}
    fi

    exec celery -A pretix.celery_app worker \
        --loglevel=info \
        --pool=${CELERY_POOL} \
        --concurrency=${CELERY_CONCURRENCY}
        
else
    echo "Error: DEPLOYMENT_TYPE must be set to either 'main' or 'worker'"
    exit 1

fi
