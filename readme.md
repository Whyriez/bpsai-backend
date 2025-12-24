Command
- flask import:csv "data/scrap/hasil_scraping_bps_gorontalo.csv"
- flask tags:auto --all | flask tags:auto --limit 10 | flask tags:auto (100 data pertama yg belum ditag)
- flask db:seed
- flask user:create-admin "siti.aminah@bps.go.id" "siti"

server side:
- venv/bin/python run
- venv/bin/pip install flasgger
- - venv/bin/pip install Flask-Caching
- ./venv/bin/flask user:create-admin "fitra@bps.go.id" "fitra"