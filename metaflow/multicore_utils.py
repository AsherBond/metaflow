import sys
import os
import traceback
from itertools import islice
from tempfile import NamedTemporaryFile
import time
import metaflow.tracing as tracing

from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    List,
    Optional,
    NoReturn,
    Tuple,
    TypeVar,
    Union,
)

try:
    # Python 2
    import cPickle as pickle
except:
    # Python 3
    import pickle

# This module reimplements select functions from the standard
# Python multiprocessing module.
#
# Three reasons why:
#
# 1) Multiprocessing has open bugs, e.g. https://bugs.python.org/issue29759
# 2) Work around limits, like the 32MB object limit in Queue, without
#    introducing an external dependency like joblib.
# 3) Supports closures and lambdas in contrast to multiprocessing.


class MulticoreException(Exception):
    pass


_A = TypeVar("_A")
_R = TypeVar("_R")


def _spawn(
    func: Callable[[_A], _R], arg: _A, dir: Optional[str]
) -> Union[Tuple[int, str], NoReturn]:
    with NamedTemporaryFile(prefix="parallel_map_", dir=dir, delete=False) as tmpfile:
        output_file = tmpfile.name

    # Make sure stdout and stderr are flushed before forking,
    # or else we may print multiple copies of the same output
    sys.stderr.flush()
    sys.stdout.flush()
    pid = os.fork()
    if pid:
        return pid, output_file
    else:
        with tracing.post_fork():
            try:
                exit_code = 1
                ret = func(arg)
                with open(output_file, "wb") as f:
                    pickle.dump(ret, f, protocol=pickle.HIGHEST_PROTOCOL)
                exit_code = 0
            except:
                # we must not let any exceptions escape this function
                # which might trigger unintended side-effects
                traceback.print_exc()
            finally:
                sys.stderr.flush()
                sys.stdout.flush()
                # we can't use sys.exit(0) here since it raises SystemExit
                # that may have unintended side-effects (e.g. triggering
                # finally blocks).
                os._exit(exit_code)


def parallel_imap_unordered(
    func: Callable[[_A], _R],
    iterable: Iterable[_A],
    max_parallel: Optional[int] = None,
    dir: Optional[str] = None,
) -> Iterator[_R]:
    """
    Parallelizes execution of a function using multiprocessing. The result
    order is not guaranteed.

    Parameters
    ----------
    func : Callable[[Any], Any]
        Function taking a single argument and returning a result
    iterable : Iterable[Any]
        Iterable over arguments to pass to fun
    max_parallel int, optional, default None
        Maximum parallelism. If not specified, it uses the number of CPUs
    dir : str, optional, default None
        If specified, it's the directory where temporary files are created

    Yields
    ------
    Any
        One result from calling func on one argument
    """
    if max_parallel is None:
        # Lazy import to save on startup time for metaflow as a whole
        from multiprocessing import cpu_count

        max_parallel = cpu_count()

    args_iter = iter(iterable)
    pids = [_spawn(func, arg, dir) for arg in islice(args_iter, max_parallel)]

    while pids:
        for idx, pid_info in enumerate(pids):
            pid, output_file = pid_info
            pid, exit_code = os.waitpid(pid, os.WNOHANG)
            if pid:
                pids.pop(idx)
                break
        else:
            time.sleep(0.1)  # Wait a bit before re-checking
            continue

        if exit_code:
            raise MulticoreException("Child failed")

        with open(output_file, "rb") as f:
            yield pickle.load(f)
        os.remove(output_file)

        arg = list(islice(args_iter, 1))
        if arg:
            pids.insert(0, _spawn(func, arg[0], dir))


def parallel_map(
    func: Callable[[_A], _R],
    iterable: Iterable[_A],
    max_parallel: Optional[int] = None,
    dir: Optional[str] = None,
) -> List[_R]:
    """
    Parallelizes execution of a function using multiprocessing. The result
    order is that of the arguments in `iterable`.

    Parameters
    ----------
    func : Callable[[Any], Any]
        Function taking a single argument and returning a result
    iterable : Iterable[Any]
        Iterable over arguments to pass to fun
    max_parallel int, optional, default None
        Maximum parallelism. If not specified, it uses the number of CPUs
    dir : str, optional, default None
        If specified, it's the directory where temporary files are created

    Returns
    -------
    List[Any]
        Results. The items in the list are in the same order as the items
        in `iterable`.
    """

    def wrapper(arg_with_idx):
        idx, arg = arg_with_idx
        return idx, func(arg)

    res = parallel_imap_unordered(
        wrapper, enumerate(iterable), max_parallel=max_parallel, dir=dir
    )
    return [r for _, r in sorted(res)]
