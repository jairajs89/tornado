from tornado.escape import json_decode, utf8
from tornado.iostream import IOStream
from tornado.testing import LogTrapTestCase, AsyncHTTPTestCase
from tornado.util import b
from tornado.web import RequestHandler, _O, authenticated, Application, asynchronous

import binascii
import logging
import re
import socket
import tornado.ioloop

class CookieTestRequestHandler(RequestHandler):
    # stub out enough methods to make the secure_cookie functions work
    def __init__(self):
        # don't call super.__init__
        self._cookies = {}
        self.application = _O(settings=dict(cookie_secret='0123456789'))

    def get_cookie(self, name):
        return self._cookies.get(name)

    def set_cookie(self, name, value, expires_days=None):
        self._cookies[name] = value

class SecureCookieTest(LogTrapTestCase):
    def test_round_trip(self):
        handler = CookieTestRequestHandler()
        handler.set_secure_cookie('foo', b('bar'))
        self.assertEqual(handler.get_secure_cookie('foo'), b('bar'))

    def test_cookie_tampering_future_timestamp(self):
        handler = CookieTestRequestHandler()
        # this string base64-encodes to '12345678'
        handler.set_secure_cookie('foo', binascii.a2b_hex(b('d76df8e7aefc')))
        cookie = handler._cookies['foo']
        match = re.match(b(r'12345678\|([0-9]+)\|([0-9a-f]+)'), cookie)
        assert match
        timestamp = match.group(1)
        sig = match.group(2)
        self.assertEqual(handler._cookie_signature('foo', '12345678',
                                                   timestamp), sig)
        # shifting digits from payload to timestamp doesn't alter signature
        # (this is not desirable behavior, just confirming that that's how it
        # works)
        self.assertEqual(
            handler._cookie_signature('foo', '1234', b('5678') + timestamp),
            sig)
        # tamper with the cookie
        handler._cookies['foo'] = utf8('1234|5678%s|%s' % (timestamp, sig))
        # it gets rejected
        assert handler.get_secure_cookie('foo') is None

class CookieTest(AsyncHTTPTestCase, LogTrapTestCase):
    def get_app(self):
        class SetCookieHandler(RequestHandler):
            def get(self):
                # Try setting cookies with different argument types
                # to ensure that everything gets encoded correctly
                self.set_cookie("str", "asdf")
                self.set_cookie("unicode", u"qwer")
                self.set_cookie("bytes", b("zxcv"))

        class GetCookieHandler(RequestHandler):
            def get(self):
                self.write(self.get_cookie("foo"))

        return Application([
                ("/set", SetCookieHandler),
                ("/get", GetCookieHandler)])

    def test_set_cookie(self):
        response = self.fetch("/set")
        self.assertEqual(response.headers.get_list("Set-Cookie"),
                         ["str=asdf; Path=/",
                          "unicode=qwer; Path=/",
                          "bytes=zxcv; Path=/"])

    def test_get_cookie(self):
        response = self.fetch("/get", headers={"Cookie": "foo=bar"})
        self.assertEqual(response.body, b("bar"))

class AuthRedirectRequestHandler(RequestHandler):
    def initialize(self, login_url):
        self.login_url = login_url

    def get_login_url(self):
        return self.login_url

    @authenticated
    def get(self):
        # we'll never actually get here because the test doesn't follow redirects
        self.send_error(500)

class AuthRedirectTest(AsyncHTTPTestCase, LogTrapTestCase):
    def get_app(self):
        return Application([('/relative', AuthRedirectRequestHandler,
                             dict(login_url='/login')),
                            ('/absolute', AuthRedirectRequestHandler,
                             dict(login_url='http://example.com/login'))])

    def test_relative_auth_redirect(self):
        self.http_client.fetch(self.get_url('/relative'), self.stop,
                               follow_redirects=False)
        response = self.wait()
        self.assertEqual(response.code, 302)
        self.assertEqual(response.headers['Location'], '/login?next=%2Frelative')

    def test_absolute_auth_redirect(self):
        self.http_client.fetch(self.get_url('/absolute'), self.stop,
                               follow_redirects=False)
        response = self.wait()
        self.assertEqual(response.code, 302)
        self.assertTrue(re.match(
            'http://example.com/login\?next=http%3A%2F%2Flocalhost%3A[0-9]+%2Fabsolute',
            response.headers['Location']), response.headers['Location'])


class ConnectionCloseHandler(RequestHandler):
    def initialize(self, test):
        self.test = test

    @asynchronous
    def get(self):
        self.test.on_handler_waiting()

    def on_connection_close(self):
        self.test.on_connection_close()

class ConnectionCloseTest(AsyncHTTPTestCase, LogTrapTestCase):
    def get_app(self):
        return Application([('/', ConnectionCloseHandler, dict(test=self))])

    def test_connection_close(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        s.connect(("localhost", self.get_http_port()))
        self.stream = IOStream(s, io_loop=self.io_loop)
        self.stream.write(b("GET / HTTP/1.0\r\n\r\n"))
        self.wait()

    def on_handler_waiting(self):
        logging.info('handler waiting')
        self.stream.close()

    def on_connection_close(self):
        logging.info('connection closed')
        self.stop()

class EchoHandler(RequestHandler):
    def get(self, path):
        self.write(dict(path=path, args=self.request.arguments))

class RequestEncodingTest(AsyncHTTPTestCase, LogTrapTestCase):
    def get_app(self):
        return Application([("/(.*)", EchoHandler)])

    def test_question_mark(self):
        # Ensure that url-encoded question marks are handled properly
        self.assertEqual(json_decode(self.fetch('/%3F').body),
                         dict(path='?', args={}))
        self.assertEqual(json_decode(self.fetch('/%3F?%3F=%3F').body),
                         dict(path='?', args={'?': ['?']}))

