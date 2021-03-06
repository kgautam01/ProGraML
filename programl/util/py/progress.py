# Copyright 2019-2020 the ProGraML authors.
#
# Contact Chris Cummins <chrisc.101@gmail.com>.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""This module aims to help evaluate the progress of long-running jobs."""
import datetime
import threading
import time
import typing
from contextlib import contextmanager
from typing import Callable, Optional, Union

import tqdm
from absl import flags, logging

from programl.util.py import humanize

FLAGS = flags.FLAGS


class ProfileTimer(object):
    """A profiling timer."""

    def __init__(self):
        self.start: datetime.datetime = datetime.datetime.utcnow()
        self.end: Optional[datetime.datetime] = None

    def Stop(self):
        if self.end:
            return
        self.end = datetime.datetime.utcnow()

    @property
    def elapsed(self) -> float:
        if self.end:
            return (self.end - self.start).total_seconds()
        else:
            return (datetime.datetime.utcnow() - self.start).total_seconds()

    @property
    def elapsed_ms(self) -> int:
        return int(round(self.elapsed * 1000))

    def __repr__(self):
        return humanize.Duration(self.elapsed)


@logging.skip_log_prefix
@contextmanager
def Profile(
    name: typing.Union[str, typing.Callable[[int], str]] = "",
    print_to: typing.Callable[[str], None] = lambda msg: logging.info(msg),
) -> ProfileTimer:
    """A context manager which prints the elapsed time upon exit.
    Args:
      name: The name of the task being profiled. A callback may be provided which
        is called at task completion with the elapsed duration in seconds as its
        argument.
      print_to: The function to print the result to.
    """
    name = name or "completed"
    timer = ProfileTimer()
    yield timer
    timer.Stop()
    if callable(name):
        name = name(timer.elapsed)
    print_to(f"{name} in {timer}")


class Progress(threading.Thread):
    """A job with intermediate progress and a progress bar."""

    def __init__(
        self,
        name: str,
        i: int,
        n: Optional[int] = None,
        unit: str = "",
        vertical_position: int = 0,
        leave: Optional[bool] = None,
    ):
        """Instantiate a long-running job.

        Args:
          args: Arguments for ProgressBarContext().
          kwargs: Keyword arguments for ProgressBarContext().
        """
        self.ctx = ProgressBarContext(
            name=name,
            i=i,
            n=n,
            unit=unit,
            vertical_position=vertical_position,
            leave=leave,
        )
        super(Progress, self).__init__()

        self.run = self.Run
        self.Start = self.start
        self.IsAlive = self.is_alive

    def Run(self):
        """The run method. This should progress from [self.ctx.i, self.ctx.n]."""
        raise NotImplementedError("abstract class")


def Run(progress: Progress, refresh_time: float = 0.2, patience: int = 0) -> Progress:
    """Run the given progress job until completion, updating the progress bar.

    Args:
      progress: The job to run.
      refresh_time: The number of seconds between updates to the progress bar.
      patience: If a positive value, this is the maximum allowed seconds to run
        without a change to the progress before raising an error.

    Raises:
      OSError: If patience is set, and no progress has been made within the given
        number of seconds.
    """
    progress.Start()
    last_i = progress.ctx.i
    last_progress = time.time()

    while progress.is_alive():
        current_time = time.time()
        if progress.ctx.i != last_i:
            last_i = progress.ctx.i
            last_progress = current_time
        elif patience and (current_time - last_progress) > patience:
            raise OSError(
                f"Failed to make progress after "
                f"{current_time - last_progress:.0f} seconds"
            )
        progress.ctx.Refresh()
        progress.join(refresh_time)
    progress.ctx.Refresh()
    progress.ctx.bar.close()
    return progress


class ProgressContext(object):
    """A context for logging and profiling."""

    def __init__(self, print_context):
        """Constructor.

        Args:
          print_context: A context for printing.
        """
        self.print_context = print_context

    @logging.skip_log_prefix
    def Log(self, *args, **kwargs):
        """Log a message."""
        logging.info(*args, **kwargs, print_context=self.print_context)

    @logging.skip_log_prefix
    def Warning(self, *args, **kwargs):
        """Log a warning."""
        logging.warning(*args, **kwargs, print_context=self.print_context)

    @logging.skip_log_prefix
    def Error(self, *args, **kwargs):
        """Log an error."""
        logging.warning(*args, **kwargs, print_context=self.print_context)

    @logging.skip_log_prefix
    def Profile(self, level: int, msg: Union[str, Callable[[int], str]] = ""):
        """Return a profiling context."""
        return Profile(
            msg,
            print_to=lambda x: self.Log(level, "%s", x),
        )

    def print(self, *args, **kwargs):
        with self.print_context():
            print(*args, **kwargs)


NullContext = ProgressContext(None)


class ProgressBarContext(ProgressContext):
    """The context for logging and profiling with a progress bar."""

    def __init__(
        self,
        name: str,
        i: int,
        n: Optional[int] = None,
        unit: str = "",
        vertical_position: int = 0,
        leave: Optional[bool] = None,
    ):
        """Construct a new progress.

        Args:
          name: The name of the job being processed. This is a prefix for the
            progress bar output.
          i: The starting value for the job.
          n: The end value for the job. If not known this may be None. When None,
            no estimated time or "bar" is printed during progress.
          unit: An optional unit for progress bar.
          vertical_position: The vertical position of the progress bar. Use this
            when there are multiple progress bar concurrently.
        """
        self.name = name
        self.i = int(i)
        self.n = n
        self.unit = unit
        self.vertical_position = vertical_position

        # Force conversion to int for counters.
        if self.n is not None:
            self.n = int(self.n)

        # Prepend whitespace to the unit
        unit = f" {unit}" if unit else unit

        # Create the progress bar.
        self.bar = tqdm.tqdm(
            desc=self.name,
            initial=self.i,
            total=self.n,
            unit=unit,
            position=self.vertical_position,
            leave=leave,
        )
        self.print_context = self.bar.external_write_mode

    def ToProgressContext(self) -> ProgressContext:
        """Construct a progress context from the given progress context with bar.

        This method is useful for sending the progress context to worker processes
        during multimprocessing. By removing self.bar attribute, we need only
        serialize the logging context.
        """
        return ProgressContext(self.print_context)

    def Refresh(self):
        """Refresh the progress bar."""
        self.bar.n = self.i
        self.bar.refresh()
