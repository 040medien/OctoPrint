__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Copyright (C) 2018 The OctoPrint Project - Released under terms of the AGPLv3 License"

import collections
import copy
import logging
import queue
import threading
import time

from octoprint.comm.job import (
    CopyJobMixin,
    LocalGcodeFilePrintjob,
    LocalGcodeStreamjob,
    SDFilePrintjob,
)
from octoprint.comm.protocol import (
    FanControlProtocolMixin,
    Fdm3dPrinterProtocolMixin,
    FileStreamingProtocolMixin,
    MotorControlProtocolMixin,
    Protocol,
    ProtocolState,
)
from octoprint.comm.protocol.reprap.commands import Command, to_command
from octoprint.comm.protocol.reprap.commands.atcommand import AtCommand
from octoprint.comm.protocol.reprap.commands.gcode import GcodeCommand
from octoprint.comm.protocol.reprap.flavors import (
    GenericFlavor,
    all_flavors,
    lookup_flavor,
)
from octoprint.comm.protocol.reprap.scripts import GcodeScript
from octoprint.comm.protocol.reprap.util import (
    LineHistory,
    PositionRecord,
    SendToken,
    TemperatureRecord,
    normalize_command_handler_result,
)
from octoprint.comm.protocol.reprap.util.queues import (
    CommandQueue,
    JobQueue,
    QueueMarker,
    SendQueue,
    SendQueueMarker,
)
from octoprint.comm.scripts import InvalidScript, UnknownScript
from octoprint.comm.transport import (
    EofTransportException,
    LineAwareTransportWrapper,
    PushingTransportWrapper,
    PushingTransportWrapperListener,
    TimeoutTransportException,
    Transport,
    TransportListener,
)
from octoprint.comm.util.parameters import (
    BooleanType,
    FloatType,
    GroupChoiceType,
    IntegerType,
    ParamGroup,
    SmallChoiceType,
    SmallListType,
    Value,
)
from octoprint.events import Events
from octoprint.util import ResettableTimer, TypeAlreadyInQueue
from octoprint.util import dummy_gettext as gettext
from octoprint.util import protectedkeydict, to_bytes, to_unicode

GCODE_TO_EVENT = {
    # pause for user input
    "M226": Events.WAITING,
    "M0": Events.WAITING,
    "M1": Events.WAITING,
    # dwell command
    "G4": Events.DWELL,
    # part cooler
    "M245": Events.COOLING,
    # part conveyor
    "M240": Events.CONVEYOR,
    # part ejector
    "M40": Events.EJECT,
    # user alert
    "M300": Events.ALERT,
    # home print head
    "G28": Events.HOME,
    # emergency stop
    "M112": Events.E_STOP,
    # motors on/off
    "M80": Events.POWER_ON,
    "M81": Events.POWER_OFF,
}

CAPABILITY_AUTOREPORT_TEMP = "AUTOREPORT_TEMP"
CAPABILITY_AUTOREPORT_SD_STATUS = "AUTOREPORT_SD_STATUS"
CAPABILITY_BUSY_PROTOCOL = "BUSY_PROTOCOL"
CAPABILITY_EMERGENCY_PARSER = "EMERGENCY_PARSER"
CAPABILITY_CHAMBER_TEMP = "CHAMBER_TEMPERATURE"


class FallbackValue(object):
    def __init__(self, value, fallback=None, test=None):
        self.value = value
        self.fallback = fallback

        self.test = test
        if self.test is None:
            self.test = lambda x: x is not None

    @property
    def effective(self):
        if self.test(self.value):
            return self.value
        return self.fallback


class BooleanCallbackValue(object):
    def __init__(self, callback):
        assert callable(callback)
        self.callback = callback

    def __nonzero__(self):
        return self.__bool__()

    def __bool__(self):
        return bool(self.callback())


class ReprapGcodeProtocol(
    Protocol,
    Fdm3dPrinterProtocolMixin,
    MotorControlProtocolMixin,
    FanControlProtocolMixin,
    FileStreamingProtocolMixin,
    PushingTransportWrapperListener,
    TransportListener,
):
    name = "Reprap GCODE Protocol"
    key = "reprap"

    supported_jobs = [LocalGcodeFilePrintjob, LocalGcodeStreamjob, SDFilePrintjob]

    @classmethod
    def get_connection_options(cls):
        return [
            GroupChoiceType(
                "flavor",
                "Firmware Flavor",
                sorted(
                    [Value(f.key, title=f.name) for f in all_flavors()],
                    key=lambda x: x.title,
                ),
                ParamGroup(
                    "flavor_overrides",
                    "Flavor Overrides",
                    [
                        SmallChoiceType(
                            "send_checksum",
                            gettext("Send a checksum with the command"),
                            [
                                Value("printing", title=gettext("When printing")),
                                Value("always", title=gettext("Always")),
                                Value("never", title=gettext("Never")),
                            ],
                        ),
                        SmallChoiceType(
                            "trigger_ok_after_resend",
                            gettext("Trigger ok after resend"),
                            [
                                Value(
                                    "detect", title=gettext("If detected as necessary")
                                ),
                                Value("always", title=gettext("Always")),
                                Value("never", title=gettext("Never")),
                            ],
                            expert=True,
                            help=gettext(
                                'Some Marlin forks lack an acknowledging `ok` with their resend requests. Set this to "Always" or "If detected as necessary" if you run into communication stalls on resend requests.'
                            ),
                        ),
                        BooleanType(
                            "unknown_requires_ack",
                            gettext("Unknown commands require `ok`"),
                        ),
                        BooleanType(
                            "unknown_with_checksum",
                            gettext("Send unknown commands with line number & checksum"),
                        ),
                        BooleanType(
                            "detect_external_heatups",
                            gettext(
                                "Try to detect externally triggered blocking heatups"
                            ),
                        ),
                        BooleanType(
                            "block_while_dwelling",
                            gettext(
                                "Block all communication while a dwell command (`G4`) is active"
                            ),
                        ),
                        BooleanType(
                            "sd_relative_path",
                            gettext("Select SD files by relative path"),
                        ),
                        BooleanType(
                            "sd_always_available",
                            gettext("Always assume that the SD card is present"),
                        ),
                        BooleanType(
                            "heatup_abortable",
                            gettext(
                                "Allow cancelling blocking heatups if the firmware supports it"
                            ),
                        ),
                        SmallListType(
                            "blocked_commands",
                            gettext("Blocked commands"),
                            help=gettext(
                                "Use this to specify commands that should never be sent to the printer. Just the G or M code, comma separated. Defaults to `M0` and `M1` since most firmware will block until a button on the controller has been pressed if it receives either of those two commands."
                            ),
                        ),
                        SmallListType(
                            "checksum_requiring_commands",
                            gettext("Checksum requiring commands"),
                            help=gettext(""),
                        ),
                        SmallListType(
                            "long_running_commands",
                            gettext("Long running commands"),
                            help=gettext(""),
                        ),
                        SmallListType(
                            "asynchronous_commands",
                            gettext("Asynchronous commands"),
                            help=gettext(""),
                            expert=True,
                        ),
                        SmallListType(
                            "pausing_commands",
                            gettext("Pausing commands"),
                            help=gettext(""),
                            expert=True,
                        ),
                        SmallListType(
                            "emergency_commands",
                            gettext("Emergency commands"),
                            help=gettext(""),
                            expert=True,
                        ),
                    ],
                    help=gettext(
                        "You can override the firmware flavor default settings here. You usually should *not* need to do that. Only change anything if the defaults don't work for your printer."
                    ),
                    advanced=True,
                ),
                defaults=dict((f.key, f.overridable()) for f in all_flavors()),
                default="generic",
            ),
            ParamGroup(
                "error_handling",
                gettext("Error handling"),
                [
                    SmallChoiceType(
                        "firmware_errors",
                        gettext("What to do on a firmware error (`Error:` or `!!`)"),
                        [
                            Value(
                                "disconnect", title=gettext("Disconnect from the printer")
                            ),
                            Value(
                                "cancel",
                                title=gettext(
                                    "Cancel any ongoing prints but stay connected to the printer"
                                ),
                                help=gettext(
                                    "Please note that if you choose this, OctoPrint will still disconnect from the printer in  case of *fatal* errors reported by your firmware (e.g. `kill() called`, `fatal:`)"
                                ),
                            ),
                            Value(
                                "ignore",
                                title=gettext("Ignore"),
                                warning=True,
                                help=gettext(
                                    "Only choose this if your firmware sends error messages that are not actual errors. Might mask printer issues (even fatal errors!), be careful!"
                                ),
                            ),
                        ],
                        default="disconnect",
                    ),
                    # TODO: implement send_m112 flag
                    BooleanType(
                        "send_m112",
                        gettext("Send `M112` on disconnect due to error"),
                        default=True,
                        warning=False,
                        help=gettext(
                            "If this is checked, OctoPrint will try to send an `M112` prior to a disconnect due to a (fatal) error in order to disable heaters and motors."
                        ),
                    ),
                ],
                advanced=True,
            ),
            ParamGroup(
                "pausing",
                gettext("Pausing"),
                [
                    BooleanType(
                        "log_position_on_pause",
                        gettext("Log position on pause"),
                        default=True,
                        warning=True,
                        help=gettext(
                            "If you disable this, the `pause_position` placeholders in your pause/resume GCODE scripts will stay unpopulated! However, pausing speed might improve slightly."
                        ),
                    )
                ],
                advanced=True,
            ),
            ParamGroup(
                "cancelling",
                gettext("Cancelling"),
                [
                    BooleanType(
                        "log_position_on_cancel",
                        gettext("Log position on cancel"),
                        default=True,
                        warning=True,
                        help=gettext(
                            "If you disable this, the `cancel_position` placeholders in your cancel GCODE script and the corresponding data in the print recovery data will stay unpopulated! However, cancelling speed might improve slightly."
                        ),
                    )
                ],
                advanced=True,
            ),
            ParamGroup(
                "intervals",
                gettext("Query intervals"),
                [
                    FloatType(
                        "temperature_interval_idle",
                        gettext("Temperature (idle)"),
                        default=5,
                        unit="sec",
                        help=gettext("When printing or idle"),
                    ),
                    FloatType(
                        "temperature_interval_target",
                        gettext("Temperature (target set)"),
                        default=2,
                        unit="sec",
                        help=gettext("When idle and a target temperature is set"),
                    ),
                    FloatType(
                        "temperature_interval_auto",
                        gettext("Temperature (autoreport)"),
                        default=2,
                        unit="sec",
                        help=gettext("Autoreport interval to request from the firmware"),
                    ),
                    FloatType(
                        "sdstatus_interval_normal",
                        gettext("SD Status"),
                        default=1,
                        unit="sec",
                        help=gettext("When printing from the printer's SD"),
                    ),
                    FloatType(
                        "sdstatus_interval_auto",
                        gettext("SD status (autoreport)"),
                        default=1,
                        unit="sec",
                        help=gettext("Autoreport interval to request from the firmware"),
                    ),
                ],
                advanced=True,
            ),
            ParamGroup(
                "timeouts",
                gettext("Timeouts"),
                [
                    FloatType(
                        "communication_timeout_normal",
                        gettext("Communication"),
                        default=30,
                        unit="sec",
                        help=gettext("`busy` protocol support not detected"),
                    ),
                    FloatType(
                        "communication_timeout_busy",
                        gettext("Communication (busy)"),
                        default=3,
                        unit="sec",
                        help=gettext("`busy` protocol support detected"),
                    ),
                    FloatType(
                        "connection_timeout",
                        gettext("Connection timeout"),
                        default=10,
                        unit="sec",
                    ),
                    FloatType(
                        "detection_timeout",
                        gettext("Autodetection"),
                        default=1,
                        unit="sec",
                        help="",
                    ),
                    FloatType(
                        "position_log_wait",
                        gettext("Position query"),
                        default=10,
                        unit="sec",
                    ),
                    FloatType(
                        "baudrate_detection_pause",
                        gettext("Initial baudrate detection pause"),
                        expert=True,
                        default=1.0,
                        unit="sec",
                        help=gettext(
                            "Some controllers need a bit of a breather before baudrate detection after initial connect. If baudrate detection fails due to a hanging controller, try increasing this value."
                        ),
                    ),
                    FloatType(
                        "resendok_timeout",
                        gettext("Timeout for `ok` after resend detection"),
                        expert=True,
                        default=0.5,
                        unit="sec",
                        help=gettext(
                            "Some firmwares do not send an `ok` after a resend request. This timeout configures how long OctoPrint will wait for one until it assumes this particular bug to be present and generate its own."
                        ),
                    ),
                    IntegerType(
                        "max_consecutive_timeouts_idle",
                        gettext("Max. consecutive timeouts while idle"),
                        expert=True,
                        default=2,
                        help=gettext(
                            "Set to 0 to disable consecutive timeout detection and handling."
                        ),
                    ),
                    IntegerType(
                        "max_consecutive_timeouts_printing",
                        gettext("Max. consecutive timeouts while printing"),
                        expert=True,
                        default=5,
                        help=gettext(
                            "Set to 0 to disable consecutive timeout detection and handling."
                        ),
                    ),
                    IntegerType(
                        "max_consecutive_timeouts_long",
                        gettext("Max. consecutive timeouts during long running commands"),
                        expert=True,
                        default=5,
                        help=gettext(
                            "Set to 0 to disable consecutive timeout detection and handling."
                        ),
                    ),
                ],
                advanced=True,
            ),
            ParamGroup(
                "capabilities",
                gettext("Firmware capability support"),
                [
                    BooleanType(
                        "autoreport_temp",
                        gettext("Enable temperature autoreporting if supported"),
                        default=True,
                        help=gettext(
                            "Uncheck this if you want to disable temperature autoreporting, even if the firmware supports it"
                        ),
                    ),
                    BooleanType(
                        "autoreport_sd_status",
                        gettext("Enable sd status autoreporting if supported"),
                        default=True,
                        help=gettext(
                            "Uncheck this if you want to disable sd status autoreporting, even if the firmware supports it"
                        ),
                    ),
                    BooleanType(
                        "emergency_parser",
                        gettext("Use emergency parser if supported"),
                        default=True,
                        help=gettext(
                            "Uncheck this if you want to disable force-sending emergency commands to compatible printers, even if the firmware supports it"
                        ),
                    ),
                    BooleanType(
                        "busy_protocol",
                        gettext("Use `busy` protocol if supported"),
                        default=True,
                        help=gettext(
                            "Uncheck this if you want to disable use of the `busy` protocol, even if the firmware supports it"
                        ),
                    ),
                    BooleanType(
                        "chamber_temp",
                        gettext("Enable chamber temperature management if supported"),
                        default=True,
                        help=gettext(
                            "Uncheck this if you want to disable chamber temperature management, even if the firmware supports it"
                        ),
                    ),
                ],
                advanced=True,
            ),
        ]

    @staticmethod
    def get_flavor_attributes_starting_with(flavor, prefix):
        return [x for x in dir(flavor) if x.startswith(prefix)]

    @classmethod
    def get_attributes_starting_with(cls, prefix):
        return dict((x, getattr(cls, x)) for x in dir(cls) if x.startswith(prefix))

    def __init__(self, flavor=None, *args, **kwargs):
        super(ReprapGcodeProtocol, self).__init__(*args, **kwargs)

        self.flavor = lookup_flavor(flavor)
        if self.flavor is None:
            self.flavor = GenericFlavor

        self.flavor_overrides = kwargs.get("flavor_overrides", {})
        self.flavor = self.flavor.with_overrides(self.flavor_overrides)

        self.set_current_args(
            flavor=self.flavor.key, flavor_overrides=self.flavor_overrides
        )

        self._logger = logging.getLogger(__name__)
        self._commdebug_logger = logging.getLogger("COMMDEBUG")
        self._phase_logger = logging.getLogger(__name__ + ".command_phases")

        self._terminal_log = collections.deque([], 20)

        self.timeouts = {
            "connection": kwargs.get("timeouts", {}).get("connection_timeout", 30.0),
            "communication": kwargs.get("timeouts", {}).get(
                "communication_timeout_normal", 10.0
            ),
            "communication_busy": kwargs.get("timeouts", {}).get(
                "communication_timeout_busy", 2.0
            ),
        }
        self.interval = {
            "temperature_idle": kwargs.get("intervals", {}).get(
                "temperature_interval_idle", 5.0
            ),
            "temperature_printing": kwargs.get("intervals", {}).get(
                "temperature_interval_printing", 5.0
            ),
            "temperature_autoreport": kwargs.get("intervals", {}).get(
                "temperature_interval_autoreport", 2.0
            ),
            "sd_status": kwargs.get("intervals", {}).get(
                "sd_status_interval_normal", 2.0
            ),
            "sd_status_autoreport": kwargs.get("intervals", {}).get(
                "sd_status_interval_auto", 1.0
            ),
        }
        self.max_consecutive_timeouts = {
            "idle": kwargs.get("timeouts", {}).get("max_consecutive_idle", 2),
            "printing": kwargs.get("timeouts", {}).get("max_consecutive_printing", 5),
            "long": kwargs.get("timeouts", {}).get("max_consecutive_long", 5),
        }

        self.firmware_errors = kwargs.get("error_handling", {}).get(
            "firmware_errors", "disconnect"
        )

        flavor_comm_attrs = self.get_flavor_attributes_starting_with(self.flavor, "comm_")
        flavor_message_attrs = self.get_flavor_attributes_starting_with(
            self.flavor, "message_"
        )
        flavor_error_attrs = self.get_flavor_attributes_starting_with(
            self.flavor, "error_"
        )

        self._comm_messages = flavor_comm_attrs
        self._registered_messages = flavor_comm_attrs + flavor_message_attrs
        self._current_registered_messages = self._registered_messages
        self._error_messages = flavor_error_attrs

        self._handlers_gcode = self.get_attributes_starting_with("_gcode_")
        self._handlers_atcommand = self.get_attributes_starting_with("_atcommand_")
        self._handlers_command_phase = self.get_attributes_starting_with(
            "_command_phase_"
        )

        self._capability_support = {}
        self._capability_support[CAPABILITY_AUTOREPORT_TEMP] = kwargs.get(
            "capabilities", {}
        ).get("autoreport_temp", True)
        self._capability_support[CAPABILITY_AUTOREPORT_SD_STATUS] = kwargs.get(
            "capabilities", {}
        ).get("autoreport_sd_status", True)
        self._capability_support[CAPABILITY_EMERGENCY_PARSER] = kwargs.get(
            "capabilities", {}
        ).get("emergency_parser", True)
        self._capability_support[CAPABILITY_BUSY_PROTOCOL] = kwargs.get(
            "capabilities", {}
        ).get("busy_protocol", True)
        self._capability_support[CAPABILITY_CHAMBER_TEMP] = kwargs.get(
            "capabilities", {}
        ).get("chamber_temp", True)

        self._transport = None

        self._internal_flags = {
            "temperatures": TemperatureRecord(),
            "heating_start": None,
            "heating_lost": 0,
            "heating": False,
            "temperature_autoreporting": False,
            "temperature_offsets": {},
            "current_tool": 0,
            "former_tool": 0,
            "current_f": None,
            "current_z": None,
            "sd_available": False,
            "sd_files": [],
            "sd_files_temp": None,
            "sd_status_autoreporting": False,
            "resend_requested": None,
            "resend_linenumber": None,
            "resend_count": None,
            "long_running_command": False,
            "only_from_job": BooleanCallbackValue(
                lambda: self.state in ProtocolState.PROCESSING_STATES
                and self._job
                and self._job.exclusive
            ),
            "trigger_events": BooleanCallbackValue(
                lambda: not (
                    self.state in ProtocolState.PROCESSING_STATES
                    and self._job
                    and self._job.exclusive
                )
            ),
            "expect_continous_comms": BooleanCallbackValue(
                lambda: self.state in ProtocolState.PROCESSING_STATES
                and self._job
                and not self._job.parallel
            ),
            "ignore_ok": 0,
            "job_on_hold": BooleanCallbackValue(lambda: self.job_on_hold),
            "timeout": None,
            "timeout_consecutive": 0,
            "ok_timeout": None,
            "dwelling_until": None,
            "firmware_identified": False,
            "firmware_name": None,
            "firmware_info": {},
            "firmware_capabilities": {},
            "firmware_capability_support": {},
            "busy_detected": False,
            "multiline_error": False,
        }
        self._protected_flags = protectedkeydict(self._internal_flags)

        self._job_queue = JobQueue()
        self._command_queue = CommandQueue()
        self._send_queue = SendQueue()
        self._clear_to_send = SendToken(max=10, name="protocol.clear_to_send")

        self._current_linenumber = 1
        self._last_lines = LineHistory(max=50)
        self._last_communication_error = None

        # last known temperatures
        self.pause_temperature = TemperatureRecord()
        self.cancel_temperature = TemperatureRecord()

        # last known positions
        self.last_position = PositionRecord()
        self.pause_position = PositionRecord()
        self.cancel_position = PositionRecord()

        # pause and cancel processing
        self._record_pause_data = False
        self._record_cancel_data = False

        # hooks
        self._gcode_hooks = {
            "queuing": self._plugin_manager.get_hooks(
                "octoprint.comm.protocol.gcode.queuing"
            ),
            "queued": self._plugin_manager.get_hooks(
                "octoprint.comm.protocol.gcode.queued"
            ),
            "sending": self._plugin_manager.get_hooks(
                "octoprint.comm.protocol.gcode.sending"
            ),
            "sent": self._plugin_manager.get_hooks("octoprint.comm.protocol.gcode.sent"),
            "receiving": self._plugin_manager.get_hooks(
                "octoprint.comm.protocol.gcode.receiving"
            ),
        }

        self._error_hooks = self._plugin_manager.get_hooks(
            "octoprint.comm.protocol.gcode.error"
        )

        self._atcommand_hooks = (
            {
                "queuing": self._plugin_manager.get_hooks(
                    "octoprint.comm.protocol.atcommand.queuing"
                ),
                "sending": self._plugin_manager.get_hooks(
                    "octoprint.comm.protocol.atcommand.sending"
                ),
            },
        )

        self._action_hooks = self._plugin_manager.get_hooks(
            "octoprint.comm.protocol.action"
        )

        self._script_hooks = self._plugin_manager.get_hooks(
            "octoprint.comm.protocol.scripts"
        )

        self._temperature_hooks = self._plugin_manager.get_hooks(
            "octoprint.comm.protocol.temperatures.received"
        )

        self._firmware_info_hooks = self._plugin_manager.get_hooks(
            "octoprint.comm.firmware.info"
        )

        self._firmware_cap_hooks = self._plugin_manager.get_hooks(
            "octoprint.comm.firmware.capabilities"
        )

        # polling
        self._temperature_poller = None
        self._sd_status_poller = None

        # script processing
        self._suppress_scripts = set()

        # user logging
        self._action_users = {}

        # resend logging
        self._log_resends = True
        self._log_resends_rate_start = None
        self._log_resends_rate_count = 0
        self._log_resends_rate_frame = 60
        self._log_resends_max = 5

        # mutexes
        self._line_mutex = threading.RLock()
        self._send_queue_mutex = threading.RLock()
        self._pause_mutex = threading.RLock()
        self._cancel_mutex = threading.RLock()
        self._suppress_scripts_mutex = threading.RLock()
        self._action_users_mutex = threading.RLock()

        # timers
        self._pause_position_timer = None
        self._cancel_position_timer = None
        self._resend_ok_timer = None

        # sending thread
        self._send_queue_active = False
        self._sending_thread = None

    @property
    def _active(self):
        return (
            self._transport is not None
            and self._transport.active
            and self._send_queue_active
        )

    def connect(self, transport, transport_args=None, transport_kwargs=None):
        if not isinstance(transport, Transport):
            raise ValueError(
                "transport must be a Transport subclass but is a {} instead".format(
                    type(transport)
                )
            )

        self._internal_flags["timeout"] = self._get_timeout("connection")

        transport = PushingTransportWrapper(
            LineAwareTransportWrapper(transport), timeout=5.0
        )

        self._send_queue_active = True
        self._sending_thread = threading.Thread(
            target=self._send_loop, name="comm.sending_thread"
        )
        self._sending_thread.daemon = True
        self._sending_thread.start()

        super(ReprapGcodeProtocol, self).connect(
            transport, transport_args=transport_args, transport_kwargs=transport_kwargs
        )

    # cancel handling

    def _cancel_preparation_failed(self):
        self.process_protocol_log(
            "Did not receive parseable position data from printer within {}s, "
            "continuing without it".format(self.timeouts.get("position_log_wait", 10.0))
        )
        self._cancel_preparation_done()

    def _cancel_preparation_done(
        self, check_timer=True, suppress_script=False, user=None
    ):
        if user is None:
            with self._action_users_mutex:
                try:
                    user = self._action_users.pop("cancel")
                except KeyError:
                    pass

        with self._cancel_mutex:
            if self._cancel_position_timer is not None:
                self._cancel_position_timer.cancel()
                self._cancel_position_timer = None
            elif check_timer:
                return

            # TODO: recovery data
            self.notify_listeners(
                "on_protocol_job_cancelled",
                self,
                self._job,
                position=self.cancel_position.as_dict(),
                suppress_script=suppress_script,
                user=user,
            )

            def finalize():
                self.state = ProtocolState.CONNECTED

            self.send_commands(SendQueueMarker(finalize), part_of_job=True)
            self._continue_sending()

    def cancel_processing(self, error=None, user=None, tags=None, log_position=True):
        if self.state not in ProtocolState.PROCESSING_STATES + (ProtocolState.PAUSED,):
            return

        if self._job is None:
            return

        if tags is None:
            tags = set()

        def on_move_finish_requested():
            self._record_cancel_data = True

            with self._cancel_mutex:
                if self._cancel_position_timer is not None:
                    self._cancel_position_timer.cancel()
                self._cancel_position_timer = ResettableTimer(
                    self.timeouts.get("position_log_wait", 10.0),
                    self._cancel_preparation_failed,
                )
                self._cancel_position_timer.start()

            self.send_commands(
                self.flavor.command_get_position(),
                part_of_job=True,
                tags=tags | {"trigger:comm.cancel", "trigger:record_position"},
            )
            self._continue_sending()

        # cancel the job
        self.state = ProtocolState.CANCELLING

        if self.flavor.heatup_abortable:
            self.send_commands(
                self.flavor.command_abort_heatup(),
                part_of_job=True,
                tags=tags | {"trigger:comm.cancel", "trigger:abort_heatup"},
            )

        self._job.cancel(error=error, user=user, tags=tags)
        self.notify_listeners(
            "on_protocol_job_cancelling",
            self,
            self._job,
            error=error,
            user=user,
            tags=tags,
        )

        if log_position and not isinstance(self._job, CopyJobMixin):
            with self._action_users_mutex:
                self._action_users["cancel"] = user

            self.send_commands(
                self.flavor.command_finish_moving(),
                part_of_job=True,
                on_sent=on_move_finish_requested,
                tags=tags | {"trigger:comm.cancel", "trigger:record_position"},
            )
            self._continue_sending()
        else:
            self._cancel_preparation_done(check_timer=False, user=user)

    # pause handling

    def _pause_preparation_failed(self):
        self.process_protocol_log(
            "Did not receive parseable position data from printer within {}s, "
            "continuing without it".format(self.timeouts.get("position_log_wait", 10.0))
        )
        self._pause_preparation_done()

    def _pause_preparation_done(self, check_timer=True, suppress_script=False, user=None):
        if user is None:
            with self._action_users_mutex:
                try:
                    user = self._action_users.pop("pause")
                except KeyError:
                    pass

        # do we need to stop a timer?
        with self._pause_mutex:
            if self._pause_position_timer is not None:
                self._pause_position_timer.cancel()
                self._pause_position_timer = None
            elif check_timer:
                return

            self.notify_listeners(
                "on_protocol_job_paused",
                self,
                self._job,
                position=self.pause_position.as_dict(),
                suppress_script=suppress_script,
                user=user,
            )

            # wait for current commands to be sent, then switch to paused state
            def finalize():
                # only switch to PAUSED if we were still PAUSING, to avoid "swallowing" received resumes during
                # pausing
                if self.state == ProtocolState.PAUSING:
                    self.state = ProtocolState.PAUSED

            self.send_commands(SendQueueMarker(finalize), part_of_job=True)
            self._continue_sending()

    def pause_processing(
        self, user=None, tags=None, log_position=True, local_handling=True
    ):
        if self._job is None or self.state not in (
            ProtocolState.PROCESSING,
            ProtocolState.STARTING,
            ProtocolState.RESUMING,
        ):
            return

        if isinstance(self._job, CopyJobMixin):
            return

        if tags is None:
            tags = set()

        self.state = ProtocolState.PAUSING
        self._job.pause(local_handling=local_handling, user=user, tags=tags)
        self.notify_listeners(
            "on_protocol_job_pausing",
            self,
            self._job,
            local_handling=local_handling,
            user=user,
            tags=tags,
        )

        def on_move_finish_requested():
            self._record_pause_data = True

            with self._pause_mutex:
                if self._pause_position_timer is not None:
                    self._pause_position_timer.cancel()
                    self._pause_position_timer = None
                self._pause_position_timer = ResettableTimer(
                    self.timeouts.get("position_log_wait", 10.0),
                    self._pause_preparation_failed,
                )
                self._pause_position_timer.start()

            self.send_commands(
                self.flavor.command_get_position(),
                part_of_job=True,
                tags=tags
                | {"trigger:comm.set_pause", "trigger:pause", "trigger:record_position"},
            )
            self._continue_sending()

        if log_position and local_handling:
            with self._action_users_mutex:
                self._action_users["pause"] = user

            self.send_commands(
                self.flavor.command_finish_moving(),
                part_of_job=True,
                on_sent=on_move_finish_requested,
                tags=tags
                | {"trigger:comm.set_pause", "trigger:pause", "trigger:record_position"},
            )
            self._continue_sending()
        else:
            self._pause_preparation_done(
                check_timer=False, suppress_script=not local_handling, user=user
            )

    def move(
        self,
        x=None,
        y=None,
        z=None,
        e=None,
        feedrate=None,
        relative=False,
        *args,
        **kwargs
    ):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.move"}

        cmds = [self.flavor.command_move(x=x, y=y, z=z, e=e, f=feedrate)]

        if relative:
            cmds = (
                [self.flavor.command_set_relative_positioning()]
                + cmds
                + [self.flavor.command_set_absolute_positioning()]
            )

        self.send_commands(*cmds, tags=tags, **kwargs)

    def home(self, x=False, y=False, z=False, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.home"}
        self.send_commands(self.flavor.command_home(x=x, y=y, z=z), tags=tags, **kwargs)

    def change_tool(self, tool, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.change_tool"}
        self.send_commands(self.flavor.command_set_tool(tool), tags=tags, **kwargs)

    def set_feedrate_multiplier(self, multiplier, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.set_feedrate_multiplier"}
        self.send_commands(
            self.flavor.command_set_feedrate_multiplier(multiplier), tags=tags, **kwargs
        )

    def set_extrusion_multiplier(self, multiplier, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.set_extrusion_multiplier"}
        self.send_commands(
            self.flavor.command_set_extrusion_multiplier(multiplier), tags=tags, **kwargs
        )

    def set_temperature(self, heater, temperature, wait=False, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.set_temperature"}

        if heater.startswith("tool"):
            try:
                tool = int(heater[len("tool") :])
            except ValueError:
                self._logger.error(
                    "Got invalid tool heater, expected tool<n>, got: {}".format(heater)
                )
                return
            command = self.flavor.command_set_extruder_temp(
                temperature, tool=tool, wait=wait
            )
        elif heater == "bed":
            command = self.flavor.command_set_bed_temp(temperature, wait=wait)
        elif heater == "chamber":
            command = self.flavor.command_set_chamber_temp(temperature, wait=wait)
        else:
            self._logger.warn(
                "Got unknown heater identifier to set temperature on: {}".format(heater)
            )
            return

        self.send_commands(command, tags=tags, **kwargs)

    def set_temperature_offset(self, heater, offset, *args, **kwargs):
        self._internal_flags["temperature_offsets"][heater] = offset

    def get_temperature_offsets(self):
        return copy.copy(self._internal_flags["temperature_offsets"])

    ##~~ MotorControlProtocolMixin

    def set_motor_state(self, enabled, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.set_motor_state"}
        self.send_commands(self.flavor.command_set_motors(enabled), tags=tags, **kwargs)

    ##~~ FanControlProtocolMixin

    def set_fan_speed(self, speed, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.set_fan_speed"}
        self.send_commands(self.flavor.command_set_fan_speed(speed), tags=tags, **kwargs)

    ##~~ FileStreamingProtocolMixin

    def init_file_storage(self, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.init_file_storage"}
        self.send_commands(self.flavor.command_sd_init(), tags=tags, **kwargs)

    def list_files(self, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.list_files"}
        self.send_commands(self.flavor.command_sd_refresh(), tags=tags, **kwargs)

    def start_file_print(self, name, position=0, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.start_file_print"}

        self.send_commands(
            self.flavor.command_sd_select_file(name),
            self.flavor.command_sd_set_pos(position),
            self.flavor.command_sd_start(),
            tags=tags,
            **kwargs
        )

    def pause_file_print(self, *args, **kwargs):
        if kwargs.get("local_handling", True):
            tags = kwargs.pop("tags", set()) | {"trigger:protocol.pause_file_print"}
            self.send_commands(self.flavor.command_sd_pause(), tags=tags, **kwargs)

    def resume_file_print(self, *args, **kwargs):
        if kwargs.get("local_handling", True):
            tags = kwargs.pop("tags", set()) | {"trigger:protocol.resume_file_print"}
            self.send_commands(self.flavor.command_sd_resume(), tags=tags, **kwargs)

    def delete_file(self, name, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.delete_file"}
        self.send_commands(self.flavor.command_sd_delete(name), tags=tags, **kwargs)

    def record_file(self, name, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.record_file"}
        self.send_commands(self.flavor.command_sd_begin_write(name), tags=tags, **kwargs)

    def stop_recording_file(self, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.stop_recording_file"}
        self.send_commands(self.flavor.command_sd_end_write(), tags=tags, **kwargs)

    def get_file_print_status(self, *args, **kwargs):
        tags = kwargs.pop("tags", set()) | {"trigger:protocol.get_file_print_status"}
        self.send_commands(self.flavor.command_sd_status(), tags=tags, **kwargs)

    def start_file_print_status_monitor(self):
        if self._internal_flags["sd_status_autoreporting"]:
            return

        from octoprint.util import RepeatedTimer

        def poll_status():
            if self.can_send() and not self._internal_flags["sd_status_autoreporting"]:
                self.get_file_print_status()

        self._sd_status_poller = RepeatedTimer(self.interval["sd_status"], poll_status)
        self._sd_status_poller.start()

    def stop_file_print_status_monitor(self):
        if self._sd_status_poller is not None:
            self._sd_status_poller.cancel()
            self._sd_status_poller = None

    ##~~

    def can_send(self):
        return (
            self._is_operational()
            and not self._internal_flags["long_running_command"]
            and not self._internal_flags["heating"]
            and not self._internal_flags["dwelling_until"]
        )

    def send_commands(self, *commands, **kwargs):
        command_type = kwargs.get("command_type")
        on_sent = kwargs.get("on_sent")
        tags = kwargs.get("tags")
        force = kwargs.get("force", False)
        part_of_job = kwargs.get("part_of_job", False)

        def sanitize_command(c, ct, t):
            if isinstance(c, tuple):
                # unwrap 2-tuples that consist of a command & additional tags
                if len(c) != 2:
                    return None
                c, at = c

                assert isinstance(at, set)
                t |= at

            # noinspection PyCompatibility
            if isinstance(c, Command):
                return c.with_type(ct).with_tags(t)
            elif isinstance(c, QueueMarker):
                return c
            elif isinstance(c, str):
                return to_command(c, type=ct, tags=t)
            else:
                return None

        result = False
        for command in commands:
            sanitized = sanitize_command(command, command_type, tags)
            if sanitized is None:
                continue

            emergency = False
            if (
                isinstance(sanitized, GcodeCommand)
                and sanitized.code in self.flavor.emergency_commands
            ):
                emergency = True

            if part_of_job:
                if isinstance(sanitized, Command):
                    sanitized = sanitized.with_tags({"source:job"})
                self._job_queue.put((sanitized, on_sent))
                result = True

            elif (
                self.state in ProtocolState.PROCESSING_STATES
                and not self._job.parallel
                and not self.job_on_hold
                and not force
            ):
                try:
                    self._command_queue.put((sanitized, on_sent), item_type=command_type)
                    result = True
                except TypeAlreadyInQueue as e:
                    self._logger.debug("Type already in queue: " + e.type)

            elif self._is_operational() or force or emergency:
                result = (
                    self._send_commands(
                        command, on_sent=on_sent, command_type=command_type, tags=tags
                    )
                    or result
                )

        return result

    def send_script(self, script, context=None, *args, **kwargs):
        part_of_job = kwargs.get("part_of_job", False)

        if context is None:
            context = {}

        # noinspection PyCompatibility
        if isinstance(script, str):
            script = GcodeScript(self._settings, script, context=context)

        if not isinstance(script, GcodeScript):
            raise InvalidScript("Can't process this script")

        context.update(
            {
                "printer_profile": self._printer_profile,
                "last_position": self.last_position,
                "last_temperature": self._internal_flags["temperatures"].as_script_dict(),
            }
        )

        if script.name == "afterPrintPaused" or script.name == "beforePrintResumed":
            context.update(
                {
                    "pause_position": self.pause_position,
                    "pause_temperature": self.pause_temperature.as_script_dict(),
                }
            )
        elif script.name == "afterPrintCancelled":
            context.update(
                {
                    "cancel_position": self.cancel_position,
                    "cancel_temperature": self.cancel_temperature.as_script_dict(),
                }
            )

        lines = script.render(context=context)

        # process any hooks
        prefix = []
        postfix = []

        for name, hook in self._script_hooks.items():
            try:
                retval = hook(
                    self, "gcode", script.name
                )  # prefix, postfix [ , variables [ , additional tags ] ]
            except Exception:
                self._logger.exception(
                    "Error while processing hook {}.".format(name),
                    extra={"plugin": name},
                )
            else:
                if retval is None:
                    continue
                if not isinstance(retval, (list, tuple)) or len(retval) not in [2, 3, 4]:
                    continue

                def to_list(data, t):
                    # noinspection PyCompatibility
                    if isinstance(data, str):
                        data = list(x.strip() for x in data.split("\n"))

                    if isinstance(data, (list, tuple)):
                        return list(map(lambda x: (x, t), data))
                    else:
                        return None

                if len(retval) >= 3:
                    # variables are defined
                    variables = retval[2]
                    context.update({"plugins": {name: variables}})

                additional_tags = {"plugin:{}".format(name)}
                if len(retval) == 4:
                    # additional tags are defined
                    additional_tags |= set(retval[3])

                p, s = map(
                    lambda x: to_list(x, additional_tags), retval[0:2]
                )  # convert prefix & postfix
                if p:
                    prefix = list(p) + prefix
                if s:
                    postfix += list(s)

        lines = prefix + lines + postfix
        self.send_commands(
            tags={
                "trigger:protocol.send_script",
                "source:script",
                "script:{}".format(script.name),
            },
            part_of_job=part_of_job,
            *lines
        )

    def repair(self, *args, **kwargs):
        self._on_comm_ok()

    def join(self, timeout=None):
        if timeout is not None:
            stop = time.monotonic() + timeout
            while (
                self._command_queue.unfinished_tasks or self._send_queue.unfinished_tasks
            ) and time.monotonic() < stop:
                time.sleep(0.1)
            else:
                self._command_queue.join()
                self._send_queue.join()

    ##~~ State handling

    def _is_operational(self):
        return self.state not in (
            ProtocolState.DISCONNECTED,
            ProtocolState.DISCONNECTED_WITH_ERROR,
        )

    def _is_busy(self):
        return self.state in (ProtocolState.PROCESSING, ProtocolState.PAUSED)

    def _is_streaming(self):
        return (
            self._is_busy() and self._job and isinstance(self._job, LocalGcodeStreamjob)
        )

    def _on_switching_state_connecting(self, old_state):
        if old_state is not ProtocolState.DISCONNECTED:
            return

        hello = self.flavor.command_hello()
        if hello:
            self._tickle(hello)

    def _on_switching_state_connected(self, old_state):
        if old_state == ProtocolState.CONNECTING:
            self._internal_flags["timeout"] = self._get_timeout("communication")

            self._send_command(self.flavor.command_set_line(0))
            self._send_command(self.flavor.command_get_firmware_info())
            self._clear_to_send.set()

            def poll_temperature_interval():
                printing_default = 4.0
                target_default = 2.0
                threshold = 25

                if self.state in ProtocolState.PROCESSING_STATES:
                    return self.interval.get("temperature_printing", printing_default)

                tools = self._internal_flags["temperatures"].tools
                for temp in [tools[k][1] for k in tools]:
                    if temp > threshold:
                        return self.interval.get("temperature_target_set", target_default)

                bed = self._internal_flags["temperatures"].bed
                if bed and len(bed) > 0 and bed[1] is not None and bed[1] > threshold:
                    return self.interval.get("temperature_target_set", target_default)

                return self.interval.get("temperature_idle")

            def poll_temperature():
                if (
                    self._is_operational()
                    and not self._internal_flags["temperature_autoreporting"]
                    and not self._internal_flags["only_from_job"]
                    and self.can_send()
                ):
                    self._send_command(
                        self.flavor.command_get_temp(),
                        command_type="temperature_poll",
                        tags={"trigger:protocol.poll_temperature"},
                    )

            from octoprint.util import RepeatedTimer

            self._temperature_poller = RepeatedTimer(
                poll_temperature_interval, poll_temperature, run_first=True
            )
            self._temperature_poller.start()

    def _on_switched_state_starting(self, old_state):
        # reset line numbers every time we start a new print job
        self.send_commands(self.flavor.command_set_line(0))

    def _on_switched_state_disconnecting(self, old_state):
        self._handle_disconnecting(old_state, False)

    def _on_switched_state_disconnecting_with_error(self, old_state):
        self._handle_disconnecting(old_state, True)

    def _handle_disconnecting(self, old_state, error):
        if self._temperature_poller is not None:
            self._temperature_poller.cancel()
            self._temperature_poller = None

        if self._sd_status_poller is not None:
            self._sd_status_poller.cancel()
            self._sd_status_poller = None

        if not error:
            try:
                self.send_script("beforePrinterDisconnected")
                stop = time.monotonic() + 10.0  # TODO make somehow configurable
                while (
                    self._command_queue.unfinished_tasks
                    or self._send_queue.unfinished_tasks
                ) and time.monotonic() < stop:
                    time.sleep(0.1)
            except UnknownScript:
                pass
            except Exception:
                self._logger.exception(
                    "Error while trying to send beforePrinterDisconnected script"
                )

        self._send_queue_active = False

    ##~~ Job handling

    def on_job_started(self, job, *args, **kwargs):
        if job != self._job:
            return

        self.notify_listeners("on_protocol_job_started", self, job, *args, **kwargs)

        def finalize():
            self.state = ProtocolState.PROCESSING

        self.send_commands(SendQueueMarker(finalize), part_of_job=True)
        self._continue_sending()

    def on_job_cancelled(self, job, *args, **kwargs):
        if job != self._job:
            return
        self._job_processed(job)

    def on_job_paused(self, job, *args, **kwargs):
        pass

    def on_job_resumed(self, job, *args, **kwargs):
        if job != self._job:
            return

        user = kwargs.get("user")
        tags = kwargs.get("tags")
        suppress_script = not kwargs.get("local_handling", True)

        self.notify_listeners(
            "on_protocol_job_resumed",
            self,
            job,
            suppress_script=suppress_script,
            user=user,
            tags=tags,
        )

        def finalize():
            if self.state == ProtocolState.RESUMING:
                self.state = ProtocolState.PROCESSING

        self.send_commands(SendQueueMarker(finalize), part_of_job=True)
        self._continue_sending()

    def on_job_done(self, job, *args, **kwargs):
        if job != self._job:
            return

        self.state = ProtocolState.FINISHING
        self._job_processed(job)
        self.notify_listeners("on_protocol_job_finishing", self, job, *args, **kwargs)

        def finalize():
            self.state = ProtocolState.CONNECTED
            self.notify_listeners("on_protocol_job_done", self, job, *args, **kwargs)

        self.send_commands(SendQueueMarker(finalize), part_of_job=True)
        self._continue_sending()

    def _job_processed(self, job, *args, **kwargs):
        self._job.unregister_listener(self)

    def _job_on_hold_cleared(self):
        self._continue_sending()

    ##~~ Transport logging

    def on_transport_log_message(self, transport, data):
        super(ReprapGcodeProtocol, self).on_transport_log_message(transport, data)
        self._terminal_log.append("--- " + to_unicode(data.strip(), errors="replace"))

    def on_transport_log_received_data(self, transport, data):
        super(ReprapGcodeProtocol, self).on_transport_log_received_data(transport, data)
        self._terminal_log.append("<<< " + to_unicode(data.strip(), errors="replace"))

    def on_transport_log_sent_data(self, transport, data):
        super(ReprapGcodeProtocol, self).on_transport_log_sent_data(transport, data)
        self._terminal_log.append(">>> " + to_unicode(data.strip(), errors="replace"))

    ##~~ Receiving

    def on_transport_data_pushed(self, transport, data):
        if transport != self._transport:
            return
        self._receive(data)

    def on_transport_data_exception(self, transport, exception):
        if transport != self._transport:
            return

        if isinstance(exception, TimeoutTransportException):
            self._receive("")
        elif isinstance(exception, EofTransportException):
            self._receive(None)

    def _receive(self, data):
        for name, hook in self._gcode_hooks["receiving"].items():
            try:
                data = hook(self, data)
            except Exception:
                self._logger.exception(
                    "Error while processing hook {name}:".format(**locals()),
                    extra={"plugin": name},
                )
            else:
                if data is None:
                    data = ""

        if data is None:
            # EOF
            # TODO handle EOF
            pass

        def convert_line(line):
            if line is None:
                return None, None
            stripped_line = line.strip().strip("\0")
            return stripped_line, stripped_line.lower()

        orig_line = to_unicode(data, encoding="ascii", errors="replace").strip()
        line, lower_line = convert_line(orig_line)

        self._on_comm_any(line, lower_line)

        for message in self._current_registered_messages:
            handler_method = getattr(self, "_on_{}".format(message), None)
            if not handler_method:
                # no handler, nothing to do
                continue

            # match line against flavor specific matcher
            matches = getattr(self.flavor, message)(
                line, lower_line, self._state, self._protected_flags
            )
            if isinstance(matches, tuple) and len(matches) == 2:
                matches, continue_further = matches
            else:
                continue_further = not matches

            if matches:
                message_args = {}

                parse_method = getattr(self.flavor, "parse_{}".format(message), None)
                if parse_method:
                    # flavor specific parser? run it
                    parse_result = parse_method(
                        line, lower_line, self._state, self._protected_flags
                    )
                    if parse_result is None:
                        if continue_further:
                            continue
                        else:
                            break

                    # add parser result to kwargs
                    message_args.update(parse_result)

                # before handler: flavor.before_comm_* or flavor.before_message_*
                before_handler = getattr(self.flavor, "before_{}".format(message), None)
                if before_handler:
                    before_handler(**message_args)

                # run handler
                result = handler_method(**message_args)

                # after handler: flavor.after_comm_* or flavor.after_message_*
                after_handler = getattr(self.flavor, "after_{}".format(message), None)
                if after_handler:
                    after_handler(result, **message_args)

                if not result:
                    # nothing or False returned? handled -> only continue further if instructed
                    if continue_further:
                        continue
                    else:
                        break

            elif not continue_further:
                break

        else:
            # unknown message
            pass

    def _on_comm_any(self, line, lower_line):
        timeout, _ = self.flavor.comm_timeout(
            line, lower_line, self._state, self._protected_flags
        )
        if not timeout:
            self._internal_flags["timeout_consecutive"] = 0

        offsets = [
            self.timeouts.get(
                "communication_busy"
                if self._internal_flags["busy_detected"]
                else "communication",
                0.0,
            ),
        ]
        if self._temperature_poller:
            offsets.append(self._temperature_poller.interval())
        self._internal_flags["timeout"] = self._get_max_timeout(*offsets)

        if len(line):
            if (
                self._internal_flags["dwelling_until"]
                and time.monotonic() > self._internal_flags["dwelling_until"]
            ):
                self._internal_flags["dwelling_until"] = False

        if self.state == ProtocolState.CONNECTING and len(line):
            hello = self.flavor.command_hello()
            if hello:
                self._tickle(hello)

        if (
            self._resend_ok_timer
            and line
            and not getattr(self.flavor, "comm_ok", lambda *a, **kwa: True)(
                line, lower_line, self._state, self._protected_flags
            )
        ):
            # we got anything but an ok after a resend request - this means the ok after the resend request
            # was in fact missing and we now need to trigger the timer
            self._resend_ok_timer.cancel()
            self._resend_simulate_ok()

    def _on_comm_timeout(self, line, lower_line):
        general_message = "Configure long running commands or increase communication timeout if that happens regularly on specific commands or long moves."

        # figure out which consecutive timeout maximum we have to use
        if self._internal_flags["long_running_command"]:
            consecutive_max = self.max_consecutive_timeouts["long"]
        elif self.state in (
            ProtocolState.PROCESSING,
            ProtocolState.PAUSING,
            ProtocolState.CANCELLING,
        ):
            consecutive_max = self.max_consecutive_timeouts["printing"]
        else:
            consecutive_max = self.max_consecutive_timeouts["idle"]

        # now increment the timeout counter
        self._internal_flags["timeout_consecutive"] += 1
        self._logger.debug(
            "Now at {} consecutive timeouts".format(
                self._internal_flags["timeout_consecutive"]
            )
        )

        if 0 < consecutive_max < self._internal_flags["timeout_consecutive"]:
            # too many consecutive timeouts, we give up
            message = (
                "No response from printer after {} consecutive communication timeouts. "
                "Have to consider it dead.".format(consecutive_max + 1)
            )
            self._logger.info(message)
            self.process_protocol_log(message)

            # TODO error handling
            self.error = "Too many consecutive timeouts"

            self.disconnect(error=True)

        elif self._internal_flags["resend_requested"] is not None:
            message = "Communication timeout during an active resend, resending same line again to trigger response from printer."
            self._logger.info(message)
            self.notify_listeners(
                "on_protocol_log", self, message + " " + general_message
            )
            if self._send_same_from_resend():
                self._commdebug_logger.debug(
                    "on_comm_timeout => clear_to_send.set: timeout during active resend (counter: {})".format(
                        self._clear_to_send.counter
                    )
                )
                self._clear_to_send.set()

        elif self._internal_flags["heating"]:
            # blocking heatup active, consider that finished
            message = "Timeout while in an active heatup, considering heatup to be over"
            self._logger.info(message)
            self._finish_heatup()

        elif self._internal_flags["long_running_command"]:
            # long running command active, ignore timeout
            self._logger.debug(
                "Ran into a communication timeout, but a command known to be a long runner is currently active"
            )

        elif self.state in (ProtocolState.PROCESSING, ProtocolState.PAUSED):
            # printing, try to tickle the printer
            message = "Communication timeout while printing, trying to trigger response from printer."
            self._logger.info(message)
            self.notify_listeners(
                "on_protocol_log_message", self, message + " " + general_message
            )
            if self._send_command(GcodeCommand("M105", type="temperature")):
                self._commdebug_logger.debug(
                    "on_comm_timeout => clear_to_send.set: timeout while printing (counter: {})".format(
                        self._clear_to_send.counter
                    )
                )
                self._clear_to_send.set()

        elif self._clear_to_send.blocked():
            # timeout while idle and no oks left, let's try to tickle the printer
            message = "Communication timeout while idle, trying to trigger response from printer."
            self._logger.info(message)
            self.notify_listeners(
                "on_protocol_log_message", self, message + " " + general_message
            )
            self._commdebug_logger.debug(
                "on_comm_timeout => clear_to_send.set: timeout while idle (counter: {})".format(
                    self._clear_to_send.counter
                )
            )
            self._clear_to_send.set()

        self._internal_flags["ok_timeout"] = self._get_timeout(
            "communication_busy"
            if self._internal_flags["busy_detected"]
            else "communication"
        )

        return self.state != ProtocolState.PROCESSING and line == ""

    def _on_comm_ok(self, wait=False):
        if self._internal_flags["ignore_ok"] > 0:
            self._internal_flags["ignore_ok"] -= 1
            if self._internal_flags["ignore_ok"] < 0:
                self._internal_flags["ignore_ok"] = 0
            self._logger.debug(
                "Ignoring this ok, ignore counter is now {}".format(
                    self._internal_flags["ignore_ok"]
                )
            )
            return

        if self.state == ProtocolState.CONNECTING:
            self.state = ProtocolState.CONNECTED
            return

        if self._resend_ok_timer:
            self._resend_ok_timer.cancel()
            self._resend_ok_timer = None

        self._internal_flags["ok_timeout"] = self._get_timeout(
            "communication_busy"
            if self._internal_flags["busy_detected"]
            else "communication"
        )
        self._commdebug_logger.debug(
            "on_comm_ok => clear_to_send.set: {} (counter: {})".format(
                "wait" if wait else "ok", self._clear_to_send.counter
            )
        )
        self._clear_to_send.set()

        # reset long running commands, persisted current tools and heatup counters on ok

        self._internal_flags["long_running_command"] = False

        if self._internal_flags["former_tool"]:
            self._internal_flags["current_tool"] = self._internal_flags["former_tool"]
            self._internal_flags["former_tool"] = None

        self._finish_heatup()

        if self.state not in ProtocolState.OPERATIONAL_STATES:
            return

        # process ongoing resend requests and queues if we are operational

        if self._internal_flags["resend_requested"] is not None:
            self._send_next_from_resend()
        else:
            self._continue_sending()

    def _on_comm_ignore_ok(self):
        self._internal_flags["ignore_ok"] += 1
        self._logger.info(
            "Ignoring next ok, counter is now at {}".format(
                self._internal_flags["ignore_ok"]
            )
        )

    def _on_comm_wait(self):
        if self.state == ProtocolState.PROCESSING and not self.job_on_hold:
            self._on_comm_ok(wait=True)

    def _on_comm_resend(self, linenumber):
        try:
            if linenumber is None:
                return False

            if (
                self._internal_flags["resend_requested"] is None
                and linenumber == self._current_linenumber
            ):
                # We don't expect to have an active resend request and the printer is requesting a resend of
                # a line we haven't yet sent.
                #
                # This means the printer got a line from us with N = self._current_linenumber - 1 but had already
                # acknowledged that. This can happen if the last line was resent due to a timeout during an active
                # (prior) resend request.
                #
                # We will ignore this resend request and just continue normally.
                self._logger.debug(
                    "Ignoring resend request for line {} == current line, we haven't sent that yet so the printer got N-1 twice from us, probably due to a timeout".format(
                        linenumber
                    )
                )
                self.process_protocol_log(
                    "Ignoring resend request for line {}, we haven't sent that yet".format(
                        linenumber
                    )
                )
                return

            last_communication_error = self._last_communication_error
            self._last_communication_error = None

            if (
                last_communication_error is not None
                and last_communication_error == "linenumber"
                and linenumber == self._internal_flags["resend_requested"]
                and self._internal_flags["resend_count"]
                < self._current_linenumber - linenumber - 1
            ):
                self._logger.debug(
                    "Ignoring resend request for line {}, that still originates from lines we sent before we got the first resend request".format(
                        linenumber
                    )
                )
                self.process_protocol_log(
                    "Ignoring resend request for line {}, originates from lines sent earlier".format(
                        linenumber
                    )
                )
                self._internal_flags["resend_count"] += 1
                return

            if linenumber not in self._last_lines:
                self._logger.error(
                    "Printer requested to resend a line we don't have: {}".format(
                        linenumber
                    )
                )
                if self._is_busy():
                    # TODO set error state & log
                    self.cancel_processing(error=True)
                else:
                    return

            self._internal_flags["resend_requested"] = linenumber
            self._internal_flags["resend_linenumber"] = linenumber
            self._internal_flags["resend_count"] = 0

            # if we log resends, make sure we don't log more resends than the set rate within a window
            #
            # this it to prevent the log from getting flooded for extremely bad communication issues
            if self._log_resends:
                now = time.monotonic()
                new_rate_window = (
                    self._log_resends_rate_start is None
                    or self._log_resends_rate_start + self._log_resends_rate_frame < now
                )
                in_rate = self._log_resends_rate_count < self._log_resends_max

                if new_rate_window or in_rate:
                    if new_rate_window:
                        self._log_resends_rate_start = now
                        self._log_resends_rate_count = 0

                    self._to_logfile_with_terminal(
                        "Got a resend request from the printer: requested line = {}, "
                        "current line = {}".format(linenumber, self._current_linenumber)
                    )
                    self._log_resends_rate_count += 1

            self._send_queue.resend_active = True

        finally:
            if self.flavor.trigger_ok_after_resend == "always":
                self._on_comm_ok()
            elif self.flavor.trigger_ok_after_resend == "detect":
                self._resend_ok_timer = threading.Timer(
                    self.timeouts.get("resendOk", 1.0), self._resend_simulate_ok
                )
                self._resend_ok_timer.start()

    def _resend_simulate_ok(self):
        self._resend_ok_timer = None
        self._on_comm_ok()
        self._logger.info(
            "Firmware didn't send an 'ok' with their resend request. That's a known bug "
            "with some firmware variants out there. Simulating an ok to continue..."
        )

    def _on_comm_start(self):
        if self.state == ProtocolState.CONNECTING:
            self.state = ProtocolState.CONNECTED

        elif self.state in ProtocolState.OPERATIONAL_STATES:
            with self.job_put_on_hold():
                idle = self._state == ProtocolState.CONNECTED
                if idle:
                    message = (
                        "Printer sent 'start' while already connected. External reset? "
                        "Resetting state to be on the safe side."
                    )
                    self.process_protocol_log(message)
                    self._logger.warn(message)

                    self._on_external_reset()

                else:
                    message = (
                        "Printer sent 'start' while processing a job. External reset? "
                        "Aborting job since printer lost state."
                    )
                    self.process_protocol_log(message)
                    self._logger.warn(message)

                    self._on_external_reset()
                    self.cancel_processing(log_position=False)

            self.notify_listeners("on_protocol_reset", idle)

    def _on_comm_error(self, line, lower_line, error):
        for message in self._error_messages:
            handler_method = getattr(self, "_on_{}".format(message), None)
            if not handler_method:
                continue

            if getattr(self.flavor, message)(
                line, lower_line, error, self._state, self._protected_flags
            ):
                message_args = {}

                parse_method = getattr(self.flavor, "parse_{}".format(message), None)
                if parse_method:
                    parse_result = parse_method(
                        line, lower_line, error, self._state, self._protected_flags
                    )
                    if parse_result is None:
                        break

                    message_args.update(parse_result)

                if not handler_method(**message_args):
                    break
        else:
            for name, hook in self._error_hooks.items():
                try:
                    ret = hook(self, error)
                except Exception:
                    self._logger.exception(
                        "Error while processing hook {name}:".format(**locals()),
                        extra={"plugin": name},
                    )
                else:
                    if ret:
                        # some plugin handled this, we'll return
                        return

            # unknown error message
            self.error = line

            if self.firmware_errors != "ignore":
                if self.firmware_errors == "disconnect" or self.flavor.error_sdcard(
                    line, lower_line, error, self._state, self._protected_flags
                ):
                    # disconnect
                    self._trigger_error(error, "firmware")

                elif self._state in ProtocolState.PROCESSING_STATES:
                    # cancel the print
                    self.cancel_processing(error=error)
                    self._clear_to_send.set()

            else:
                message = (
                    "--- WARNING! Received an error from the printer's firmware, ignoring that as configured "
                    "but you might want to investigate what happened here! Error: {}".format(
                        error
                    )
                )
                self._to_logfile_with_terminal(message, level=logging.WARNING)
                self.process_protocol_log(message)
                self._clear_to_send.set()

    def _on_error_communication(self, error_type):
        self._last_communication_error = error_type

    def _on_comm_busy(self):
        self._internal_flags["ok_timeout"] = self._get_timeout(
            "communcation_busy"
            if self._internal_flags["busy_detected"]
            else "communication"
        )

        if not self._internal_flags["busy_detected"] and self._capability_support.get(
            CAPABILITY_BUSY_PROTOCOL, False
        ):
            self.process_protocol_log(
                "Printer seems to support the busy protocol, adjusting timeouts and setting busy "
                "interval accordingly"
            )
            self._internal_flags["busy_detected"] = True

            new_communication_timeout = self.timeouts.get("communication_busy", 2)
            self._transport.timeout = new_communication_timeout
            busy_interval = max(int(new_communication_timeout) - 1, 1)

            self._logger.info(
                "Printer seems to support the busy protocol, telling it to set the busy "
                'interval to our "communication_busy" timeout - 1s = {}s'.format(
                    busy_interval
                )
            )

            self._set_busy_protocol_interval(interval=busy_interval)

    def _on_comm_action_command(self, line, lower_line, action):
        if action == "cancel":
            self.process_protocol_log("Cancelling on request of the printer...")
            self.cancel_processing()
        elif action == "pause":
            self.process_protocol_log("Pausing on request of the printer...")
            self.pause_processing()
        elif action == "paused":
            self.process_protocol_log(
                "Printer signalled that it paused, switching state..."
            )
            self.pause_processing(local_handling=False)
        elif action == "resume":
            self.process_protocol_log("Resuming on request of the printer...")
            self.resume_processing()
        elif action == "resumed":
            self.process_protocol_log(
                "Printer signalled that it resumed, switching state..."
            )
            self.resume_processing(local_handling=False)
        elif action == "disconnect":
            self.process_protocol_log("Disconnecting on request of the printer...")
            # TODO inform printer about forced disconnect
            self.disconnect()
        else:
            for hook in self._action_hooks:
                try:
                    self._action_hooks[hook](self, line, action)
                except Exception:
                    self._logger.exception(
                        "Error while calling hook {} with action command {}".format(
                            self._action_hooks[hook], action
                        )
                    )
                    continue

    def _on_message_temperature(self, max_tool_num, temperatures, heatup_detected):
        if heatup_detected:
            self._logger.debug("Externally triggered heatup detected")
            self._internal_flags["heating"] = True
            self._internal_flags["heatup_start"] = time.monotonic()

        shared_nozzle = self._printer_profile["extruder"]["sharedNozzle"]
        current_tool_key = "T{}".format(self._internal_flags["current_tool"])

        for name, hook in self._temperature_hooks.items():
            try:
                temperatures = hook(self, temperatures)
                if temperatures is None or not temperatures:
                    return
            except Exception:
                self._logger.exception(
                    "Error while processing temperatures in {}, skipping".format(name),
                    extra={"plugin": name},
                )

        if current_tool_key in temperatures:
            for x in range(max_tool_num + 1):
                tool = "T{}".format(x)
                if tool not in temperatures:
                    if shared_nozzle:
                        # replicate the current temperature across all tools (see #2077)
                        actual, target = temperatures[current_tool_key]
                    else:
                        continue
                else:
                    actual, target = temperatures[tool]
                self._internal_flags["temperatures"].set_tool(
                    x, actual=actual, target=target
                )

        if "B" in temperatures and self._printer_profile["heatedBed"]:
            actual, target = temperatures["B"]
            self._internal_flags["temperatures"].set_bed(actual=actual, target=target)

        if "C" in temperatures and (
            self._capability_support.get(CAPABILITY_CHAMBER_TEMP, False)
            or self._printer_profile["heatedChamber"]
        ):
            actual, target = temperatures["C"]
            self._internal_flags["temperatures"].set_chamber(actual=actual, target=target)

        self.notify_listeners(
            "on_protocol_temperature",
            self,
            self._internal_flags["temperatures"].as_dict(),
        )

    def _on_message_position(self, position):
        self.last_position.valid = True
        self.last_position.x = position.get("x")
        self.last_position.y = position.get("y")
        self.last_position.z = position.get("z")

        t_and_f_trustworthy = self._job is None or not self._job.parallel

        if "e" in position:
            self.last_position.e = position.get("e")
        else:
            # multiple extruder coordinates provided, find current one
            self.last_position.e = (
                position.get("e{}".format(self._internal_flags["current_tool"]))
                if t_and_f_trustworthy
                else None
            )

        for key in [key for key in position if key.startswith("e") and len(key) > 1]:
            setattr(self.last_position, key, position.get(key))

        self.last_position.t = (
            self._internal_flags["current_tool"] if t_and_f_trustworthy else None
        )
        self.last_position.f = (
            self._internal_flags["current_f"] if t_and_f_trustworthy else None
        )

        reason = None

        if self._record_pause_data:
            reason = "pause"
            self._record_pause_data = False
            self.pause_position.copy_from(self.last_position)
            self.pause_temperature.copy_from(self._internal_flags["temperatures"])
            self._pause_preparation_done()

        if self._record_cancel_data:
            reason = "cancel"
            self._record_cancel_data = False
            self.cancel_position.copy_from(self.last_position)
            self.cancel_temperature.copy_from(self._internal_flags["temperatures"])
            self._cancel_preparation_done()

        self.notify_listeners(
            "on_protocol_position_all_update", self, position, reason=reason
        )

    def _on_message_firmware_info(self, firmware_name, data):
        self._event_bus.fire(
            Events.FIRMWARE_DATA, {"name": firmware_name, "data": copy.copy(data)}
        )

        if not self._internal_flags["firmware_identified"] and firmware_name:
            self._logger.info('Printer reports firmware name "{}"'.format(firmware_name))

            for flavor in all_flavors():
                if (
                    flavor.identifier
                    and callable(flavor.identifier)
                    and flavor.identifier(firmware_name, data)
                ):
                    self._logger.info(
                        'Detected firmware flavor "{}", switching...'.format(flavor.key)
                    )
                    self._switch_flavor(flavor)

            self._internal_flags["firmware_identified"] = True
            self._internal_flags["firmware_name"] = firmware_name
            self._internal_flags["firmware_info"] = data

            # notify plugins
            for name, hook in self._firmware_info_hooks.items():
                try:
                    hook(self, firmware_name, copy.copy(data))
                except Exception:
                    self._logger.exception(
                        "Error processing firmware info hook {}:".format(name),
                        extra={"plugin": name},
                    )

    def _on_message_firmware_capability(self, cap, enabled):
        self._internal_flags["firmware_capabilities"][cap] = enabled

        if self._capability_support.get(cap, False):
            if cap == CAPABILITY_AUTOREPORT_TEMP and enabled:
                self._logger.info(
                    "Firmware states that it supports temperature autoreporting"
                )
                self._set_autoreport_temperature_interval()
            elif cap == CAPABILITY_AUTOREPORT_SD_STATUS and enabled:
                self._logger.info(
                    "Firmware states that it supports sd status autoreporting"
                )
                self._set_autoreport_sdstatus_interval()
            elif cap == CAPABILITY_EMERGENCY_PARSER and enabled:
                self._logger.info(
                    "Firmware states that it supports emergency GCODEs to be sent without waiting for an acknowledgement first"
                )

        # notify plugins
        for name, hook in self._firmware_cap_hooks.items():
            try:
                hook(
                    self,
                    cap,
                    enabled,
                    copy.copy(self._internal_flags["firmware_capabilities"]),
                )
            except Exception:
                self._logger.exception(
                    "Error processing firmware capability hook {}:".format(name),
                    extra={"plugin": name},
                )

    def _on_message_sd_init_ok(self):
        self._internal_flags["sd_available"] = True
        self.list_files()
        self.notify_listeners(
            "on_protocol_file_storage_available",
            self,
            self._internal_flags["sd_available"],
        )

    def _on_message_sd_init_fail(self):
        self._internal_flags["sd_available"] = False
        self.notify_listeners(
            "on_protocol_file_storage_available",
            self,
            self._internal_flags["sd_available"],
        )

    def _on_message_sd_begin_file_list(self):
        self._internal_flags["sd_files_temp"] = []

    def _on_message_sd_end_file_list(self):
        self._internal_flags["sd_files"] = self._internal_flags["sd_files_temp"]
        self._internal_flags["sd_files_temp"] = None
        self.notify_listeners(
            "on_protocol_file_list", self, self._internal_flags["sd_files"]
        )

    def _on_message_sd_entry(self, name, long_name, size):
        self._internal_flags["sd_files_temp"].append((name, long_name, size))

    def _on_message_sd_file_opened(self, name, long_name, size):
        self.notify_listeners(
            "on_protocol_file_print_started", self, name, long_name, size
        )

    def _on_message_sd_done_printing(self):
        self.notify_listeners("on_protocol_file_print_done", self)

    def _on_message_sd_printing_byte(self, current, total):
        self.notify_listeners("on_protocol_sd_status", self, current, total)

    def _finish_heatup(self):
        if self._internal_flags["heating"]:
            if self._internal_flags["heating_start"]:
                self._internal_flags["heating_lost"] = self._internal_flags[
                    "heating_lost"
                ] + (time.monotonic() - self._internal_flags["heating_start"])
                self._internal_flags["heating_start"] = None
            self._internal_flags["heating"] = False

    def _on_external_reset(self):
        with self._send_queue.blocked():
            self._clear_to_send.clear(completely=True)
            with self._command_queue.blocked():
                self._command_queue.clear()
            self._send_queue.clear()

            with self._line_mutex:
                self._current_linenumber = 0
                self._last_lines.clear()

        self.flavor = GenericFlavor.with_overrides(self.flavor_overrides)

        self.send_commands(self.flavor.command_hello(), self.flavor.command_set_line(0))

        if self._internal_flags["temperature_autoreporting"]:
            self._set_autoreport_temperature_interval()
        if self._internal_flags["sd_status_autoreporting"]:
            self._set_autoreport_sdstatus_interval()
        if self._internal_flags["busy_detected"]:
            self._set_busy_protocol_interval()

    ##~~ Sending

    def _tickle(self, command):
        if self._send_command(command):
            if self._clear_to_send.blocked():
                self._commdebug_logger.debug(
                    "tickle => clear_to_send.set (counter: {})".format(
                        self._clear_to_send.counter
                    )
                )
                self._clear_to_send.set()

    def _continue_sending(self):
        self._commdebug_logger.debug(
            "continue_sending (counter: {}, line number: {})".format(
                self._clear_to_send.counter, self._current_linenumber
            )
        )
        while self._active:
            job_active = self.state in (
                ProtocolState.STARTING,
                ProtocolState.PROCESSING,
            ) and not (
                self._job is None
                or not self._job.active
                or isinstance(self._job, SDFilePrintjob)
            )

            if self._send_from_command_queue():
                # we found something in the command queue to send
                return True

            elif self.job_on_hold:
                # job is on hold, that means we must not send from either job or job queue
                return False

            elif self._send_from_job_queue():
                # we found something in the job queue to send
                return True

            elif job_active and self._send_next_from_job():
                # we sent the next line from the file
                return True

            elif not job_active:
                # nothing sent but also no job active, so we can just return false here
                return False

            self._logger.debug(
                "No command sent on ok while printing, doing another iteration"
            )

    def _send_same_from_resend(self):
        return self._send_next_from_resend(again=True)

    def _send_next_from_resend(self, again=False):
        self._commdebug_logger.debug(
            "send_next_from_resend (counter: {}, line number: {})".format(
                self._clear_to_send.counter, self._internal_flags.get("resend_linenumber")
            )
        )
        self._last_communication_error = None

        # Make sure we are only handling one sending job at a time
        with self._send_queue_mutex:
            if again:
                # If we are about to send the last line from the active resend request
                # again, we first need to increment resend delta. It might already
                # be set to None if the last resend line was already sent, so
                # if that's the case we set it to 0. It will then be incremented,
                # the last line will be sent again, and then the delta will be
                # decremented and set to None again, completing the cycle.
                if self._internal_flags["resend_linenumber"] is None:
                    self._internal_flags["resend_linenumber"] = self._current_linenumber
                self._internal_flags["resend_linenumber"] -= 1

            linenumber = self._internal_flags["resend_linenumber"]

            try:
                command = self._last_lines[linenumber]
            except KeyError:
                # TODO what to do?
                return False

            result = self._enqueue_for_sending(
                to_command(command), linenumber=linenumber, processed=True, resend=True
            )

            self._internal_flags["resend_linenumber"] += 1
            if self._internal_flags["resend_linenumber"] == self._current_linenumber:
                self._internal_flags["resend_requested"] = None
                self._internal_flags["resend_linenumber"] = None
                self._internal_flags["resend_count"] = 0

                self._send_queue.resend_active = False

            return result

    def _send_next_from_job(self):
        with self._send_queue_mutex:
            while self._active:
                # we loop until we've actually enqueued a line for sending
                if self.state != ProtocolState.PROCESSING:
                    # we are no longer printing, return False
                    return False

                elif self.job_on_hold:
                    # job is on hold, return false
                    return False

                line = self._job.get_next()
                if line is None:
                    # end of job, return False
                    return False

                pos = self._job.pos
                read_lines = self._job.read_lines
                actual_lines = self._job.actual_lines
                command = to_command(
                    line,
                    tags={
                        "source:file",
                        "filepos:{}".format(pos),
                        "fileline:{}".format(read_lines),
                        "fileactualline:{}".format(actual_lines),
                    },
                )
                if self._send_command(command):
                    return True

                self._logger.debug(
                    'Command "{}" from job not enqueued, doing another iteration'.format(
                        line
                    )
                )

    def _send_from_job_queue(self):
        try:
            command, on_sent = self._job_queue.get_nowait()
            result = self._send_command(command, on_sent=on_sent)
            if result:
                return True
        except queue.Empty:
            pass

        return False

    def _send_from_command_queue(self):
        # We loop here to make sure that if we do NOT send the first command
        # from the queue, we'll send the second (if there is one). We do not
        # want to get stuck here by throwing away commands.
        while True:
            if self._internal_flags["only_from_job"]:
                # irrelevant command queue => return
                return False

            try:
                entry = self._command_queue.get(block=False)
            except queue.Empty:
                # nothing in command queue
                return False

            try:
                if isinstance(entry, tuple):
                    if not len(entry) == 2:
                        # something with that entry is broken, ignore it and fetch
                        # the next one
                        continue
                    cmd, callback = entry
                else:
                    cmd = entry
                    callback = None

                if self._send_command(cmd, on_sent=callback):
                    # we actually did add this cmd to the send queue, so let's
                    # return, we are done here
                    return True
            finally:
                self._command_queue.task_done()

    def _send_commands(self, *commands, **kwargs):
        command_type = kwargs.pop("command_type", None)
        result = False
        for command in commands:
            if len(commands) > 1:
                command_type = None
            result = (
                self._send_command(command, command_type=command_type, **kwargs) or result
            )
        return result

    def _send_command(self, command, command_type=None, tags=None, on_sent=None):
        with self._send_queue_mutex:
            if isinstance(command, QueueMarker):
                if isinstance(command, SendQueueMarker):
                    self._enqueue_for_sending(command)
                    return True
                else:
                    return False

            elif isinstance(command, Command) and (command_type or tags):
                command = command.with_type(command_type).with_tags(tags)

            else:
                command = to_command(command, type=command_type, tags=tags)

            if self._internal_flags["trigger_events"]:
                results = self._process_command_phase("queuing", command)

                if not results:
                    # command is no more, return
                    return False
            else:
                results = [command]

            # process helper
            def process(command, on_sent=None):
                if command is None:
                    # no command, next entry
                    return False

                # process @ commands
                if isinstance(command, AtCommand):
                    self._process_atcommand_phase("queuing", command)

                # actually enqueue the command for sending
                if self._enqueue_for_sending(command, on_sent=on_sent):
                    if not self._is_streaming():
                        # trigger the "queued" phase only if we are not streaming to sd right now
                        self._process_command_phase("queued", command)

                        if (
                            self._internal_flags["trigger_events"]
                            and isinstance(command, GcodeCommand)
                            and command.code in GCODE_TO_EVENT
                        ):
                            self._event_bus.fire(GCODE_TO_EVENT[command.code])
                            pass
                    return True
                else:
                    return False

            # split off the final command, because that needs special treatment
            if len(results) > 1:
                last_command = results[-1]
                results = results[:-1]
            else:
                last_command = results[0]
                results = []

            # track if we enqueued anything at all
            enqueued_something = False

            # process all but the last ...
            for command in results:
                enqueued_something = process(command) or enqueued_something

            # ... and then process the last one with the on_sent callback attached
            command = last_command
            enqueued_something = process(command, on_sent=on_sent) or enqueued_something

            return enqueued_something

    def _enqueue_for_sending(
        self, command, linenumber=None, on_sent=None, processed=False, resend=False
    ):
        """
        Enqueues a command and optional command_type and linenumber in the send queue.

        Arguments:
            command (Command or SendQueueMarker): The command to send or the marker to enqueue
            linenumber (int): An optional line number to use for sending the
                command.
            on_sent (callable or None): Callback to call when the command has been sent
            processed (bool): Whether this line has already been processed or not
        """

        try:
            target = "send"
            if resend:
                target = "resend"

            if isinstance(command, Command):
                command_type = command.type
            else:
                command_type = None

            self._send_queue.put(
                (command, linenumber, on_sent, processed),
                item_type=command_type,
                target=target,
            )
            return True
        except TypeAlreadyInQueue as e:
            self._logger.debug("Type already in queue: {}".format(e.type))
            return False

    def _send_loop(self):
        """
        The send loop is responsible of sending commands in ``self._send_queue`` over the line, if it is cleared for
        sending (through received ``ok`` responses from the printer's firmware.
        """

        self._clear_to_send.wait()

        while self._send_queue_active:
            try:
                # wait until we have something in the queue
                try:
                    entry = self._send_queue.get()
                except queue.Empty:
                    continue

                try:
                    # make sure we are still active
                    if not self._send_queue_active:
                        break

                    # sleep if we are dwelling
                    now = time.monotonic()
                    if (
                        self.flavor.block_while_dwelling
                        and self._internal_flags["dwelling_until"]
                        and now < self._internal_flags["dwelling_until"]
                    ):
                        time.sleep(self._internal_flags["dwelling_until"] - now)
                        self._internal_flags["dwelling_until"] = False

                    # fetch command and optional linenumber from queue
                    command, linenumber, on_sent, processed = entry

                    if isinstance(command, SendQueueMarker):
                        command.run()
                        self._continue_sending()
                        continue

                    if linenumber is not None:
                        # line number predetermined - this only happens for resends, so we'll use the number and
                        # send directly without any processing (since that already took place on the first sending!)
                        self._do_send_with_checksum(to_bytes(command.line), linenumber)

                    else:
                        if not processed:
                            results = self._process_command_phase("sending", command)

                            if not results:
                                # No, we are not going to send this, that was a last-minute bail.
                                # However, since we already are in the send queue, our _monitor
                                # loop won't be triggered with the reply from this unsent command
                                # now, so we try to tickle the processing of any active
                                # command queues manually
                                self._commdebug_logger.debug("send_loop => no results")
                                self._continue_sending()

                                # and now let's fetch the next item from the queue
                                continue

                            # we explicitly throw away plugin hook results that try
                            # to perform command expansion in the sending/sent phase,
                            # so "results" really should only have more than one entry
                            # at this point if our core code contains a bug
                            assert len(results) == 1

                            # we only use the first (and only!) entry here
                            command = results[0]

                        if command.line.strip() == "":
                            self._logger.info(
                                "Refusing to send an empty line to the printer"
                            )

                            # same here, tickle the queues manually
                            self._commdebug_logger.debug("send_loop => empty line")
                            self._continue_sending()

                            # and fetch the next item
                            continue

                        # handle @ commands
                        if isinstance(command, AtCommand):
                            self._process_command_phase("sending", command)

                            # tickle...
                            self._commdebug_logger.debug("send_loop => at command")
                            self._continue_sending()

                            # ... and fetch the next item
                            continue

                        # now comes the part where we increase line numbers and send stuff - no turning back now
                        self._do_send(command)

                    # trigger "sent" phase and use up one "ok"
                    if on_sent is not None and callable(on_sent):
                        # we have a sent callback for this specific command, let's execute it now
                        on_sent()
                    self._process_command_phase("sent", command)

                    # we only need to use up a clear if the command we just sent was either a gcode command or if we also
                    # require ack's for unknown commands
                    use_up_clear = self.flavor.unknown_requires_ack
                    if isinstance(command, GcodeCommand):
                        use_up_clear = True
                    else:
                        self._commdebug_logger.debug(
                            "send_loop => command != instanceof GcodeCommand: {!r}".format(
                                command
                            )
                        )

                    if use_up_clear:
                        # if we need to use up a clear, do that now
                        self._commdebug_logger.debug(
                            "send_loop => clear_to_send.clear: line sent (counter: {}, line number: {})".format(
                                self._clear_to_send.counter, self._current_linenumber
                            )
                        )
                        self._clear_to_send.clear()
                    else:
                        # Otherwise we need to tickle the read queue - there might not be a reply
                        # to this command, so our _monitor loop will stay waiting until timeout. We
                        # definitely do not want that, so we tickle the queue manually here
                        self._commdebug_logger.debug("send_loop => use_up_clear == False")
                        self._continue_sending()

                finally:
                    # no matter _how_ we exit this block, we signal that we
                    # are done processing the last fetched queue entry
                    self._send_queue.task_done()

                # now we just wait for the next clear and then start again
                self._clear_to_send.wait()
            except Exception:
                self._logger.exception("Caught an exception in the send loop")
        self.notify_listeners("on_protocol_log", self, "Closing down send loop")

    def _log_command_phase(self, phase, command):
        if self._phase_logger.isEnabledFor(logging.DEBUG):
            self._phase_logger.debug("phase: {}, command: {!r}".format(phase, command))

    def _process_command_phase(self, phase, command, **kwargs):
        command = to_command(command, **kwargs)
        results = [command]

        self._log_command_phase(phase, command)

        if not self._internal_flags["trigger_events"] or phase not in (
            "queuing",
            "queued",
            "sending",
            "sent",
        ):
            return results

        # send it through the phase specific handlers provided by plugins
        for name, hook in self._gcode_hooks[phase].items():
            new_results = []
            for command in results:
                try:
                    hook_results = hook(
                        self,
                        phase,
                        str(command),
                        command.type,
                        command.code if isinstance(command, GcodeCommand) else None,
                        subcode=command.subcode
                        if isinstance(command, GcodeCommand)
                        else None,
                        tags=command.tags,
                    )
                except Exception:
                    self._logger.exception(
                        "Error while processing hook {name} for phase {phase} and command {command}:".format(
                            **locals()
                        )
                    )
                else:
                    normalized = normalize_command_handler_result(
                        command,
                        hook_results,
                        tags_to_add={
                            "source:rewrite",
                            "phase:{}".format(phase),
                            "plugin:{}".format(name),
                        },
                    )

                    # make sure we don't allow multi entry results in anything but the queuing phase
                    if phase not in ("queuing",) and len(normalized) > 1:
                        self._logger.error(
                            "Error while processing hook {name} for phase {phase} and command {command}: Hook returned multi-entry result for phase {phase} and command {command}. That's not supported, if you need to do multi expansion of commands you need to do this in the queuing phase. Ignoring hook result and sending command as-is.".format(
                                **locals()
                            )
                        )
                        new_results.append(command)
                    else:
                        new_results += normalized
            if not new_results:
                # hook handler returned None or empty list for all commands, so we'll stop here and return a full out empty result
                return []
            results = new_results

        # if it's a gcode command send it through the specific handler if it exists
        new_results = []
        modified = False
        for command in results:
            if isinstance(command, GcodeCommand):
                gcode_handler = "_gcode_" + command.code + "_" + phase
                if gcode_handler in self._handlers_gcode:
                    # noinspection PyCallingNonCallable
                    handler_results = getattr(self, gcode_handler)(command)
                    new_results += normalize_command_handler_result(
                        command, handler_results
                    )
                    modified = True
                else:
                    new_results.append(command)
            else:
                new_results.append(command)

        if modified:
            if not new_results:
                # gcode handler returned None or empty list for all commands, so we'll stop here and return a full out empty result
                return []
            else:
                results = new_results

        # send it through the phase specific command handler if it exists
        command_phase_handler = "_command_phase_" + phase
        if command_phase_handler in self._handlers_command_phase:
            new_results = []
            for command in results:
                handler_results = getattr(self, command_phase_handler)(command)
                new_results += normalize_command_handler_result(command, handler_results)
            results = new_results

        # finally return whatever we resulted on
        return results

    def _process_atcommand_phase(self, phase, command):
        if (self._is_streaming()) or phase not in ("queuing", "sending"):
            return

        # send it through the phase specific handlers provided by plugins
        for name, hook in self._atcommand_hooks[phase].items():
            try:
                hook(
                    self, phase, command.atcommand, command.parameters, tags=command.tags
                )
            except Exception:
                self._logger.exception(
                    "Error while processing hook {} for phase {} and command {}:".format(
                        name, phase, command.atcommand
                    )
                )

        # trigger built-in handler if available
        atcommand_handler = "_atcommand_{}_{}".format(command.atcommand, phase)
        if atcommand_handler in self._handlers_atcommand:
            try:
                getattr(self, atcommand_handler)(command)
            except Exception:
                self._logger.exception(
                    "Error in handler for phase {} and command {}".format(
                        phase, command.atcommand
                    )
                )

    ##~~ actual sending via serial

    def _needs_checksum(self, command):
        if not self._transport.message_integrity:
            # transport does not have message integrity, let's see if we'll send a checksum
            command_requiring_checksum = (
                isinstance(command, GcodeCommand)
                and command.code in self.flavor.checksum_requiring_commands
            )
            command_allowing_checksum = (
                isinstance(command, GcodeCommand) or self.flavor.unknown_with_checksum
            )
            return command_requiring_checksum or (
                command_allowing_checksum and self._checksum_enabled
            )

        else:
            # transport has message integrity, no checksum
            return False

    @property
    def _checksum_enabled(self):
        if not self._transport.message_integrity:
            return self.flavor.send_checksum != "never" and (
                self.state == ProtocolState.PROCESSING  # TODO does job need checksum?
                or self.flavor.send_checksum == "always"
                or not self._internal_flags["firmware_identified"]
            )
        else:
            # transport has message integrity, no checksum
            return False

    def _emergency_force_send(self, command, message, *args, **kwargs):
        """
        Args:
                command (Command): the command to send
                message (unicode): the message to log
        """
        # only jump the queue with our command if the EMERGENCY_PARSER capability is available
        if not self._internal_flags["firmware_capability_support"].get(
            CAPABILITY_EMERGENCY_PARSER, False
        ) or not self._internal_flags["firmware_capabilities"].get(
            CAPABILITY_EMERGENCY_PARSER, False
        ):
            return

        self._logger.info(message)
        self._do_send(command)

        # use up an ok since we will get one back for this command and don't want to get out of sync
        self._clear_to_send.clear()

        return (None,)

    def _do_send(self, command):
        """
        Args:
                command (Command): the command to send
        """
        if self._needs_checksum(command):
            self._do_increment_and_send_with_checksum(command.bytes)
        else:
            self._do_send_without_checksum(command.bytes)

    def _do_increment_and_send_with_checksum(self, line):
        """
        Args:
                line (bytes): the line to send
        """
        with self._line_mutex:
            line_number = self._current_linenumber
            self._add_to_last_lines(to_unicode(line), line_number=line_number)
            self._current_linenumber += 1
            self._do_send_with_checksum(line, line_number)

    def _do_send_with_checksum(self, line, line_number):
        """
        Args:
                line (bytes): the line to send
                line_number (int): the line number to send
        """
        command_to_send = b"N" + str(line_number).encode("ascii") + b" " + line
        checksum = 0
        for c in bytearray(command_to_send):
            checksum ^= c
        command_to_send = command_to_send + b"*" + str(checksum).encode("ascii")
        self._do_send_without_checksum(command_to_send)

    def _do_send_without_checksum(self, data):
        """
        Args:
                data (bytes): the data to send
        """
        self._transport.write(data + b"\n")

    def _add_to_last_lines(self, command, line_number=None):
        # type: (str, int) -> None
        self._last_lines.append(command, line_number=line_number)

    ##~~ gcode command handlers

    def _gcode_T_sent(self, command):
        if command.tool:
            self._internal_flags["current_tool"] = int(command.tool)

    def _gcode_G0_sent(self, command):
        if command.z is not None and self._internal_flags["current_z"] != command.z:
            self._internal_flags["current_z"] = command.z
            self.notify_listeners(
                "on_protocol_position_z_update", self, self._internal_flags["current_z"]
            )
        if command.f is not None:
            self._internal_flags["current_f"] = command.f

    _gcode_G1_sent = _gcode_G0_sent

    def _gcode_G2_sent(self, command):
        if command.f is not None:
            self._internal_flags["current_f"] = command.f

    _gcode_G3_sent = _gcode_G2_sent
    _gcode_G28_sent = _gcode_G2_sent

    def _gcode_M140_queuing(self, command):
        if not self._printer_profile["heatedBed"]:
            self.process_protocol_log(
                'Warn: Not sending "{}", printer profile has no heated bed'.format(
                    command
                )
            )
            return (None,)  # Don't send bed commands if we don't have a heated bed

    _gcode_M190_queuing = _gcode_M140_queuing

    def _gcode_M155_sending(self, command):
        try:
            interval = int(command.s)
            self._internal_flags["temperature_autoreporting"] = self._internal_flags[
                "firmware_capabilities"
            ].get(CAPABILITY_AUTOREPORT_TEMP, False) and (interval > 0)
        except Exception:
            pass

    def _gcode_M27_sending(self, command):
        try:
            interval = int(command.s)
            self._internal_flags["sd_status_autoreporting"] = self._internal_flags[
                "firmware_capabilities"
            ].get(CAPABILITY_AUTOREPORT_SD_STATUS, False) and (interval > 0)
        except Exception:
            pass

    def _apply_temperature_offset(self, heater, command, support_r=False):
        offset = self._internal_flags["temperature_offsets"].get(heater, 0)
        if offset == 0 or "source:file" not in command.tags:
            return

        try:
            if command.s is not None:
                return command.with_args(s=float(command.s) + offset)
            elif command.r is not None and support_r:
                return command.with_args(r=float(command.r) + offset)
        except Exception:
            self._logger.exception("Error applying temperature offset")

    def _gcode_M104_sending(self, command, support_r=False):
        if command.t:
            tool_num = command.t
        else:
            tool_num = self._internal_flags["current_tool"]

        return self._apply_temperature_offset(
            "tool{}".format(tool_num), command, support_r=support_r
        )

    def _gcode_M109_sending(self, command):
        return self._gcode_M104_sending(command, support_r=True)

    def _gcode_M140_sending(self, command):
        return self._apply_temperature_offset("bed", command, support_r=False)

    def _gcode_M190_sending(self, command):
        return self._apply_temperature_offset("bed", command, support_r=True)

    def _gcode_M141_sending(self, command):
        return self._apply_temperature_offset("chamber", command, support_r=False)

    def _gcode_M191_sending(self, command):
        return self._apply_temperature_offset("chamber", command, support_r=True)

    def _gcode_M104_sent(self, command, wait=False, support_r=False):
        tool_num = self._internal_flags["current_tool"]
        if command.t:
            tool_num = command.t

            if wait:
                self._internal_flags["former_tool"] = self._internal_flags["current_tool"]
                self._internal_flags["current_tool"] = tool_num

        target = None
        if command.s is not None:
            target = float(command.s)
        elif command.r is not None and support_r:
            target = float(command.r)

        if target:
            self._internal_flags["temperatures"].set_tool(tool_num, target=target)
            self.notify_listeners(
                "on_protocol_temperature",
                self,
                self._internal_flags["temperatures"].as_dict(),
            )

    def _gcode_M140_sent(self, command, wait=False, support_r=False):
        target = None
        if command.s is not None:
            target = float(command.s)
        elif command.r is not None and support_r:
            target = float(command.r)

        if target:
            self._internal_flags["temperatures"].set_bed(target=target)
            self.notify_listeners(
                "on_protocol_temperature",
                self,
                self._internal_flags["temperatures"].as_dict(),
            )

    def _gcode_M141_sent(self, command, wait=False, support_r=False):
        target = None
        if command.s is not None:
            target = float(command.s)
        elif command.r is not None and support_r:
            target = float(command.r)

        if target:
            self._internal_flags["temperatures"].set_chamber(target=target)
            self.notify_listeners(
                "on_protocol_temperature",
                self,
                self._internal_flags["temperatures"].as_dict(),
            )

    def _gcode_M109_sent(self, command):
        self._internal_flags["heatup_start"] = time.monotonic()
        self._internal_flags["long_running_command"] = True
        self._internal_flags["heating"] = True
        self._gcode_M104_sent(command, wait=True, support_r=True)

    def _gcode_M190_sent(self, command):
        self._internal_flags["heatup_start"] = time.monotonic()
        self._internal_flags["long_running_command"] = True
        self._internal_flags["heating"] = True
        self._gcode_M140_sent(command, wait=True, support_r=True)

    def _gcode_M191_sent(self, command):
        self._internal_flags["heatup_start"] = time.monotonic()
        self._internal_flags["long_running_command"] = True
        self._internal_flags["heating"] = True
        self._gcode_M141_sent(command, wait=True, support_r=True)

    def _gcode_M116_sent(self, command):
        self._internal_flags["heatup_start"] = time.monotonic()
        self._internal_flags["long_running_command"] = True
        self._internal_flags["heating"] = True

    def _gcode_M110_sending(self, command):
        new_line_number = None
        if command.n:
            try:
                new_line_number = int(command.n)
            except Exception:
                pass
        else:
            new_line_number = 0

        with self._line_mutex:
            self._logger.info(
                "M110 detected, setting current line number to {}".format(new_line_number)
            )

            # send M110 command with new line number
            self._current_linenumber = new_line_number

            # after a reset of the line number we have no way to determine what line exactly the printer now wants
            self._last_lines.clear()
        self._internal_flags["resend_linenumber"] = None

    def _gcode_M112_queuing(self, command):
        # emergency stop, jump the queue with the M112
        emergency_stop = self.flavor.command_emergency_stop().bytes
        self._do_send_without_checksum(emergency_stop)
        self._do_increment_and_send_with_checksum(emergency_stop)

        # No idea if the printer is still listening or if M112 won. Just in case
        # we'll now try to also manually make sure all heaters are shut off - better
        # safe than sorry. We do this ignoring the queue since at this point it
        # is irrelevant whether the printer has sent enough ack's or not, we
        # are going to shutdown the connection in a second anyhow.
        extruder_count = self._printer_profile["extruder"]["count"]
        shared_nozzle = self._printer_profile["extruder"]["sharedNozzle"]
        if extruder_count > 1 and not shared_nozzle:
            for tool in range(extruder_count):
                self._do_increment_and_send_with_checksum(
                    self.flavor.command_set_extruder_temp(0, tool=tool).bytes
                )
        else:
            self._do_increment_and_send_with_checksum(
                self.flavor.command_set_extruder_temp(0).bytes
            )

        if self._printer_profile["heatedBed"]:
            self._do_increment_and_send_with_checksum(
                self.flavor.command_set_bed_temp(0).bytes
            )

        # close to reset host state
        # TODO needs error handling
        """
        self._errorValue = "Closing serial port due to emergency stop M112."
        self._log(self._errorValue)
        self.close(is_error=True)
        """

        # fire the M112 event since we sent it and we're going to prevent the caller from seeing it
        event = GCODE_TO_EVENT.get("M112")
        if event is not None:
            self._event_bus.fire(event)

        # return None 1-tuple to eat the one that is queuing because we don't want to send it twice
        # I hope it got it the first time because as far as I can tell, there is no way to know
        return (None,)

    # TODO
    # def _gcode_M114_queued(self, *args, **kwargs):
    #     self._reset_position_timers()
    # _gcode_M114_sent = _gcode_M114_queued

    def _gcode_G4_sent(self, command):
        timeout = 0
        if command.p is not None:
            try:
                timeout = float(command.p) / 1000.0
            except ValueError:
                pass
        elif command.s is not None:
            try:
                timeout = float(command.s)
            except ValueError:
                pass

        self._internal_flags["timeout"] = (
            self._get_timeout(
                "communication_busy"
                if self._internal_flags["busy_detected"]
                else "communication"
            )
            + timeout
        )
        self._internal_flags["dwelling_until"] = time.monotonic() + timeout

    ##~~ atcommand handlers

    def _atcommand_pause_queuing(self, command):
        tags = command.tags
        if tags is None:
            tags = set()
        if "script:afterPrintPaused" not in tags:
            self.pause_processing(tags={"trigger:atcommand_pause"})

    def _atcommand_cancel_queuing(self, command):
        tags = command.tags
        if tags is None:
            tags = set()
        if "script:afterPrintCancelled" not in tags:
            self.cancel_processing(tags={"trigger:atcommand_cancel"})

    _atcommand_abort_queuing = _atcommand_cancel_queuing

    def _atcommand_resume_queuing(self, command):
        tags = command.tags
        if tags is None:
            tags = set()
        if "script:beforePrintResumed" not in tags:
            self.resume_processing(tags={"trigger:atcommand_resume"})

    ##~~ command phase handlers

    def _command_phase_queuing(self, command):
        if isinstance(command, GcodeCommand):
            if (
                command.code in self.flavor.emergency_commands
                and command.code != self.flavor.command_emergency_stop().code
            ):
                message = "Force-sending {} to the printer".format(command)
                self._logger.info(message)
                return self._emergency_force_send(command, message)

            if (
                self.state in ProtocolState.PROCESSING_STATES
                and command.code in self.flavor.pausing_commands
            ):
                self._logger.info("Pausing print job due to command {}".format(command))
                self.pause_processing(tags=command.tags)

            if command.code in self.flavor.blocked_commands:
                self._logger.info(
                    "Not sending {} to printer, it's configured as a blocked command".format(
                        command
                    )
                )
                return (None,)

    def _command_phase_sending(self, command, gcode=None):
        if gcode is not None and gcode.code in self.flavor.long_running_commands:
            self._internal_flags["long_running_command"] = True

    ##~~ autoreporting

    def _set_autoreport_temperature_interval(self, interval=None):
        if interval is None:
            try:
                interval = int(self.interval.get("temperature_autoreport", 2))
            except Exception:
                interval = 2
        self.send_commands(
            self.flavor.command_autoreport_temperature(interval=interval),
            tags={"trigger:set_autoreport_temperature_interval"},
        )

    def _set_autoreport_sdstatus_interval(self, interval=None):
        if interval is None:
            try:
                interval = int(self.interval.get("sd_status_autoreport", 1))
            except Exception:
                interval = 1
        self.send_commands(
            self.flavor.command_autoreport_sd_status(interval=interval),
            tags={"trigger:protocol.set_autoreport_sdstatus_interval"},
        )

    ##~~ busy protocol

    def _set_busy_protocol_interval(self, interval=None):
        if interval is None:
            try:
                interval = max(int(self.timeouts.get("communication_busy", 3)) - 1, 1)
            except Exception:
                interval = 2
        self.send_commands(
            self.flavor.command_busy_protocol_interval(interval),
            tags={"trigger:protocol.set_busy_protocol_interval"},
        )

    ##~~ helpers

    def _get_timeout(self, timeout_type):
        if timeout_type in self.timeouts:
            return time.monotonic() + self.timeouts[timeout_type]
        else:
            return time.monotonic()

    def _get_max_timeout(self, *offsets):
        if len(offsets) == 0:
            offsets = (0.0,)
        return time.monotonic() + max(offsets)

    def _to_logfile_with_terminal(self, message=None, level=logging.INFO):
        log = "Last lines in terminal:\n" + "\n".join(
            map(lambda x: "| {}".format(x), list(self._terminal_log))
        )
        if message is not None:
            log = message + "\n| " + log
        self._logger.log(level, log)

    def _switch_flavor(self, new_flavor):
        self.flavor = new_flavor.with_overrides(self.flavor_overrides)

        flavor_comm_attrs = self.get_flavor_attributes_starting_with(self.flavor, "comm_")
        flavor_message_attrs = self.get_flavor_attributes_starting_with(
            self.flavor, "message_"
        )
        flavor_error_attrs = self.get_flavor_attributes_starting_with(
            self.flavor, "error_"
        )

        self._comm_messages = flavor_comm_attrs
        self._registered_messages = flavor_comm_attrs + flavor_message_attrs
        self._current_registered_messages = self._registered_messages
        self._error_messages = flavor_error_attrs

    def _trigger_error(self, text, reason, disconnect=True):
        self.state = ProtocolState.ERROR
        self._event_bus.fire(Events.ERROR, {"error": text, "reason": reason})
        if disconnect:
            self.disconnect(error=True)
