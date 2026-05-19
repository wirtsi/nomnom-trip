# Example queries

## Status check

```
$ python scripts/query.py --status

source      last run                   age         status  rows
------------------------------------------------------------------------------
michelin    2026-05-09T03:01:14         0.2d        ok      +17231/0
raisin      2026-05-09T03:14:42         0.2d        ok      +987/13
splendido   2026-05-09T03:15:50         0.2d        ok      +0/151

Total rows in DB:
  michelin    17231
  raisin      8042
  splendido   151
```

## Near a city

```
$ python scripts/query.py --near "Bologna, Italy" --radius-km 5 --limit 10
```

## Near hotel coordinates with a cuisine filter

```
$ python scripts/query.py --lat 44.4949 --lng 11.3426 \
    --radius-km 2 --cuisine "Italian" --limit 5
```

## Just one source

```
$ python scripts/query.py --near "Lyon" --sources splendido
```

## JSON output (for piping)

```
$ python scripts/query.py --near "Rome" --json | jq '.[] | {name, source, distance_km}'
```

## Wine-focused trip planning

```
$ python scripts/query.py --near "Trentino-Alto Adige, Italy" \
    --keyword "natural" --radius-km 50 --limit 50
```

## Refresh just one source

```
$ python scripts/sync.py --source michelin
```

## Test a small Raisin batch before full sync

```
$ python scripts/sync.py --source raisin --max-pages 50
```
