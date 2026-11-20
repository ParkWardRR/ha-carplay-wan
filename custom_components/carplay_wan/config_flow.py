"""Config flow: point Home Assistant at the agent on the box."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from aiohttp import ClientError, ClientTimeout
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_NAME, DEFAULT_PORT, DOMAIN

STEP_USER = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


class CarPlayWanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the box's address and prove something is listening on it."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = user_input[CONF_HOST], user_input[CONF_PORT]
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            try:
                cars = await self._probe(host, port)
            except ClientError:
                errors["base"] = "cannot_connect"
            except TimeoutError:
                errors["base"] = "timeout"
            except ValueError:
                # Something answered, but it was not the agent. Worth its own
                # message: pointing this at the carplayd metrics port by
                # mistake is the obvious wrong turn, and "cannot connect"
                # would send people looking for a network fault.
                errors["base"] = "not_the_agent"
            else:
                return self.async_create_entry(
                    title=DEFAULT_NAME,
                    data={CONF_HOST: host, CONF_PORT: port},
                    description=f"{len(cars)} car(s)",
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER, errors=errors
        )

    async def _probe(self, host: str, port: int) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)
        async with session.get(
            f"http://{host}:{port}/state", timeout=ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        cars = data.get("cars") if isinstance(data, dict) else None
        if not isinstance(cars, dict):
            raise ValueError("no cars in the reply")
        return cars
