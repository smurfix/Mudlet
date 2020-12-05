import subprocess
import errno
import trio
import anyio
import os
import sys
from contextlib import asynccontextmanager,AsyncExitStack
from mudpyc.server import Server as MudpycServer, WebServer
from mudpyc.util import combine_dict
from asynctelnet import BaseServer as TelnetServer, server_loop
from pathlib import Path
import greenback
import weakref
from typing import Optional, Union, Any
from functools import partial
import logging

await_ = greenback.await_

DEFAULT_CFG = dict(
    name="MudletTest",
    display=42,
    mudlet=dict(
        path="../../src/mudlet",
    ),
    mudpyc=dict(
    ),
    tests="tests",
)

def locktest(lockfile):
    try:
        with open(lockfile, "r") as lock:
            pid = int(lock.read(20))
    except EnvironmentError as err:
        if err.errno != errno.ENOENT:
            raise
        return False
    else:
        try:
            os.kill(pid, 0)
        except EnvironmentError as err:
            if err.errno != errno.ESRCH:
                raise
            return False
        else:
            return True

class TestEnv:
    """
    The Mudlet test environment
    """
    def __init__(self, cfg):
        self.cfg = combine_dict(cfg, DEFAULT_CFG)
        self.cfg['tests'] = Path(self.cfg['tests'])
        self.log=logging.getLogger("mudtest")

    async def run_x(self, display, *, task_status=trio.TASK_STATUS_IGNORED):
        lockfile = "/tmp/.X%d-lock" % display
        if locktest(lockfile):
            raise RuntimeError("There's already an X server on display %d!" % display)

        if "DISPLAY" in os.environ:
            env=os.environ.copy()
            self.log.info("Starting nested X server.")
            self._tg.start_soon(partial(trio.run_process, ["Xephyr","-screen","1200x1000x16",":"+str(display)], stdout=sys.stdout, stderr=sys.stderr, env=env))
            old_display = os.environ["DISPLAY"]
        else:
            self.log.info("Starting X vnc server.")
            self._tg.start_soon(partial(trio.run_process, ["Xtightvnc","-geometry","1200x1000","-depth","16",":"+str(display)], stdout=sys.stdout, stderr=sys.stderr))
            old_display = None

        os.environ["DISPLAY"] = ":%d"%display
        try:
            with trio.fail_after(5):
                while True:
                    if not locktest(lockfile):
                        await trio.sleep(0.1)
                        continue
                    if os.path.exists("/tmp/.X11-unix/X%d"%display):
                        break
                    await trio.sleep(0.1)
            task_status.started()
            await trio.sleep(99999)
        finally:
            if old_display is None:
                del os.environ["DISPLAY"]
            else:
                os.environ["DISPLAY"] = old_display

    async def run_wm(self, display, *, task_status=trio.TASK_STATUS_IGNORED):
        self.log.info("Starting minimal display manager.")
        err = None
        async def wm():
            global err
            try:
                await trio.run_process(["ratpoison","-d",":%d"%display], stdout=sys.stdout, stderr=sys.stderr)
            except subprocess.CalledProcessError as e:
                err = e
        self._tg.start_soon(wm)

        # TODO figure out how to determine whether the WM is running
        await trio.sleep(1)
        task_status.started()

        # stupid heuristic
        await trio.sleep(5)
        if err is not None:
            raise err

    async def main(self, script:Optional[Path] = None):
        if script is None:
            script = "all"

        with open("config/profiles/LocalTest/port","w", encoding="utf-16-be") as f:
            print("\0\n"+str(50000+self.cfg['display']),end="",file=f);

        async with trio.open_nursery() as tg:
            self._tg = tg
            await tg.start(self.run_x, self.cfg['display'])
            await tg.start(self.run_wm, self.cfg['display'])
            tr = TestRun(self,script)
            await tr.run()
            tg.cancel_scope.cancel()
            pass # end of nursery
        pass # end of function


class MudletTelnet(TelnetServer):
    def __init__(self, runner, *a, **kw):
        self.runner = weakref.ref(runner)
        self._connected = trio.Event()
        super().__init__(*a, **kw)

    async def setup(self):
        self.runner().log.info("Telnet link open.")
        self._connected.set()
        await super().setup()
        if self._charset is None:
            await self.request_charset()
        self.runner().log.info("Telnet setup done.")
        self.runner().set_telnet(self)

    async def wait(self):
        await self._connected.wait()


class MudletLink(MudpycServer):
    def __init__(self, runner):
        runner.log.info("Starting Python link server.")
        self.runner = runner
        super().__init__(name="MudletTester", cfg=runner.env.cfg["mudpyc"])



class TestRun:
    mudlet=None
    telnet=None
    mudpyc=None
    globals=None
    _mudlet_done = None
    _telnet_done = None
    _mudpyc_done = None
    _mudlet_task = None
    _telnet_task = None
    _mudpyc_task = None

    def __init__(self, env:TestEnv, script:Path):
        self.env=env
        self.script=script
        self.log=logging.getLogger(("mudlet."+script).replace("/",".").replace("..",".").replace("..","."))

    def make_globals(self):
        if self.globals is not None:
            return self.globals
        res = {}
        for k in dir(self):
            if k.startswith("cmd__"):
                res[k[5:]] = getattr(self,k)
        self.globals = res
        return res

    def set_telnet(self, telnet):
        self.telnet = telnet
        self._readq_w,self._readq_r = trio.open_memory_channel(5)
        self._telnet_done.set()

    async def run(self):
        async with trio.open_nursery() as tg, \
                AsyncExitStack() as exs:
            self._tg = tg
            self._stack = exs
            await greenback.ensure_portal()

            self.cmd__include(self.script)
            tg.cancel_scope.cancel()

    def cmd__run(self, fn):
        """
        Script: run(FILENAME)

        Runs the test in FILENAME as a self-contained sub-test.
        Mudlet must not be already running.
        """
        if self.telnet is not None:
            raise RuntimeError("You already started Telnet.")
        if self.mudlet is not None:
            raise RuntimeError("You already started Mudlet.")
        if self.mudpyc is not None:
            raise RuntimeError("You already started MudPyC.")
        self.log.debug("Run %s",fn)
        tr = TestRun(self.env,fn)
        await_(tr.run())
        self.log.debug("Done running %s",fn)

    def cmd__include(self, fn):
        """
        Script: include(FILENAME)

        Behave as if the contents of file FILENAME were written here.
        """
        try:
            with open(fn,"r") as f:
                scr = f.read()
        except FileNotFoundError:
            fn = self.env.cfg['tests'] / fn
            with open(fn,"r") as f:
                scr = f.read()
        scr = compile(scr, fn, "exec")
        self.log.debug("in %s",fn)
        eval(scr, self.make_globals())
        self.log.debug("Done: %s",fn)

    async def _mudpyc(self):
        raise NotImplementedError()

    async def _telnet_reader(self,stream):
        self.stream = stream
        try:
            while True:
                inp = await stream.readline()
                self.log.debug("IN: %s", inp)
                self._readq_w.put_nowait(inp)
        finally:
            self.stream = None
            await self._readq_w.aclose()
            self.telnet = None
            if self._telnet_done is not None:
                self._telnet_done.set()

    def cmd__readline(self, timeout:Optional[float]=None):
        """
        Script: readline(TIMEOUT)

        Reads a line of input from the MUD.
        """
        return await_(self._readline(timeout))

    def cmd__send(self, text:str, end=None):
        """
        Script: send(LINE, EOL)

        Sends a line of output to the MUD.

        Uses the standard CR+LF line end if EOL is not given.
        """
        s = "writeline" if end is None else "send"
        await_(getattr(self.telnet,s)(text+(end or ""),))

    async def _readline(self, timeout:Optional[float]=None):
        with trio.fail_after(math.inf if timeout is None else timeout):
            return await_(self._readq_r.receive())

    async def _start_telnet(self):
        async def _telnet(*, task_status=trio.TASK_STATUS_IGNORED):
            async with trio.open_nursery() as tg:
                self._telnet_done = trio.Event()
                evt = anyio.create_event()
                tg.start_soon(partial(server_loop, evt=evt,protocol_factory=partial(MudletTelnet,self), port=50000+self.env.cfg['display'], shell=self._telnet_reader))
                await evt.wait()
                task_status.started(tg.cancel_scope)

        self._telnet_task = await self._tg.start(_telnet)

    def cmd__start_telnet(self):
        """
        Script: start_telnet()

        Start the Telnet service. It is auto-terminated when the script ends
        or when this command is used again.
        """
        self._kill("telnet")
        await_(self._start_telnet())


    async def _start_mudpyc(self):
        async def _mudpyc(*, task_status=trio.TASK_STATUS_IGNORED):
            async with trio.CancelScope() as sc:
                s = WebServer(cfg, factory=partial(MudletLink,self))
                self._mudpyc_task = sc
                self.mudpyc = s
                await s.run(task_status=task_status)
        await self._tg.start(_mudpyc)

    def cmd__start_mudpyc(self, msg:Optional[str] = None):
        """
        Script: start_mudpyc()

        Start the MudPyC service.
        Starts the MudPyC client. It is auto-terminated when the script ends
        or when this command is used again.
        """
        if self._telnet_task is None:
            raise RuntimeError("You don't have a network connection yet")
        self._kill("mudpyc")
        await_(self._start_mudpyc())

    def _kill(self,name):
        done = "_%s_done" % (name,)
        task = "_%s_task" % (name,)
        if getattr(self,name) is not None:
            setattr(self,done, trio.Event())
            getattr(self,task).cancel()
            await_(getattr(self,done).wait())
            setattr(self,done, None)
            setattr(self,task, None)
            setattr(self,name, None)

    async def _start_mudlet(self):
        async def _mudlet(*, task_status=trio.TASK_STATUS_IGNORED):
            with trio.CancelScope() as sc:
                task_status.started(sc)
                try:
                    await trio.run_process([self.env.cfg["mudlet"]["path"]])
                finally:
                    if self._mudlet_done is not None:
                        self._mudlet_done.set()
        self._mudlet_task = await self._tg.start(_mudlet)

    def cmd__start_mudlet(self):
        """
        Script: start_mudlet()

        Starts the Mudlet client. It is auto-terminated when the script ends.
        or when this command is used again.
        """
        self._kill("mudlet")
        await_(self._start_mudlet())

    def cmd__wait_telnet(self):
        """
        Script: with_telnet()

        Wait until Mudlet has established a connection to the Telnet
        service (which must already be running).
        """
        if self._telnet_task is None:
            raise RuntimeError("You need to start Telnet first!")
        if self._mudlet_task is None:
            raise RuntimeError("You need to start Mudlet first!")
        await_(self._telnet_done.wait())

    def cmd__wait_mudpyc(self, text:str = None):
        """
        Script: with_mudpyc(TEXT)

        Send the given text (if any) to Mudlet. Then,
        wait until Mudlet has established a connection to the MudPyC
        service (which must already be running).
        """
        if self._mudpyc_task is None:
            raise RuntimeError("You need to start MudPyC first!")
        if text is not None:
            if self.stream is None:
                raise RuntimeError("Telnet is not connected")
            await_(self.stream.writeline(text))

    # ## ### TODO ### ## #

    def cmd__lua(self, cmd, **vars):
        """
        Script: lua(command, var=value…)

        Run the given Lua expression within Mudlet.
        Always returns an array with the return values.
        """
        raise NotImplementedError("Please implement 'cmd__lua'!")

    def cmd__is(self, value:Any):
        """
        Script: is(VALUE)

        Shortcut for
            temp = lua(``most-recent Lua command``)
            if len(temp) != 1 or temp[0] != value:
                fail("Result {temp!r} != {value!r}")
        """
        raise NotImplementedError("Please implement 'cmd__is'!")
    
    def cmd__eval(self, data_or_file:Union[Path,str]):
        """
        Script: eval(FILENAME)

        Run the given Lua statements within Mudlet.
        Instead of filename you may also pass the script in-line.
        No return value.
        """
        raise NotImplementedError("Please implement 'cmd__eval'!")

    def cmd__pixel_at(self, x:int,y:int):
        """
        Script: pixel_at(10,20)

        Returns the RGB value at this screen position.

        The result has a method ``.compare(NAME)`` which checks that the
        RGB value is identical to the named value in our storage. If the
        name doesn't exist the RGB value is stored.

        You can also directly pass in a RGB triple.
        """
        raise NotImplementedError("Please implement 'cmd__pixel_at'!")

    def cmd__image_at(self, l:int,t:int,r:int,b:int):
        """
        Script: image_at(10,20,30,40)

        Returns a copy of the screen image between (left,top,right,bottom).

        The result has a method ``.compare(FILE)`` which checks that the
        image is identical to that stored at ``FILE``; if the file does not
        exist, the image is stored there.

        You can also pass in another image directly.
        """
        raise NotImplementedError("Please implement 'cmd__image_at'!")

    def cmd__click(self, name:str, button:Optional[int]=1):
        """
        Script: click(NAME)

        Clicks at the named screen position. If the name is not known, asks
        the user to click there. 

        Just returns the position if ``button=None``.
        """
        raise NotImplementedError("Please implement 'cmd__click'!")

    def cmd__drag(self, name1:str, name2:str=None, button:Optional[int]=1):
        """
        Script: drag(NAME), drag(NAME_FROM, NAME_TO)

        Drags the mouse from the first of the named positions to the second.

        If only one name is given, appends ``_from`` and ``_to`` to it.
        """
        raise NotImplementedError("Please implement 'cmd__drag'!")

    def cmd__gmcp(self, type:str, data:Union[str,dict]):
        """
        Script: gmcp(type, data)

        Sends a GMCP message. ``type`` must be a string, ``data`` is either
        a string (sent as-is) or some other data structure (sent
        JSON-encoded).
        """
        raise NotImplementedError("Please implement 'cmd__gmcp'!")

    def cmd__read_gmcp(self):
        """
        Script: read_gmcp()

        Returns the next GMCP message from Mudlet.
        """
        raise NotImplementedError("Please implement 'cmd__read_gmcp'!")

    def cmd__success(self, message:str = None):
        """
        Script: success([MESSAGE])

        Ends the current "run" invocation.
        """
        raise NotImplementedError("Please implement 'cmd__success'!")

    def cmd__done(self):
        """
        Script: done()

        Ends the current "run" or "include" invocation.
        """
        raise NotImplementedError("Please implement 'cmd__done'!")

    def cmd__fail(self, message:str = None):
        """
        Script: fail(MESSAGE)

        Raises an error. The test run has failed.
        """
        raise NotImplementedError("Please implement 'cmd__fail'!")

    def cmd__timeout(self, timeout:int):
        """
        Script: timeout(t)

        Limits the time between this invocation of ``timeout`` and the next,
        or the end of the test, to at most ``timeout`` seconds.

        Without an argument, the previous timeout value is refreshed.
        """
        raise NotImplementedError("Please implement 'cmd__timeout'!")

    def cmd__log(self, msg:str, *a,**kw):
        """
        Script: log(MESSAGE[, ARGS…])

        Logs the message (level: INFO).
        """
        self.log.info(msg,*a,**kw)

    def cmd__sleep(self, delay:int):
        """
        Script: sleep(DELAY)

        Does nothing for DELAY seconds.
        Background processing (replying to Telnet options, MudPyC
        messaging, …) continues during the delay.
        """
        await_(trio.sleep(delay))



