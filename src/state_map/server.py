"""state_map Web Server - Simple website to create and view state_maps.

Copyright (C) 2026  CoolCat467

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

__title__ = "state_map Webserver"
__author__ = "CoolCat467"
__version__ = "0.1.1"
__license__ = "GNU General Public License Version 3"


import argparse
import dataclasses
import math
import sys
import time
from collections.abc import (
    AsyncIterator,
    Iterable,
)
from enum import IntEnum, auto
from os import makedirs, path
from typing import TYPE_CHECKING, Final, TypedDict, TypeVar
from uuid import UUID, uuid4

import platformdirs
import trio
from hypercorn.config import Config
from hypercorn.trio import serve
from quart import request
from quart.templating import stream_template
from quart_trio import QuartTrio

from state_map.elapsed import combine_end, get_elapsed
from state_map.server_utils import (
    find_ip,
    get_exception_page,
    pretty_exception,
)

if sys.version_info < (3, 11):
    import tomli as tomllib
    from exceptiongroup import BaseExceptionGroup
else:
    import tomllib

if TYPE_CHECKING:
    from typing_extensions import ParamSpec
    from werkzeug import Response as WerkzeugResponse

    PS = ParamSpec("PS")

FILE_TITLE: Final = __title__.lower().replace(" ", "-").replace("-", "_")
CONFIG_PATH: Final = trio.Path(
    platformdirs.user_config_path(FILE_TITLE, __author__),
)
DATA_PATH: Final = trio.Path(
    platformdirs.user_data_path(FILE_TITLE, __author__),
)
MAIN_CONFIG: Final = CONFIG_PATH / "config.toml"

T = TypeVar("T")


@dataclasses.dataclass
class Team:
    """Team in a state_map."""

    id_: int
    title: str
    complete: bool = False
    end_time: float = 0


class BoardStateEnum(IntEnum):
    """state_map State Enum."""

    CREATED = 0
    RUNNING = auto()
    COMPLETED = auto()


@dataclasses.dataclass
class StateMap:
    """StateMap."""

    title: str
    state: BoardStateEnum = BoardStateEnum.CREATED
    teams: list[Team] = dataclasses.field(default_factory=list)
    start_time: float = 0
    next_team_id: int = 0


class AppData(TypedDict):
    """Global shared application data."""

    state_maps: dict[UUID, StateMap]


app: Final = QuartTrio(  # pylint: disable=invalid-name
    __name__,
    static_folder="static",
    template_folder="templates",
)
APP_DATA = AppData({"state_maps": {}})


@app.get("/")
async def root_get() -> AsyncIterator[str]:
    """Handle main page GET request."""
    return await stream_template(
        "root_get.html.jinja",
        state_maps=APP_DATA["state_maps"],
    )


@app.post("/")
@pretty_exception
async def root_post() -> (
    WerkzeugResponse | AsyncIterator[str] | tuple[AsyncIterator[str], int]
):
    """Handle page POST."""
    multi_dict = await request.form
    data = multi_dict.to_dict()

    title = data.get("title", "").strip()

    max_title_length = 30

    errors = []
    if not title:
        errors.append("Missing <code>title</code> parameter.")
    elif len(title) > max_title_length:
        errors.append(
            f"Max length of title is <code>{max_title_length}</code>.",
        )
    for state_map in APP_DATA["state_maps"].values():
        if state_map.title == title:
            errors.append("Provided title already exists.")
            break
    if errors:
        return await get_exception_page(
            400,  # bad request
            "Bad Request",
            "\n<br>\n".join(errors),
            request.url,
        )

    # create state_map
    uuid = uuid4()
    APP_DATA["state_maps"][uuid] = state_map(title)

    return app.redirect(f"/state_map/{uuid}")


@app.get("/state_map/<uuid:state_map_uuid>")
@pretty_exception
async def state_map_get(
    state_map_uuid: UUID,
) -> AsyncIterator[str] | tuple[AsyncIterator[str], int]:
    """state_map page get handling."""
    state_map = APP_DATA["state_maps"].get(state_map_uuid)

    if state_map is None:
        return await get_exception_page(
            404,
            "Not Found",
            "Requested state_map not found.",
            request.url,
        )

    return await stream_template(
        "state_map_get.html.jinja",
        state_map=state_map,
        get_elapsed=get_elapsed,
    )


def parse_int_or_none(value: str) -> int | None:
    """Return parsed integer or None."""
    try:
        return int(value)
    except ValueError:
        return None


@app.post("/state_map/<uuid:state_map_uuid>")
@pretty_exception
async def state_map_post(
    state_map_uuid: UUID,
) -> tuple[AsyncIterator[str], int] | WerkzeugResponse:
    """state_map page post handling."""
    state_map = APP_DATA["state_maps"].get(state_map_uuid)

    if state_map is None:
        return await get_exception_page(
            404,
            "Not Found",
            "Requested state_map not found.",
            request.url,
        )

    multi_dict = await request.form
    data = multi_dict.to_dict()

    state_map_timer_start = "start_state_map_timer" in data
    state_map_timer_stop = "stop_state_map_timer" in data
    team_title = data.get("team_title", "").strip()
    team_stop = parse_int_or_none(data.get("team_stop", ""))
    team_complete_index: int | None = None

    max_title_length = 30

    errors = []
    if state_map_timer_start:
        if state_map.state != BoardStateEnum.CREATED:
            errors.append(
                "Cannot start state_map timer if state_map not in <code>created</code> state.",
            )
        elif not state_map.teams:
            errors.append(
                "Cannot start state_map timer if state_map has no teams.",
            )
    elif state_map_timer_stop and state_map.state != BoardStateEnum.RUNNING:
        errors.append(
            "Cannot stop state_map timer if state_map not in <code>running</code> state.",
        )
    elif team_title:
        if state_map.state != BoardStateEnum.CREATED:
            errors.append(
                "Cannot create a team while state_map timer is running.",
            )
        elif len(team_title) > max_title_length:
            errors.append(
                f"Max length of team title is <code>{max_title_length}</code>.",
            )
            team_title = None
        else:
            for team in state_map.teams:
                if team.title == team_title:
                    errors.append("Team with given title already exists.")
                    team_title = None
                    break
    elif team_stop is not None:
        if state_map.state != BoardStateEnum.RUNNING:
            errors.append(
                "Cannot stop state_map timer if state_map not in <code>running</code> state.",
            )
        elif team_stop < 0 or team_stop > len(state_map.teams):
            errors.append("Team id out of bounds.")
        else:
            for team_complete_index, team in enumerate(state_map.teams):
                if team.id_ == team_stop:
                    team_complete_index = team_complete_index
                    break
            else:
                errors.append("Team with given id does not exist.")
                team_complete_index = None

    if errors:
        return await get_exception_page(
            400,  # bad request
            "Bad Request",
            "\n<br>\n".join(errors),
            request.url,
        )

    if state_map_timer_start:
        assert state_map.state == BoardStateEnum.CREATED
        state_map.start_time = time.time()
        state_map.state = BoardStateEnum.RUNNING

    elif state_map_timer_stop:
        assert state_map.state == BoardStateEnum.RUNNING
        state_map.state = BoardStateEnum.COMPLETED

    elif team_title:
        assert state_map.state == BoardStateEnum.CREATED
        state_map.teams.append(Team(state_map.next_team_id, team_title))
        state_map.next_team_id += 1

    elif team_complete_index is not None:
        assert state_map.state == BoardStateEnum.RUNNING
        team = state_map.teams[team_complete_index]

        team.end_time = time.time()
        team.complete = True

        state_map.teams.sort(
            key=lambda team: team.end_time if team.complete else math.inf,
        )

        if all(team.complete for team in state_map.teams):
            state_map.state = BoardStateEnum.COMPLETED

    else:
        return await get_exception_page(
            400,  # bad request
            "Bad Request",
            "POST request with no valid actions to perform.",
            request.url,
        )

    return app.redirect(f"/state_map/{state_map_uuid}")


def run_server(
    secure_bind_port: int | None = None,
    insecure_bind_port: int | None = None,
    ip_addr: str | None = None,
    hypercorn: dict[str, object] | None = None,
) -> None:
    """Asynchronous Entry Point."""
    if secure_bind_port is None and insecure_bind_port is None:
        raise ValueError(
            "Port must be specified with `port` and or `ssl_port`!",
        )

    if not ip_addr:
        ip_addr = find_ip()

    if not hypercorn:
        hypercorn = {}

    ##    logs_path = DATA_PATH / "logs"
    ##    if not path.exists(logs_path):
    ##        makedirs(logs_path)

    ##    print(f"Logs Path: {str(logs_path)!r}\n")

    try:
        # Hypercorn config setup
        config: dict[str, object] = {
            "accesslog": "-",
            ##"errorlog": logs_path / time.strftime("log_%Y_%m_%d.log"),
        }
        # Load things from user controlled toml file for hypercorn
        config.update(hypercorn)
        # Override a few particularly important details if set by user
        config.update(
            {
                "worker_class": "trio",
            },
        )
        # Make sure address is in bind

        if insecure_bind_port is not None:
            raw_bound = config.get("insecure_bind", [])
            if not isinstance(raw_bound, Iterable):
                raise ValueError(
                    "main.bind must be an iterable object (set in config file)!",
                )
            bound = set(raw_bound)
            bound |= {f"{ip_addr}:{insecure_bind_port}"}
            config["insecure_bind"] = bound

            # If no secure port, use bind instead
            if secure_bind_port is None:
                config["bind"] = config["insecure_bind"]
                config["insecure_bind"] = []

            insecure_locations = combine_end(
                f"http://{addr}" for addr in sorted(bound)
            )
            print(f"Serving on {insecure_locations} insecurely")

        if secure_bind_port is not None:
            raw_bound = config.get("bind", [])
            if not isinstance(raw_bound, Iterable):
                raise ValueError(
                    "main.bind must be an iterable object (set in config file)!",
                )
            bound = set(raw_bound)
            bound |= {f"{ip_addr}:{secure_bind_port}"}
            config["bind"] = bound

            secure_locations = combine_end(
                f"https://{addr}" for addr in sorted(bound)
            )
            print(f"Serving on {secure_locations} securely")

        app.config["EXPLAIN_TEMPLATE_LOADING"] = False

        # We want pretty html, no jank
        app.jinja_options = {
            "trim_blocks": True,
            "lstrip_blocks": True,
        }

        app.add_url_rule("/<path:filename>", "static", app.send_static_file)

        config_obj = Config.from_mapping(config)

        print("(CTRL + C to quit)")

        trio.run(serve, app, config_obj)
    except BaseExceptionGroup as exc:
        caught = False
        for ex in exc.exceptions:
            if isinstance(ex, KeyboardInterrupt):
                print("Shutting down from keyboard interrupt")
                caught = True
                break
        if not caught:
            raise


DEFAULT_CONFIG_TOML: Final = """[main]
# Port server should run on.
# You might want to consider changing this to 80
port = 3004

# Port for SSL secured server to run on
#ssl_port = 443

# Helpful stack exchange website question on how to allow non root processes
# to bind to lower numbered ports
# https://superuser.com/questions/710253/allow-non-root-process-to-bind-to-port-80-and-443
# Answer I used: https://superuser.com/a/1482188/1879931

[hypercorn]
# See https://hypercorn.readthedocs.io/en/latest/how_to_guides/configuring.html#configuration-options
use_reloader = false
# SSL configuration details
#certfile = "/home/<your_username>/letsencrypt/config/live/<your_domain_name>.duckdns.org/fullchain.pem"
#keyfile = "/home/<your_username>/letsencrypt/config/live/<your_domain_name>.duckdns.org/privkey.pem"
"""


def run() -> None:
    """Run scanner server."""
    parser = argparse.ArgumentParser(
        description="Simple website to create and view state maps. ",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{__title__} v{__version__}",
        help="Show the program version and exit.",
    )
    parser.add_argument(
        "--create-default-config",
        action="store_true",
        help="Create or overwrite the default configuration file.",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Bind to localhost (127.0.0.1) instead of public ip address.",
    )

    args = parser.parse_args()

    if args.create_default_config:
        print(
            f"Creating/overwriting configuration file located at {str(MAIN_CONFIG)!r} with default...",
        )
        if not path.exists(CONFIG_PATH):
            makedirs(CONFIG_PATH)

        with open(MAIN_CONFIG, "w", encoding="utf-8") as fp:
            fp.write(DEFAULT_CONFIG_TOML)

        print("Action complete.")
        return

    if path.exists(MAIN_CONFIG):
        print(f"Reading configuration file {str(MAIN_CONFIG)!r}...\n")

        with open(MAIN_CONFIG, "rb") as fp:
            config = tomllib.load(fp)
    else:
        print(
            f"Configuration file {str(MAIN_CONFIG)!r} not found, loading default.",
        )
        config = tomllib.loads(DEFAULT_CONFIG_TOML)

    main_section = config.get("main", {})

    insecure_bind_port = main_section.get("port", None)
    secure_bind_port = main_section.get("ssl_port", None)

    hypercorn: dict[str, object] = config.get("hypercorn", {})

    ip_address: str | None = None
    if args.local:
        ip_address = "127.0.0.1"

    run_server(
        secure_bind_port=secure_bind_port,
        insecure_bind_port=insecure_bind_port,
        ip_addr=ip_address,
        hypercorn=hypercorn,
    )


if __name__ == "__main__":
    run()
