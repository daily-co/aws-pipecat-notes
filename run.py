import argparse
import asyncio
import importlib.util
import os
import sys
from contextlib import asynccontextmanager
from inspect import iscoroutinefunction, signature
from typing import Any, Callable, Dict, Optional, Tuple

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import RedirectResponse
from loguru import logger
from pipecat_ai_small_webrtc_prebuilt.frontend import SmallWebRTCPrebuiltUI

from pipecat.transports.network.webrtc_connection import IceServer, SmallWebRTCConnection

# Load environment variables
load_dotenv(override=True)

app = FastAPI()

# Store connections by pc_id
pcs_map: Dict[str, SmallWebRTCConnection] = {}

ice_servers = [
    IceServer(
        urls="stun:stun.l.google.com:19302",
    )
]

# Mount the frontend at /
app.mount("/client", SmallWebRTCPrebuiltUI)

# Store program arguments
args: argparse.Namespace = argparse.Namespace()

# Store the bot function info
run_example_func: Optional[Callable] = None


def import_bot_file(file_path: str) -> Callable:
    """Dynamically import the bot file and set global `run_example_func` function."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Bot file not found: {file_path}")

    # Extract module name without extension
    module_name = os.path.splitext(os.path.basename(file_path))[0]

    # Load the module
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if not spec or not spec.loader:
        raise ImportError(f"Could not load spec for {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    # Check for run_example function first
    if hasattr(module, "run_example"):
        run_func = module.run_example
        # Check if the function accepts a WebRTC connection
        sig = signature(run_func)
        return run_func

    raise AttributeError(f"No `run_example` function found in {file_path}")


@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/client/")


@app.post("/api/offer")
async def offer(request: dict, background_tasks: BackgroundTasks):
    pc_id = request.get("pc_id")

    if pc_id and pc_id in pcs_map:
        pipecat_connection = pcs_map[pc_id]
        logger.info(f"Reusing existing connection for pc_id: {pc_id}")
        await pipecat_connection.renegotiate(
            sdp=request["sdp"], type=request["type"], restart_pc=request.get("restart_pc", False)
        )
    else:
        pipecat_connection = SmallWebRTCConnection(ice_servers)
        await pipecat_connection.initialize(sdp=request["sdp"], type=request["type"])

        @pipecat_connection.event_handler("closed")
        async def handle_disconnected(webrtc_connection: SmallWebRTCConnection):
            logger.info(f"Discarding peer connection for pc_id: {webrtc_connection.pc_id}")
            pcs_map.pop(webrtc_connection.pc_id, None)

        # We've already checked that run_example_func exists
        assert run_example_func is not None
        background_tasks.add_task(run_example_func, pipecat_connection, args)

    answer = pipecat_connection.get_answer()
    # Updating the peer connection inside the map
    pcs_map[answer["pc_id"]] = pipecat_connection

    return answer


def main(parser: Optional[argparse.ArgumentParser] = None):
    global run_example_func
    if not parser:
        parser = argparse.ArgumentParser(description="Pipecat Bot Runner")

    parser.add_argument("bot_file", nargs="?", help="Path to the bot file", default=None)

    parser.add_argument(
        "--host", default="localhost", help="Host for HTTP server (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=7860, help="Port for HTTP server (default: 7860)"
    )
    parser.add_argument(
        "--proxy", "-x", help="A public proxy host name (no protocol, e.g. proxy.example.com)"
    )
    parser.add_argument("--verbose", "-v", action="count", default=0)
    args = parser.parse_args()

    # Log level
    logger.remove(0)
    logger.add(sys.stderr, level="TRACE" if args.verbose else "DEBUG")

    try:
        run_example_func = import_bot_file(args.bot_file)
        uvicorn.run(app, host=args.host, port=args.port)

    except Exception as e:
        logger.error(f"Error loading bot file {args.bot_file}. Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
