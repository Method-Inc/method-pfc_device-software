# Import standard python modules
import os, time, threading

# Import python types
from typing import NamedTuple, Optional, Tuple, Dict, Any, List

# Import device utilities
from device.utilities import logger, bitwise, maths
from device.utilities.communication.i2c.main import I2C
from device.utilities.communication.i2c.exceptions import I2CError
from device.utilities.communication.i2c.mux_simulator import MuxSimulator

# Import peripheral utilities
from device.peripherals.utilities import light

# Import driver elements
from device.peripherals.common.arduino.driver import ArduinoCommsDriver
from device.peripherals.modules.led_arduino import exceptions


class LEDArduinoPanel(object):
    """An led panel controlled by an arduino."""

    # Initialize var defaults
    is_shutdown: bool = True
    driver: Optional[ArduinoCommsDriver] = None

    def __init__(
        self,
        driver_name: str,
        config: Dict[str, Any],
        i2c_lock: threading.RLock,
        logger: logger.Logger,
    ) -> None:
        """Initializes panel."""

        # Initialize panel parameters
        self.driver_name = driver_name
        self.name = str(config.get("name"))
        self.full_name = driver_name + "-" + self.name
        self.pins = config.get("pins")
        self.active_low = config.get("active_low")
        self.i2c_lock = i2c_lock
        self.logger = logger

    def initialize(self) -> None:
        """Initializes panel."""
        self.logger.debug("Initializing {}".format(self.name))
        try:
            self.driver = ArduinoCommsDriver(
                name=self.full_name,
                i2c_lock=self.i2c_lock,
            )
            self.is_shutdown = False
        except Exception as e:
            self.logger.exception("Unable to initialize `{}`".format(self.name))
            self.is_shutdown = True


class LEDArduinoDriver:
    """Driver for array of led panels controlled by an Arduino"""

    # Initialize var defaults
    num_active_panels = 0
    num_expected_panels = 1

    def __init__(
        self,
        name: str,
        panel_configs: List[Dict[str, Any]],
        panel_properties: Dict[str, Any],
        i2c_lock: threading.Lock
    ) -> None:
        """Initializes driver."""

        # Initialize driver parameters
        self.panel_properties = panel_properties
        self.i2c_lock = i2c_lock
        self.simulate = False

        # Initialize logger
        logname = "Driver({})".format(name)
        self.logger = logger.Logger(logname, "peripherals")

        # Parse panel properties
        self.channels = self.panel_properties.get("channels")
        self.dac_map = self.panel_properties.get("dac_map")

        # Initialze num expected panels
        self.num_expected_panels = len(panel_configs)

        # Initialize panels
        self.panels: List[LEDArduinoPanel] = []
        for config in panel_configs:
            panel = LEDArduinoPanel(
                name, config, i2c_lock, self.logger
            )
            panel.initialize()
            self.panels.append(panel)

        # Check at least one panel is still active
        active_panels = [panel for panel in self.panels if not panel.is_shutdown]
        self.num_active_panels = len(active_panels)
        if self.num_active_panels < 1:
            raise exceptions.NoActivePanelsError(logger=self.logger)

        # Successfully initialized
        message = "Successfully initialized with {} ".format(self.num_active_panels)
        message2 = "active panels, expected {}".format(self.num_expected_panels)
        self.logger.debug(message + message2)

    def turn_on(self) -> None:
        """Turns on leds."""
        # for panel in self.panels:
            # panel.driver.write_output(255)
        channel_outputs = self.build_channel_outputs(100)
        self.logger.debug("Turning on")
        
        self.set_outputs(channel_outputs)
        return channel_outputs

    def turn_off(self) -> None:
        """Turns off leds."""
        # for panel in self.panels:
        #     panel.driver.write_output(0)
        self.logger.debug("Turning off")
        channel_outputs = self.build_channel_outputs(0)
        self.set_outputs(channel_outputs)
        return channel_outputs

    def set_spd(
        self, desired_distance: float, desired_intensity: float, desired_spectrum: Dict
    ) -> Tuple[Optional[Dict], Optional[Dict], Optional[Dict]]:
        """Sets spectral power distribution."""
        message = "Setting spd, distance={}cm, ppfd={}umol/m2/s, spectrum={}".format(
            desired_distance, desired_intensity, desired_spectrum
        )
        self.logger.debug(message)

        # Approximate spectral power distribution
        try:
            channel_outputs, output_spectrum, output_intensity = light.approximate_spd(
                self.panel_properties,
                desired_distance,
                desired_intensity,
                desired_spectrum,
            )
        except Exception as e:
            message = "approximate spd failed"
            raise exceptions.SetSPDError(message=message, logger=self.logger) from e

        # Set outputs
        self.set_outputs(channel_outputs)

        # Successfully set channel outputs
        message = "Successfully set spd, output: channels={}, spectrum={}, intensity={}umol/m2/s".format(
            channel_outputs, output_spectrum, output_intensity
        )
        self.logger.debug(message)
        return channel_outputs, output_spectrum, output_intensity  # type: ignore

    def set_outputs(self, par_setpoints: dict) -> None:
        """Sets outputs on light panels. Converts channel names to channel numbers,
        translates par setpoints to dac setpoints, then sets dac."""
        self.logger.debug("Setting outputs: {}".format(par_setpoints))

        # Check at least one panel is active
        active_panels = [panel for panel in self.panels if not panel.is_shutdown]
        self.num_active_panels = len(active_panels)
        if self.num_active_panels < 1:
            raise exceptions.NoActivePanelsError(logger=self.logger)
        message = "Setting outputs on {} active panels".format(self.num_active_panels)
        self.logger.debug(message)

        # Convert channel names to channel numbers
        converted_outputs = {}
        for name, percent in par_setpoints.items():

            # Convert channel name to channel number
            try:
                number = self.get_channel_number(name)
            except Exception as e:
                raise exceptions.SetOutputsError(logger=self.logger) from e

            # Append to converted outputs
            converted_outputs[number] = percent

        # Try to set outputs on all panels
        for panel in self.panels:

            # Scale setpoints
            # dac_setpoints = self.translate_setpoints(converted_outputs) # METHOD REMOVING
            dac_setpoints = converted_outputs

            # Set outputs on panel
            try:
                panel.driver.write_outputs(dac_setpoints)  # type: ignore
            except AttributeError as e:
                message = "Unable to set outputs on `{}`".format(panel.name)
                self.logger.error(message + ", panel not initialized")
                self.logger.error(e)
                self.logger.error(dac_setpoints)
                self.logger.error(converted_outputs)
            except Exception as e:
                message = "Unable to set outputs on `{}`".format(panel.name)
                self.logger.exception(message)
                panel.is_shutdown = True

                # TODO: Check for new events in manager
                # Manager event functions can get called any time
                # As a special case, use function to set a new_event flag in driver
                # Check it here and break if panel failed
                # Only on panel failures b/c only ultra case leading to high latency..
                # at 5 seconds per failed panel (due to retry)
                # In a grid like SMHC, 25 panels * 5 seconds is grueling...
                # How to clear flag?...event handler process funcs all remove it

        # Check at least one panel is still active
        active_panels = [panel for panel in self.panels if not panel.is_shutdown]
        self.num_active_panels = len(active_panels)
        if self.num_active_panels < 1:
            message = "failed when setting outputs"
            raise exceptions.NoActivePanelsError(message=message, logger=self.logger)

    def set_output(self, channel_name: str, par_setpoint: float) -> None:
        """Sets output on light panels. Converts channel name to channel number,
        translates par setpoint to dac setpoint, then sets dac."""
        self.logger.debug("Setting ch {}: {}".format(channel_name, par_setpoint))

        # Check at least one panel is active
        active_panels = [panel for panel in self.panels if not panel.is_shutdown]
        if len(active_panels) < 1:
            raise exceptions.NoActivePanelsError(logger=self.logger)
        message = "Setting output on {} active panels".format(self.num_active_panels)
        self.logger.debug(message)

        # Convert channel name to channel number
        try:
            channel_number = self.get_channel_number(channel_name)
        except Exception as e:
            raise exceptions.SetOutputError(logger=self.logger) from e

        # Set output on all panels
        for panel in self.panels:

            # Scale setpoint
            # dac_setpoint = self.translate_setpoint(par_setpoint) # METHOD REMOVING
            dac_setpoint = par_setpoint

            # Set output on panel
            try:
                panel.driver.write_output(dac_setpoint, pin=channel_number)  # type: ignore
            except AttributeError as e:
                message = "Unable to set output on `{}`".format(panel.name)
                self.logger.error(message + ", panel not initialized ")
                self.logger.error(e)
            except Exception as e:
                message = "Unable to set output on `{}`".format(panel.name)
                self.logger.exception(message)
                panel.is_shutdown = True

        # Check at least one panel is still active
        active_panels = [panel for panel in self.panels if not panel.is_shutdown]
        self.num_active_panels = len(active_panels)
        if self.num_active_panels < 1:
            message = "failed when setting output"
            raise exceptions.NoActivePanelsError(message=message, logger=self.logger)

    def get_channel_number(self, channel_name: str) -> int:
        """Gets channel number from channel name."""
        try:
            channel_dict = self.channels[channel_name]  # type: ignore
            channel_number = channel_dict.get("port", -1)
            return int(channel_number)
        except KeyError:
            raise exceptions.InvalidChannelNameError(
                message=channel_name, logger=self.logger
            )

    def build_channel_outputs(self, value: float) -> Dict[str, float]:
        """Build channel outputs. Sets each channel to provided value."""
        self.logger.debug("Building channel outputs")
        channel_outputs = {}
        for key in self.channels.keys():  # type: ignore
            channel_outputs[key] = value
        self.logger.debug("channel outputs = {}".format(channel_outputs))
        return channel_outputs

    def translate_setpoints(self, par_setpoints: Dict) -> Dict:
        """Translates par setpoints to dac setpoints."""
        self.logger.debug("Translating setpoints")

        # Build interpolation lists
        dac_list = []
        par_list = []
        for dac_percent, par_percent in self.dac_map.items():  # type: ignore
            dac_list.append(float(dac_percent))
            par_list.append(float(par_percent))

        self.logger.debug("dac_list = {}".format(dac_list))
        self.logger.debug("par_list = {}".format(par_list))

        # Get dac setpoints
        dac_setpoints = {}
        for key, par_setpoint in par_setpoints.items():
            dac_setpoint = maths.interpolate(par_list, dac_list, par_setpoint)
            self.logger.info(dac_setpoint)
            dac_setpoint = dac_setpoint * 255 / 100 # scale to Arduino 0-255
            dac_setpoints[key] = dac_setpoint

        # Successfully translated dac setpoints
        self.logger.debug(
            "Translated setpoints from {} to {}".format(par_setpoints, dac_setpoints)
        )
        return dac_setpoints

    def translate_setpoint(self, par_setpoint: float) -> float:
        """Translates par setpoint to dac setpoint."""

        # Build interpolation lists
        dac_list = []
        par_list = []
        for dac_percent, par_percent in self.dac_map.items():  # type: ignore
            dac_list.append(float(dac_percent))
            par_list.append(float(par_percent))

        # Get dac setpint
        dac_setpoint = maths.interpolate(par_list, dac_list, par_setpoint)
        dac_setpoint = dac_setpoint * 255 / 100 # scale to Arduino 0-255

        # Successfully translated dac setpoint
        return dac_setpoint  # type: ignore


    ###### METHOD HELPER FUNCTION 

    def map_to_255(value) -> float:
        return (value * 255 / 100)
    