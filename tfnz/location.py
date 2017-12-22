# Copyright (c) 2017 David Preece, All rights reserved.
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

import enum
import logging
import socket
import time
import requests
from base64 import b64decode
from subprocess import run, CalledProcessError, DEVNULL
from requests.exceptions import ConnectionError
from messidge import default_location
from messidge.client.connection import Connection
from . import TaggedCollection, Taggable, Waitable
from .docker import Docker
from .endpoint import WebEndpoint
from .node import Node
from .send import Sender
from .tunnel import Tunnel
from .volume import Volume
from .container import ExternalContainer


class RankBias(enum.Enum):
    """An enumeration to set priorities when ranking nodes"""
    cpu = 0
    memory = 1


class Location(Waitable):
    """The root location object.

        :param location: An optional fqdn of the location (i.e. tiny.20ft.nz).
        :param location_ip: A optional explicit ip for the broker.
        :param quiet: Set true to not configure logging.
        :param debug_log: Set true to log at DEBUG logging level.
        """

    def __init__(self, location: str=None, *, location_ip: str=None,
                 quiet: bool=False, debug_log: bool=False):
        super().__init__()

        # collect parameters
        self.location = location if location is not None else default_location(prefix="~/.20ft")
        self.nodes = {}
        self.volumes = TaggedCollection()
        self.last_best_nodes = None
        self.last_best_node_idx = None
        self.tunnels = {}
        self.endpoints = {}
        self.domains = None
        self.last_heartbeat = time.time()

        # see if we even can connect...
        ip = location_ip if location_ip is not None else self.location
        try:
            run(['ping', '-c', '1', ip], check=True, stdout=DEVNULL)
        except CalledProcessError:
            raise RuntimeError("Cannot ping the requested ip: " + ip)

        # set up logging
        if debug_log and quiet:
            raise ValueError("Can't select both quiet and verbose logging")
        if debug_log or quiet is False:
            logging.basicConfig(level=logging.DEBUG if debug_log else logging.INFO,
                                format='%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s',
                                datefmt='%m%d%H%M%S')

        # connect
        self.conn = Connection(self.location, prefix="~/.20ft", location_ip=location_ip, exit_on_exception=True)
        self.user_pk = self.conn.keys.public_binary()
        self.conn.register_commands(self, Location._commands)
        self.conn.start()
        self.conn.wait_until_ready()  # will throw if the connection had a problem
        self.wait_until_ready()  # doesn't return until a resource offer is made
        self.conn.loop.register_on_idle(self._heartbeat)

    def disconnect(self, container=None):
        """Disconnect from the location - without calling this the object cannot be garbage collected"""
        # the container is passed if you use 'disconnect' as a termination function on a container
        # i.e. it needs to be there, don't take it off!
        if self.conn is None:  # already disconnected
            return
        logging.info("Disconnecting")
        for endpoint in list(self.endpoints.values()):
            [endpoint.unpublish(cluster) for cluster in list(endpoint.clusters.values())]
        self.endpoints.clear()
        for tunnel in list(self.tunnels.values()):
            tunnel.destroy()
        self.tunnels.clear()
        self.conn.disconnect()
        self.conn = None
        self.nodes.clear()

    def node(self,) -> [Node]:
        """Returns a node.

           :return: A node object"""
        return self.ranked_nodes()[0]

    def ranked_nodes(self, bias: RankBias=RankBias.memory) -> [Node]:
        """Ranks the nodes in order of resource availability.

        :param bias: prioritise memory or cpu availability
        :return: A list of node objects.

        Note that the difference in processor performance is accounted for and is measured in passmarks."""
        nodes = self.nodes.values()
        if len(nodes) == 0:
            raise ValueError("The location has no nodes")
        if bias == RankBias.cpu:
            return sorted(nodes, key=lambda node: node.stats['cpu'], reverse=True)
        if bias == RankBias.memory:
            return sorted(nodes, key=lambda node: node.stats['memory'], reverse=True)

    def create_volume(self, tag: str=None, async: bool=True) -> Volume:
        """Creates a new volume

        :param tag: A globally visible tag (to make the volume globally visible).
        :param async: Enables asynchronous writes.
        :return: The new Volume object.

        Note that asynchronous writes cannot damage a ZFS filesystem although the physical state may lag behind the
        logical state by a number of seconds. Asynchronous ZFS is *very* much faster than synchronous."""
        tag = Taggable.valid_tag(tag)
        msg = self.conn.send_blocking_cmd(b'create_volume', {'user': self.user_pk,
                                                             'tag': tag,
                                                             'async': async})
        logging.info("Created volume: " + str(msg.uuid))
        vol = Volume(self, msg.uuid, tag)
        self.volumes.add(vol)
        return vol

    def destroy_volume(self, volume: Volume):
        """Destroys an existing volume. This is not a 'move to trash', it will be destroyed.

        :param volume: The volume to be destroyed."""
        if not isinstance(volume, Volume):
            raise TypeError()
        self.conn.send_blocking_cmd(b'destroy_volume', {'user': self.user_pk,
                                                        'volume': volume.uuid})
        logging.info("Destroyed volume: " + str(volume.uuid))
        self.volumes.remove(volume)

    def all_volumes(self) -> [Volume]:
        """Returns a list of all volumes on this node.

        :return: A list of Volume objects."""
        return list(self.volumes.values())

    def volume(self, key) -> Volume:
        """Return the volume with this uuid, tag or display_name.

        :param key: The uuid or tag of the volume object to be returned.
        :return: A Volume object."""
        return self.volumes.get(self.user_pk, key)

    def endpoint_for(self, fqdn) -> WebEndpoint:
        """Return a WebEndpoint for the given fqdn.

        :param fqdn: The fully qualified name the endpoint will represent.
        :return: A WebEndpoint object."""
        for domain, ep in self.endpoints.items():
            if fqdn.endswith(domain):
                return ep
        raise ValueError("There is no endpoint capable of serving: " + fqdn)

    def container_for(self, tag) -> ExternalContainer:
        """Return a connection onto a container owned by another session, but advertised through a tag.

        :param tag: The tag the container was created with.
        :return: An ExternalContainer object."""
        msg = self.conn.send_blocking_cmd(b'find_tag', {'tag': tag})
        return ExternalContainer(self.conn, msg.params['uuid'], msg.params['node'], msg.params['ip'])

    def ensure_image_uploaded(self, docker_image_id: str, descr: dict=None):
        """Sends missing docker layers to the location.

        :param docker_image_id: use the short form id or name:tag
        :param descr: a previously found docker description

        This is not a necessary step and is implied when spawning a container unless specifically disabled.
        The layers are uploaded on a background thread."""

        # Send the missing layers (if any)
        if descr is None:
            descr = Docker.description(docker_image_id, self.conn)
        layers = Sender.layer_stack(descr)
        to_upload = Sender.upload_requirements(layers, self.conn)  # if none, does not need a local docker
        logging.info("Ensuring layers (%d) are uploaded for: %s" % (len(layers), docker_image_id))
        if len(to_upload) > 0:
            logging.info("Layers to upload: %d of %d" % (len(to_upload), len(layers)))
            Sender.send(docker_image_id, to_upload, self.conn)
        return layers

    def _heartbeat(self):
        if time.time() - self.last_heartbeat < 60:
            return
        self.last_heartbeat = time.time()
        self.conn.send_cmd(b'heartbeat')

    def _tunnel_onto(self, container, port, localport, bind) -> Tunnel:
        # called from Container
        if isinstance(port, str):
            port = int(port)
        if isinstance(localport, str):
            localport = int(localport)

        # create the tunnel
        container.wait_until_ready()  # otherwise the IP address may not exist on the node and creation will fail
        tunnel = Tunnel(self.conn, container.parent(), container, port, localport, bind)
        self.tunnels[tunnel.uuid] = tunnel
        tunnel.connect()  # connection done 'late' so we can get the tunnel into tunnels first
        return tunnel

    def _wait_http_200(self, container, dest_port, fqdn, path, localport=None) -> Tunnel:
        # called from Container
        # needs to resolve to localhost because that's where the tunnel will be
        addr = socket.gethostbyname(fqdn)
        if addr != '127.0.0.1':
            raise ValueError("FQDN '%s' does not resolve to localhost" % fqdn)
        logging.info("Waiting on http 200: " + str(container.uuid))

        # OK
        tnl = self._tunnel_onto(container, dest_port, localport, None)
        url = 'http://%s:%d/%s' % (fqdn, tnl.localport(), path if path is not None else '')

        # poll until it's alive
        attempts_remaining = 60
        while True:
            try:
                r = requests.get(url)
                if r.status_code == 200:
                    logging.info("Connected onto: " + url)
                    break
            except ConnectionError:
                pass
            attempts_remaining -= 1
            if attempts_remaining == 0:
                raise ValueError("Could not connect to: " + url)
            time.sleep(0.5)
        return tnl

    def _destroy_tunnel(self, tunnel: Tunnel, container=None):
        # Called from Container
        if container is not None:
            if tunnel.container != container:
                raise ValueError("Tried to destroy a tunnel actually connected to a different container")
        tunnel.destroy()
        del self.tunnels[tunnel.uuid]

    def _from_proxy(self, msg):
        try:
            self.tunnels[msg.uuid].from_proxy(msg)
        except KeyError:
            logging.debug("Data arrived from a proxy we already closed")

    def _close_proxy(self, msg):
        try:
            self.tunnels[msg.uuid].close_proxy(msg)
        except KeyError:
            logging.debug("Asked to close a proxy that we already closed")

    def _resource_offer(self, msg):
        self.endpoints = {dom['domain']: WebEndpoint(self.conn, dom['domain']) for dom in msg.params['domains']}
        self.nodes = {node[0]: Node(self, node[0], self.conn, node[1]) for node in msg.params['nodes']}
        self.volumes = TaggedCollection([Volume(self, vol['uuid'], vol['tag']) for vol in msg.params['volumes']])

        self.mark_as_ready()  # only ready once we've dealt with the resource offer

    def _update_stats(self, msg):
        if msg.params['node'] not in self.nodes:
            logging.debug("Received updated stats from a node we didn't know existed: " + str(msg.params['node']))
            return
        node = self.nodes[msg.params['node']]
        node._update_stats(msg.params['stats'])
        self.last_best_nodes = None  # force a refresh next time 'best node' is called

    def _log(self, msg):
        if msg.params['error']:
            logging.error(msg.params['log'])
        else:
            logging.info(msg.params['log'])

    _commands = {b'resource_offer': ([], False),
                 b'update_stats': (['node', 'stats'], False),
                 b'from_proxy': (['proxy'], False),
                 b'close_proxy': (['proxy'], False),
                 b'log': (['error', 'log'], False)}

    def __repr__(self):
        return "<tfnz.location.Location object at %x (nodes=%d)>" % (id(self), len(self.nodes))
