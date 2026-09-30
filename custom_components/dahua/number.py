"""Number platform for Dahua: how long the deterrence white light stays on.

The duration is offered as a slider rather than a fixed value because the useful
setting depends on why the light is on: a few seconds to acknowledge a visitor, a
long window while somebody is working outside.

**The field is discovered, not assumed.** The entity is created only for a channel
whose last poll actually contains `MANUAL_DURATION_FIELD`, so a camera that does not
report it gets no entity rather than a slider that writes into nothing. That also
avoids gating on a model name, which is the bug class behind #570, #676 and #690.

`MANUAL_DURATION_FIELD` is the one thing here that has not been confirmed against
hardware. Measured on a DHI-NVR5464-16P-EI, the `Lighting_V2` table reports

    AIMixLightObjFilterTypes[0], AIMixLightSwitchDelay, Correction, FarLight[0].Angle,
    FarLight[0].Light, LightType, MiddleLight[0].Angle, MiddleLight[0].Light, Mode,
    NearLight[0].Angle, NearLight[0].Light, PercentOfMaxBrightness, Sensitive

and no duration field of any kind, across sixteen channels. Dahua tables are
model dependent, so that is evidence about one recorder rather than proof the field
never exists. Until a device is seen reporting it, this platform creates nothing,
which is the intended behaviour rather than a bug to chase.
"""
import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTime

from custom_components.dahua import DahuaDataUpdateCoordinator, entry_coordinators

from .entity import DahuaBaseEntity

_LOGGER = logging.getLogger(__package__)

# One write at a time. Every entity here sends a command to the device, and these
# boxes are measurably intolerant of concurrent requests: see the note in select.py.
PARALLEL_UPDATES = 1

# The Lighting_V2 leaf the white light's manual duration lives in. Change this in one
# place once a device is seen reporting it; both the read and the write use it.
MANUAL_DURATION_FIELD = "ManualDuration"

# Ten seconds is the shortest useful acknowledgement, two hours the longest window
# anyone has asked for, and the device counts in whole seconds.
MINIMUM_SECONDS = 10
MAXIMUM_SECONDS = 7200
STEP_SECONDS = 10


def manual_duration_key(channel: int, profile_mode: str, light_index: int) -> str:
    """The polled key this entity reads, in the shape the rest of the table uses.

    `Lighting_V2[channel][profile][index].<Field>`, matching
    `async_set_lighting_v2` and `async_set_lighting_v2_raw`, so the read and the
    write cannot drift apart.
    """
    return "table.Lighting_V2[{0}][{1}][{2}].{3}".format(
        channel, profile_mode, light_index, MANUAL_DURATION_FIELD
    )


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Add a duration slider for each channel whose device reports one."""
    for coordinator in entry_coordinators(entry).values():
        entities = []

        if coordinator.supports_security_light() and _reports_duration(coordinator):
            entities.append(DahuaDeterrenceLightDuration(coordinator, entry))
        elif coordinator.supports_security_light():
            _LOGGER.debug(
                "Channel %s has a security light but reports no %s, so no duration "
                "control is offered",
                coordinator.get_channel(),
                MANUAL_DURATION_FIELD,
            )

        if entities:
            # Each channel's entities belong to its own subentry (#899); without this
            # they are filed under the host and the channel devices come up empty.
            async_add_entities(
                entities, config_subentry_id=coordinator.subentry_id)


def _reports_duration(coordinator: DahuaDataUpdateCoordinator) -> bool:
    return manual_duration_key(
        coordinator.get_channel(),
        coordinator.get_profile_mode(),
        coordinator.get_illuminator_index(),
    ) in (coordinator.data or {})


class DahuaDeterrenceLightDuration(DahuaBaseEntity, NumberEntity):
    """How long the white light stays on when it is turned on manually."""

    _attr_translation_key = "deterrence_light_duration"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = MINIMUM_SECONDS
    _attr_native_max_value = MAXIMUM_SECONDS
    _attr_native_step = STEP_SECONDS

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry) -> None:
        DahuaBaseEntity.__init__(self, coordinator, entry)
        NumberEntity.__init__(self)
        self._attr_unique_id = (
            f"{coordinator.get_serial_number()}_deterrence_light_duration")

    @property
    def unique_id(self):
        return self._attr_unique_id

    def _key(self) -> str:
        return manual_duration_key(
            self._coordinator.get_channel(),
            self._coordinator.get_profile_mode(),
            self._coordinator.get_illuminator_index(),
        )

    @property
    def native_value(self) -> float | None:
        """What the device last reported, not what was last written here.

        Reading it back off the poll is what makes the slider show the camera's real
        setting after a restart, and what makes a write that the device silently
        refused visible rather than remembered locally as if it had worked.
        """
        raw = (self._coordinator.data or {}).get(self._key())
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            # The table is strings off a CGI response; anything unparsable means
            # unknown rather than an entity that raises on every state write.
            _LOGGER.debug("Unusable %s for channel %s: %r",
                          MANUAL_DURATION_FIELD, self._coordinator.get_channel(), raw)
            return None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.client.async_set_lighting_v2_field(
            self._coordinator.get_channel(),
            self._coordinator.get_profile_mode(),
            self._coordinator.get_illuminator_index(),
            MANUAL_DURATION_FIELD,
            int(value),
        )
        # Refresh rather than assume: the state comes back from the device, so a
        # refusal shows up as the old value instead of a slider that moved.
        await self._coordinator.async_refresh()
