"""CLI and entry point for Redline Job Linker."""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
import logging
import sys
import webbrowser

from .config import load_config, save_config
from .server import JobLinkerRequestHandler, JobLinkerServerContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("redline_job_linker")


def main() -> None:
    parser = argparse.ArgumentParser(description="Redline CNC Multi-Job Workflow Linker & Batcher")
    parser.add_argument("--port", type=int, default=None, help="Port to bind web server (default: 8088)")
    parser.add_argument("--watch-dir", type=str, default=None, help="Path to watch for incoming CAM files")
    parser.add_argument("--controller-host", type=str, default=None, help="Redline controller IP or hostname")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open web browser")
    args = parser.parse_args()

    cfg = load_config()
    if args.port:
        cfg.server_port = args.port
    if args.watch_dir:
        cfg.watched_dir = args.watch_dir
    if args.controller_host:
        cfg.controller_host = args.controller_host
    save_config(cfg)

    context = JobLinkerServerContext(cfg)

    class CustomHandler(JobLinkerRequestHandler):
        def __init__(self, *h_args, **h_kwargs):
            self.context = context
            super().__init__(*h_args, **h_kwargs)

    server_address = ("0.0.0.0", cfg.server_port)
    httpd = ThreadingHTTPServer(server_address, CustomHandler)

    url = f"http://localhost:{cfg.server_port}"
    _LOGGER.info("=" * 60)
    _LOGGER.info("🚀 Redline Multi-Job Workflow Linker running at: %s", url)
    if cfg.watched_dir:
        _LOGGER.info("📁 Watching CAM directory: %s", cfg.watched_dir)
    else:
        _LOGGER.info("📁 Watched CAM directory not set yet. Configure it in web UI settings.")
    _LOGGER.info("🤖 Controller target: %s:%s (%s)", cfg.controller_host, cfg.controller_port, cfg.controller_subfolder)
    _LOGGER.info("=" * 60)

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _LOGGER.info("Shutting down Redline Job Linker...")
        if context.watcher:
            context.watcher.stop()
        httpd.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
