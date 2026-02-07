release: python manage.py migrate --noinput && python manage.py ensure_superuser && python manage.py ensure_seed
web: python manage.py collectstatic --noinput && gunicorn brotherwillies.wsgi --bind 0.0.0.0:$PORT
