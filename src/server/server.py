from __future__ import annotations

import json
import logging
import subprocess
from time import sleep, time
from os.path import expanduser

from utils import call_repeatedly

import psutil
from fabric import Connection
from sshconf import read_ssh_config
from wakeonlan import send_magic_packet
from paramiko.ssh_exception import SSHException, NoValidConnectionsError

logging.basicConfig(
    level=logging.CRITICAL,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%a %d-%b-%Y %H:%M:%S"
)
for _ in logging.root.manager.loggerDict:
    logging.getLogger(_).disabled = True

logger = logging.getLogger(__file__)
logger.setLevel(logging.DEBUG)


class Server:
    PING_RETRY_LIMIT = 4
    # seconds to wait before sending a WoL packet to a non-response client (when on mains power)
    # will also be used as the interval to check if the power is back on after a power outage.
    WOL_PACKET_INTERVAL = 10

    SSH_RETRY_LIMIT = 5
    SSH_RETRY_INTERVAL = 10  # in seconds

    BATTERY_CHECK_INTERVAL = 5  # seconds to wait before checking for a power outage.
    STATS_CHECK_INTERVAL = 10  # seconds to wait before fetching new statistics from client.

    SHUTDOWN_DELAY = 4  # how many minutes to wait before shutting down client after detecting an outage
    BATTERY_CHECK_INTERVAL_DURING_SHUTDOWN = 1  # value for BATTERY_CHECK_INTERVAL when a shutdown has been scheduled.

    UI = True

    def __init__(
            self,
            client: str,
            mac_address: str,
            shared_data: dict,
            battery_check_interval: int = None,
            stats_check_interval: int = None,
            shutdown_delay: int = None,
            ui: bool = None,
    ):
        """
        This class will run the main process which will perform all the main "protection" functionality, the TUI will
        run in another process and will rely on this process for collecting information to display.

        :param client: Must be a host declared in ~/.ssh/config
        :param mac_address: Mac address of the Wake on Lan interface of the server.
        """

        self.client = client
        self.mac_address = mac_address
        self.ip = read_ssh_config(expanduser("~/.ssh/config")).host(client)["hostname"]

        if battery_check_interval is not None:
            self.BATTERY_CHECK_INTERVAL = battery_check_interval
        if stats_check_interval is not None:
            self.STATS_CHECK_INTERVAL = stats_check_interval
        if shutdown_delay is not None:
            self.SHUTDOWN_DELAY = shutdown_delay
        if ui is not None:
            self.UI = ui

        self.connection: Connection | None = None

        # Will be an actual simple dictionary in case ui=False, otherwise `multiprocessing.Manager.dict`.
        self.shared_data: dict = shared_data

        self.kill_stats_loop = None

    @property
    def client_is_alive(self) -> bool:
        return subprocess.call(
            ["ping", "-c", str(self.PING_RETRY_LIMIT), str(self.ip)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        ) == 0

    @property
    def on_mains_power(self) -> bool:
        battery = psutil.sensors_battery()
        if battery is None:
            # Returning True because if you're running this on a machine without a battery, you're probably testing and
            # also if you think about it, on a desktop, battery status is always True ;)
            return True
        return battery.power_plugged

    def send_wol_packet(self):
        logger.info("Sending WoL Packet...")
        send_magic_packet(self.mac_address)

    def ssh_exec(self, cmd: str) -> str:
        logger.debug(f"Executing via SSH: '{cmd}'")
        return self.connection.run(cmd, hide=True).stdout.strip()

    def wait_for_client_wakeup(self) -> None:
        # Blocks execution until the client responds to pings.

        logger.info("Waiting for client wakeup...")

        while True:
            if self.client_is_alive:
                logger.info("Client is alive!")
                return

            # Don't wake up the client if there's no power.
            if self.on_mains_power:
                logger.info("Detected mains power but client is still not alive...")
                self.send_wol_packet()
            else:
                logger.info("Power outage detected...")

            logger.info(f"Waiting for {'boot' if self.on_mains_power else 'power'}...")
            sleep(self.WOL_PACKET_INTERVAL)

    def ssh_connect(self) -> bool:
        """
        Will establish an ssh connection, accessible via `Server.connection`.

        :return: True if ssh connection was successfully established, False otherwise.
        """
        retry_count = 0

        while retry_count < self.SSH_RETRY_LIMIT:
            try:
                # do your fancy ssh-fu in the ~/.ssh/config file, trying to keep this simple.
                self.connection = Connection(self.client)
                self.connection.open()
                self.shared_data["connection"] = True
                return True
            except SSHException:
                logger.error("Encountered error while establishing SSH connection.")
                retry_count += 1

        self.shared_data["connection"] = False
        return False

    def wait_for_connection(self):
        logger.info("Waiting for connection...")
        self.wait_for_client_wakeup()
        while not self.ssh_connect():
            self.wait_for_client_wakeup()
            sleep(self.SSH_RETRY_INTERVAL)
        logger.info("*" * 10 + " Connection Established " + "*" * 10)

    def battery_loop(self) -> bool:
        """
        :return: True if no shutdown was issued, False otherwise.
        """

        logger.info("Checking battery!")
        if self.on_mains_power:
            return True

        logger.critical("Power outage detected.")

        start_time = int(time())
        shutdown_time = start_time + int(self.SHUTDOWN_DELAY * 60)

        self.shared_data["shutdown_scheduled"] = True
        self.shared_data["shutdown_timestamp"] = shutdown_time  # epoch time for when the shutdown will be executed.

        # Execute shutdown delay
        while time() <= shutdown_time:
            if self.on_mains_power:
                self.shared_data["shutdown_scheduled"] = False
                del self.shared_data["shutdown_timestamp"]
                logger.info("Back on mains power, aborting shutdown!")
                return True

            sleep(self.BATTERY_CHECK_INTERVAL_DURING_SHUTDOWN)

        # Still no power, execute shutdown.
        logger.critical("!" * 10 + " SHUTDOWN ISSUED " + "!" * 10)
        self.ssh_exec("systemctl poweroff")
        self.connection.close()
        self.connection = None
        sleep(20)  # Arbitrary sleep value to give time to the SSH service to shut down.
        return False

    def stats_loop(self):
        try:
            self.shared_data["stats"] = json.loads(self.ssh_exec("bd-client"))
        except (SSHException, NoValidConnectionsError):
            pass

    def main(self):
        # Don't call this directly, call `Server.run` instead.

        while True:
            self.wait_for_connection()

            if self.UI:
                self.kill_stats_loop = call_repeatedly(self.STATS_CHECK_INTERVAL, self.stats_loop)

            logger.info("Entering battery loop...")
            while self.battery_loop():
                sleep(self.BATTERY_CHECK_INTERVAL)
            logger.info("Battery loop exit...")

            if self.UI:
                self.kill_stats_loop()

    def run(self) -> None:
        # Main function to start the server, do not call `Server.main` directly.

        logger.info("#" * 20 + " Session Start " + "#" * 20)

        while True:
            try:
                self.main()
            except (SSHException, NoValidConnectionsError):
                if self.connection is not None:
                    self.connection.close()
                self.connection = None
            except KeyboardInterrupt:
                if self.connection is not None:
                    self.connection.close()
                return
