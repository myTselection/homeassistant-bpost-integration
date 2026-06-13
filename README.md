# My bpost integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/sdebruyn/homeassistant-bpost-integration/main.svg)](https://results.pre-commit.ci/latest/github/sdebruyn/homeassistant-bpost-integration/main)
![GitHub Workflow Status (branch)](https://img.shields.io/github/workflow/status/sdebruyn/homeassistant-bpost-integration/Validate/main)
![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)

This is a custom component for [Home Assistant](https://home-assistant.io/)
that allows you to see parcels listed in your bpost Mijn bpost account.

This integration signs in through the same bpost account flow used by
[Mijn bpost](https://www.bpost.be/nl/mijn-bpost).

## Features

Only features with a checked box are available at the moment. Other features are planned for the future.

### Entities

#### Sensor

* [x] The amount of parcels expected for delivery: `sensor.parcels_due`
* [x] A sensor per expected parcel, with tracking details in the attributes

#### Binary sensor

* [x] If you're expecting a parcel: `binary_sensor.expecting_parcel`

### Services

* [ ] Start tracking a new parcel

## Installation

### Installing the integration as a custom component

This repository is included in the [HACS](https://hacs.xyz) repositories.

### Configure the integration in Home Assistant

1. Go to your Home Assistant settings > Integrations and add a new integration.
2. Search for `bpost` and select it.
3. Enter the email address and password for your bpost account.
4. All entities mentioned above are now available.

## Dashboard examples

### Markdown card

```yaml
type: markdown
title: bpost parcels
content: >
  {% set parcels = state_attr('sensor.parcels_due', 'parcels') or [] %}

  {% if parcels | count == 0 %}
  No parcels expected.
  {% else %}
  **{{ states('sensor.parcels_due') }} parcel{{ 's' if parcels | count != 1 else '' }} expected**

  {% for parcel in parcels %}
  ---

  **{{ parcel.get('sender') or parcel.get('tracking_id', 'Parcel') }}**

  Status: {{ parcel.get('status', 'Expected') }}

  {% if parcel.get('expected_delivery') %}
  Delivery: {{ parcel.get('expected_delivery') }}
  {% endif %}

  Tracking: `{{ parcel.get('tracking_id', 'Unknown') }}`
  {% endfor %}
  {% endif %}
```

### Conditional card

Only show parcel details when at least one parcel is expected:

```yaml
type: conditional
conditions:
  - entity: binary_sensor.expecting_parcel
    state: "on"
card:
  type: markdown
  title: bpost parcels
  content: >
    {% set parcels = state_attr('sensor.parcels_due', 'parcels') or [] %}

    **{{ parcels | count }} parcel{{ 's' if parcels | count != 1 else '' }} expected**

    {% for parcel in parcels %}
    ---

    **{{ parcel.get('sender') or parcel.get('tracking_id', 'Parcel') }}**

    Status: {{ parcel.get('status', 'Expected') }}

    {% if parcel.get('expected_delivery') %}
    Delivery: {{ parcel.get('expected_delivery') }}
    {% endif %}

    Tracking: `{{ parcel.get('tracking_id', 'Unknown') }}`
    {% endfor %}
```

### Auto-entities card

If you use the `auto-entities` custom card, you can automatically list the dynamic parcel sensors:

```yaml
type: custom:auto-entities
card:
  type: entities
  title: bpost parcels
filter:
  include:
    - entity_id: sensor.parcel_*
sort:
  method: name
```

If bpost changes the Mijn bpost page payload, the integration may need an
extractor update. Enable debug logging for `custom_components.bpost` when
reporting parcel parsing issues.

## License

MIT license

## Contributing

1. Install [poetry](https://python-poetry.org/)
2. Clone the repository
3. Install dependencies `poetry install`
4. Activate venv: `poetry shell`
5. Configure pre-commit: `pre-commit install`
6. Configure your IDE to use the poetry venv (`poetry env info`)
