#!/bin/sh
# Lanzador instalado en /usr/bin/dotdeck
exec env PYTHONPATH=/usr/lib/dotdeck${PYTHONPATH:+:$PYTHONPATH} python3 -m dotdeck "$@"
