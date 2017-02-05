import asyncio

import six
from six.moves import urllib

from . import packet
from . import server
from . import asyncio_socket


class AsyncServer(server.Server):
    def is_asyncio_based(self):
        return True

    def async_modes(self):
        return ['aiohttp']

    def attach(self, app, engineio_path='engine.io'):
        self._async['create_route'](app, self, '/{}/'.format(engineio_path))

    async def send(self, sid, data, binary=None):
        """Send a message to a client.

        :param sid: The session id of the recipient client.
        :param data: The data to send to the client. Data can be of type
                     ``str``, ``bytes``, ``list`` or ``dict``. If a ``list``
                     or ``dict``, the data will be serialized as JSON.
        :param binary: ``True`` to send packet as binary, ``False`` to send
                       as text. If not given, unicode (Python 2) and str
                       (Python 3) are sent as text, and str (Python 2) and
                       bytes (Python 3) are sent as binary.
        """
        try:
            socket = self._get_socket(sid)
        except KeyError:
            # the socket is not available
            self.logger.warning('Cannot send to sid %s', sid)
            return
        await socket.send(packet.Packet(packet.MESSAGE, data=data,
                                        binary=binary))

    async def disconnect(self, sid=None):
        """Disconnect a client.

        :param sid: The session id of the client to close. If this parameter
                    is not given, then all clients are closed.
        """
        if sid is not None:
            self._get_socket(sid).close()
            del self.sockets[sid]
        else:
            for client in six.itervalues(self.sockets):
                client.close()
            self.sockets = {}

    async def handle_request(self, *args, **kwargs):
        """Handle an HTTP request from the client.

        This is the entry point of the Engine.IO application.

        This function returns the HTTP response to deliver to the client.
        """
        environ = self._async['translate_request'](*args, **kwargs)
        method = environ['REQUEST_METHOD']
        query = urllib.parse.parse_qs(environ.get('QUERY_STRING', ''))
        if 'j' in query:
            self.logger.warning('JSONP requests are not supported')
            r = self._bad_request()
        else:
            sid = query['sid'][0] if 'sid' in query else None
            b64 = False
            if 'b64' in query:
                if query['b64'][0] == "1" or query['b64'][0].lower() == "true":
                    b64 = True
            if method == 'GET':
                if sid is None:
                    transport = query.get('transport', ['polling'])[0]
                    if transport != 'polling' and transport != 'websocket':
                        self.logger.warning('Invalid transport %s', transport)
                        r = self._bad_request()
                    else:
                        r = await self._handle_connect(environ, transport,
                                                       b64)
                else:
                    if sid not in self.sockets:
                        self.logger.warning('Invalid session %s', sid)
                        r = self._bad_request()
                    else:
                        socket = self._get_socket(sid)
                        try:
                            packets = await socket.handle_get_request(environ)
                            if isinstance(packets, list):
                                r = self._ok(packets, b64=b64)
                            else:
                                r = packets
                        except IOError:
                            if sid in self.sockets:  # pragma: no cover
                                del self.sockets[sid]
                            r = self._bad_request()
                        if sid in self.sockets and self.sockets[sid].closed:
                            del self.sockets[sid]
            elif method == 'POST':
                if sid is None or sid not in self.sockets:
                    self.logger.warning('Invalid session %s', sid)
                    r = self._bad_request()
                else:
                    socket = self._get_socket(sid)
                    try:
                        await socket.handle_post_request(environ)
                        r = self._ok()
                    except ValueError:
                        r = self._bad_request()
            else:
                self.logger.warning('Method %s not supported', method)
                r = self._method_not_found()
        if not isinstance(r, dict):
            return r or []
        if self.http_compression and \
                len(r['response']) >= self.compression_threshold:
            encodings = [e.split(';')[0].strip() for e in
                         environ.get('ACCEPT_ENCODING', '').split(',')]
            for encoding in encodings:
                if encoding in self.compression_methods:
                    r['response'] = \
                        getattr(self, '_' + encoding)(r['response'])
                    r['headers'] += [('Content-Encoding', encoding)]
                    break
        cors_headers = self._cors_headers(environ)
        return self._async['make_response'](r['status'],
                                            r['headers'] + cors_headers,
                                            r['response'])

    def start_background_task(self, target, *args, **kwargs):
        raise RuntimeError('Not implemented, use asyncio.')

    def sleep(self, seconds=0):
        raise RuntimeError('Not implemented, use asyncio.')

    async def _handle_connect(self, environ, transport, b64=False):
        """Handle a client connection request."""
        sid = self._generate_id()
        s = asyncio_socket.AsyncSocket(self, sid)
        self.sockets[sid] = s

        pkt = packet.Packet(
            packet.OPEN, {'sid': sid,
                          'upgrades': self._upgrades(sid, transport),
                          'pingTimeout': int(self.ping_timeout * 1000),
                          'pingInterval': int(self.ping_interval * 1000)})
        await s.send(pkt)

        if await self._trigger_event('connect', sid, environ) is False:
            self.logger.warning('Application rejected connection')
            del self.sockets[sid]
            return self._unauthorized()

        if transport == 'websocket':
            return await s.handle_get_request(environ)
        else:
            s.connected = True
            headers = None
            if self.cookie:
                headers = [('Set-Cookie', self.cookie + '=' + sid)]
            return self._ok(await s.poll(), headers=headers, b64=b64)

    async def _trigger_event(self, event, *args, **kwargs):
        """Invoke an event handler."""
        if event in self.handlers:
            if asyncio.iscoroutinefunction(self.handlers[event]):
                await self.handlers[event](*args)
            else:
                self.handlers[event](*args)