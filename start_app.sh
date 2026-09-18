#!/bin/bash
cd /home/ubuntu/ai-swautomorph
export FLASK_ENV=production
gunicorn --bind 0.0.0.0:5000 --workers 4 --timeout 120 wsgi:app
