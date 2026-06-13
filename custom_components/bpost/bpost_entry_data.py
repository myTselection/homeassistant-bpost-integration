from datetime import timedelta
from logging import Logger
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BpostAuthenticationError, BpostConnectionError, BpostWebApi
from .const import (
    ATTR_PARCELS,
    BINARY_SENSOR_EXPECTING_PARCEL,
    CONF_PASSWORD,
    SENSOR_PARCELS_DUE,
)

async def async_update_sensors(bpost_api: BpostWebApi):
    """Fetch parcel data and shape it for Home Assistant entities."""
    parcels = await bpost_api.async_fetch_parcels()
    parcel_attributes = [parcel.attributes for parcel in parcels]

    sensor_data: dict[str, dict[str, Any]] = {
        SENSOR_PARCELS_DUE: {
            "data": len(parcels),
            "extra": {ATTR_PARCELS: parcel_attributes},
        }
    }
    for parcel in parcels:
        sensor_data[f"parcel_{_slugify(parcel.tracking_id)}"] = {
            "data": parcel.status or "expected",
            "name": parcel.name,
            "extra": parcel.attributes,
        }

    binary_sensor_data = {
        BINARY_SENSOR_EXPECTING_PARCEL: {
            "data": bool(parcels),
            "extra": {ATTR_PARCELS: parcel_attributes},
        },
    }
    return {
        "sensor": sensor_data,
        "binary_sensor": binary_sensor_data,
    }


class BpostEntryData:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, logger: Logger) -> None:
        super().__init__()
        self.api = BpostWebApi(
            session=async_get_clientsession(hass),
            email=entry.data[CONF_EMAIL],
            password=entry.data[CONF_PASSWORD],
        )

        async def async_update_data() -> dict[str, Any]:
            try:
                return await async_update_sensors(self.api)
            except BpostAuthenticationError as ex:
                raise ConfigEntryAuthFailed(ex)
            except BpostConnectionError as ex:
                raise UpdateFailed(f"Error communicating with bpost: {ex}") from ex

        self.coordinator = DataUpdateCoordinator(
            hass,
            logger,
            name="bpost",
            update_method=async_update_data,
            update_interval=timedelta(hours=1),
        )


def _slugify(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "_" for character in value).strip("_")
