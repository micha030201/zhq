import re
import random
import pickle
import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta
from asyncio import sleep

import aionationstates
from sanic import Sanic
from sanic.response import redirect, text


app = Sanic(__name__)

logger = logging.getLogger(__name__)

REGION = aionationstates.normalize('the communist bloc')


class Nation(aionationstates.Nation):
    """Main interface for our fellow Z-Day participants, who happen to
    be all our regional population plus a teensy bit more.
    """
    _nations = {}
    _cure_target = None

    def __init__(self, nationname):
        self.last_zactive = datetime(2017, 1, 1)
        super().__init__(nationname)

    @classmethod
    async def grab(cls, nationname):
        """Fetch a nation for further processing.

        Also implements autorefresh timer logic.
        """
        nationname = aionationstates.normalize(nationname)
        try:
            nation = cls._nations[nationname]
        except KeyError:
            nation = cls(nationname)
            await nation.refresh()
            cls._nations[nationname] = nation
        else:
            if nation.last_refreshed < datetime.utcnow() - timedelta(minutes=5):
                await nation.refresh()
        return nation

    async def refresh(self):
        """Get & save fresh data about a nation from the API."""
        region, zombie = await (self.region() + self.zombie())
        self.is_export = zombie.action == 'export'
        self.is_in_region = aionationstates.normalize(region) == REGION
        self.zombies = zombie.zombies
        self.last_refreshed = datetime.utcnow()

    def bump_zactive(self, timestamp):
        # The check to ensure that the timestamp provided is in the future
        # relative to the one we have can be omitted, as this method is only
        # called from the current happenings feed.
        self.last_zactive = timestamp

    @classmethod
    def cure_target(cls):
        if cls._cure_target is None or cls._cure_target.zombies < 40:
            cls._cure_target = max(
                (n for n in cls._nations.values() if not n.is_export),
                key=lambda n: n.zombies
            )
        return cls._cure_target

    @classmethod
    def exterminate_target(cls):
        return random.choice(
            [
                n for n in cls._nations.values()
                if n.is_export
                and n.zactive_at < datetime.utcnow() - timedelta(minutes=5)
            ] or [n for n in cls._nations.values() if n.is_export]
        )


async def process_happening(happening):
    # Possible happenings: ??? TODO
    match = re.match(r'@@(.+?)@@ (.+?) @@(.+?)@@ .+? (\d+)', happening.text)
    if match:
        sender = await Nation.grab(match.group(1))
        recepient = await Nation.grab(match.group(3))
        action = match.group(2)
        impact = int(match.group(4))

        sender.bump_zactive(happening.timestamp)
        if action == 'something something zombie hordes':
            if happening.timestamp > sender.refreshed_at:
                sender.is_export = True
            if happening.timestamp > recepient.refreshed_at:
                recepient.zombies += impact
        else:
            if happening.timestamp > sender.refreshed_at:
                sender.is_export = False
            if happening.timestamp > recepient.refreshed_at:
                recepient.zombies -= impact
        return

    match = re.match('@@(.+?)@@ relocated from %%(.+?)%% to %%(.+?)%%')
    if match:
        nation = await Nation.grab(match.group(1))
        # Should already be normalized
        region_from = match.group(2)
        region_to = match.group(3)

        if region_from == REGION:
            nation.is_in_region = False
        # Although we only monitor happenings for a particular region, we still
        # can from time to time get the cases where a nation moves between two
        # completely separate regions.  The thing is, NationStates doesn't
        # really have the concept of a 'regional happening,' they are just
        # happenings from every nation in the region.  Thus, a happening doesn't
        # have to be related to the region at all, just to one of the nations in
        # it.
        elif region_to == REGION:
            # Refreshing checks region, so altering the flag here is not
            # necessary.
            await nation.refresh()
            # Don't wait for the first strike
            nation.bump_zactive(happening.timestamp)
        return


async def happening_loop():
    gen = aionationstates.world.new_happenings(
        # Ideally, we would also specify a type, but I'm unsure whether
        # Z-Day hapenings will have any.  TODO?
        poll_period=10,
        regions=REGION
    )
    async for happening in gen:
        await process_happening(happening)


async def update_loop():
    """Automatically refresh nations not mentioned in happenings."""
    first = True
    while True:
        for nationname in await aionationstates.region(REGION).nations():
            await Nation.grab(nationname)
            if not first:
                # We want to gather data quickly during the first run, so the
                # ratelimit only kicks in on the second and up.
                await sleep(2)


async def supervisor(coroutine_function):
    while True:
        try:
            await coroutine_function()
        except Exception:
            logger.exception('exception in background process:')
            await sleep(5)


@app.route('/cure')
def cure_target(request):
    return redirect(Nation.cure_target().url)


@app.route('/exterminate')
def exterminate_target(request):
    try:
        return redirect(Nation.exterminate_target().url)
    except Exception:
        return text('Seems like there are no nations to exterminate!'
                    'Try reloading in a few minutes.')


if __name__ == '__main__':
    aionationstates.set_user_agent(
        "Kethania's Z-Day script -- Really sorry for all the requests!")
    with suppress(Exception):
        with open('known_nation_cache', 'rb') as f:
            Nation._nations = pickle.load(f)
    print(Nation._nations)
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(asyncio.gather(
            supervisor(happening_loop),
            supervisor(update_loop),
            app.create_server(port=5000)
        ))
    finally:
        loop.stop()
        loop.close()
        with open('known_nation_cache', 'wb') as f:
            pickle.dump(Nation._nations, f)
