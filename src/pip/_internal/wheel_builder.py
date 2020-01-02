"""Orchestrator for building wheels from InstallRequirements.
"""

# The following comment should be removed at some point in the future.
# mypy: strict-optional=False

import logging
import os.path
import re
import shutil

from pip._internal.models.link import Link
from pip._internal.operations.build.wheel import build_wheel_pep517
from pip._internal.operations.build.wheel_legacy import build_wheel_legacy
from pip._internal.utils.logging import indent_log
from pip._internal.utils.marker_files import has_delete_marker_file
from pip._internal.utils.misc import ensure_dir, hash_file
from pip._internal.utils.setuptools_build import make_setuptools_clean_args
from pip._internal.utils.subprocess import call_subprocess
from pip._internal.utils.temp_dir import TempDirectory
from pip._internal.utils.typing import MYPY_CHECK_RUNNING
from pip._internal.utils.unpacking import unpack_file
from pip._internal.utils.urls import path_to_url
from pip._internal.vcs import vcs

if MYPY_CHECK_RUNNING:
    from typing import (
        Any, Callable, Iterable, List, Optional, Pattern, Tuple,
    )

    from pip._internal.cache import WheelCache
    from pip._internal.operations.prepare import (
        RequirementPreparer
    )
    from pip._internal.req.req_install import InstallRequirement

    BinaryAllowedPredicate = Callable[[InstallRequirement], bool]
    BuildResult = Tuple[List[InstallRequirement], List[InstallRequirement]]

logger = logging.getLogger(__name__)


def _contains_egg_info(
        s, _egg_info_re=re.compile(r'([a-z0-9_.]+)-([a-z0-9_.!+-]+)', re.I)):
    # type: (str, Pattern[str]) -> bool
    """Determine whether the string looks like an egg_info.

    :param s: The string to parse. E.g. foo-2.1
    """
    return bool(_egg_info_re.search(s))


def should_build(
    req,  # type: InstallRequirement
    need_wheel,  # type: bool
    check_binary_allowed,  # type: BinaryAllowedPredicate
):
    # type: (...) -> Optional[bool]
    """Return whether an InstallRequirement should be built into a wheel."""
    if req.constraint:
        # never build requirements that are merely constraints
        return False
    if req.is_wheel:
        if need_wheel:
            logger.info(
                'Skipping %s, due to already being wheel.', req.name,
            )
        return False

    if need_wheel:
        # i.e. pip wheel, not pip install
        return True

    if req.editable or not req.source_dir:
        return False

    if not check_binary_allowed(req):
        logger.info(
            "Skipping wheel build for %s, due to binaries "
            "being disabled for it.", req.name,
        )
        return False

    return True


def should_cache(
    req,  # type: InstallRequirement
    check_binary_allowed,  # type: BinaryAllowedPredicate
):
    # type: (...) -> Optional[bool]
    """
    Return whether a built InstallRequirement can be stored in the persistent
    wheel cache, assuming the wheel cache is available, and should_build()
    has determined a wheel needs to be built.
    """
    if not should_build(
        req, need_wheel=False, check_binary_allowed=check_binary_allowed
    ):
        # never cache if pip install (need_wheel=False) would not have built
        # (editable mode, etc)
        return False

    if req.link and req.link.is_vcs:
        # VCS checkout. Build wheel just for this run
        # unless it points to an immutable commit hash in which
        # case it can be cached.
        assert not req.editable
        assert req.source_dir
        vcs_backend = vcs.get_backend_for_scheme(req.link.scheme)
        assert vcs_backend
        if vcs_backend.is_immutable_rev_checkout(req.link.url, req.source_dir):
            return True
        return False

    link = req.link
    base, ext = link.splitext()
    if _contains_egg_info(base):
        return True

    # Otherwise, build the wheel just for this run using the ephemeral
    # cache since we are either in the case of e.g. a local directory, or
    # no cache directory is available to use.
    return False


def _collect_buildset(
    requirements,  # type: Iterable[InstallRequirement]
    wheel_cache,  # type: WheelCache
    check_binary_allowed,  # type: BinaryAllowedPredicate
    need_wheel,  # type: bool
):
    # type: (...) -> List[Tuple[InstallRequirement, str]]
    """Return the list of InstallRequirement that need to be built,
    with the persistent or temporary cache directory where the built
    wheel needs to be stored.
    """
    buildset = []
    cache_available = bool(wheel_cache.cache_dir)
    for req in requirements:
        if not should_build(
            req,
            need_wheel=need_wheel,
            check_binary_allowed=check_binary_allowed,
        ):
            continue
        if (
            cache_available and
            should_cache(req, check_binary_allowed)
        ):
            cache_dir = wheel_cache.get_path_for_link(req.link)
        else:
            cache_dir = wheel_cache.get_ephem_path_for_link(req.link)
        buildset.append((req, cache_dir))
    return buildset


def _always_true(_):
    # type: (Any) -> bool
    return True


def _build_one(
    req,  # type: InstallRequirement
    output_dir,  # type: str
    build_options,  # type: List[str]
    global_options,  # type: List[str]
):
    # type: (...) -> Optional[str]
    """Build one wheel.

    :return: The filename of the built wheel, or None if the build failed.
    """
    try:
        ensure_dir(output_dir)
    except OSError as e:
        logger.warning(
            "Building wheel for %s failed: %s",
            req.name, e,
        )
        return None

    # Install build deps into temporary directory (PEP 518)
    with req.build_env:
        return _build_one_inside_env(
            req, output_dir, build_options, global_options
        )


def _build_one_inside_env(
    req,  # type: InstallRequirement
    output_dir,  # type: str
    build_options,  # type: List[str]
    global_options,  # type: List[str]
):
    # type: (...) -> Optional[str]
    with TempDirectory(kind="wheel") as temp_dir:
        if req.use_pep517:
            wheel_path = build_wheel_pep517(
                name=req.name,
                backend=req.pep517_backend,
                metadata_directory=req.metadata_directory,
                build_options=build_options,
                tempd=temp_dir.path,
            )
        else:
            wheel_path = build_wheel_legacy(
                name=req.name,
                setup_py_path=req.setup_py_path,
                source_dir=req.unpacked_source_directory,
                global_options=global_options,
                build_options=build_options,
                tempd=temp_dir.path,
            )

        if wheel_path is not None:
            wheel_name = os.path.basename(wheel_path)
            dest_path = os.path.join(output_dir, wheel_name)
            try:
                wheel_hash, length = hash_file(wheel_path)
                shutil.move(wheel_path, dest_path)
                logger.info('Created wheel for %s: '
                            'filename=%s size=%d sha256=%s',
                            req.name, wheel_name, length,
                            wheel_hash.hexdigest())
                logger.info('Stored in directory: %s', output_dir)
                return dest_path
            except Exception as e:
                logger.warning(
                    "Building wheel for %s failed: %s",
                    req.name, e,
                )
        # Ignore return, we can't do anything else useful.
        if not req.use_pep517:
            _clean_one_legacy(req, global_options)
        return None


def _clean_one_legacy(req, global_options):
    # type: (InstallRequirement, List[str]) -> bool
    clean_args = make_setuptools_clean_args(
        req.setup_py_path,
        global_options=global_options,
    )

    logger.info('Running setup.py clean for %s', req.name)
    try:
        call_subprocess(clean_args, cwd=req.source_dir)
        return True
    except Exception:
        logger.error('Failed cleaning build dir for %s', req.name)
        return False


class WheelBuilder(object):
    """Build wheels from a RequirementSet."""

    def __init__(
        self,
        preparer,  # type: RequirementPreparer
    ):
        # type: (...) -> None
        self.preparer = preparer

    def build(
        self,
        requirements,  # type: Iterable[InstallRequirement]
        should_unpack,  # type: bool
        wheel_cache,  # type: WheelCache
        build_options,  # type: List[str]
        global_options,  # type: List[str]
        check_binary_allowed=None,  # type: Optional[BinaryAllowedPredicate]
    ):
        # type: (...) -> BuildResult
        """Build wheels.

        :param should_unpack: If True, after building the wheel, unpack it
            and replace the sdist with the unpacked version in preparation
            for installation.
        :return: The list of InstallRequirement that succeeded to build and
            the list of InstallRequirement that failed to build.
        """
        if check_binary_allowed is None:
            # Binaries allowed by default.
            check_binary_allowed = _always_true

        buildset = _collect_buildset(
            requirements,
            wheel_cache=wheel_cache,
            check_binary_allowed=check_binary_allowed,
            need_wheel=not should_unpack,
        )
        if not buildset:
            return [], []

        # TODO by @pradyunsg
        # Should break up this method into 2 separate methods.

        # Build the wheels.
        logger.info(
            'Building wheels for collected packages: %s',
            ', '.join([req.name for (req, _) in buildset]),
        )

        with indent_log():
            build_successes, build_failures = [], []
            for req, cache_dir in buildset:
                wheel_file = _build_one(
                    req, cache_dir, build_options, global_options
                )
                if wheel_file:
                    # Update the link for this.
                    req.link = Link(path_to_url(wheel_file))
                    req.local_file_path = req.link.file_path
                    assert req.link.is_wheel
                    if should_unpack:
                        # XXX: This is mildly duplicative with prepare_files,
                        # but not close enough to pull out to a single common
                        # method.
                        # The code below assumes temporary source dirs -
                        # prevent it doing bad things.
                        if (
                            req.source_dir and
                            not has_delete_marker_file(req.source_dir)
                        ):
                            raise AssertionError(
                                "bad source dir - missing marker")
                        # Delete the source we built the wheel from
                        req.remove_temporary_source()
                        # set the build directory again - name is known from
                        # the work prepare_files did.
                        req.source_dir = req.ensure_build_location(
                            self.preparer.build_dir
                        )
                        # extract the wheel into the dir
                        unpack_file(req.link.file_path, req.source_dir)
                    build_successes.append(req)
                else:
                    build_failures.append(req)

        # notify success/failure
        if build_successes:
            logger.info(
                'Successfully built %s',
                ' '.join([req.name for req in build_successes]),
            )
        if build_failures:
            logger.info(
                'Failed to build %s',
                ' '.join([req.name for req in build_failures]),
            )
        # Return a list of requirements that failed to build
        return build_successes, build_failures
