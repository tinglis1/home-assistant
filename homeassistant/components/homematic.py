"""
Support for Homematic devices.

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/homematic/
"""
import os
import time
import logging
from datetime import timedelta
from functools import partial

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.const import (EVENT_HOMEASSISTANT_STOP, STATE_UNKNOWN,
                                 CONF_USERNAME, CONF_PASSWORD, CONF_PLATFORM,
                                 ATTR_ENTITY_ID)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers import discovery
from homeassistant.config import load_yaml_config_file
from homeassistant.util import Throttle

DOMAIN = 'homematic'
REQUIREMENTS = ["pyhomematic==0.1.14"]

HOMEMATIC = None
HOMEMATIC_LINK_DELAY = 0.5

MIN_TIME_BETWEEN_UPDATE_HUB = timedelta(seconds=300)
MIN_TIME_BETWEEN_UPDATE_VAR = timedelta(seconds=60)

DISCOVER_SWITCHES = 'homematic.switch'
DISCOVER_LIGHTS = 'homematic.light'
DISCOVER_SENSORS = 'homematic.sensor'
DISCOVER_BINARY_SENSORS = 'homematic.binary_sensor'
DISCOVER_COVER = 'homematic.cover'
DISCOVER_CLIMATE = 'homematic.climate'

ATTR_DISCOVER_DEVICES = 'devices'
ATTR_PARAM = 'param'
ATTR_CHANNEL = 'channel'
ATTR_NAME = 'name'
ATTR_ADDRESS = 'address'
ATTR_VALUE = 'value'

EVENT_KEYPRESS = 'homematic.keypress'
EVENT_IMPULSE = 'homematic.impulse'

SERVICE_VIRTUALKEY = 'virtualkey'
SERVICE_SET_VALUE = 'set_value'

HM_DEVICE_TYPES = {
    DISCOVER_SWITCHES: ['Switch', 'SwitchPowermeter'],
    DISCOVER_LIGHTS: ['Dimmer'],
    DISCOVER_SENSORS: ['SwitchPowermeter', 'Motion', 'MotionV2',
                       'RemoteMotion', 'ThermostatWall', 'AreaThermostat',
                       'RotaryHandleSensor', 'WaterSensor', 'PowermeterGas',
                       'LuxSensor', 'WeatherSensor', 'WeatherStation'],
    DISCOVER_CLIMATE: ['Thermostat', 'ThermostatWall', 'MAXThermostat'],
    DISCOVER_BINARY_SENSORS: ['ShutterContact', 'Smoke', 'SmokeV2', 'Motion',
                              'MotionV2', 'RemoteMotion', 'WeatherSensor',
                              'TiltSensor'],
    DISCOVER_COVER: ['Blind']
}

HM_IGNORE_DISCOVERY_NODE = [
    'ACTUAL_TEMPERATURE',
    'ACTUAL_HUMIDITY'
]

HM_ATTRIBUTE_SUPPORT = {
    'LOWBAT': ['Battery', {0: 'High', 1: 'Low'}],
    'ERROR': ['Sabotage', {0: 'No', 1: 'Yes'}],
    'RSSI_DEVICE': ['RSSI', {}],
    'VALVE_STATE': ['Valve', {}],
    'BATTERY_STATE': ['Battery', {}],
    'CONTROL_MODE': ['Mode', {0: 'Auto', 1: 'Manual', 2: 'Away', 3: 'Boost'}],
    'POWER': ['Power', {}],
    'CURRENT': ['Current', {}],
    'VOLTAGE': ['Voltage', {}],
    'WORKING': ['Working', {0: 'No', 1: 'Yes'}],
}

HM_PRESS_EVENTS = [
    'PRESS_SHORT',
    'PRESS_LONG',
    'PRESS_CONT',
    'PRESS_LONG_RELEASE'
]

HM_IMPULSE_EVENTS = [
    'SEQUENCE_OK'
]

_LOGGER = logging.getLogger(__name__)

CONF_RESOLVENAMES_OPTIONS = [
    'metadata',
    'json',
    'xml',
    False
]

CONF_LOCAL_IP = 'local_ip'
CONF_LOCAL_PORT = 'local_port'
CONF_REMOTE_IP = 'remote_ip'
CONF_REMOTE_PORT = 'remote_port'
CONF_RESOLVENAMES = 'resolvenames'
CONF_DELAY = 'delay'
CONF_VARIABLES = 'variables'


DEVICE_SCHEMA = vol.Schema({
    vol.Required(CONF_PLATFORM): "homematic",
    vol.Required(ATTR_NAME): cv.string,
    vol.Required(ATTR_ADDRESS): cv.string,
    vol.Optional(ATTR_CHANNEL, default=1): vol.Coerce(int),
    vol.Optional(ATTR_PARAM): cv.string,
})

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_LOCAL_IP): cv.string,
        vol.Optional(CONF_LOCAL_PORT, default=8943): cv.port,
        vol.Required(CONF_REMOTE_IP): cv.string,
        vol.Optional(CONF_REMOTE_PORT, default=2001): cv.port,
        vol.Optional(CONF_RESOLVENAMES, default=False):
            vol.In(CONF_RESOLVENAMES_OPTIONS),
        vol.Optional(CONF_USERNAME, default="Admin"): cv.string,
        vol.Optional(CONF_PASSWORD, default=""): cv.string,
        vol.Optional(CONF_DELAY, default=0.5): vol.Coerce(float),
        vol.Optional(CONF_VARIABLES, default=False): cv.boolean,
    }),
}, extra=vol.ALLOW_EXTRA)

SCHEMA_SERVICE_VIRTUALKEY = vol.Schema({
    vol.Required(ATTR_ADDRESS): cv.string,
    vol.Required(ATTR_CHANNEL): vol.Coerce(int),
    vol.Required(ATTR_PARAM): cv.string,
})

SCHEMA_SERVICE_SET_VALUE = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Required(ATTR_VALUE): cv.match_all,
})


def virtualkey(hass, address, channel, param):
    """Send virtual keypress to homematic controlller."""
    data = {
        ATTR_ADDRESS: address,
        ATTR_CHANNEL: channel,
        ATTR_PARAM: param,
    }

    hass.services.call(DOMAIN, SERVICE_VIRTUALKEY, data)


def set_value(hass, entity_id, value):
    """Change value of homematic system variable."""
    data = {
        ATTR_ENTITY_ID: entity_id,
        ATTR_VALUE: value,
    }

    hass.services.call(DOMAIN, SERVICE_SET_VALUE, data)


# pylint: disable=unused-argument,too-many-locals
def setup(hass, config):
    """Setup the Homematic component."""
    global HOMEMATIC, HOMEMATIC_LINK_DELAY
    from pyhomematic import HMConnection

    component = EntityComponent(_LOGGER, DOMAIN, hass)

    local_ip = config[DOMAIN].get(CONF_LOCAL_IP)
    local_port = config[DOMAIN].get(CONF_LOCAL_PORT)
    remote_ip = config[DOMAIN].get(CONF_REMOTE_IP)
    remote_port = config[DOMAIN].get(CONF_REMOTE_PORT)
    resolvenames = config[DOMAIN].get(CONF_RESOLVENAMES)
    username = config[DOMAIN].get(CONF_USERNAME)
    password = config[DOMAIN].get(CONF_PASSWORD)
    HOMEMATIC_LINK_DELAY = config[DOMAIN].get(CONF_DELAY)
    use_variables = config[DOMAIN].get(CONF_VARIABLES)

    if remote_ip is None or local_ip is None:
        _LOGGER.error("Missing remote CCU/Homegear or local address")
        return False

    # Create server thread
    bound_system_callback = partial(_system_callback_handler, hass, config)
    HOMEMATIC = HMConnection(local=local_ip,
                             localport=local_port,
                             remote=remote_ip,
                             remoteport=remote_port,
                             systemcallback=bound_system_callback,
                             resolvenames=resolvenames,
                             rpcusername=username,
                             rpcpassword=password,
                             interface_id="homeassistant")

    # Start server thread, connect to peer, initialize to receive events
    HOMEMATIC.start()

    # Stops server when Homeassistant is shutting down
    hass.bus.listen_once(EVENT_HOMEASSISTANT_STOP, HOMEMATIC.stop)
    hass.config.components.append(DOMAIN)

    # regeister homematic services
    descriptions = load_yaml_config_file(
        os.path.join(os.path.dirname(__file__), 'services.yaml'))

    hass.services.register(DOMAIN, SERVICE_VIRTUALKEY,
                           _hm_service_virtualkey,
                           descriptions[DOMAIN][SERVICE_VIRTUALKEY],
                           schema=SCHEMA_SERVICE_VIRTUALKEY)

    entities = []

    ##
    # init HM variable
    variables = HOMEMATIC.getAllSystemVariables() if use_variables else {}
    hm_var_store = {}
    if variables is not None:
        for key, value in variables.items():
            varia = HMVariable(key, value)
            hm_var_store.update({key: varia})
            entities.append(varia)

    # add homematic entites
    entities.append(HMHub(hm_var_store, use_variables))
    component.add_entities(entities)

    ##
    # register set_value service if exists variables
    if not variables:
        return True

    def _service_handle_value(service):
        """Set value on homematic variable object."""
        variable_list = component.extract_from_service(service)

        value = service.data[ATTR_VALUE]

        for hm_variable in variable_list:
            hm_variable.hm_set(value)

    hass.services.register(DOMAIN, SERVICE_SET_VALUE,
                           _service_handle_value,
                           descriptions[DOMAIN][SERVICE_SET_VALUE],
                           schema=SCHEMA_SERVICE_SET_VALUE)

    return True


# pylint: disable=too-many-branches
def _system_callback_handler(hass, config, src, *args):
    """Callback handler."""
    if src == 'newDevices':
        _LOGGER.debug("newDevices with: %s", str(args))
        # pylint: disable=unused-variable
        (interface_id, dev_descriptions) = args
        key_dict = {}
        # Get list of all keys of the devices (ignoring channels)
        for dev in dev_descriptions:
            key_dict[dev['ADDRESS'].split(':')[0]] = True

        # Register EVENTS
        # Search all device with a EVENTNODE that include data
        bound_event_callback = partial(_hm_event_handler, hass)
        for dev in key_dict:
            if dev not in HOMEMATIC.devices:
                continue

            hmdevice = HOMEMATIC.devices.get(dev)
            # have events?
            if len(hmdevice.EVENTNODE) > 0:
                _LOGGER.debug("Register Events from %s", dev)
                hmdevice.setEventCallback(callback=bound_event_callback,
                                          bequeath=True)

        # If configuration allows autodetection of devices,
        # all devices not configured are added.
        if key_dict:
            for component_name, discovery_type in (
                    ('switch', DISCOVER_SWITCHES),
                    ('light', DISCOVER_LIGHTS),
                    ('cover', DISCOVER_COVER),
                    ('binary_sensor', DISCOVER_BINARY_SENSORS),
                    ('sensor', DISCOVER_SENSORS),
                    ('climate', DISCOVER_CLIMATE)):
                # Get all devices of a specific type
                found_devices = _get_devices(discovery_type, key_dict)

                # When devices of this type are found
                # they are setup in HA and an event is fired
                if found_devices:
                    # Fire discovery event
                    discovery.load_platform(hass, component_name, DOMAIN, {
                        ATTR_DISCOVER_DEVICES: found_devices
                    }, config)


def _get_devices(device_type, keys):
    """Get the Homematic devices."""
    device_arr = []

    # pylint: disable=too-many-nested-blocks
    for key in keys:
        device = HOMEMATIC.devices[key]
        class_name = device.__class__.__name__
        metadata = {}

        # is class supported by discovery type
        if class_name not in HM_DEVICE_TYPES[device_type]:
            continue

        # Load metadata if needed to generate a param list
        if device_type == DISCOVER_SENSORS:
            metadata.update(device.SENSORNODE)
        elif device_type == DISCOVER_BINARY_SENSORS:
            metadata.update(device.BINARYNODE)

        params = _create_params_list(device, metadata, device_type)
        if params:
            # Generate options for 1...n elements with 1...n params
            for channel in range(1, device.ELEMENT + 1):
                _LOGGER.debug("Handling %s:%i", key, channel)
                if channel in params:
                    for param in params[channel]:
                        name = _create_ha_name(
                            name=device.NAME,
                            channel=channel,
                            param=param
                        )
                        device_dict = {
                            CONF_PLATFORM: "homematic",
                            ATTR_ADDRESS: key,
                            ATTR_NAME: name,
                            ATTR_CHANNEL: channel
                        }
                        if param is not None:
                            device_dict.update({ATTR_PARAM: param})

                        # Add new device
                        try:
                            DEVICE_SCHEMA(device_dict)
                            device_arr.append(device_dict)
                        except vol.MultipleInvalid as err:
                            _LOGGER.error("Invalid device config: %s",
                                          str(err))
                else:
                    _LOGGER.debug("Channel %i not in params", channel)
        else:
            _LOGGER.debug("Got no params for %s", key)
    _LOGGER.debug("%s autodiscovery: %s", device_type, str(device_arr))
    return device_arr


def _create_params_list(hmdevice, metadata, device_type):
    """Create a list from HMDevice with all possible parameters in config."""
    params = {}
    merge = False

    # use merge?
    if device_type in (DISCOVER_SENSORS, DISCOVER_BINARY_SENSORS):
        merge = True

    # Search in sensor and binary metadata per elements
    for channel in range(1, hmdevice.ELEMENT + 1):
        param_chan = []
        for node, meta_chan in metadata.items():
            try:
                # Is this attribute ignored?
                if node in HM_IGNORE_DISCOVERY_NODE:
                    continue
                if meta_chan == 'c' or meta_chan is None:
                    # Only channel linked data
                    param_chan.append(node)
                elif channel == 1:
                    # First channel can have other data channel
                    param_chan.append(node)
            except (TypeError, ValueError):
                _LOGGER.error("Exception generating %s (%s)",
                              hmdevice.ADDRESS, str(metadata))

        # default parameter is merge is off
        if len(param_chan) == 0 and not merge:
            param_chan.append(None)

        # Add to channel
        if len(param_chan) > 0:
            params.update({channel: param_chan})

    _LOGGER.debug("Create param list for %s with: %s", hmdevice.ADDRESS,
                  str(params))
    return params


def _create_ha_name(name, channel, param):
    """Generate a unique object name."""
    # HMDevice is a simple device
    if channel == 1 and param is None:
        return name

    # Has multiple elements/channels
    if channel > 1 and param is None:
        return "{} {}".format(name, channel)

    # With multiple param first elements
    if channel == 1 and param is not None:
        return "{} {}".format(name, param)

    # Multiple param on object with multiple elements
    if channel > 1 and param is not None:
        return "{} {} {}".format(name, channel, param)


def setup_hmdevice_discovery_helper(hmdevicetype, discovery_info,
                                    add_callback_devices):
    """Helper to setup Homematic devices with discovery info."""
    for config in discovery_info[ATTR_DISCOVER_DEVICES]:
        _LOGGER.debug("Add device %s from config: %s",
                      str(hmdevicetype), str(config))

        # create object and add to HA
        new_device = hmdevicetype(config)
        new_device.link_homematic()

        add_callback_devices([new_device])

    return True


def _hm_event_handler(hass, device, caller, attribute, value):
    """Handle all pyhomematic device events."""
    try:
        channel = int(device.split(":")[1])
        address = device.split(":")[0]
        hmdevice = HOMEMATIC.devices.get(address)
    except (TypeError, ValueError):
        _LOGGER.error("Event handling channel convert error!")
        return

    # is not a event?
    if attribute not in hmdevice.EVENTNODE:
        return

    _LOGGER.debug("Event %s for %s channel %i", attribute,
                  hmdevice.NAME, channel)

    # keypress event
    if attribute in HM_PRESS_EVENTS:
        hass.bus.fire(EVENT_KEYPRESS, {
            ATTR_NAME: hmdevice.NAME,
            ATTR_PARAM: attribute,
            ATTR_CHANNEL: channel
        })
        return

    # impulse event
    if attribute in HM_IMPULSE_EVENTS:
        hass.bus.fire(EVENT_KEYPRESS, {
            ATTR_NAME: hmdevice.NAME,
            ATTR_CHANNEL: channel
        })
        return

    _LOGGER.warning("Event is unknown and not forwarded to HA")


def _hm_service_virtualkey(call):
    """Callback for handle virtualkey services."""
    address = call.data.get(ATTR_ADDRESS)
    channel = call.data.get(ATTR_CHANNEL)
    param = call.data.get(ATTR_PARAM)

    if address not in HOMEMATIC.devices:
        _LOGGER.error("%s not found for service virtualkey!", address)
        return
    hmdevice = HOMEMATIC.devices.get(address)

    # if param exists for this device
    if param not in hmdevice.ACTIONNODE:
        _LOGGER.error("%s not datapoint in hm device %s", param, address)
        return

    # channel exists?
    if channel > hmdevice.ELEMENT:
        _LOGGER.error("%i is not a channel in hm device %s", channel, address)
        return

    # call key
    hmdevice.actionNodeData(param, 1, channel)


class HMHub(Entity):
    """The Homematic hub. I.e. CCU2/HomeGear."""

    def __init__(self, variables_store, use_variables=False):
        """Initialize Homematic hub."""
        self._state = STATE_UNKNOWN
        self._store = variables_store
        self._use_variables = use_variables

        self.update()

    @property
    def name(self):
        """Return the name of the device."""
        return 'Homematic'

    @property
    def state(self):
        """Return the state of the entity."""
        return self._state

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        return {}

    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return "mdi:gradient"

    @property
    def available(self):
        """Return true if device is available."""
        return True if HOMEMATIC is not None else False

    def update(self):
        """Update Hub data and all HM variables."""
        self._update_hub_state()
        self._update_variables_state()

    @Throttle(MIN_TIME_BETWEEN_UPDATE_HUB)
    def _update_hub_state(self):
        """Retrieve latest state."""
        if HOMEMATIC is None:
            return
        state = HOMEMATIC.getServiceMessages()
        self._state = STATE_UNKNOWN if state is None else len(state)

    @Throttle(MIN_TIME_BETWEEN_UPDATE_VAR)
    def _update_variables_state(self):
        """Retrive all variable data and update hmvariable states."""
        if HOMEMATIC is None or not self._use_variables:
            return
        variables = HOMEMATIC.getAllSystemVariables()
        if variables is not None:
            for key, value in variables.items():
                if key in self._store:
                    self._store.get(key).hm_update(value)


class HMVariable(Entity):
    """The Homematic system variable."""

    def __init__(self, name, state):
        """Initialize Homematic hub."""
        self._state = state
        self._name = name

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def state(self):
        """Return the state of the entity."""
        return self._state

    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return "mdi:code-string"

    @property
    def should_poll(self):
        """Return false. Homematic Hub object update variable."""
        return False

    def hm_update(self, value):
        """Update variable over Hub object."""
        if value != self._state:
            self._state = value
            self.update_ha_state()

    def hm_set(self, value):
        """Set variable on homematic controller."""
        if HOMEMATIC is not None:
            if isinstance(self._state, bool):
                value = cv.boolean(value)
            else:
                value = float(value)
            HOMEMATIC.setSystemVariable(self._name, value)
            self._state = value
            self.update_ha_state()


class HMDevice(Entity):
    """The Homematic device base object."""

    # pylint: disable=too-many-instance-attributes
    def __init__(self, config):
        """Initialize a generic Homematic device."""
        self._name = config.get(ATTR_NAME)
        self._address = config.get(ATTR_ADDRESS)
        self._channel = config.get(ATTR_CHANNEL)
        self._state = config.get(ATTR_PARAM)
        self._data = {}
        self._hmdevice = None
        self._connected = False
        self._available = False

        # Set param to uppercase
        if self._state:
            self._state = self._state.upper()

        # Generate name
        if not self._name:
            self._name = _create_ha_name(name=self._address,
                                         channel=self._channel,
                                         param=self._state)

    @property
    def should_poll(self):
        """Return false. Homematic states are pushed by the XML RPC Server."""
        return False

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def assumed_state(self):
        """Return true if unable to access real state of the device."""
        return not self._available

    @property
    def available(self):
        """Return true if device is available."""
        return self._available

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        attr = {}

        # no data available to create
        if not self.available:
            return attr

        # Generate an attributes list
        for node, data in HM_ATTRIBUTE_SUPPORT.items():
            # Is an attributes and exists for this object
            if node in self._data:
                value = data[1].get(self._data[node], self._data[node])
                attr[data[0]] = value

        # static attributes
        attr['ID'] = self._hmdevice.ADDRESS

        return attr

    def link_homematic(self):
        """Connect to Homematic."""
        # device is already linked
        if self._connected:
            return True

        # pyhomematic is loaded
        if HOMEMATIC is None:
            return False

        # Does a HMDevice from pyhomematic exist?
        if self._address in HOMEMATIC.devices:
            # Init
            self._hmdevice = HOMEMATIC.devices[self._address]
            self._connected = True

            # Check if Homematic class is okay for HA class
            _LOGGER.info("Start linking %s to %s", self._address, self._name)
            try:
                # Init datapoints of this object
                self._init_data()
                if HOMEMATIC_LINK_DELAY:
                    # We delay / pause loading of data to avoid overloading
                    # of CCU / Homegear when doing auto detection
                    time.sleep(HOMEMATIC_LINK_DELAY)
                self._load_data_from_hm()
                _LOGGER.debug("%s datastruct: %s", self._name, str(self._data))

                # Link events from pyhomatic
                self._subscribe_homematic_events()
                self._available = not self._hmdevice.UNREACH
                _LOGGER.debug("%s linking done", self._name)
            # pylint: disable=broad-except
            except Exception as err:
                self._connected = False
                _LOGGER.error("Exception while linking %s: %s",
                              self._address, str(err))
        else:
            _LOGGER.debug("%s not found in HOMEMATIC.devices", self._address)

    def _hm_event_callback(self, device, caller, attribute, value):
        """Handle all pyhomematic device events."""
        _LOGGER.debug("%s received event '%s' value: %s", self._name,
                      attribute, value)
        have_change = False

        # Is data needed for this instance?
        if attribute in self._data:
            # Did data change?
            if self._data[attribute] != value:
                self._data[attribute] = value
                have_change = True

        # If available it has changed
        if attribute is 'UNREACH':
            self._available = bool(value)
            have_change = True

        # If it has changed data point, update HA
        if have_change:
            _LOGGER.debug("%s update_ha_state after '%s'", self._name,
                          attribute)
            self.update_ha_state()

    def _subscribe_homematic_events(self):
        """Subscribe all required events to handle job."""
        channels_to_sub = {}

        # Push data to channels_to_sub from hmdevice metadata
        for metadata in (self._hmdevice.SENSORNODE, self._hmdevice.BINARYNODE,
                         self._hmdevice.ATTRIBUTENODE,
                         self._hmdevice.WRITENODE, self._hmdevice.EVENTNODE,
                         self._hmdevice.ACTIONNODE):
            for node, channel in metadata.items():
                # Data is needed for this instance
                if node in self._data:
                    # chan is current channel
                    if channel == 'c' or channel is None:
                        channel = self._channel
                    # Prepare for subscription
                    try:
                        if int(channel) >= 0:
                            channels_to_sub.update({int(channel): True})
                    except (ValueError, TypeError):
                        _LOGGER("Invalid channel in metadata from %s",
                                self._name)

        # Set callbacks
        for channel in channels_to_sub:
            _LOGGER.debug("Subscribe channel %s from %s",
                          str(channel), self._name)
            self._hmdevice.setEventCallback(callback=self._hm_event_callback,
                                            bequeath=False,
                                            channel=channel)

    def _load_data_from_hm(self):
        """Load first value from pyhomematic."""
        if not self._connected:
            return False

        # Read data from pyhomematic
        for metadata, funct in (
                (self._hmdevice.ATTRIBUTENODE,
                 self._hmdevice.getAttributeData),
                (self._hmdevice.WRITENODE, self._hmdevice.getWriteData),
                (self._hmdevice.SENSORNODE, self._hmdevice.getSensorData),
                (self._hmdevice.BINARYNODE, self._hmdevice.getBinaryData)):
            for node in metadata:
                if node in self._data:
                    self._data[node] = funct(name=node, channel=self._channel)

        return True

    def _hm_set_state(self, value):
        """Set data to main datapoint."""
        if self._state in self._data:
            self._data[self._state] = value

    def _hm_get_state(self):
        """Get data from main datapoint."""
        if self._state in self._data:
            return self._data[self._state]
        return None

    def _init_data(self):
        """Generate a data dict (self._data) from the Homematic metadata."""
        # Add all attributes to data dict
        for data_note in self._hmdevice.ATTRIBUTENODE:
            self._data.update({data_note: STATE_UNKNOWN})

        # init device specified data
        self._init_data_struct()

    def _init_data_struct(self):
        """Generate a data dict from the Homematic device metadata."""
        raise NotImplementedError
