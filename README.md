# Warsaw smog widget

A small desktop widget for Ubuntu that shows air quality in Warsaw on a map:
official GIOŚ stations, the city's Airly sensors and volunteer-run community
sensors, coloured by the Polish air quality index.

## Features

- Map of the whole city, or zoomed on any of the 18 districts.
- Three data sources, told apart by marker shape: GIOŚ (circle), Airly
  (diamond), Sensor.Community (triangle).
- Hover a marker for its PM10 / PM2.5 readings; otherwise the district average.
- Optional street labels next to every marker.
- Auto-refresh every 5 to 60 minutes, plus a Refresh button.

## Install and run

    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0   # usually already present
    python3 warsaw_smog.py

Other ways to start it:

    python3 warsaw_smog.py --district Ursus      # start zoomed on a district
    python3 warsaw_smog.py --demo                # made-up readings, for trying the interface
    python3 warsaw_smog.py --snapshot out.png    # save one picture and exit

## Using it

- **Move:** drag the widget with the left mouse button.
- **District:** the button at the top right opens the list of districts;
  "Whole city" takes you back. In a district view the Airly and community
  sensors appear as well.
- **Details:** hover a marker to see its name, category and readings. With
  nothing hovered, a district view shows the average of all sensors in view
  and the time of the latest measurement.
- **Colour scale:** the white dot marks the hovered sensor, or the average.
- **Labels:** tick "labels" under the map to show a name next to every marker.
- **Refresh:** the "Refresh" button fetches new data now; the selector next to
  it sets how often this happens automatically.
- **Right-click** anywhere for the same options and Quit.

Your district, label and refresh settings are remembered.

## Start at login

Save this as `~/.config/autostart/warsaw-smog.desktop`, with the real path:

    [Desktop Entry]
    Type=Application
    Name=Warsaw smog
    Exec=python3 /path/to/warsaw_smog.py
    X-GNOME-Autostart-enabled=true

## Data sources

**GIOŚ** (Chief Inspectorate of Environmental Protection): the official
reference stations. Values are hourly, so they can be up to an hour or so
behind. No setup needed.

**Sensor.Community**: sensors run by volunteers, reporting every few minutes.
No setup needed. Their position is deliberately approximate, so the widget
shows them as "near" a street. They are low-cost devices and read high in fog
and very humid air.

**Airly** (optional): the city's sensor network, mostly at schools. It needs
your own free API key:

1. Register at https://developer.airly.org and copy your key.
2. Save it in a file only you can read:

       install -m 600 /dev/null ~/.config/warsaw-smog-airly.key
       nano ~/.config/warsaw-smog-airly.key      # paste the key, save

   Alternatively, set the `AIRLY_API_KEY` environment variable.
3. Restart the widget and pick a district.

The free key allows 100 requests a day, so the widget shows the 8 Airly sensors
nearest the district centre and updates each one every 3 hours. The line at the
bottom shows how many requests you have left today. Your key is personal;
Airly's terms do not allow sharing it.

Airly and community markers use the same Polish index colours as GIOŚ,
calculated from their PM10 and PM2.5 readings. Treat them as indicative next to
the GIOŚ reference stations.

## Privacy and network use

The widget contacts only these services, and sends no personal data:

- `api.gios.gov.pl` for GIOŚ readings.
- `tile.openstreetmap.org` for the map; tiles are downloaded once and kept.
- `data.sensor.community` for community sensors, while a district is open.
- `nominatim.openstreetmap.org` to find the street for each community sensor,
  once per sensor.
- `airapi.airly.eu`, only if you set an Airly key.

It uses no third-party Python packages. Downloaded map tiles and street names
are kept in `~/.cache/warsaw-smog/`, settings in `~/.config/warsaw-smog.json`.

## Known limitations

- Under Wayland (the Ubuntu default) the widget cannot pin itself to the
  desktop or remember its position; under X11 it can.
- District views are centred by hand and show a rectangle round the district,
  so sensors just across the boundary are included.

## Attribution

- Air quality data: Główny Inspektorat Ochrony Środowiska (GIOŚ),
  https://powietrze.gios.gov.pl
- Community sensor data: Sensor.Community, https://sensor.community
- Optional sensor data: Airly, https://airly.org/map/en/
- Map: © OpenStreetMap contributors

## Credits

Created in collaboration by Hasherezade and Claude Fable 5.2 (Anthropic).
