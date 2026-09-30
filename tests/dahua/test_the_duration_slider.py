"""Which channels get a duration slider, and where its value comes from.

The design decision worth testing is that **the entity is discovered, not assumed**. A
slider is created only for a channel whose last poll actually contains the field it
writes to, so a camera that does not report one gets no entity at all rather than a
control that writes into nothing and appears to work.

That is deliberately not a model whitelist. Gating a capability on a model prefix is
the bug class behind #570, #676 and #690, and it fails the same way every time: the
feature is missing on a device that supports it, or present on one that does not, and
the only fix is another name in the list.

Measured while writing this: a DHI-NVR5464-16P-EI reports `Mode`, `LightType`,
`Correction`, `Sensitive`, `PercentOfMaxBrightness`, the three light banks and two AI
mix light fields, and **no duration field at all**, across sixteen channels. So on that
recorder these tests describe a platform that correctly creates nothing.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import number as number_platform
from custom_components.dahua.number import (
    MANUAL_DURATION_FIELD, MAXIMUM_SECONDS, MINIMUM_SECONDS,
    DahuaDeterrenceLightDuration, manual_duration_key)

CHANNEL = 2
PROFILE = "1"
LIGHT_INDEX = 0
SUBENTRY = "subentry-for-channel-2"


def _coordinator(*, reports=True, security_light=True, value="60", channel=CHANNEL):
    key = manual_duration_key(channel, PROFILE, LIGHT_INDEX)
    return SimpleNamespace(
        data={key: value} if reports else {"table.Lighting_V2[2][1][0].Mode": "Manual"},
        subentry_id=SUBENTRY,
        client=_Client(),
        get_channel=lambda: channel,
        get_profile_mode=lambda: PROFILE,
        get_illuminator_index=lambda: LIGHT_INDEX,
        get_serial_number=lambda: "SERIAL_%d" % channel,
        supports_security_light=lambda: security_light,
        async_refresh=_refreshed,
    )


async def _refreshed():
    return None


class _Client:
    def __init__(self):
        self.written = []

    async def async_set_lighting_v2_field(self, channel, profile_mode, light_index,
                                         field, value):
        self.written.append((channel, profile_mode, light_index, field, value))


@pytest.fixture
def added(monkeypatch):
    """Records what the platform publishes, and with which subentry."""
    calls = []

    def async_add_entities(entities, **kwargs):
        calls.append((list(entities), kwargs))

    return SimpleNamespace(calls=calls, callback=async_add_entities)


def _setup(monkeypatch, added, *coordinators):
    monkeypatch.setattr(
        number_platform, "entry_coordinators",
        lambda entry: {index: c for index, c in enumerate(coordinators)})
    return number_platform.async_setup_entry(
        SimpleNamespace(), SimpleNamespace(entry_id="e1"), added.callback)


# --- the key the read and the write share ------------------------------------


def test_the_key_matches_the_shape_the_rest_of_the_table_uses():
    """`Lighting_V2[channel][profile][index].Field`, the same as
    `async_set_lighting_v2_raw`. If the read and the write disagreed the slider would
    show one value and change another."""
    assert manual_duration_key(2, "1", 0) == (
        "table.Lighting_V2[2][1][0].%s" % MANUAL_DURATION_FIELD)


def test_the_key_carries_the_channel_and_the_profile():
    """A recorder polls every channel into one table, and the profile changes between
    day and night, so both have to be in the key or the slider reads somebody else's
    setting."""
    assert manual_duration_key(9, "0", 1) != manual_duration_key(2, "0", 1)
    assert manual_duration_key(2, "0", 1) != manual_duration_key(2, "1", 1)


# --- which channels get one -------------------------------------------------


async def test_a_channel_that_reports_the_field_gets_a_slider(monkeypatch, added):
    await _setup(monkeypatch, added, _coordinator())

    entities, _kwargs = added.calls[0]
    assert [type(e).__name__ for e in entities] == ["DahuaDeterrenceLightDuration"]


async def test_a_channel_that_does_not_report_it_gets_nothing(monkeypatch, added):
    """The design decision. A slider writing to a field the device never mentioned
    would look like it worked, because nothing in the CGI reply says otherwise."""
    await _setup(monkeypatch, added, _coordinator(reports=False))

    assert added.calls == []


async def test_no_security_light_means_no_slider(monkeypatch, added):
    """Even where the field is reported. A duration for a light the device does not
    have is a control with nothing behind it."""
    await _setup(monkeypatch, added, _coordinator(security_light=False))

    assert added.calls == []


async def test_only_the_channels_that_report_it_get_one(monkeypatch, added):
    """The recorder case. Channels differ, and the decision is taken per channel."""
    await _setup(monkeypatch, added,
                 _coordinator(channel=2, reports=True),
                 _coordinator(channel=3, reports=False),
                 _coordinator(channel=4, reports=True))

    assert len(added.calls) == 2, added.calls


async def test_the_slider_is_filed_under_its_own_channel(monkeypatch, added):
    """#899. Without the subentry every channel's entities land on the host device and
    the channel devices come up empty."""
    await _setup(monkeypatch, added, _coordinator())

    _entities, kwargs = added.calls[0]
    assert kwargs.get("config_subentry_id") == SUBENTRY


# --- what the slider shows --------------------------------------------------


def _entity(coordinator):
    entity = object.__new__(DahuaDeterrenceLightDuration)
    entity._coordinator = coordinator
    return entity


def test_the_value_comes_off_the_last_poll():
    """Not from what was last written here. Reading it back is what makes the slider
    survive a restart and what makes a refused write visible."""
    assert _entity(_coordinator(value="90")).native_value == 90


def test_a_value_the_device_sent_as_text_is_still_a_number():
    """The table is strings out of a CGI response."""
    assert _entity(_coordinator(value="60")).native_value == 60


@pytest.mark.parametrize("raw", ["", "soon", None, "1.5.2"])
def test_an_unusable_value_reads_as_unknown(raw):
    """Unknown rather than an entity that raises on every state write."""
    assert _entity(_coordinator(value=raw)).native_value is None


def test_a_channel_with_no_value_has_none():
    assert _entity(_coordinator(reports=False)).native_value is None


def test_the_bounds_are_a_sensible_window():
    """Ten seconds is the shortest useful acknowledgement and two hours the longest
    window anyone asked for; the device counts whole seconds."""
    assert MINIMUM_SECONDS == 10
    assert MAXIMUM_SECONDS == 7200
    assert MINIMUM_SECONDS < MAXIMUM_SECONDS


# --- and what it writes -----------------------------------------------------


async def test_the_write_names_the_key_it_read():
    """Same channel, profile, index and field as `native_value` used, so the slider
    cannot show one entry's value and change another's."""
    coordinator = _coordinator()
    entity = _entity(coordinator)

    await entity.async_set_native_value(120)

    assert coordinator.client.written == [
        (CHANNEL, PROFILE, LIGHT_INDEX, MANUAL_DURATION_FIELD, 120)]


async def test_the_value_is_written_as_a_whole_number():
    """Home Assistant hands a float to a number entity, and the field is seconds."""
    coordinator = _coordinator()

    await _entity(coordinator).async_set_native_value(90.0)

    assert coordinator.client.written[0][-1] == 90
    assert isinstance(coordinator.client.written[0][-1], int)


async def test_the_device_is_re_read_after_a_write():
    """So the state shown comes back from the device. Assuming the write landed is how
    a slider ends up showing a value the camera refused."""
    refreshed = []
    coordinator = _coordinator()

    async def _refresh():
        refreshed.append(1)

    coordinator.async_refresh = _refresh

    await _entity(coordinator).async_set_native_value(30)

    assert refreshed == [1]
