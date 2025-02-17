import threading
from typing import List, Tuple

import pytest
import requests
import werkzeug
from werkzeug.exceptions import MethodNotAllowed, NotFound
from werkzeug.routing import RequestRedirect

from localstack.http import Request, Response, Router
from localstack.http.router import E, RequestArguments, route
from localstack.utils.common import get_free_tcp_port


def noop(*args, **kwargs):
    """Test dispatcher that does nothing"""
    return Response()


def echo_params_json(request: Request, params: dict[str, str]):
    """Test dispatcher that echoes the url match parameters as json"""
    r = Response()
    r.set_json(params)
    return r


class RequestCollector:
    """Test dispatcher that collects requests into a list"""

    requests: List[Tuple[Request, E, RequestArguments]]

    def __init__(self) -> None:
        super().__init__()
        self.requests = []

    def __call__(self, request: Request, endpoint: E, args: RequestArguments) -> Response:
        self.requests.append((request, endpoint, args))
        return Response()


class TestRouter:
    # these are sanity check for the router and dispatching logic. since the matching is done by werkzeug's Map,
    # there is no need for thorough testing URL matching.

    def test_dispatch_raises_not_found(self):
        router = Router()
        router.add("/foobar", noop)
        with pytest.raises(NotFound):
            assert router.dispatch(Request("GET", "/foo"))

    def test_default_dispatcher_invokes_correct_endpoint(self):
        router = Router()

        def index(_: Request, args) -> Response:
            response = Response()
            response.set_json(args)
            return response

        def users(_: Request, args) -> Response:
            response = Response()
            response.set_json(args)
            return response

        router.add("/", index)
        router.add("/users/<int:user_id>", users)

        assert router.dispatch(Request("GET", "/")).json == {}
        assert router.dispatch(Request("GET", "/users/12")).json == {"user_id": 12}

    def test_dispatch_with_host_matching(self):
        router = Router()

        def ep_all(_: Request, args) -> Response:
            response = Response()
            response.set_json(dict(method="all", **args))
            return response

        def ep_index1(_: Request, args) -> Response:
            response = Response()
            response.set_json(dict(method="1", **args))
            return response

        def ep_index2(_: Request, args) -> Response:
            response = Response()
            response.set_json(dict(method="2", **args))
            return response

        router.add("/", ep_index1, host="localhost:<port>")
        router.add("/", ep_index2, host="localhost:12345")
        router.add("/all", ep_all, host="<host>")

        def invoke(path, server, port):
            return router.dispatch(Request("GET", path, server=(server, port))).json

        assert invoke("/", "localhost", 4566) == {"method": "1", "port": "4566"}
        assert invoke("/", "localhost", 12345) == {"method": "2"}
        assert invoke("/all", "127.0.0.1", None) == {"method": "all", "host": "127.0.0.1"}
        assert invoke("/all", "127.0.0.1", 12345) == {"method": "all", "host": "127.0.0.1:12345"}

        with pytest.raises(NotFound):
            invoke("/", "localstack.cloud", None)

    def test_custom_dispatcher(self):
        collector = RequestCollector()
        router = Router(dispatcher=collector)

        router.add("/", "index")
        router.add("/users/<int:id>", "users")

        router.dispatch(Request("GET", "/"))
        router.dispatch(Request("GET", "/users/12"))

        _, endpoint, args = collector.requests[0]
        assert endpoint == "index"
        assert args == {}

        _, endpoint, args = collector.requests[1]
        assert endpoint == "users"
        assert args == {"id": 12}

    def test_regex_path_dispatcher(self):
        router = Router()
        rgx = r"([^.]+)endpoint(.*)"
        regex = f"/<regex('{rgx}'):dist>/"
        router.add(path=regex, endpoint=noop)
        assert router.dispatch(Request(method="GET", path="/test-endpoint"))
        with pytest.raises(NotFound):
            router.dispatch(Request(method="GET", path="/test-not-point"))

    def test_regex_host_dispatcher(self):
        router = Router()
        rgx = r"\.cloudfront.(net|localhost\.localstack\.cloud)"
        router.add(path="/", endpoint=noop, host=f"<dist_id><regex('{rgx}'):host>:<port>")
        assert router.dispatch(
            Request(
                method="GET",
                headers={"Host": "ad91f538.cloudfront.localhost.localstack.cloud:5446"},
            )
        )
        with pytest.raises(NotFound):
            router.dispatch(
                Request(
                    method="GET",
                    headers={"Host": "ad91f538.cloudfront.amazon.aws.com:5446"},
                )
            )

    def test_port_host_dispatcher(self):
        collector = RequestCollector()
        router = Router(dispatcher=collector)
        router.add(path="/", endpoint=noop, host="localhost.localstack.cloud<port:port>")
        # matches with the port!
        assert router.dispatch(
            Request(
                method="GET",
                headers={"Host": "localhost.localstack.cloud:4566"},
            )
        )
        assert collector.requests.pop()[2] == {"port": 4566}
        # matches without the port!
        assert router.dispatch(
            Request(
                method="GET",
                headers={"Host": "localhost.localstack.cloud"},
            )
        )
        assert collector.requests.pop()[2] == {"port": None}

        # invalid port
        with pytest.raises(NotFound):
            router.dispatch(
                Request(
                    method="GET",
                    headers={"Host": "localhost.localstack.cloud:544a6"},
                )
            )

        # does not match the host
        with pytest.raises(NotFound):
            router.dispatch(
                Request(
                    method="GET",
                    headers={"Host": "localstack.cloud:5446"},
                )
            )

    def test_path_converter(self):
        router = Router()
        router.add(path="/<path:path>", endpoint=echo_params_json)

        assert router.dispatch(Request(path="/my")).json == {"path": "my"}
        assert router.dispatch(Request(path="/my/")).json == {"path": "my/"}
        assert router.dispatch(Request(path="/my//path")).json == {"path": "my//path"}
        assert router.dispatch(Request(path="/my//path/")).json == {"path": "my//path/"}
        assert router.dispatch(Request(path="/my/path foobar")).json == {"path": "my/path foobar"}
        assert router.dispatch(Request(path="//foobar")).json == {"path": "foobar"}
        assert router.dispatch(Request(path="//foobar/")).json == {"path": "foobar/"}

    def test_path_converter_with_args(self):
        router = Router()
        router.add(path="/with-args/<some_id>/<path:path>", endpoint=echo_params_json)

        assert router.dispatch(Request(path="/with-args/123456/my")).json == {
            "some_id": "123456",
            "path": "my",
        }

        # werkzeug no longer removes trailing slashes in matches
        assert router.dispatch(Request(path="/with-args/123456/my/")).json == {
            "some_id": "123456",
            "path": "my/",
        }

        # works with sub paths
        assert router.dispatch(Request(path="/with-args/123456/my/path")).json == {
            "some_id": "123456",
            "path": "my/path",
        }

        # no sub path raises 404
        with pytest.raises(NotFound):
            router.dispatch(Request(path="/with-args/123456"))

        with pytest.raises(NotFound):
            router.dispatch(Request(path="/with-args/123456/"))

        # with the default slash behavior of the URL map (merge_slashes=False), werkzeug tries to redirect
        # the call to /with-args/123456/my/ (note: this is desirable for web servers, not always for us
        # though)
        with pytest.raises(RequestRedirect):
            assert router.dispatch(Request(path="/with-args/123456//my/"))

    def test_path_converter_and_regex_converter_in_host(self):
        router = Router()
        router.add(
            path="/<path:path>",
            host="foobar.us-east-1.opensearch.localhost.localstack.cloud<regex('(?::.*)?'):port>",
            endpoint=echo_params_json,
        )
        assert router.dispatch(
            Request(
                method="GET",
                path="/_cluster/health",
                headers={"Host": "foobar.us-east-1.opensearch.localhost.localstack.cloud:4566"},
            )
        ).json == {"path": "_cluster/health", "port": ":4566"}

    def test_path_converter_and_port_converter_in_host(self):
        router = Router()
        router.add(
            path="/<path:path>",
            host="foobar.us-east-1.opensearch.localhost.localstack.cloud<port:port>",
            endpoint=echo_params_json,
        )
        assert router.dispatch(
            Request(
                method="GET",
                path="/_cluster/health",
                headers={"Host": "foobar.us-east-1.opensearch.localhost.localstack.cloud:4566"},
            )
        ).json == {"path": "_cluster/health", "port": 4566}

        assert router.dispatch(
            Request(
                method="GET",
                path="/_cluster/health",
                headers={"Host": "foobar.us-east-1.opensearch.localhost.localstack.cloud"},
            )
        ).json == {"path": "_cluster/health", "port": None}

    def test_path_converter_and_greedy_regex_in_host(self):
        router = Router()
        router.add(
            path="/<path:path>",
            # note how the regex '.*' will also include the port (so port will not do anything)
            host="foobar.us-east-1.opensearch.<regex('.*'):host><port:port>",
            endpoint=echo_params_json,
        )
        assert router.dispatch(
            Request(
                method="GET",
                path="/_cluster/health",
                headers={"Host": "foobar.us-east-1.opensearch.localhost.localstack.cloud:4566"},
            )
        ).json == {
            "path": "_cluster/health",
            "host": "localhost.localstack.cloud:4566",
            "port": None,
        }

    def test_remove_rule(self):
        router = Router()

        def index(_: Request, args) -> Response:
            return Response(b"index")

        def users(_: Request, args) -> Response:
            return Response(b"users")

        rule0 = router.add("/", index)
        rule1 = router.add("/users/<int:user_id>", users)

        assert router.dispatch(Request("GET", "/")).data == b"index"
        assert router.dispatch(Request("GET", "/users/12")).data == b"users"

        router.remove_rule(rule1)

        assert router.dispatch(Request("GET", "/")).data == b"index"
        with pytest.raises(NotFound):
            assert router.dispatch(Request("GET", "/users/12"))

        router.remove_rule(rule0)
        with pytest.raises(NotFound):
            assert router.dispatch(Request("GET", "/"))
        with pytest.raises(NotFound):
            assert router.dispatch(Request("GET", "/users/12"))

    def test_remove_rules(self):
        router = Router()

        class MyRoutes:
            @route("/a")
            @route("/a2")
            def route_a(self, request, args):
                return Response(b"a")

            @route("/b")
            def route_b(self, request, args):
                return Response(b"b")

        rules = router.add(MyRoutes())

        assert router.dispatch(Request("GET", "/a")).data == b"a"
        assert router.dispatch(Request("GET", "/a2")).data == b"a"
        assert router.dispatch(Request("GET", "/b")).data == b"b"

        router.remove(rules)

        with pytest.raises(NotFound):
            assert router.dispatch(Request("GET", "/a"))

        with pytest.raises(NotFound):
            assert router.dispatch(Request("GET", "/a2"))

        with pytest.raises(NotFound):
            assert router.dispatch(Request("GET", "/b"))

    def test_remove_non_existing_rule(self):
        router = Router()

        def index(_: Request, args) -> Response:
            return Response(b"index")

        rule = router.add("/", index)
        router.remove_rule(rule)

        with pytest.raises(KeyError) as e:
            router.remove_rule(rule)
        e.match("no such rule")

    def test_router_route_decorator(self):
        router = Router()

        @router.route("/users")
        @router.route("/alternative-users")
        def user(_: Request, args):
            assert not args
            return Response("user")

        @router.route("/users/<int:user_id>")
        def user_id(_: Request, args):
            assert args
            return Response(f"{args['user_id']}")

        assert router.dispatch(Request("GET", "/users")).data == b"user"
        assert router.dispatch(Request("GET", "/alternative-users")).data == b"user"
        assert router.dispatch(Request("GET", "/users/123")).data == b"123"

    def test_add_route_endpoint_with_object(self):
        class MySuperApi:
            @route("/users")
            def user(self, _: Request, args):
                # should be inherited
                assert not args
                return Response("user")

        class MyApi(MySuperApi):
            @route("/users/<int:user_id>")
            def user_id(self, _: Request, args):
                assert args
                return Response(f"{args['user_id']}")

            def foo(self, _: Request, args):
                # should be ignored
                raise NotImplementedError

        api = MyApi()
        router = Router()
        rules = router.add(api)
        assert len(rules) == 2

        assert router.dispatch(Request("GET", "/users")).data == b"user"
        assert router.dispatch(Request("GET", "/users/123")).data == b"123"

    def test_add_route_endpoint_with_object_per_method(self):
        # tests whether there can be multiple rules with different methods to the same URL
        class MyApi:
            @route("/my_api", methods=["GET"])
            def do_get(self, request: Request, _args):
                # should be inherited
                return Response(f"{request.path}/do-get")

            @route("/my_api", methods=["HEAD"])
            def do_head(self, request: Request, _args):
                # should be inherited
                return Response(f"{request.path}/do-head")

            @route("/my_api", methods=["POST", "PUT"])
            def do_post(self, request: Request, _args):
                # should be inherited
                return Response(f"{request.path}/do-post-or-put")

        api = MyApi()
        router = Router()
        rules = router.add(api)
        assert len(rules) == 3

        assert router.dispatch(Request("GET", "/my_api")).data == b"/my_api/do-get"
        assert router.dispatch(Request("HEAD", "/my_api")).data == b"/my_api/do-head"
        assert router.dispatch(Request("POST", "/my_api")).data == b"/my_api/do-post-or-put"
        assert router.dispatch(Request("PUT", "/my_api")).data == b"/my_api/do-post-or-put"

        with pytest.raises(MethodNotAllowed):
            router.dispatch(Request("DELETE", "/my_api"))

    def test_head_requests_are_routed_to_get_handlers(self):
        @route("/my_api", methods=["GET"])
        def do_get(request: Request, _args):
            # should be inherited
            return Response(f"{request.path}/do-get")

        router = Router()
        router.add(do_get)

        assert router.dispatch(Request("GET", "/my_api")).data == b"/my_api/do-get"
        assert router.dispatch(Request("HEAD", "/my_api")).data == b"/my_api/do-get"


class TestWsgiIntegration:
    def test_with_werkzeug(self):
        # setup up router
        router = Router()

        def index(_: Request, args) -> Response:
            return Response(b"index")

        def echo_json(request: Request, args) -> Response:
            response = Response()
            response.set_json(request.json)
            return response

        def users(_: Request, args) -> Response:
            response = Response()
            response.set_json(args)
            return response

        router.add("/", index)
        router.add("/users/<int:user_id>", users, host="<host>:<port>")
        router.add("/echo/", echo_json, methods=["POST"])

        # serve router through werkzeug
        @werkzeug.Request.application
        def app(request: werkzeug.Request) -> werkzeug.Response:
            return router.dispatch(request)

        host = "localhost"
        port = get_free_tcp_port()
        url = f"http://{host}:{port}"

        server = werkzeug.serving.make_server(host, port, app=app, threaded=True)
        t = threading.Thread(target=server.serve_forever)
        t.start()

        try:
            resp = requests.get(f"{url}/")
            assert resp.ok
            assert resp.content == b"index"

            resp = requests.get(f"{url}/users/123")
            assert resp.ok
            assert resp.json() == {"user_id": 123, "host": host, "port": str(port)}

            resp = requests.get(f"{url}/users")
            assert not resp.ok

            resp = requests.post(f"{url}/echo", json={"foo": "bar", "a": 420})
            assert resp.ok
            assert resp.json() == {"foo": "bar", "a": 420}
        finally:
            server.shutdown()
            t.join(timeout=10)
