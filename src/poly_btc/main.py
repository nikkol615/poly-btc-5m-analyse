from __future__ import annotations

import asyncio
import signal
from typing import Any

from .binance_client import BinanceSpotClient
from .chainlink_snapshot import ChainlinkSnapshotRotator
from .clob_client import CLOBClient
from .db import BatchWriter, apply_schema, close_pool, init_pool
from .gamma import GammaDiscovery
from .log import get_logger, setup_logging
from .resolver import run_forever as resolver_run_forever
from .rtds_client import RTDSClient

log = get_logger(__name__)


async def _amain() -> None:
    setup_logging()
    await init_pool()
    await apply_schema()

    writer = BatchWriter(flush_interval=1.0)
    writer.start()

    rtds = RTDSClient(writer)
    clob = CLOBClient(writer)
    binance = BinanceSpotClient(writer)
    chainlink = ChainlinkSnapshotRotator(writer)

    async def on_new_market(m: dict[str, Any]) -> None:
        await rtds.add_slug(m["slug"])
        await clob.add_tokens(m["token_up"], m["token_down"])

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with GammaDiscovery(on_new_market=on_new_market) as discovery:
        # Prime the subscription sets before the WSS clients open their sockets,
        # so the initial subscription frames cover already-active markets.
        await discovery.scan_once()

        tasks = [
            asyncio.create_task(discovery.run_forever(), name="discovery"),
            asyncio.create_task(rtds.run_forever(), name="rtds"),
            asyncio.create_task(clob.run_forever(), name="clob"),
            asyncio.create_task(binance.run_forever(), name="binance"),
            asyncio.create_task(chainlink.run_forever(), name="chainlink-rotator"),
            asyncio.create_task(resolver_run_forever(), name="resolver"),
        ]
        log.info("collector_started")
        await stop.wait()
        log.info("collector_stopping")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    await rtds.stop()
    await clob.stop()
    await binance.stop()
    await chainlink.stop()
    await writer.stop()
    await close_pool()
    log.info("collector_stopped")


def run() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    run()
