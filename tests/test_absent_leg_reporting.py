"""The absent-leg reporter must fire, and must not cry wolf.

Both halves matter. A reporter that never fires is the bug it exists to catch,
and one that fires on every run trains the reader to skip the line, which is the
same outcome by a slower route.

These use pytest's own `pytester` to run a real inner session, because asserting
on the hook's internals would prove the hook was written, not that it reports.
"""
import pathlib

import pytest

pytest_plugins = ["pytester"]

CONFTEST = (
    "import sys, pathlib\n"
    "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))\n"
    "from conftest_under_test import pytest_terminal_summary  # noqa: F401\n"
)


@pytest.fixture()
def inner(pytester, request):
    """A throwaway test session that loads this template's real hook."""
    real = pathlib.Path(request.config.rootdir) / "tests" / "conftest.py"
    pytester.makepyfile(conftest_under_test=real.read_text())
    pytester.makeconftest(CONFTEST)
    return pytester


def test_it_names_a_leg_that_was_entirely_skipped(inner):
    inner.makepyfile(
        test_legs="""
        import pytest

        @pytest.fixture(params=["sqlite", "postgres"])
        def backend(request):
            if request.param == "postgres":
                pytest.skip("TEST_DATABASE_URL is not set")
            return request.param

        def test_one(backend):
            assert backend == "sqlite"

        def test_two(backend):
            assert backend == "sqlite"
        """
    )
    result = inner.runpytest()
    result.stdout.fnmatch_lines(["*ENTIRE PARAMETRISED LEG DID NOT RUN*"])
    result.stdout.fnmatch_lines(["*every test parametrised [[]postgres[]] was skipped*"])


def test_it_stays_quiet_when_every_leg_ran(inner):
    # Crying wolf on a healthy run is how the line gets ignored on the run that
    # matters, which is the same failure as never firing.
    inner.makepyfile(
        test_legs="""
        import pytest

        @pytest.fixture(params=["sqlite", "postgres"])
        def backend(request):
            return request.param

        def test_one(backend):
            assert backend
        """
    )
    result = inner.runpytest()
    assert "ENTIRE PARAMETRISED LEG DID NOT RUN" not in result.stdout.str()


def test_a_partly_skipped_leg_is_not_reported_as_absent(inner):
    # One skipped case in a leg that otherwise ran is an ordinary skip. Only a
    # leg with NOTHING executed is the finding.
    inner.makepyfile(
        test_legs="""
        import pytest

        @pytest.fixture(params=["sqlite", "postgres"])
        def backend(request):
            return request.param

        def test_one(backend):
            if backend == "postgres":
                pytest.skip("just this one")
            assert True

        def test_two(backend):
            assert True
        """
    )
    result = inner.runpytest()
    assert "ENTIRE PARAMETRISED LEG DID NOT RUN" not in result.stdout.str()
