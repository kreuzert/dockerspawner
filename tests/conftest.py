"""pytest config for dockerspawner tests"""

import inspect
import json
import os
from socket import AddressFamily
from textwrap import indent
from unittest import mock

import jupyterhub
import psutil
import pytest
import pytest_asyncio
from docker import from_env as docker_from_env
from docker.errors import APIError
from jupyterhub import version_info as jh_version_info
from jupyterhub.tests.conftest import app as jupyterhub_app  # noqa: F401
from jupyterhub.tests.conftest import io_loop  # noqa: F401
from jupyterhub.tests.conftest import ssl_tmpdir  # noqa: F401
from jupyterhub.tests.conftest import user  # noqa: F401
from jupyterhub.tests.mocking import MockHub

if 'event_loop' in inspect.signature(io_loop).parameters:
    # older JupyterHub (< 5.x), io_loop requires deprecated event_loop
    from jupyterhub.tests.conftest import event_loop  # noqa: F401

from dockerspawner import DockerSpawner, SwarmSpawner, SystemUserSpawner

# import base jupyterhub fixtures

# make Hub connectable from docker by default
# do this here because the `app` fixture has already loaded configuration
MockHub.hub_ip = "0.0.0.0"

if os.environ.get("HUB_CONNECT_IP"):
    MockHub.hub_connect_ip = os.environ["HUB_CONNECT_IP"]
else:
    # get docker interface explicitly by default
    # on GHA, the ip for hostname resolves to a 10.x
    # address that is not connectable from within containers
    # but the docker0 address is connectable
    for iface, addrs in psutil.net_if_addrs().items():
        if 'docker' not in iface:
            continue
        ipv4_addrs = [addr for addr in addrs if addr.family == AddressFamily.AF_INET]
        if not ipv4_addrs:
            print(f"No ipv4 addr for {iface}: {addrs}")
            continue
        ipv4 = ipv4_addrs[0]
        print(f"Found docker interfaces, using {iface}: {ipv4}")
        MockHub.hub_connect_ip = ipv4.address
        break
    else:
        print(f"Did not find docker interface in: {', '.join(psutil.net_if_addrs())}")


def pytest_collection_modifyitems(items):
    # apply loop_scope="module" to all async tests by default
    # this can be hopefully be removed in favor of config if
    # https://github.com/pytest-dev/pytest-asyncio/issues/793
    # is addressed
    pytest_asyncio_tests = (
        item for item in items if pytest_asyncio.is_async_test(item)
    )
    asyncio_scope_marker = pytest.mark.asyncio(loop_scope="module")
    for async_test in pytest_asyncio_tests:
        # add asyncio marker _if_ not already present
        asyncio_marker = async_test.get_closest_marker('asyncio')
        if not asyncio_marker or not asyncio_marker.kwargs:
            async_test.add_marker(asyncio_scope_marker, append=False)


@pytest.fixture
def app(jupyterhub_app):  # noqa: F811
    app = jupyterhub_app
    app.config.DockerSpawner.prefix = "dockerspawner-test"
    # If it's a prerelease e.g. (2, 0, 0, 'rc4', '') use full tag
    if len(jh_version_info) > 3 and jh_version_info[3]:
        tag = jupyterhub.__version__
        app.config.DockerSpawner.image = f"quay.io/jupyterhub/singleuser:{tag}"
    return app


@pytest.fixture
def named_servers(app):
    with mock.patch.dict(
        app.tornado_settings,
        {"allow_named_servers": True, "named_server_limit_per_user": 2},
    ):
        yield


@pytest.fixture
def dockerspawner_configured_app(app, named_servers):
    """Configure JupyterHub to use DockerSpawner"""
    # app.config.DockerSpawner.remove = True
    with mock.patch.dict(app.tornado_settings, {"spawner_class": DockerSpawner}):
        yield app


@pytest.fixture
def swarmspawner_configured_app(app, named_servers):
    """Configure JupyterHub to use DockerSpawner"""
    with (
        mock.patch.dict(app.tornado_settings, {"spawner_class": SwarmSpawner}),
        mock.patch.dict(app.config.SwarmSpawner, {"network_name": "bridge"}),
    ):
        yield app


@pytest.fixture
def systemuserspawner_configured_app(app, named_servers):
    """Configure JupyterHub to use DockerSpawner"""
    with mock.patch.dict(app.tornado_settings, {"spawner_class": SystemUserSpawner}):
        yield app


@pytest.fixture(autouse=True, scope="session")
def docker():
    """Fixture to return a connected docker client

    cleans up any containers we leave in docker
    """
    d = docker_from_env()
    try:
        yield d

    finally:
        # cleanup our containers
        for c in d.containers.list(all=True):
            if c.name.startswith("dockerspawner-test"):
                c.stop()
                c.remove()
        try:
            services = d.services.list()
        except (APIError, TypeError):
            # e.g. services not available
            # podman gives TypeError
            return
        else:
            for s in services:
                if s.name.startswith("dockerspawner-test"):
                    s.remove()


# make sure reports are available during yield fixtures
# from pytest docs: https://docs.pytest.org/en/latest/example/simple.html#making-test-result-information-available-in-fixtures


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # execute all other hooks to obtain the report object
    outcome = yield
    rep = outcome.get_result()

    # set a report attribute for each phase of a call, which can
    # be "setup", "call", "teardown"

    setattr(item, "rep_" + rep.when, rep)


@pytest.fixture(autouse=True)
def debug_docker(request, docker):
    """Debug docker state after tests"""
    yield
    if not hasattr(request.node, 'rep_call'):
        return
    if not request.node.rep_call.failed:
        return

    print("executing test failed", request.node.nodeid)
    containers = docker.containers.list(all=True)
    for c in containers:
        print(f"Container {c.name}: {c.status}")

    for c in containers:
        logs = indent(c.logs().decode('utf8', 'replace'), '  ')
        print(f"Container {c.name} logs:\n{logs}")

    for c in containers:
        container_info = json.dumps(
            docker.api.inspect_container(c.id),
            indent=2,
            sort_keys=True,
        )
        print(f"Container {c.name}: {container_info}")


_username_counter = 0


@pytest.fixture()
def username():
    global _username_counter
    _username_counter += 1
    return f"test-user-{_username_counter}"
