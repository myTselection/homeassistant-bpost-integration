"""Config flow for bpost integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .api import BpostAuthenticationError, BpostConnectionError, BpostWebApi
from .const import CONF_PASSWORD, CONF_POSTAL_CODE, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_EMAIL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_POSTAL_CODE): str,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


async def validate_login(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate bpost credentials against the web login."""
    email_address = data.get(CONF_EMAIL)
    if email_address is None:
        raise ValueError("No email address provided")
    password = data.get(CONF_PASSWORD)
    if password is None:
        raise ValueError("No password provided")

    email_address = email_address.lower()

    bpost_api = BpostWebApi(
        session=async_get_clientsession(hass),
        email=email_address,
        password=password,
        postal_code=data.get(CONF_POSTAL_CODE),
    )
    try:
        await bpost_api.async_login()
    except BpostAuthenticationError as exc:
        raise InvalidAuth(exc) from exc
    except BpostConnectionError as exc:
        raise CannotConnect(exc) from exc

    result = {
        CONF_EMAIL: email_address,
        CONF_PASSWORD: password,
    }
    if data.get(CONF_POSTAL_CODE):
        result[CONF_POSTAL_CODE] = data[CONF_POSTAL_CODE]
    return result


class BpostConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore
    """Handle a config flow for BPost."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""

        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP_USER_EMAIL_SCHEMA)

        errors = {}

        try:
            info = await validate_login(self.hass, user_input)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except ValueError:
            errors["base"] = "invalid_login"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            await self.async_set_unique_id(info[CONF_EMAIL])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=info[CONF_EMAIL], data=info)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_EMAIL_SCHEMA, errors=errors)

    async def async_create_or_update_entry(self, info: dict[str, Any]) -> FlowResult:
        """Create or update entry."""
        existing_entry = await self.async_set_unique_id(info[CONF_EMAIL])
        if existing_entry:
            self.hass.config_entries.async_update_entry(existing_entry, data={**existing_entry.data, **info})
            await self.hass.config_entries.async_reload(existing_entry.entry_id)
            return self.async_abort(reason="reauth_successful")
        else:
            return self.async_create_entry(title=info[CONF_EMAIL], data=info)

    async def async_step_reauth(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle reauthentication confirmation."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=STEP_REAUTH_SCHEMA,
            )

        errors = {}
        try:
            info = await validate_login(
                self.hass,
                {CONF_EMAIL: self.unique_id, CONF_PASSWORD: user_input[CONF_PASSWORD]},
            )
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            return await self.async_create_or_update_entry(info)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
