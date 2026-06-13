from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import entity_registry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .bpost_entry_data import BpostEntryData
from .const import DOMAIN


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Configure all sensors and expose as entities."""

    entry_data: BpostEntryData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BpostSensor(entry_data.coordinator, sensor_id) for sensor_id in entry_data.coordinator.data["sensor"].keys()
    )

    def add_new_entities() -> None:
        entities = entity_registry.async_entries_for_config_entry(entity_registry.async_get(hass), entry.entry_id)
        current_ids = [
            entity.unique_id.removeprefix(f"{DOMAIN}_sensor_")
            for entity in entities
            if entity.platform == DOMAIN and entity.domain == "sensor" and entity.unique_id
        ]
        data_ids = entry_data.coordinator.data["sensor"].keys()
        to_add = [entity_id for entity_id in data_ids if entity_id not in current_ids]

        async_add_entities(BpostSensor(entry_data.coordinator, sensor_id) for sensor_id in to_add)

    entry_data.coordinator.async_add_listener(add_new_entities)


class BpostSensor(CoordinatorEntity, SensorEntity):
    """Sensor providing information about bpost My Mail and parcels."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DataUpdateCoordinator, sensor_id: str):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self.sensor_id = sensor_id

    @property
    def native_value(self) -> StateType:
        return self.coordinator.data["sensor"][self.sensor_id]["data"]

    @property
    def unique_id(self) -> str | None:
        return f"{DOMAIN}_sensor_{self.sensor_id}"

    @property
    def name(self) -> str | None:
        data = self.coordinator.data["sensor"][self.sensor_id]
        return data.get("name") or self.sensor_id.replace("_", " ").capitalize()

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        return self.coordinator.data["sensor"][self.sensor_id].get("extra")
