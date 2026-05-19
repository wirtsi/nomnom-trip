#!/usr/bin/env python3
"""Generate folium map for summer trip."""
import json
import sys
import folium
from folium.plugins import MarkerCluster

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
    
    m = folium.Map(
        location=[45.2, 12.8],
        zoom_start=6,
        tiles="CartoDB dark_matter",
        control_scale=True,
    )

    lats, lngs = [], []
    stop_legends = []
    
    for leg_name, leg_data in data.items():
        stop = leg_data['stop']
        if stop['lat'] is None:
            continue
        lats.append(stop['lat'])
        lngs.append(stop['lng'])
        
        # Add stop marker (gold)
        _dsp = f'<span style="color:#FF8C00;font-size:13px">{stop["dates"]}</span>' if stop['dates'] else ""
        stop_popup = f"""
        <div style="font-family:sans-serif;min-width:240px;max-width:340px">
        <h4 style="color:#FFD700;margin:0 0 6px;border-bottom:1px solid #444;padding-bottom:4px">
        🏨 {stop['name']} {_dsp}
        </h4>
        <p><strong>{stop['desc']}</strong></p>
        <p>{len(leg_data['restaurants'])} restaurants nearby</p>
        <div style="max-height:360px;overflow-y:auto;font-size:12px">
        """ + "".join([
            f"""
            <div style="border-bottom:1px solid #333;padding:5px 0">
                <div style="display:flex;align-items:center;gap:6px">
                    <span style="width:8px;height:8px;border-radius:50%;background:{source_colors.get(r['source'],'#888')};display:inline-block"></span>
                    <b style="font-size:13px;color:#fff">{r['name']}</b>
                </div>
                <div style="color:#aaa;margin-top:2px">{r['source']} • {r['dist_km']}km{' • '+(r.get('cuisine') or '')}</div>
                {((r.get('description') or '')[:90] + '...') if r.get('description') else ''}
            </div>
            """ for r in leg_data['restaurants'][:20]
        ]) + "</div></div>"

        folium.CircleMarker(
            location=[stop['lat'], stop['lng']],
            radius=14,
            color='#FFA500',
            fill_color='#FFD700',
            fill_opacity=0.9,
            weight=3,
            popup=folium.Popup(stop_popup, max_width=450),
            tooltip=f"🏨 {stop['name']}{' (' + stop['dates'] + ')' if stop['dates'] else ''}"
        ).add_to(m)
        
        # Add restaurant pins
        for r in leg_data['restaurants']:
            lats.append(r['lat'])
            lngs.append(r['lng'])
            color = source_colors.get(r['source'], '#808080')
            
            tag_badge = ""
            tags = str(r.get('tags', ''))
            if 'michelin:THREE_STARS' in tags:
                tag_badge = '⭐⭐⭐ '
            elif 'michelin:TWO_STARS' in tags:
                tag_badge = '⭐⭐ '
            elif 'michelin:ONE_STAR' in tags:
                tag_badge = '⭐ '
            elif 'michelin:BIB_GOURMAND' in tags:
                tag_badge = '💚 '
            elif 'natural-wine' in tags:
                tag_badge = '🍷 '
                
            popup = f"""
            <div style="font-family:sans-serif;min-width:220px;max-width:300px">
                <h4 style="margin:0 0 4px;color:#fff">{tag_badge}{r['name']}</h4>
                <p style="margin:2px 0;color:#ccc;font-size:12px">
                    <b>{r['source']}</b> • {r['dist_km']}km{' • '+(r.get('cuisine') or '')}
                </p>
                <p style="margin:2px 0;color:#aaa;font-size:11px;line-height:1.3">{(r.get('description') or '')[:160]}</p>
                {f'<a href="{r.get("source_url")}" target="_blank" style="color:#6495ED;font-size:12px">View source →</a>' if r.get('source_url') else ''}
            </div>
            """
            
            folium.CircleMarker(
                location=[r['lat'], r['lng']],
                radius=4,
                color=color,
                fill_color=color,
                fill_opacity=0.7,
                weight=1,
                popup=folium.Popup(popup, max_width=320),
                tooltip=f"{r['name']} ({r['source']}, {r['dist_km']}km)"
            ).add_to(m)

    # Fit bounds
    if lats and lngs:
        sw = [min(lats) - 0.5, min(lngs) - 0.5]
        ne = [max(lats) + 0.5, max(lngs) + 0.5]
        m.fit_bounds([sw, ne])

    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/trip_map_folium.html"
    m.save(out)
    print(f"Map saved to {out}")
    total = sum(len(v['restaurants']) for v in data.values())
    print(f"Total stops: {len(data)}, restaurants: {total}")

if __name__ == '__main__':
    main()
