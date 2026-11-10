# Warsaw smog widget

Small borderless GTK window: GIOŚ stations on a map of Warsaw, coloured by the
Polish air quality index; hover a station for PM10 / PM2.5.

## Run

    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0   # usually already present
    python3 warsaw_smog.py            # live data
    python3 warsaw_smog.py --demo     # fake readings, no GIOŚ requests
    python3 warsaw_smog.py --snapshot out.png   # render one frame, no window

Left-drag moves it. The button at the top right ("Whole city" with an arrow)
opens the district list. Everything else is in the two rows under the map, and
also in the right-click menu (which has Quit).

## Refreshing

- **Refresh** fetches everything now. It does nothing while a refresh is
  already running.
- The selector next to it ("every 30 min") sets the auto-refresh interval:
  5, 10, 15, 30 or 60 minutes. The choice is remembered, and the new interval
  counts from the moment you pick it.

What a faster interval actually buys you differs per source:

- Community sensors report every few minutes, so they do update at 5 minutes.
- GIOŚ publishes one value per hour; asking more often returns the same data.
- Airly readings are reused for 3 hours whatever the interval (and whatever
  the Refresh button), because of the 100-requests-a-day limit; see below.

## Districts

The top-right button (or right-click > District) zooms the map on one of the 18 districts (or back to
"Whole city"); the choice is remembered. `--district NAME` sets it from the
command line. While zoomed and not hovering a marker, the line under the map
shows the district average: the category, mean PM10 / PM2.5, how many sensors
went into it and the time of the latest measurement. The header keeps showing
the city-wide worst.

The average covers every sensor with a reading inside the map view, each
counted equally whatever its source (GIOŚ, Airly, community), and the category
is the Polish index of the mean concentrations. Because the view is a rectangle
round the district centre, sensors just across the boundary are included.

District views are hand-placed centre points with a fixed zoom (the `DISTRICTS`
table at the top of the script), not official boundaries.

## The colour scale

The bar under the readings is the Polish air quality index, very good to very
bad. The white dot marks whatever the line above it describes: the sensor you
are hovering, otherwise the district average (or the city mean in the
whole-city view). Its position within a colour shows how far through that
category's concentration range the value is, taking the worse of PM10 and
PM2.5. No dot means no reading.

## Start at login

Save as `~/.config/autostart/warsaw-smog.desktop` (fix the path):

    [Desktop Entry]
    Type=Application
    Name=Warsaw smog
    Exec=python3 /path/to/warsaw_smog.py
    X-GNOME-Autostart-enabled=true

## More sensors: Airly (optional)

GIOŚ runs only a handful of reference stations in Warsaw. The denser network
(the city's ~108 sensors in the districts, mostly at schools, plus private
ones) is Airly hardware, and its data is only available through Airly's API:

1. Register at https://developer.airly.org and copy your API key.
2. Store it where only you can read it:

       install -m 600 /dev/null ~/.config/warsaw-smog-airly.key
       nano ~/.config/warsaw-smog-airly.key      # paste the key, save

   (or export `AIRLY_API_KEY` in the environment instead).
3. Restart the widget and pick a district.

Airly sensors appear as diamonds, in district views only. The free key allows
100 requests a day and one request returns one sensor, so the widget fetches
the 8 sensors nearest the district centre and reuses each reading for 3 hours
(about 64 requests a day on one district, the rest is headroom for switching).
The status line below the map shows how many requests are left; when the
limit is hit the sensors turn grey until the counter resets. The limits are the
`AIRLY_*` constants at the top of the script.

Airly diamonds are coloured with the same Polish index scale as GIOŚ, derived
by the widget from the PM10 / PM2.5 values (worse of the two); it is not
Airly's own CAQI index. These are low-cost optical sensors, so treat them as
indicative next to the GIOŚ reference stations.

Airly's API terms: one key per person, not to be shared; no redistribution or
database building (readings are kept in memory only); if you ever publish
something built on this, their attribution rules (source named, link to
map.airly.eu, logo) apply.

## Community sensors: Sensor.Community (no key)

District views also show volunteer-run Sensor.Community (formerly Luftdaten)
sensors as triangles. This is open data: one request covers the whole city and
returns the last 5 minutes of readings, refreshed with everything else. Indoor
sensors are skipped.

The API gives no street address, so the widget looks the street up from each
sensor's coordinates with OpenStreetMap's Nominatim. All streets for a district
are preloaded as soon as you select it, at Nominatim's limit of one lookup per
second; the status line below the map counts them ("streets 7/23") and
says so if the lookup fails. Results stay in memory while the widget runs and
are also saved to `~/.cache/warsaw-smog/geocode.json`, so a district you have
opened before is instant, also after a restart. A sensor shows as "#12345"
until its street arrives. Sensor.Community blurs sensor positions on purpose, so read the
result as "near this street"; it can be the next street over.

## Labels

Tick "labels" just below the map (or right-click > Show labels)
to keep a name next to every marker; the setting is remembered. Every marker
gets a label: each one is put on whichever side of its marker (right, left,
above, below or a diagonal) covers the least of the other labels and markers.
In a very tight cluster labels may still touch; hovering a marker always shows
its full name under the map.

These are cheap optical sensors, readings are near-instant rather than hourly
means, and they read high in fog and very humid air, so expect them to be
noisier than both GIOŚ and Airly.

Sensor.Community and OpenStreetMap ask clients to identify themselves: set
`CONTACT` at the top of the script to an e-mail or URL if you are happy for it
to be sent in the User-Agent header (to every host the widget contacts).

## Network behaviour

- `api.gios.gov.pl` (v1 API): station list and per-station sensor lists once a
  week (cached in `~/.cache/warsaw-smog/metadata.json`); every 30 minutes one
  index request per station plus one request per PM10 / PM2.5 sensor.
- `tile.openstreetmap.org`: four z10 tiles for the city view plus up to nine z11/z12 tiles per
  district you open, each downloaded once and kept in
  `~/.cache/warsaw-smog/tiles/`.

- `airapi.airly.eu`: only if a key is configured, as described above.
- `data.sensor.community`: one request per refresh while a district is open.
- `nominatim.openstreetmap.org`: one reverse-geocoding request per community
  sensor, once ever; it receives the sensor's (public) coordinates.

Nothing else is contacted; no telemetry, no third-party Python packages.

## Attribution

Air quality data: Główny Inspektorat Ochrony Środowiska (GIOŚ).
Map: © OpenStreetMap contributors.
Optional sensor data: Airly (https://airly.org/map/en/).
Community sensor data: Sensor.Community (https://sensor.community).

## Credits

Created in collaboration by Hasherezade and Claude Fable 5.2 (Anthropic).
