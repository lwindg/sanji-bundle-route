#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import os
import netifaces
import logging
from threading import Lock
from time import sleep
from sanji.model import Model
import json
import re
import sh

import ip


_logger = logging.getLogger("sanji.route")
_update_default_lock = Lock()


class IPRouteError(Exception):
    pass


class IPRoute(Model):
    """
    A model to handle IP Route configuration.

    Attributes:
        model: database with json format.
    """

    UPDATE_INTERVAL = 60

    def __init__(self, *args, **kwargs):
        super(IPRoute, self).__init__(*args, **kwargs)

        self._path = kwargs["path"]
        self.interfaces = {}

        # find correct interface if shell command is required
        self._cmd_regex = re.compile(r"\$\(([\S\s]+)\)")
        self._routes = self._get_routes()

    def set_wan_event_cb(self, cb):
        self._wan_event_cb = cb

    def _get_routes(self):
        routes = []
        for iface in self.model.db:
            match = self._cmd_regex.match(iface)
            if not match:
                routes.append(iface)
                continue
            try:
                with open("{}/iface_cmd.sh".format(self._path_root), "w") as f:
                    f.write(match.group(1))
                _iface = sh.sh("{}/iface_cmd.sh".format(self._path_root))
                routes.append(str(_iface).rstrip())
            except Exception as e:
                _logger.debug(e)
        return routes

    def run(self):
        while True:
            sleep(self.UPDATE_INTERVAL)
            try:
                self.try_update_default(self._routes)
            except Exception as e:
                _logger.debug(e)

    def save(self):
        """
        Save and backup the configuration.
        """
        self.model.save_db()
        self.model.backup_db()

    def list_interfaces(self):
        """
        List available interfaces.
        """
        # retrieve all interfaces
        try:
            ifaces = ip.addr.interfaces()
        except:
            return {}

        # list connected interfaces
        data = []
        for iface in ifaces:
            try:
                iface_info = ip.addr.ifaddresses(iface)
            except:
                continue
            if iface_info["link"] is True:
                inet_ip = [inet["ip"]
                           for inet in iface_info["inet"]
                           if "" != inet["ip"]]
                if len(inet_ip) and \
                        (iface in self.interfaces and
                         self.interfaces[iface]["status"] is True and
                         self.interfaces[iface]["wan"] is True):
                    data.append(iface)
        return data

    def get_priority_list(self):
        """Get priority list settings for default route
        """
        return self.model.db

    def set_priority_list(self, priority_list):
        """Get priority list settings for default route
        """
        self.model.db = priority_list
        self.save()

        self._routes = self._get_routes()
        self.try_update_default(self._routes)
        return self.model.db

    def get_default(self):
        """
        Retrieve current default gateway

        Return:
            default: dict format with "interface" and/or "gateway"
        """
        gws = netifaces.gateways()
        default = {}
        if gws['default'] != {} and netifaces.AF_INET in gws['default']:
            gw = gws['default'][netifaces.AF_INET]
        else:
            return default

        default["wan"] = True
        default["status"] = True
        default["gateway"] = gw[0]
        default["interface"] = gw[1]
        return default

    def update_default(self, default):
        """
        Update default gateway. If updated failed, should recover to previous
        one.

        Args:
            default: dict format with "interface" required and "gateway"
                     optional.
        """
        # delete the default gateway
        if not default or ("interface" not in default and
                           "gateway" not in default):
            ip.route.delete("default")

        # change the default gateway
        # FIXME: only "gateway" without interface is also available
        # FIXME: add "secondary" default route rule
        else:
            ip.route.delete("default")
            if "gateway" in default and "interface" in default:
                ip.route.add("default", default["interface"],
                             default["gateway"])
            elif "interface" in default:
                ip.route.add("default", default["interface"])
            elif "gateway" in default:
                ip.route.add("default", "", default["gateway"])
            else:
                raise IPRouteError("Invalid default route.")

            # update DNS
            if "interface" in default and self._wan_event_cb:
                self._wan_event_cb(default["interface"])

    def _try_update_default(self, routes):
        """
        Try to update the default gateway.

        Args:
            routes: array format of default gateway list with priority.
                    For example:
                    ["wwan0", "eth0"]
        """
        ifaces = self.list_interfaces()
        if not ifaces:
            # FIXME: keep or clean?
            # self.update_default({})
            raise IPRouteError("Interfaces should be UP.")

        default = {}
        for iface in routes:
            if iface in ifaces:
                default["interface"] = iface
                break
        else:
            self.update_default({})
            return

        # find gateway by interface
        default.update(self.interfaces[default["interface"]])

        current = self.get_default()
        if current.get("interface", "") != default.get("interface", "") or \
                current.get("gateway", "") != default.get("gateway", ""):
            self.update_default(default)

    def try_update_default(self, routes):
        with _update_default_lock:
            try:
                self._try_update_default(routes)
            except IPRouteError as e:
                _logger.debug(e)

    def set_default(self, default):
        """
        Update default gateway by given info.
        """
        try:
            self.update_default(default)
        except Exception as e:
            # try database if failed
            try:
                self.try_update_default(self._routes)
            except IPRouteError as e2:
                _logger.debug(
                    "Failed to recover the default gateway: {}".format(e2))
            error = "Update default gateway failed: {}".format(e)
            _logger.error(error)
            raise IPRouteError(error)

    def update_iface_db(self, iface):
        """
        Save the interface name with its gateway and update the default
        gateway if needed.

        If gateway is not specified, use the previous value. Only delete the
        gateway when gateway attribute is empty.

        Args:
            interface: dict format with interface "name" and/or "gateway".
        """
        if "status" not in iface:
            iface["status"] = True
        if "wan" not in iface:
            iface["wan"] = True

        # update the router information
        if iface["name"] not in self.interfaces:
            self.interfaces[iface["name"]] = {}
        self.interfaces[iface["name"]]["status"] = iface["status"]
        self.interfaces[iface["name"]]["wan"] = iface["wan"]
        if "gateway" in iface:
            self.interfaces[iface["name"]]["gateway"] = iface["gateway"]

        # check if the default gateway need to be modified
        self.try_update_default(self._routes)

    def get_iface_db(self):
        return self.interfaces


if __name__ == "__main__":
    FORMAT = "%(asctime)s - %(levelname)s - %(lineno)s - %(message)s"
    logging.basicConfig(level=0, format=FORMAT)
    _logger = logging.getLogger("sanji.route")

    path = "/usr/lib/sanji-1.0.bak/route"
    route = IPRoute(name="route", path=path)