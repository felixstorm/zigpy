import logging

import zigpy.appdb
import zigpy.device
import zigpy.quirks
import zigpy.types as t
import zigpy.util
import zigpy.zcl
import zigpy.zdo

LOGGER = logging.getLogger(__name__)


class ControllerApplication(zigpy.util.ListenableMixin):
    def __init__(self, database_file=None):
        self._send_sequence = 0
        self.devices = {}
        self._listeners = {}
        self._ieee = None
        self._nwk = None

        if database_file is not None:
            self._dblistener = zigpy.appdb.PersistingListener(database_file, self)
            self.add_listener(self._dblistener)
            self._dblistener.load()

    async def startup(self, auto_form=False):
        """Perform a complete application startup"""
        raise NotImplementedError

    async def form_network(self, channel=15, pan_id=None, extended_pan_id=None):
        """Form a new network"""
        raise NotImplementedError

    def add_device(self, ieee, nwk):
        assert isinstance(ieee, t.EUI64)
        # TODO: Shut down existing device

        dev = zigpy.device.Device(self, ieee, nwk)
        self.devices[ieee] = dev
        return dev

    def device_initialized(self, device):
        """Used by a device to signal that it is initialized"""
        self.listener_event('raw_device_initialized', device)
        device = zigpy.quirks.get_device(device)
        self.devices[device.ieee] = device
        self.listener_event('device_initialized', device)

    async def remove(self, ieee):
        assert isinstance(ieee, t.EUI64)
        dev = self.devices.pop(ieee, None)
        if not dev:
            LOGGER.debug("Device not found for removal: %s", ieee)
            return
        LOGGER.info("Removing device 0x%04x (%s)", dev.nwk, ieee)

        # Only force device to leave if we're the ZigBee coordinator ourselves
        if self.nwk == 0x0000:
            zdo_worked = False
            try:
                resp = await dev.zdo.leave()
                zdo_worked = resp[0] == 0
            except Exception as exc:
                pass
            if not zdo_worked:
                await self.force_remove(dev)
        else:
            LOGGER.info("  Somebody else is coordinating this ZigBee network, so just removing from our database.")

        self.listener_event('device_removed', dev)

    async def force_remove(self, dev):
        raise NotImplementedError

    def deserialize(self, sender, endpoint_id, cluster_id, data):
        return sender.deserialize(endpoint_id, cluster_id, data)

    def handle_message(self, sender, is_reply, profile, cluster, src_ep, dst_ep, tsn, command_id, args):
        return sender.handle_message(is_reply, profile, cluster, src_ep, dst_ep, tsn, command_id, args)

    def handle_join(self, nwk, ieee, parent_nwk):
        LOGGER.info("Device 0x%04x (%s) joined the network", nwk, ieee)
        if ieee in self.devices:
            dev = self.get_device(ieee)
            if dev.nwk != nwk:
                LOGGER.debug("Device %s changed id (0x%04x => 0x%04x)", ieee, dev.nwk, nwk)
                dev.nwk = nwk
            elif dev.initializing or dev.status == zigpy.device.Status.ENDPOINTS_INIT:
                LOGGER.debug("Skip initialization for existing device %s", ieee)
                return
        else:
            dev = self.add_device(ieee, nwk)

        self.listener_event('device_joined', dev)
        dev.schedule_initialize()

    def handle_leave(self, nwk, ieee):
        LOGGER.info("Device 0x%04x (%s) left the network", nwk, ieee)
        dev = self.devices.get(ieee, None)
        if dev is not None:
            self.listener_event('device_left', dev)

    def add_update_device_from_network(self, nwk, ieee):
        LOGGER.info("Adding or updating device 0x%04x (%s) from the network", nwk, ieee)
        if ieee in self.devices:
            dev = self.get_device(ieee)
            if dev.nwk != nwk:
                LOGGER.debug("Device %s changed id (0x%04x => 0x%04x)", ieee, dev.nwk, nwk)
                dev.nwk = nwk
            elif dev.initializing:
                LOGGER.warning("Skipping initialization as device is already initializing %s", ieee)
                return dev
            dev.status = zigpy.device.Status.NEW
        else:
            dev = self.add_device(ieee, nwk)

        dev.schedule_initialize()

        return dev

    @zigpy.util.retryable_request
    async def request(self, nwk, profile, cluster, src_ep, dst_ep, sequence, data, expect_reply=True, timeout=10):
        raise NotImplementedError

    def permit(self, time_s=60):
        raise NotImplementedError

    def permit_with_key(self, node, code, time_s=60):
        raise NotImplementedError

    def get_sequence(self):
        self._send_sequence = (self._send_sequence + 1) % 256
        return self._send_sequence

    def get_device(self, ieee=None, nwk=None):
        if ieee is not None:
            return self.devices[ieee]

        for dev in self.devices.values():
            # TODO: Make this not terrible
            if dev.nwk == nwk:
                return dev

        raise KeyError

    @property
    def ieee(self):
        return self._ieee

    @property
    def nwk(self):
        return self._nwk
