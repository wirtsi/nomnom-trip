#!/usr/bin/env python3
"""Generate interactive Leaflet HTML map from trip_restaurants.json."""
import json
import sys
from pathlib import Path

def main():
    with open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/trip_restaurants.json") as f:
        data = json.load(f)

    source_colors = {
        "michelin": "#C41E3A",
        "gambero": "#FF8C00",
        "raisin": "#722F37",
        "blog": "#228B22",
        "splendido": "#4169E1",
    }
    source_labels = {
        "michelin": "Michelin",
        "gambero": "Gambero Rosso",
        "raisin": "Raisin (Natural Wine)",
        "blog": "Blog/Editorial",
        "splendido": "Splendido",
    }

    def tag_badge(tags):
        tags = tags or ""
        if "michelin:THREE_STARS" in tags:
            return '<span style="background:#C41E3A;color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;">⭐⭐⭐ 3 Stars</span>'
        if "michelin:TWO_STARS" in tags:
            return '<span style="background:#C41E3A;color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;">⭐⭐ 2 Stars</span>'
        if "michelin:ONE_STAR" in tags:
            return '<span style="background:#C41E3A;color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;">⭐ 1 Star</span>'
        if "michelin:BIB_GOURMAND" in tags:
            return '<span style="background:#228B22;color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;">💚 Bib Gourmand</span>'
        if "michelin:selected" in tags:
            return '<span style="background:#666;color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;">Michelin Selected</span>'
        if "natural-wine" in tags:
            return '<span style="background:#722F37;color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;">🍷 Natural Wine</span>'
        if "editorial" in tags:
            return '<span style="background:#4169E1;color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;">📰 Editorial</span>'
        return ''

    parts = []
    parts.append("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>nomnom 2026 Summer Trip</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#1a1a2e;color:#eee;}
#map{height:100vh;width:100vw}
.popup-stop{max-width:360px}
.popup-stop h4{color:#FFD700;font-size:15px;margin-bottom:6px;border-bottom:1px solid #444;padding-bottom:6px}
.popup-stop .rest-list{max-height:380px;overflow-y:auto;margin-top:8px}
.rest-item{padding:5px 0;border-bottom:1px solid #222;display:flex;align-items:flex-start;gap:8px}
.rest-item:last-child{border-bottom:none}
.rest-dot{width:10px;height:10px;border-radius:50%;margin-top:4px;flex-shrink:0;border:1px solid rgba(255,255,255,0.3)}
.rest-name{font-weight:600;color:#fff;font-size:13px}
.rest-meta{color:#888;font-size:11px;margin-top:1px}
.rest-tag{margin-top:3px;display:block}
.popup-res{max-width:300px}
.popup-res h4{color:#fff;font-size:14px;margin-bottom:4px}
.popup-res p{color:#ccc;font-size:12px;line-height:1.4;margin-top:4px}
.popup-res a{color:#6495ED;text-decoration:none}
.popup-res a:hover{text-decoration:underline}
.legend{background:rgba(26,26,46,0.95);border:1px solid #333;border-radius:8px;padding:12px;font-size:12px;line-height:1.8}
.legend-item{display:flex;align-items:center;gap:8px}
.legend-dot{width:10px;height:10px;border-radius:50%;border:1px solid rgba(255,255,255,0.3)}
.stop-ring{fill:#FFD700;stroke:#FFA500;stroke-width:3;fill-opacity:0.9}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:#1a1a2e}
::-webkit-scrollbar-thumb{background:#444;border-radius:3px}
</style>
</head>
<body>
<div id="map"></div>
<script>
const map=L.map('map').setView([45.5,12.5],6);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
    attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains:'abcd',maxZoom:19
}).addTo(map);
const sourceColors=""" + json.dumps(source_colors) + """;
const sourceLabels=""" + json.dumps(source_labels) + """;
""")

    bounds = []
    for leg_name, leg_data in data.items():
        stop = leg_data['stop']
        if stop['lat'] is None:
            continue
        bounds.append(f"[{stop['lat']},{stop['lng']}]")
        parts.append(f"""
L.circleMarker([{stop['lat']},{stop['lng']}],{{radius:16,fillColor:'#FFD700',color:'#FFA500',weight:3,fillOpacity:0.9}})
.addTo(map).bindPopup(`<div class="popup-stop">
<h4>🏨 {stop['name']} {'<span style="color:#FF8C00">'+stop['dates']+'</span>' if stop['dates'] else ''}</h4>
<p><strong>{stop['desc']}</strong></p>
<p>{len(leg_data['restaurants'])} curated restaurants nearby</p>
<div class="rest-list">
"""+"".join([f"""
<div class="rest-item">
<div class="rest-dot" style="background:{source_colors.get(r['source'],'#888')}"></div>
<div>
<div class="rest-name">{r['name']}</div>
<div class="rest-meta">{source_labels.get(r['source'],r['source'])} • {r['dist_km']}km {' • '+(r['cuisine'] or '')}</div>
{tag_badge(r.get('tags',''))}
</div>
</div>
""" for r in leg_data['restaurants'][:20]])+f"""
</div></div>`);
""")

        for r in leg_data['restaurants']:
            color = source_colors.get(r['source'], '#808080')
            bounds.append(f"[{r['lat']},{r['lng']}]")
            parts.append(f"""
L.circleMarker([{r['lat']},{r['lng']}],{{radius:4,fillColor:'{color}',color:'{color}',weight:1,fillOpacity:0.7}})
.addTo(map).bindPopup(`<div class="popup-res">
<h4>{r['name']}</h4>
<p><strong>{source_labels.get(r['source'],r['source'])}</strong> • {r['dist_km']}km{' • '+(r['cuisine'] if r['cuisine'] else '')}</p>
{tag_badge(r.get('tags',''))}
<p>{(r.get('description') or '')[:180]}</p>
<a href="{r.get('source_url','')}" target="_blank">View source →</a>
</div>`);
""")

    parts.append(f"""
const bounds=L.latLngBounds([{']] , ['.join(bounds)}]);
map.fitBounds(bounds,{{padding:[50,50]}});
const legend=L.control({{position:'bottomright'}});
legend.onAdd=function(){{const d=L.DomUtil.create('div','legend');d.innerHTML='<strong style="font-size:14px;color:#fff">Sources</strong><br>'+Object.entries(sourceColors).map(([k,v])=>`<div class="legend-item"><div class="legend-dot" style="background:${{v}}"></div>${{sourceLabels[k]}}</div>`).join('')+'<div class="legend-item"><div class="legend-dot" style="background:#FFD700;border:2px solid #FFA500"></div>🏨 Trip Stop</div>';return d;}};
legend.addTo(map);
</script>
</body></html>""")

    out = Path('/tmp/trip_map.html')
    out.write_text('\n'.join(parts))
    print(f"Map written to {out}")
    total = sum(len(v['restaurants']) for v in data.values())
    print(f"Total stops: {len(data)}, restaurants: {total}")

if __name__ == '__main__':
    main()
