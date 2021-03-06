"""
This component provides light support for the Philips Hue system.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/light.hue/
"""
import asyncio
from datetime import timedelta
import logging
import random

import async_timeout

import homeassistant.components.hue as hue
from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_COLOR_TEMP, ATTR_EFFECT, ATTR_FLASH, ATTR_RGB_COLOR,
    ATTR_TRANSITION, ATTR_XY_COLOR, EFFECT_COLORLOOP, EFFECT_RANDOM,
    FLASH_LONG, FLASH_SHORT, SUPPORT_BRIGHTNESS,
    SUPPORT_COLOR_TEMP, SUPPORT_EFFECT, SUPPORT_FLASH, SUPPORT_RGB_COLOR,
    SUPPORT_TRANSITION, SUPPORT_XY_COLOR, Light)
import homeassistant.util.color as color_util

DEPENDENCIES = ['hue']
SCAN_INTERVAL = timedelta(seconds=5)

_LOGGER = logging.getLogger(__name__)

SUPPORT_HUE_ON_OFF = (SUPPORT_FLASH | SUPPORT_TRANSITION)
SUPPORT_HUE_DIMMABLE = (SUPPORT_HUE_ON_OFF | SUPPORT_BRIGHTNESS)
SUPPORT_HUE_COLOR_TEMP = (SUPPORT_HUE_DIMMABLE | SUPPORT_COLOR_TEMP)
SUPPORT_HUE_COLOR = (SUPPORT_HUE_DIMMABLE | SUPPORT_EFFECT |
                     SUPPORT_RGB_COLOR | SUPPORT_XY_COLOR)
SUPPORT_HUE_EXTENDED = (SUPPORT_HUE_COLOR_TEMP | SUPPORT_HUE_COLOR)

SUPPORT_HUE = {
    'Extended color light': SUPPORT_HUE_EXTENDED,
    'Color light': SUPPORT_HUE_COLOR,
    'Dimmable light': SUPPORT_HUE_DIMMABLE,
    'On/Off plug-in unit': SUPPORT_HUE_ON_OFF,
    'Color temperature light': SUPPORT_HUE_COLOR_TEMP
    }

ATTR_IS_HUE_GROUP = 'is_hue_group'
# Minimum Hue Bridge API version to support groups
# 1.4.0 introduced extended group info
# 1.12 introduced the state object for groups
# 1.13 introduced "any_on" to group state objects
GROUP_MIN_API_VERSION = (1, 13, 0)


async def async_setup_platform(hass, config, async_add_devices,
                               discovery_info=None):
    """Set up the Hue lights."""
    if discovery_info is None:
        return

    bridge = hass.data[hue.DOMAIN][discovery_info['host']]
    cur_lights = {}
    cur_groups = {}

    api_version = tuple(
        int(v) for v in bridge.api.config.apiversion.split('.'))

    allow_groups = bridge.allow_groups
    if allow_groups and api_version < GROUP_MIN_API_VERSION:
        _LOGGER.warning('Please update your Hue bridge to support groups')
        allow_groups = False

    # Hue updates all lights via a single API call.
    #
    # If we call a service to update 2 lights, we only want the API to be
    # called once.
    #
    # The throttle decorator will return right away if a call is currently
    # in progress. This means that if we are updating 2 lights, the first one
    # is in the update method, the second one will skip it and assume the
    # update went through and updates it's data, not good!
    #
    # The current mechanism will make sure that all lights will wait till
    # the update call is done before writing their data to the state machine.
    #
    # An alternative approach would be to disable automatic polling by Home
    # Assistant and take control ourselves. This works great for polling as now
    # we trigger from 1 time update an update to all entities. However it gets
    # tricky from inside async_turn_on and async_turn_off.
    #
    # If automatic polling is enabled, Home Assistant will call the entity
    # update method after it is done calling all the services. This means that
    # when we update, we know all commands have been processed. If we trigger
    # the update from inside async_turn_on, the update will not capture the
    # changes to the second entity until the next polling update because the
    # throttle decorator will prevent the call.

    progress = None
    light_progress = set()
    group_progress = set()

    async def request_update(is_group, object_id):
        """Request an update.

        We will only make 1 request to the server for updating at a time. If a
        request is in progress, we will join the request that is in progress.

        This approach is possible because should_poll=True. That means that
        Home Assistant will ask lights for updates during a polling cycle or
        after it has called a service.

        We keep track of the lights that are waiting for the request to finish.
        When new data comes in, we'll trigger an update for all non-waiting
        lights. This covers the case where a service is called to enable 2
        lights but in the meanwhile some other light has changed too.
        """
        nonlocal progress

        progress_set = group_progress if is_group else light_progress
        progress_set.add(object_id)

        if progress is not None:
            return await progress

        progress = asyncio.ensure_future(update_bridge())
        result = await progress
        progress = None
        light_progress.clear()
        group_progress.clear()
        return result

    async def update_bridge():
        """Update the values of the bridge.

        Will update lights and, if enabled, groups from the bridge.
        """
        tasks = []
        tasks.append(async_update_items(
            hass, bridge, async_add_devices, request_update,
            False, cur_lights, light_progress
        ))

        if allow_groups:
            tasks.append(async_update_items(
                hass, bridge, async_add_devices, request_update,
                True, cur_groups, group_progress
            ))

        await asyncio.wait(tasks)

    await update_bridge()


async def async_update_items(hass, bridge, async_add_devices,
                             request_bridge_update, is_group, current,
                             progress_waiting):
    """Update either groups or lights from the bridge."""
    import aiohue

    if is_group:
        api = bridge.api.groups
    else:
        api = bridge.api.lights

    try:
        with async_timeout.timeout(4):
            await api.update()
    except (asyncio.TimeoutError, aiohue.AiohueException):
        if not bridge.available:
            return

        _LOGGER.error('Unable to reach bridge %s', bridge.host)
        bridge.available = False

        for light_id, light in current.items():
            if light_id not in progress_waiting:
                light.async_schedule_update_ha_state()

        return

    if not bridge.available:
        _LOGGER.info('Reconnected to bridge %s', bridge.host)
        bridge.available = True

    new_lights = []

    for item_id in api:
        if item_id not in current:
            current[item_id] = HueLight(
                api[item_id], request_bridge_update, bridge, is_group)

            new_lights.append(current[item_id])
        elif item_id not in progress_waiting:
            current[item_id].async_schedule_update_ha_state()

    if new_lights:
        async_add_devices(new_lights)


class HueLight(Light):
    """Representation of a Hue light."""

    def __init__(self, light, request_bridge_update, bridge, is_group=False):
        """Initialize the light."""
        self.light = light
        self.async_request_bridge_update = request_bridge_update
        self.bridge = bridge
        self.is_group = is_group

        if is_group:
            self.is_osram = False
            self.is_philips = False
        else:
            self.is_osram = light.manufacturername == 'OSRAM'
            self.is_philips = light.manufacturername == 'Philips'

    @property
    def unique_id(self):
        """Return the ID of this Hue light."""
        return self.light.uniqueid

    @property
    def name(self):
        """Return the name of the Hue light."""
        return self.light.name

    @property
    def brightness(self):
        """Return the brightness of this light between 0..255."""
        if self.is_group:
            return self.light.action.get('bri')
        return self.light.state.get('bri')

    @property
    def xy_color(self):
        """Return the XY color value."""
        if self.is_group:
            return self.light.action.get('xy')
        return self.light.state.get('xy')

    @property
    def color_temp(self):
        """Return the CT color value."""
        if self.is_group:
            return self.light.action.get('ct')
        return self.light.state.get('ct')

    @property
    def is_on(self):
        """Return true if device is on."""
        if self.is_group:
            return self.light.state['any_on']
        return self.light.state['on']

    @property
    def available(self):
        """Return if light is available."""
        return self.bridge.available and (self.is_group or
                                          self.bridge.allow_unreachable or
                                          self.light.state['reachable'])

    @property
    def supported_features(self):
        """Flag supported features."""
        return SUPPORT_HUE.get(self.light.type, SUPPORT_HUE_EXTENDED)

    @property
    def effect_list(self):
        """Return the list of supported effects."""
        return [EFFECT_COLORLOOP, EFFECT_RANDOM]

    async def async_turn_on(self, **kwargs):
        """Turn the specified or all lights on."""
        command = {'on': True}

        if ATTR_TRANSITION in kwargs:
            command['transitiontime'] = int(kwargs[ATTR_TRANSITION] * 10)

        if ATTR_XY_COLOR in kwargs:
            if self.is_osram:
                color_hue, sat = color_util.color_xy_to_hs(
                    *kwargs[ATTR_XY_COLOR])
                command['hue'] = color_hue / 360 * 65535
                command['sat'] = sat / 100 * 255
            else:
                command['xy'] = kwargs[ATTR_XY_COLOR]
        elif ATTR_RGB_COLOR in kwargs:
            if self.is_osram:
                hsv = color_util.color_RGB_to_hsv(
                    *(int(val) for val in kwargs[ATTR_RGB_COLOR]))
                command['hue'] = hsv[0] / 360 * 65535
                command['sat'] = hsv[1] / 100 * 255
                command['bri'] = hsv[2] / 100 * 255
            else:
                xyb = color_util.color_RGB_to_xy(
                    *(int(val) for val in kwargs[ATTR_RGB_COLOR]))
                command['xy'] = xyb[0], xyb[1]
        elif ATTR_COLOR_TEMP in kwargs:
            temp = kwargs[ATTR_COLOR_TEMP]
            command['ct'] = max(self.min_mireds, min(temp, self.max_mireds))

        if ATTR_BRIGHTNESS in kwargs:
            command['bri'] = kwargs[ATTR_BRIGHTNESS]

        flash = kwargs.get(ATTR_FLASH)

        if flash == FLASH_LONG:
            command['alert'] = 'lselect'
            del command['on']
        elif flash == FLASH_SHORT:
            command['alert'] = 'select'
            del command['on']
        else:
            command['alert'] = 'none'

        effect = kwargs.get(ATTR_EFFECT)

        if effect == EFFECT_COLORLOOP:
            command['effect'] = 'colorloop'
        elif effect == EFFECT_RANDOM:
            command['hue'] = random.randrange(0, 65535)
            command['sat'] = random.randrange(150, 254)
        elif self.is_philips:
            command['effect'] = 'none'

        if self.is_group:
            await self.light.set_action(**command)
        else:
            await self.light.set_state(**command)

    async def async_turn_off(self, **kwargs):
        """Turn the specified or all lights off."""
        command = {'on': False}

        if ATTR_TRANSITION in kwargs:
            command['transitiontime'] = int(kwargs[ATTR_TRANSITION] * 10)

        flash = kwargs.get(ATTR_FLASH)

        if flash == FLASH_LONG:
            command['alert'] = 'lselect'
            del command['on']
        elif flash == FLASH_SHORT:
            command['alert'] = 'select'
            del command['on']
        else:
            command['alert'] = 'none'

        if self.is_group:
            await self.light.set_action(**command)
        else:
            await self.light.set_state(**command)

    async def async_update(self):
        """Synchronize state with bridge."""
        await self.async_request_bridge_update(self.is_group, self.light.id)

    @property
    def device_state_attributes(self):
        """Return the device state attributes."""
        attributes = {}
        if self.is_group:
            attributes[ATTR_IS_HUE_GROUP] = self.is_group
        return attributes
